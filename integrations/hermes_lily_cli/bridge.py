from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_HERMES_COMMAND = ("hermes",)
DEFAULT_TOOLSETS = ("web", "terminal", "file", "code_execution", "skills")
DEFAULT_TIMEOUT_SECONDS = 180.0
STDOUT_LIMIT = 12_000
STDERR_LIMIT = 4_000

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s]+"),
)


@dataclass(frozen=True)
class HermesLilyConfig:
    """Configuration for the open-source Lily bridge.

    The bridge intentionally calls an installed ``hermes`` binary through its
    public CLI. It does not patch, import, copy, or mutate Hermes source files.
    """

    command: tuple[str, ...] = DEFAULT_HERMES_COMMAND
    provider: str = ""
    model: str = ""
    cwd: str = ""
    hermes_home: str = ""
    toolsets: tuple[str, ...] = DEFAULT_TOOLSETS
    skills: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    accept_hooks: bool = True
    ignore_rules: bool = False
    yolo: bool = False
    extra_args: tuple[str, ...] = ()
    extra_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HermesLilyResult:
    ok: bool
    status: str
    response: str
    request_id: str
    latency_ms: int
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "response": self.response,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "evidence": dict(self.evidence or {}),
        }


class HermesLilyBridge:
    def __init__(self, config: HermesLilyConfig | None = None) -> None:
        self.config = config or HermesLilyConfig()

    def run(self, goal: str, *, metadata: Mapping[str, Any] | None = None) -> HermesLilyResult:
        clean_goal = str(goal or "").strip()
        request_id = f"lily-{uuid.uuid4().hex[:12]}"
        if not clean_goal:
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="goal is required",
                request_id=request_id,
                latency_ms=0,
                evidence={"error": "empty_goal"},
            )

        command = build_hermes_command(clean_goal, self.config)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.cwd or None,
                env=_build_env(self.config),
                text=True,
                capture_output=True,
                timeout=max(1.0, float(self.config.timeout_seconds or DEFAULT_TIMEOUT_SECONDS)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            latency_ms = _latency_ms(started)
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="Hermes execution timed out.",
                request_id=request_id,
                latency_ms=latency_ms,
                evidence={
                    "stop_reason": "timeout",
                    "stdout_preview": scrub_text(exc.stdout or "", STDOUT_LIMIT),
                    "stderr_preview": scrub_text(exc.stderr or "", STDERR_LIMIT),
                    "command": public_command(command),
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )
        except OSError as exc:
            latency_ms = _latency_ms(started)
            return HermesLilyResult(
                ok=False,
                status="failed",
                response="Hermes process could not be started.",
                request_id=request_id,
                latency_ms=latency_ms,
                evidence={
                    "stop_reason": "process_error",
                    "error_type": exc.__class__.__name__,
                    "command": public_command(command),
                    "metadata": scrub_json_value(dict(metadata or {})),
                },
            )

        latency_ms = _latency_ms(started)
        stdout = scrub_text(completed.stdout or "", STDOUT_LIMIT).strip()
        stderr = scrub_text(completed.stderr or "", STDERR_LIMIT).strip()
        ok = completed.returncode == 0
        return HermesLilyResult(
            ok=ok,
            status="completed" if ok else "failed",
            response=stdout or stderr,
            request_id=request_id,
            latency_ms=latency_ms,
            evidence={
                "stop_reason": "finished" if ok else "runtime_process_error",
                "returncode": completed.returncode,
                "stdout_chars": len(completed.stdout or ""),
                "stderr": stderr if not ok else "",
                "command": public_command(command),
                "provider": self.config.provider,
                "model": self.config.model,
                "toolsets": list(self.config.toolsets),
                "skills": list(self.config.skills),
                "metadata": scrub_json_value(dict(metadata or {})),
            },
        )


def build_hermes_command(goal: str, config: HermesLilyConfig) -> list[str]:
    command = list(config.command or DEFAULT_HERMES_COMMAND)
    if not command:
        raise ValueError("Hermes command cannot be empty")
    command.extend(["-z", goal])
    if config.provider:
        command.extend(["--provider", config.provider])
    if config.model:
        command.extend(["--model", config.model])
    if config.toolsets:
        command.extend(["--toolsets", ",".join(config.toolsets)])
    for skill in config.skills:
        if skill:
            command.extend(["--skills", skill])
    if config.accept_hooks:
        command.append("--accept-hooks")
    if config.ignore_rules:
        command.append("--ignore-rules")
    if config.yolo:
        command.append("--yolo")
    command.extend(config.extra_args)
    return command


def command_from_string(value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return DEFAULT_HERMES_COMMAND
    return tuple(shlex.split(text)) or DEFAULT_HERMES_COMMAND


def tuple_from_csv(value: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    else:
        raw_items = list(value)
    return tuple(str(item).strip() for item in raw_items if str(item).strip())


def public_command(command: Sequence[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for item in command:
        text = str(item)
        if hide_next:
            out.append("<prompt>")
            hide_next = False
            continue
        if text == "-z":
            out.append(text)
            hide_next = True
            continue
        out.append(scrub_text(text, 180))
    return out


def scrub_text(value: str, limit: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def scrub_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "<truncated>"
    if isinstance(value, str):
        return scrub_text(value, 500)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {
            scrub_text(str(key), 120): scrub_json_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [scrub_json_value(item, depth=depth + 1) for item in value[:50]]
    return scrub_text(str(value), 500)


def _build_env(config: HermesLilyConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.hermes_home:
        env["HERMES_HOME"] = str(Path(config.hermes_home).expanduser())
    if config.accept_hooks:
        env.setdefault("HERMES_ACCEPT_HOOKS", "1")
    for key, value in dict(config.extra_env or {}).items():
        if value:
            env[str(key)] = str(value)
    return env


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
