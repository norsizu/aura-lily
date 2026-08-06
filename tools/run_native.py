#!/usr/bin/env python3
"""Run the Aura Lily HTTP bridge and ESP32 gateway on the host.

The launcher deliberately uses only the Python standard library.  It keeps
the two services in one foreground process so Ctrl-C stops both cleanly, and
it creates the local state directories used by the native installation.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTTP_PORT = 8765
DEFAULT_WS_PORT = 8787


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting shell variables."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def path_from_env(name: str, default: str) -> Path:
    value = os.environ.get(name, default).strip()
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def first_command(value: str) -> str:
    parts = shlex.split(value or "hermes")
    return parts[0] if parts else "hermes"


def resolve_hermes_command() -> str | None:
    """Find Hermes on PATH or beside the Python executable used by the launcher."""

    configured = os.environ.get("HERMES_COMMAND", "hermes")
    parts = shlex.split(configured)
    executable = parts[0] if parts else "hermes"
    candidate = Path(executable).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return configured
    if shutil.which(executable):
        return configured

    venv_candidate = Path(sys.executable).resolve().parent / executable
    if venv_candidate.is_file():
        parts[0] = str(venv_candidate)
        resolved = shlex.join(parts)
        os.environ["HERMES_COMMAND"] = resolved
        return resolved
    return None


def build_bridge_command() -> list[str]:
    http_host = os.environ.get("AURA_LILY_HTTP_HOST", "0.0.0.0")
    http_port = env_int("AURA_LILY_HTTP_PORT", DEFAULT_HTTP_PORT)
    command = [
        sys.executable,
        "-m",
        "integrations.hermes_lily_cli.server",
        "--host",
        http_host,
        "--port",
        str(http_port),
        "--provider",
        os.environ.get("HERMES_PROVIDER", ""),
        "--model",
        os.environ.get("HERMES_MODEL", ""),
        "--timeout",
        os.environ.get("HERMES_TIMEOUT", "180"),
        "--toolsets",
        os.environ.get("HERMES_TOOLSETS", "web,terminal,file,code_execution,skills"),
        "--skills",
        os.environ.get("HERMES_SKILLS", ""),
        "--hermes-home",
        os.environ.get("HERMES_HOME", ".aura/hermes"),
        "--hermes-command",
        os.environ.get("HERMES_COMMAND", "hermes"),
        "--max-concurrency",
        os.environ.get("HERMES_MAX_CONCURRENCY", "1"),
        "--queue-timeout",
        os.environ.get("HERMES_QUEUE_TIMEOUT", "30"),
    ]
    if os.environ.get("HERMES_CWD"):
        command.extend(["--cwd", os.environ["HERMES_CWD"]])
    if os.environ.get("HERMES_IGNORE_RULES", "0").lower() in {"1", "true", "yes", "on"}:
        command.append("--ignore-rules")
    if os.environ.get("HERMES_NO_ACCEPT_HOOKS", "0").lower() in {"1", "true", "yes", "on"}:
        command.append("--no-accept-hooks")
    if os.environ.get("HERMES_YOLO", "0").lower() in {"1", "true", "yes", "on"}:
        command.append("--yolo")
    return command


def build_gateway_command() -> list[str]:
    http_port = env_int("AURA_LILY_HTTP_PORT", DEFAULT_HTTP_PORT)
    return [
        sys.executable,
        "-m",
        "integrations.hermes_lily_cli.gateway",
        "--host",
        os.environ.get("AURA_LILY_WS_HOST", "0.0.0.0"),
        "--port",
        str(env_int("AURA_LILY_WS_PORT", DEFAULT_WS_PORT)),
        "--bridge-url",
        os.environ.get("AURA_LILY_BRIDGE_URL", f"http://127.0.0.1:{http_port}/turn"),
        "--bridge-timeout",
        os.environ.get("AURA_GATEWAY_BRIDGE_TIMEOUT_SECONDS", "30"),
    ]


def prepare_native_dirs() -> None:
    for name, default in (
        ("HERMES_HOME", ".aura/hermes"),
        ("HERMES_CWD", ".aura/workspace"),
        ("AURA_COMPANION_HOME", ".aura/companion"),
        ("AURA_PERSONA_HOME", ".aura/persona"),
    ):
        path_from_env(name, default).mkdir(parents=True, exist_ok=True)
    for name in ("AURA_LILY_HERMES_CONFIG_PATH", "AURA_LILY_AURA_RUNTIME_CONFIG_PATH"):
        value = os.environ.get(name, "").strip()
        if value:
            path_from_env(name, value).parent.mkdir(parents=True, exist_ok=True)


def bridge_is_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except (OSError, ValueError, TypeError):
        return False


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Aura Lily natively on this computer.")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="path to a KEY=VALUE env file")
    args = parser.parse_args()

    load_env_file(Path(args.env_file).expanduser())
    os.chdir(ROOT)
    prepare_native_dirs()

    hermes_command = resolve_hermes_command()
    if hermes_command is None:
        hermes_executable = first_command(os.environ.get("HERMES_COMMAND", "hermes"))
        print(
            f"Hermes command not found: {hermes_executable}. "
            "Install hermes-agent in this Python environment or set HERMES_COMMAND.",
            file=sys.stderr,
        )
        return 2

    bridge_port = env_int("AURA_LILY_HTTP_PORT", DEFAULT_HTTP_PORT)
    processes: list[subprocess.Popen[str]] = []

    def stop_all(*_: object) -> None:
        for process in reversed(processes):
            terminate_process(process)

    signal.signal(signal.SIGINT, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    print(f"Aura Lily bridge: http://127.0.0.1:{bridge_port}", flush=True)
    bridge = subprocess.Popen(build_bridge_command(), cwd=ROOT, env=os.environ.copy())
    processes.append(bridge)
    for _ in range(60):
        if bridge.poll() is not None:
            stop_all()
            return bridge.returncode or 1
        if bridge_is_healthy(bridge_port):
            break
        time.sleep(0.5)
    else:
        print("Aura Lily bridge did not become healthy within 30 seconds.", file=sys.stderr)
        stop_all()
        return 1

    print(
        f"Aura Lily gateway: ws://0.0.0.0:{env_int('AURA_LILY_WS_PORT', DEFAULT_WS_PORT)}/ws",
        flush=True,
    )
    gateway = subprocess.Popen(build_gateway_command(), cwd=ROOT, env=os.environ.copy())
    processes.append(gateway)

    try:
        while True:
            bridge_code = bridge.poll()
            gateway_code = gateway.poll()
            if bridge_code is not None or gateway_code is not None:
                stop_all()
                return gateway_code if gateway_code is not None else (bridge_code or 1)
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop_all()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
