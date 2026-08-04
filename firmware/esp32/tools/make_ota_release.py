#!/usr/bin/env python3
"""Build a public Aura OTA directory from an ESP-IDF build.

The application binary is always published. Resource files are optional and
are copied individually so the device can update them atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def https_base_url(value: str) -> str:
    text = value.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("--base-url must be an HTTPS directory URL")
    return text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--assets-version", required=True)
    parser.add_argument("--base-url", required=True, type=https_base_url)
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument("--assets-dir", type=Path, default=Path("assets"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        help="Asset path relative to assets/. Repeat for each changed file.",
    )
    args = parser.parse_args()

    app_source = args.build_dir / "aura_doudou.bin"
    if not app_source.is_file():
        parser.error(f"application binary not found: {app_source}")

    args.output.mkdir(parents=True, exist_ok=True)
    app_name = f"aura-{args.version}.bin"
    app_target = args.output / app_name
    shutil.copy2(app_source, app_target)

    resources = []
    for relative_text in args.asset:
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            parser.error(f"unsafe asset path: {relative_text}")
        source = args.assets_dir / relative
        if not source.is_file():
            parser.error(f"asset not found: {source}")
        target = args.output / "resources" / args.assets_version / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        resources.append(
            {
                "path": relative.as_posix(),
                "url": (
                    f"{args.base_url.rstrip('/')}/resources/"
                    f"{args.assets_version}/{relative.as_posix()}"
                ),
                "sha256": sha256(target),
                "size": target.stat().st_size,
            }
        )

    manifest = {
        "schema": 1,
        "channel": "stable",
        "app": {
            "version": args.version,
            "url": f"{args.base_url.rstrip('/')}/{app_name}",
            "sha256": sha256(app_target),
            "size": app_target.stat().st_size,
        },
        "resources": {"version": args.assets_version, "files": resources},
    }
    manifest_target = args.output / "manifest.json"
    manifest_target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(manifest_target)


if __name__ == "__main__":
    main()
