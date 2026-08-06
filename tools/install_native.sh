#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${AURA_PYTHON:-python3}"
HERMES_VERSION="${HERMES_AGENT_VERSION:-0.15.2}"

cd "$ROOT_DIR"
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "hermes-agent==${HERMES_VERSION}"

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf 'Created .env from .env.example. Add your provider keys before starting Aura Lily.\n'
fi

mkdir -p .aura/hermes .aura/workspace .aura/companion .aura/persona
printf 'Native installation complete. Run: %s\n' '.venv/bin/python tools/run_native.py'
