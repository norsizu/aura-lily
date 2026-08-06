#!/usr/bin/env python3
"""Run the live provider gate through Aura's real AgentTask HTTP path.

This is stricter than ``agent_task_smoke.py``:

- starts a temporary local Aura Life backend unless ``--base-url`` is given;
- skips cleanly when provider env is absent;
- defaults to no external tools so the gate checks provider + AgentTask loop,
  not web search availability;
- prints only scrubbed summaries.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "companion"
if str(COMPANION) not in sys.path:
    sys.path.insert(0, str(COMPANION))

from aura_companion.agent_runtime.provider_diagnostics import diagnose_provider_config
from aura_companion.agent_runtime.provider_routes import api_key_envs_for_provider, resolve_provider_settings

SERVER = ROOT / "lazycat" / "aura-life" / "backend" / "aura_life_server.py"
WEB_DIST = ROOT / "apps" / "web" / "dist"
DEFAULT_GOAL = "请用严格 JSON 完成任务：{\"action\":\"finish\",\"answer\":\"Agent provider gate ok\",\"thought_summary\":\"连通性检查\"}"
DEFAULT_SENTINEL = "Agent provider gate ok"
TERMINAL = {"completed", "failed", "cancelled"}
SECRET_KEYS = {
    "AURA_AGENT_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MINIMAX_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "TUZI_API_KEY",
    "TU_ZI_API_KEY",
    "CODEX_API_KEY",
}


_MANAGED_PROCESSES: set[subprocess.Popen[str]] = set()
_CLEANUP_REGISTERED = False
_PREVIOUS_SIGNAL_HANDLERS: dict[int, Any] = {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="")
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--with-tools", action="store_true")
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--sentinel", default=DEFAULT_SENTINEL)
    args = parser.parse_args()

    with managed_backend(args.base_url, port=int(args.port or 0)) as backend:
        base_url = str(args.base_url or "").rstrip("/")
        if not base_url:
            base_url = backend["base_url"]
        health = wait_health(base_url, timeout=15.0 if backend.get("proc") is not None else 3.0)
        if not health.get("ok"):
            print(json.dumps({"ok": False, "stage": "health", "health": scrub_payload(health)}, ensure_ascii=False, indent=2))
            return 2

        readiness = backend_or_env_provider_readiness(base_url)
        if not readiness["ready"]:
            payload = {"ok": False, "skipped": True, "stage": "provider_env", "provider": readiness}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 6 if args.require_live else 0

        result = run_smoke(
            base_url=base_url,
            goal=args.goal,
            timeout=float(args.timeout),
            with_tools=bool(args.with_tools),
            sentinel=str(args.sentinel or ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 4


def provider_readiness(env: dict[str, str]) -> dict[str, Any]:
    enabled = str(env.get("AURA_AGENT_RUNTIME_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    provider = str(env.get("AURA_AGENT_PROVIDER") or "kimi").strip() or "kimi"
    model = str(env.get("AURA_AGENT_MODEL") or "kimi-k2.5").strip() or "kimi-k2.5"
    base_url = str(env.get("AURA_AGENT_BASE_URL") or "").strip()
    adapter_env = str(env.get("AURA_AGENT_ADAPTER") or "").strip()
    wire_env = str(env.get("AURA_AGENT_WIRE_API") or "").strip()
    adapter_conflict = bool(adapter_env and wire_env and adapter_env.lower() != wire_env.lower())
    adapter = adapter_env or wire_env
    try:
        settings = resolve_provider_settings(provider, model=model, base_url=base_url, adapter=adapter)
        provider = settings.provider_name
        model = settings.model
        base_url = settings.base_url
        adapter = settings.adapter
        adapter_source = settings.adapter_source
        diagnostic = diagnose_provider_config(
            provider,
            model=model,
            base_url=base_url,
            adapter=adapter,
            runtime_backend=str(env.get("AURA_AGENT_BACKEND") or ""),
            env=env,
        )
        provider_error = ""
    except ValueError as exc:
        adapter_source = "invalid"
        provider_error = str(exc)
        diagnostic = None
    api_key_present = any(str(env.get(key) or "").strip() for key in api_key_candidates(provider))
    diagnostic_ok = bool(diagnostic.ok) if diagnostic is not None else False
    diagnostic_errors = list(diagnostic.errors) if diagnostic is not None else []
    return {
        "ready": bool(enabled and api_key_present and not provider_error and diagnostic_ok),
        "runtime_enabled": enabled,
        "provider": provider,
        "model": model,
        "adapter": adapter,
        "adapter_source": adapter_source,
        "adapter_env_conflict": adapter_conflict,
        "endpoint_host": urlsplit(base_url).hostname or "",
        "api_key_present": api_key_present,
        "diagnostic_ok": diagnostic_ok,
        "diagnostic_errors": diagnostic_errors,
        "error": provider_error,
        "source": "process_env",
    }


def backend_or_env_provider_readiness(base_url: str) -> dict[str, Any]:
    if base_url:
        payload = request_json(
            "GET",
            f"{base_url.rstrip('/')}/api/life/providers/agent/config",
            timeout=5.0,
        )
        if payload.get("ok"):
            config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
            diagnostic = payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else {}
            return {
                "ready": bool(
                    config.get("runtime_enabled")
                    and config.get("api_key_present")
                    and config.get("route_runnable", diagnostic.get("route_runnable", diagnostic.get("ok")))
                ),
                "runtime_enabled": bool(config.get("runtime_enabled")),
                "provider": config.get("provider"),
                "model": config.get("model"),
                "adapter": config.get("adapter"),
                "runtime_backend": config.get("runtime_backend") or diagnostic.get("runtime_backend"),
                "endpoint_host": config.get("endpoint_host") or diagnostic.get("endpoint_host"),
                "api_key_present": bool(config.get("api_key_present")),
                "diagnostic_ok": bool(diagnostic.get("ok")),
                "route_runnable": bool(config.get("route_runnable", diagnostic.get("route_runnable", diagnostic.get("ok")))),
                "route_errors": config.get("route_errors") or diagnostic.get("route_errors") or diagnostic.get("errors") or [],
                "source": "backend_provider_config",
                "error": diagnostic.get("error") or payload.get("error") or "",
            }
        return {
            "ready": False,
            "runtime_enabled": False,
            "provider": "",
            "model": "",
            "adapter": "",
            "endpoint_host": "",
            "api_key_present": False,
            "diagnostic_ok": False,
            "source": "backend_provider_config",
            "error": payload.get("error") or "provider_config_unreachable",
        }
    return provider_readiness(os.environ)


def api_key_candidates(provider: str) -> tuple[str, ...]:
    clean = provider.strip().lower()
    keys = ["AURA_AGENT_API_KEY", f"{clean.upper().replace('-', '_')}_API_KEY"]
    try:
        keys.extend(api_key_envs_for_provider(clean))
    except Exception:
        pass
    if clean in {"minimax", "minimax-cn"}:
        keys.extend(["MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"])
    if clean in {"kimi", "kimi-for-coding"}:
        keys.extend(["KIMI_API_KEY", "MOONSHOT_API_KEY"])
    if clean in {"tuzi", "tu-zi", "gac", "gaccode"}:
        keys.extend(["TUZI_API_KEY", "TU_ZI_API_KEY", "CODEX_API_KEY"])
    return tuple(dict.fromkeys(keys))


def start_backend(*, port: int, state_root: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{ROOT / 'companion'}:{ROOT / 'lazycat' / 'aura-life' / 'backend'}:{env.get('PYTHONPATH', '')}"
    env["AURA_LIFE_HOST"] = "127.0.0.1"
    env["AURA_LIFE_PORT"] = str(port)
    args = [
        sys.executable,
        str(SERVER),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--root",
        str(state_root),
        "--static-dir",
        str(WEB_DIST if WEB_DIST.exists() else ROOT / "lazycat" / "aura-life" / "content"),
        "--admin-key",
        "",
        "--mode",
        "standalone",
    ]
    return subprocess.Popen(
        args,
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@contextlib.contextmanager
def managed_backend(base_url: str = "", *, port: int = 0):
    """Start a temporary backend and guarantee cleanup on normal or signal exit."""

    register_cleanup_handlers()
    clean_base_url = str(base_url or "").rstrip("/")
    if clean_base_url:
        yield {"base_url": clean_base_url, "proc": None, "temp_dir": None}
        return

    temp_dir = tempfile.TemporaryDirectory(prefix="aura-provider-gate-")
    proc: subprocess.Popen[str] | None = None
    try:
        selected_port = int(port or find_free_port())
        clean_base_url = f"http://127.0.0.1:{selected_port}"
        proc = start_backend(port=selected_port, state_root=Path(temp_dir.name))
        _MANAGED_PROCESSES.add(proc)
        yield {"base_url": clean_base_url, "proc": proc, "temp_dir": temp_dir.name}
    finally:
        if proc is not None:
            terminate_process(proc)
        temp_dir.cleanup()


def register_cleanup_handlers() -> None:
    global _CLEANUP_REGISTERED
    if _CLEANUP_REGISTERED:
        return
    _CLEANUP_REGISTERED = True
    atexit.register(cleanup_managed_processes)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            _PREVIOUS_SIGNAL_HANDLERS[sig] = signal.getsignal(sig)
            signal.signal(sig, _signal_cleanup_handler)
        except (OSError, ValueError):
            continue


def _signal_cleanup_handler(signum: int, frame: Any) -> None:
    cleanup_managed_processes()
    previous = _PREVIOUS_SIGNAL_HANDLERS.get(signum)
    if callable(previous):
        previous(signum, frame)
        return
    raise SystemExit(128 + int(signum))


def cleanup_managed_processes() -> None:
    for proc in list(_MANAGED_PROCESSES):
        terminate_process(proc)


def terminate_process(proc: subprocess.Popen[str]) -> None:
    _MANAGED_PROCESSES.discard(proc)
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def wait_health(base_url: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    last: dict[str, Any] = {"ok": False, "error": "not_checked"}
    while time.monotonic() < deadline:
        last = request_json("GET", f"{base_url}/health", timeout=2.0)
        if last.get("ok"):
            return last
        time.sleep(0.25)
    return last


def run_smoke(*, base_url: str, goal: str, timeout: float, with_tools: bool, sentinel: str) -> dict[str, Any]:
    task_goal = goal_with_sentinel_contract(goal, sentinel)
    body: dict[str, Any] = {
        "goal": task_goal,
        "caller_id": "agent.task.provider_gate",
        "namespace": "provider_gate",
        "tools": [] if not with_tools else ["web.search", "http.fetch", "html.extract", "report.compose"],
        "budget": {"max_iterations": 2 if not with_tools else 4, "max_wall_seconds": min(60, max(5, timeout - 5)), "max_tokens": 800},
        "metadata": {"source": "agent_provider_gate.py", "smoke": True},
    }
    if with_tools:
        body["metadata"]["tool_approvals"] = ["web.search", "http.fetch"]

    submitted = request_json("POST", f"{base_url}/api/life/agent/tasks", body=body, timeout=10.0)
    task = task_from(submitted)
    task_id = str(task.get("task_id") or "")
    if not submitted.get("ok") or not task_id:
        return {"ok": False, "stage": "submit", "submitted": compact_payload(submitted)}

    deadline = time.monotonic() + max(1.0, timeout)
    last: dict[str, Any] = submitted
    while time.monotonic() < deadline:
        last = request_json("GET", f"{base_url}/api/life/agent/tasks/{quote(task_id)}", timeout=8.0)
        task = task_from(last)
        status = str(task.get("status") or "")
        if status in TERMINAL:
            stop_reason = str(task.get("stop_reason") or "")
            summary = str(task.get("result_summary") or "")
            sentinel_ok = not sentinel or sentinel in summary
            ok = bool(last.get("ok")) and status == "completed" and stop_reason == "finished" and sentinel_ok
            return {
                "ok": ok,
                "stage": "terminal",
                "task_id": task_id,
                "status": status,
                "stop_reason": stop_reason,
                "sentinel_ok": sentinel_ok,
                "summary_preview": summary[:240],
                "error": str(task.get("error") or "")[:240],
            }
        time.sleep(1.0)
    return {"ok": False, "stage": "timeout", "task_id": task_id, "last": compact_payload(last)}


def goal_with_sentinel_contract(goal: str, sentinel: str) -> str:
    clean_goal = str(goal or "").strip()
    clean_sentinel = str(sentinel or "").strip()
    if not clean_sentinel:
        return clean_goal
    if clean_sentinel in clean_goal:
        return clean_goal
    return (
        f"{clean_goal}\n\n"
        "Provider gate contract: when and only when the task is genuinely complete, "
        f"finish with an answer that contains this exact sentinel: {clean_sentinel}"
    )


def request_json(method: str, url: str, *, body: dict[str, Any] | None = None, timeout: float = 12.0) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local Aura backend smoke.
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"ok": False, "error": raw[:240]}
        payload["http_status"] = exc.code
        return scrub_payload(payload)
    except (TimeoutError, URLError, OSError) as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:240]}


def task_from(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    task = result.get("task") if isinstance(result.get("task"), dict) else payload.get("task")
    return task if isinstance(task, dict) else {}


def compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task = task_from(payload)
    error = payload.get("error")
    compact: dict[str, Any] = {"ok": bool(payload.get("ok")), "http_status": payload.get("http_status")}
    if task:
        compact["task"] = {
            "task_id": task.get("task_id"),
            "status": task.get("status"),
            "stop_reason": task.get("stop_reason"),
            "error": str(task.get("error") or "")[:240],
        }
    if error:
        compact["error"] = scrub_payload(error if isinstance(error, dict) else {"message": str(error)[:240]})
    return compact


def scrub_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key) in SECRET_KEYS or "key" in str(key).lower() else scrub_payload(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    text = str(value)
    if text.startswith("sk-"):
        return "<redacted>"
    return value


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
