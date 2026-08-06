from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from typing import Any

from .bridge import DEFAULT_TIMEOUT_SECONDS, HermesLilyBridge, HermesLilyConfig, command_from_string, tuple_from_csv
from .server import build_config, make_handler, parse_args as parse_server_args


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aura-lily-smoke",
        description="Run CLI and HTTP smoke checks for Aura Lily's Hermes bridge.",
    )
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--goal", default="请只回复：Aura Lily smoke ok。")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--toolsets", default=",".join(HermesLilyConfig().toolsets))
    parser.add_argument("--skills", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--hermes-command", default="hermes")
    parser.add_argument("--skip-cli", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    results: dict[str, Any] = {"ok": True, "checks": {}}

    if not args.skip_cli:
        cli_result = run_cli_check(args)
        results["checks"]["cli"] = cli_result.to_dict()
        results["ok"] = bool(results["ok"] and cli_result.ok)

    if not args.skip_http:
        http_result = run_http_check(args)
        results["checks"]["http"] = http_result
        results["ok"] = bool(results["ok"] and http_result.get("ok"))

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results["ok"] else 1


def run_cli_check(args: argparse.Namespace):
    config = HermesLilyConfig(
        command=command_from_string(args.hermes_command),
        provider=str(args.provider or ""),
        model=str(args.model or ""),
        cwd=str(args.cwd or ""),
        hermes_home=str(args.hermes_home or ""),
        toolsets=tuple_from_csv(args.toolsets),
        skills=tuple_from_csv(args.skills),
        timeout_seconds=float(args.timeout),
    )
    return HermesLilyBridge(config).run(str(args.goal))


def run_http_check(args: argparse.Namespace) -> dict[str, Any]:
    port = int(args.port or find_free_port(args.host))
    server_args = [
        "--host",
        args.host,
        "--port",
        str(port),
        "--timeout",
        str(args.timeout),
        "--hermes-command",
        args.hermes_command,
        "--toolsets",
        args.toolsets,
        "--queue-timeout",
        "5",
    ]
    if args.provider:
        server_args.extend(["--provider", args.provider])
    if args.model:
        server_args.extend(["--model", args.model])
    if args.skills:
        server_args.extend(["--skills", args.skills])
    if args.cwd:
        server_args.extend(["--cwd", args.cwd])
    if args.hermes_home:
        server_args.extend(["--hermes-home", args.hermes_home])

    config = build_config(parse_server_args(server_args))
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wait_for_server(args.host, port)
    try:
        health = request_json("GET", args.host, port, "/health", timeout=5)
        turn = request_json(
            "POST",
            args.host,
            port,
            "/turn",
            body={"goal": args.goal},
            timeout=max(5, int(args.timeout) + 5),
        )
        return {"ok": bool(health.get("ok") and turn.get("ok")), "health": health, "turn": turn}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request_json(
    method: str,
    host: str,
    port: int,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: int = 5,
    allow_error: bool = False,
) -> dict[str, Any]:
    conn = HTTPConnection(host, port, timeout=timeout)
    payload = b""
    headers = {}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read().decode("utf-8")
        if response.status >= 400 and not allow_error:
            raise RuntimeError(f"{method} {path} returned HTTP {response.status}: {data}")
        return json.loads(data or "{}")
    finally:
        conn.close()


def find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def wait_for_server(host: str, port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            request_json("GET", host, port, "/health", timeout=1)
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"server did not start on {host}:{port}")


if __name__ == "__main__":
    raise SystemExit(main())
