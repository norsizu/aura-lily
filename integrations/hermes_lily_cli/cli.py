from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bridge import (
    DEFAULT_TIMEOUT_SECONDS,
    HermesLilyBridge,
    HermesLilyConfig,
    command_from_string,
    tuple_from_csv,
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aura-lily-hermes",
        description="Run a Hermes agent task without patching Hermes source files.",
    )
    parser.add_argument("goal", nargs="?", help="task text; omit to read stdin")
    parser.add_argument("--json-input", action="store_true", help="read a JSON request from stdin")
    parser.add_argument("--provider", default="", help="Hermes provider override")
    parser.add_argument("--model", default="", help="Hermes model override")
    parser.add_argument("--toolsets", default=",".join(HermesLilyConfig().toolsets))
    parser.add_argument("--skills", default="", help="comma-separated Hermes skills to preload")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--cwd", default="", help="working directory for Hermes")
    parser.add_argument("--hermes-home", default="", help="optional HERMES_HOME override")
    parser.add_argument("--hermes-command", default="hermes", help="Hermes command, e.g. 'hermes' or '/path/hermes'")
    parser.add_argument("--ignore-rules", action="store_true")
    parser.add_argument("--no-accept-hooks", action="store_true")
    parser.add_argument("--yolo", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    payload: dict[str, Any] = {}
    if args.json_input:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "status": "failed", "error": f"invalid json: {exc}"}, ensure_ascii=False))
            return 2

    goal = str(payload.get("goal") or args.goal or "").strip()
    if not goal and not args.json_input:
        goal = sys.stdin.read().strip()

    config = HermesLilyConfig(
        command=command_from_string(str(payload.get("hermes_command") or args.hermes_command)),
        provider=str(payload.get("provider") or args.provider or ""),
        model=str(payload.get("model") or args.model or ""),
        cwd=str(payload.get("cwd") or args.cwd or ""),
        hermes_home=str(payload.get("hermes_home") or args.hermes_home or ""),
        toolsets=tuple_from_csv(payload.get("toolsets") or args.toolsets),
        skills=tuple_from_csv(payload.get("skills") or args.skills),
        timeout_seconds=float(payload.get("timeout_seconds") or args.timeout),
        accept_hooks=not bool(payload.get("no_accept_hooks") or args.no_accept_hooks),
        ignore_rules=bool(payload.get("ignore_rules") or args.ignore_rules),
        yolo=bool(payload.get("yolo") or args.yolo),
    )
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if config.cwd:
        Path(config.cwd).expanduser()

    result = HermesLilyBridge(config).run(goal, metadata=metadata)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
