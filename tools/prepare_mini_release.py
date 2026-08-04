#!/usr/bin/env python3
"""Prepare a dry-run or local export of the Aura Lily release allowlist.

This script is intentionally conservative: it never deletes files and does not
publish anything. By default it only prints the release file list. Pass
`--export --dest <dir>` to copy the allowlisted files into a clean directory.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    path: str
    required: bool = True


@dataclass(frozen=True)
class CollectedFile:
    source: Path
    entry: str


ALLOWLIST = [
    Entry(".dockerignore"),
    Entry(".env.example"),
    Entry(".gitignore"),
    Entry("Dockerfile"),
    Entry("README.md"),
    Entry("AURA_LILY_STATUS_AND_DEPLOYMENT.md"),
    Entry("LICENSE", required=False),
    Entry("docker-compose.yml"),
    Entry("firmware/esp32/CMakeLists.txt"),
    Entry("firmware/esp32/partitions.csv"),
    Entry("firmware/esp32/sdkconfig.defaults"),
    Entry("firmware/esp32/assets/"),
    Entry("firmware/esp32/main/"),
    Entry("firmware/esp32/tools/"),
    Entry("integrations/__init__.py"),
    Entry("integrations/hermes_lily_cli/"),
    Entry("integrations/aura_persona_gateway/"),
    Entry("requirements.txt"),
    Entry("tools/agent_provider_doctor.py"),
    Entry("tools/agent_provider_gate.py"),
    Entry("tools/prepare_mini_release.py"),
    Entry("tools/voice_latency_benchmark.py"),
    Entry("tools/voice_latency_matrix.py"),
    Entry("tests/test_hermes_lily_cli.py"),
    Entry("tests/test_aura_persona_gateway.py", required=False),
]


EXCLUDE_DIRS = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "managed_components",
    "node_modules",
    "target",
}

EXCLUDE_SUFFIXES = {
    ".app",
    ".bak",
    ".db",
    ".dmg",
    ".log",
    ".pyc",
    ".tsbuildinfo",
}

EXCLUDE_NAMES = {
    ".DS_Store",
    "sdkconfig.old",
}

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"api[_-]?key\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"password\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"secret\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"token\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE),
    re.compile(r"100\.84\."),
    re.compile(r"127\.0\.0\.1:3000"),
]

BOUNDARY_PATTERNS = [
    re.compile(r"aura_companion\.life"),
    re.compile(r"aura_companion\.persona"),
    re.compile(r"aura_companion\.social"),
    re.compile(r"aura_companion\.cultural"),
    re.compile(r"apps/desktop"),
]
SECRET_SCAN_RE = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in SECRET_PATTERNS), re.IGNORECASE)
BOUNDARY_SCAN_RE = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in BOUNDARY_PATTERNS))

SCAN_IGNORE_RELATIVE_PATHS = {
    Path("docs/REPO_CLEANUP_INVENTORY_2026-05-18.md"),
    Path("tests/test_aura_persona_gateway.py"),
    Path("tests/test_hermes_lily_cli.py"),
    Path("tools/prepare_mini_release.py"),
    Path("tools/voice_latency_benchmark.py"),
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def should_exclude(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return True
    if path.name in EXCLUDE_NAMES:
        return True
    if path.suffix in EXCLUDE_SUFFIXES:
        return True
    return False


def iter_files(root: Path, entry: Entry) -> tuple[list[Path], list[str]]:
    source = root / entry.path
    warnings: list[str] = []
    if not source.exists():
        level = "missing required" if entry.required else "missing optional"
        warnings.append(f"{level}: {entry.path}")
        return [], warnings

    if source.is_file():
        return ([] if should_exclude(source) else [source]), warnings

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name not in EXCLUDE_DIRS]
        for filename in filenames:
            candidate = current / filename
            if not should_exclude(candidate):
                files.append(candidate)
    return sorted(files), warnings


def collect(root: Path) -> tuple[list[CollectedFile], list[str]]:
    files: list[CollectedFile] = []
    warnings: list[str] = []
    seen: set[Path] = set()
    for entry in ALLOWLIST:
        entry_files, entry_warnings = iter_files(root, entry)
        warnings.extend(entry_warnings)
        for file_path in entry_files:
            rel = file_path.relative_to(root)
            if rel not in seen:
                files.append(CollectedFile(source=file_path, entry=entry.path))
                seen.add(rel)
    return sorted(files, key=lambda item: item.source), warnings


def copy_files(root: Path, dest: Path, files: list[CollectedFile]) -> None:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"Destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    for item in files:
        source = item.source
        rel = source.relative_to(root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def scan_text_file(path: Path, pattern: re.Pattern[str]) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pattern.search(line):
            hits.append(f"{path}:{lineno}: {pattern.pattern}")
    return hits


def scan_export(dest: Path) -> tuple[list[str], list[str]]:
    secret_hits: list[str] = []
    boundary_hits: list[str] = []
    for path in sorted(p for p in dest.rglob("*") if p.is_file()):
        rel = path.relative_to(dest)
        if rel in SCAN_IGNORE_RELATIVE_PATHS:
            continue
        secret_hits.extend(scan_text_file(path, SECRET_SCAN_RE))
        boundary_hits.extend(scan_text_file(path, BOUNDARY_SCAN_RE))
    return secret_hits, boundary_hits


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="copy allowlisted files")
    parser.add_argument("--dest", type=Path, help="destination directory for --export")
    parser.add_argument("--scan", action="store_true", help="scan exported files")
    parser.add_argument("--list", action="store_true", help="print all allowlisted files")
    parser.add_argument("--verbose", action="store_true", help="print each file and its allowlist source")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    files, warnings = collect(root)

    print(f"repo: {root}")
    print(f"allowlist entries: {len(ALLOWLIST)}")
    print(f"exportable files: {len(files)}")

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.list or not args.export:
        for item in files:
            rel = item.source.relative_to(root)
            if args.verbose:
                print(f"{rel}\tallowlist={item.entry}")
            else:
                print(rel)

    if not args.export:
        print("dry-run only; pass --export --dest <dir> to copy files")
        return 0

    dest = args.dest or Path(tempfile.mkdtemp(prefix="aura-lily-export-"))
    copy_files(root, dest, files)
    print(f"exported: {dest}")
    if args.verbose:
        print("export manifest:")
        for item in files:
            rel = item.source.relative_to(root)
            print(f"{rel}\tallowlist={item.entry}")

    if args.scan:
        secret_hits, boundary_hits = scan_export(dest)
        if secret_hits:
            print("secret/private endpoint scan hits:", file=sys.stderr)
            print("\n".join(secret_hits), file=sys.stderr)
        if boundary_hits:
            print("closed-boundary scan hits:", file=sys.stderr)
            print("\n".join(boundary_hits), file=sys.stderr)
        if secret_hits or boundary_hits:
            return 2
        print("scan: ok")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
