from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "tools" / "run_native.py"
    spec = importlib.util.spec_from_file_location("aura_lily_native_launcher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_native_launcher_loads_env_without_overwriting_shell(tmp_path, monkeypatch):
    launcher = _load_launcher()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nAURA_TEST_VALUE=from-file\nQUOTED_VALUE='hello world'\n", encoding="utf-8"
    )
    monkeypatch.setenv("AURA_TEST_VALUE", "from-shell")
    monkeypatch.delenv("QUOTED_VALUE", raising=False)

    launcher.load_env_file(env_file)

    assert launcher.os.environ["AURA_TEST_VALUE"] == "from-shell"
    assert launcher.os.environ["QUOTED_VALUE"] == "hello world"


def test_native_launcher_builds_two_local_service_commands(monkeypatch):
    launcher = _load_launcher()
    monkeypatch.setenv("AURA_LILY_HTTP_PORT", "18765")
    monkeypatch.setenv("AURA_LILY_WS_PORT", "18787")
    monkeypatch.setenv("HERMES_HOME", ".aura/hermes")
    monkeypatch.setenv("HERMES_CWD", ".aura/workspace")

    bridge = launcher.build_bridge_command()
    gateway = launcher.build_gateway_command()

    assert bridge[:3] == [launcher.sys.executable, "-m", "integrations.hermes_lily_cli.server"]
    assert bridge[bridge.index("--port") + 1] == "18765"
    assert bridge[bridge.index("--cwd") + 1] == ".aura/workspace"
    assert gateway[:3] == [launcher.sys.executable, "-m", "integrations.hermes_lily_cli.gateway"]
    assert gateway[gateway.index("--bridge-url") + 1] == "http://127.0.0.1:18765/turn"
    assert gateway[gateway.index("--port") + 1] == "18787"


def test_native_launcher_resolves_hermes_from_its_virtualenv(tmp_path, monkeypatch):
    launcher = _load_launcher()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hermes.chmod(0o755)
    monkeypatch.setattr(launcher.sys, "executable", str(bin_dir / "python"))
    monkeypatch.setenv("HERMES_COMMAND", "hermes")
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

    resolved = launcher.resolve_hermes_command()

    assert resolved == str(hermes)
    assert launcher.os.environ["HERMES_COMMAND"] == str(hermes)
