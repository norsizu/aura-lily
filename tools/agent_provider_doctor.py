#!/usr/bin/env python3
"""Diagnose Aura Agent provider configuration without printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPANION = ROOT / "companion"
if str(COMPANION) not in sys.path:
    sys.path.insert(0, str(COMPANION))

from aura_companion.agent_runtime.provider_diagnostics import diagnose_provider_config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=os.getenv("AURA_AGENT_PROVIDER", ""))
    parser.add_argument("--model", default=os.getenv("AURA_AGENT_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("AURA_AGENT_BASE_URL", ""))
    parser.add_argument("--adapter", default=os.getenv("AURA_AGENT_ADAPTER", "") or os.getenv("AURA_AGENT_WIRE_API", ""))
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    args = parser.parse_args()

    diag = diagnose_provider_config(
        args.provider,
        model=args.model,
        base_url=args.base_url,
        adapter=args.adapter,
        env=os.environ,
    )
    payload = diag.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(payload)
    return 0 if diag.ok else 2


def print_human(payload: dict) -> None:
    status = "OK" if payload.get("ok") else "NEEDS ATTENTION"
    print(f"Aura Agent Provider Doctor: {status}")
    print(f"- provider: {payload.get('provider_name') or '<missing>'}")
    print(f"- model: {payload.get('model') or '<missing>'}")
    print(f"- adapter: {payload.get('adapter') or '<auto>'} ({payload.get('adapter_source')})")
    print(f"- base_url: {payload.get('base_url') or '<missing>'}")
    print(f"- endpoint_host: {payload.get('endpoint_host') or '<missing>'}")
    print(f"- api_key_present: {bool(payload.get('api_key_present'))}")
    credential_env = payload.get("credential_env") or {}
    if credential_env:
        configured = [key for key, value in credential_env.items() if value == "configured"]
        missing = [key for key, value in credential_env.items() if value != "configured"]
        print(f"- credential_env configured: {', '.join(configured) if configured else '<none>'}")
        print(f"- credential_env accepted: {', '.join(configured + missing)}")
    capabilities = payload.get("capabilities") or []
    print(f"- capabilities: {', '.join(capabilities) if capabilities else '<unknown>'}")
    for key in ("warnings", "errors", "hints"):
        values = payload.get(key) or []
        if values:
            print(f"- {key}:")
            for item in values:
                print(f"  - {item}")


if __name__ == "__main__":
    raise SystemExit(main())
