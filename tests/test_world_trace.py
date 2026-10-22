"""W5 追溯工具：只读打开 companion.db，摊开一天的世界模型与账本流水。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.world import build_world_snapshot
from integrations.aura_persona_gateway.world_ledger import apply_world_change

ROOT = Path(__file__).resolve().parents[1]

import tools.world_trace as world_trace  # noqa: E402

CITY = "上海"
DAY_KEY = "2026-08-04"
NOW = 1_785_800_000.0
# 必须带 status=fresh + 中文 condition，build_world_snapshot 才会认这份天气。
WEATHER = {
    "status": "fresh",
    "city": CITY,
    "condition": "小雨",
    "temperature": 21.0,
    "humidity": 88.0,
    "source": "test",
    "observed_at": "2026-08-04T10:00:00+08:00",
}


def _config(tmp_path: Path) -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True)
    return PersonaGatewayConfig(
        enabled=True,
        persona_home=str(persona_home),
        companion_home=str(companion_home),
        hermes_home=str(tmp_path / "hermes-home"),
        aura_home_city=CITY,
    )


def _build_db(tmp_path: Path) -> tuple[PersonaGatewayConfig, LilyPersonaStore]:
    """真跑一遍 build_world_snapshot，留下状态、日程和账本流水。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update({"satiety": 30, "energy": 40, "mood": 60, "beans": 50, "trust": 50, "stress": 10})
    state["metadata"] = {"social_need": 70, "curiosity": 40, "privacy_sensitivity": 30, "resource_comfort": 30}
    state["metadata"]["emotion_state"] = {"label": "平静", "intensity": 3}
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        weather_snapshot=WEATHER,
        now=NOW,
    )
    assert snapshot["today_plan"]
    assert snapshot["recent_events"]
    # 账本流水用真实时钟写 created_at，测试里把它搬进目标那一天，
    # 这样 --day 的过滤逻辑才被真正跑到。
    events = store.list_recent_life_events(config.scope, event_prefixes=("ledger.",), limit=20)
    assert events
    import sqlite3

    conn = sqlite3.connect(str(config.companion_db_path))
    try:
        conn.executemany(
            "UPDATE companion_life_event SET created_at=? WHERE id=?",
            [(NOW - 1800 + index * 60, int(event["id"])) for index, event in enumerate(events)],
        )
        conn.commit()
    finally:
        conn.close()
    return config, store


def test_collect_trace_reports_state_plan_and_ledger(tmp_path):
    config, store = _build_db(tmp_path)

    payload = world_trace.collect_trace(config.companion_db_path, day_key=DAY_KEY)

    assert payload["day_key"] == DAY_KEY
    assert payload["city"] == CITY
    assert payload["scope"] == {
        "platform": config.scope.platform,
        "chat_id": config.scope.chat_id,
        "user_id": config.scope.user_id,
    }

    tracked = payload["state"]["tracked"]
    assert set(tracked) >= {"mood", "energy", "satiety", "stress", "trust", "beans", "affinity_xp"}
    assert tracked["beans"] == 40  # 下雨心情 -1，饿了买零食花了 10 金豆
    drives = payload["state"]["hidden_drives"]
    assert set(drives) == {"social_need", "curiosity", "privacy_sensitivity", "resource_comfort"}
    assert payload["state"]["emotion_state"] == {"label": "平静", "intensity": 3}

    plan = payload["plan"]
    assert len(plan) >= 8
    times = [item["time"] for item in plan if item["time"]]
    assert times == sorted(times)  # 按时间升序
    why_seen = [item for item in plan if item["why_text"]]
    assert why_seen, "每个槽位都应该能追溯到 why"
    assert all(item["why_rule"] for item in why_seen)
    assert any("下雨" in item["why_text"] for item in why_seen)

    ledger = payload["ledger"]
    assert ledger
    assert all(item["event_type"].startswith("ledger.") for item in ledger)
    by_type = {item["event_type"] for item in ledger}
    assert "ledger.weather" in by_type
    assert "ledger.drive_blend" in by_type
    assert "ledger.hunger_drive" in by_type
    weather = next(item for item in ledger if item["event_type"] == "ledger.weather")
    assert weather["event_key"] == f"{DAY_KEY}:rain"
    assert weather["cause"]
    assert weather["changes"] == ["心情 60→59", "体力 40→39"]
    snack = next(item for item in ledger if item["event_type"] == "ledger.hunger_drive")
    assert "饱腹 30 ≤ 30" in snack["cause"]
    assert "金豆 50→40" in snack["changes"]
    drive = next(item for item in ledger if item["event_type"] == "ledger.drive_blend")
    assert drive["changes"] == ["社交 70→64"]
    assert "社交从 70 漂到 64" == drive["cause"]
    # 流水按时间升序。
    stamps = [item["time"] for item in ledger]
    assert stamps == sorted(stamps)


def test_collect_trace_matches_render_output(tmp_path):
    config, _store = _build_db(tmp_path)

    payload = world_trace.collect_trace(config.companion_db_path, day_key=DAY_KEY)
    text = world_trace._render(payload)

    assert "== 当前状态 ==" in text
    assert "隐藏驱动" in text
    assert "情绪状态" in text
    assert "== 今日日程 ==" in text
    assert "why(" in text
    assert "== 世界账本流水 ==" in text
    assert "cause：" in text
    assert "→" in text


def test_collect_trace_handles_missing_rows(tmp_path):
    config, _store = _build_db(tmp_path)

    payload = world_trace.collect_trace(config.companion_db_path, day_key="1999-01-01")

    assert payload["plan"] == []
    assert payload["ledger"] == []
    assert payload["city"] == ""
    assert "当天没有日程" in world_trace._render(payload)


def _force_created_at(db_path: Path, keys: dict[str, float]) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "UPDATE companion_life_event SET created_at=? WHERE event_key=?",
            [(float(stamp), key) for key, stamp in keys.items()],
        )
        conn.commit()
    finally:
        conn.close()


def test_collect_trace_reports_parent_key_and_custom_scope(tmp_path):
    config, store = _build_db(tmp_path)
    store.record_life_event(
        config.scope,
        event_type="ledger.world_clock",
        event_key="trace:parent",
        title="父事件",
        description="上一件事",
        visibility="private",
    )
    parent_id = store.list_recent_life_events(
        config.scope, event_prefix="ledger.world_clock", limit=1,
    )[0]["id"]
    apply_world_change(
        store,
        config.scope,
        store.get_or_create_state(config.scope),
        {"energy": -4},
        source="world_clock",
        cause="跟着父事件发生",
        title="子事件",
        event_key="trace:child",
        parent_key=f"life_event:{parent_id}",
    )
    _force_created_at(
        config.companion_db_path,
        {"trace:parent": NOW - 2400, "trace:child": NOW - 1200},
    )

    payload = world_trace.collect_trace(config.companion_db_path, day_key=DAY_KEY)
    child = next(item for item in payload["ledger"] if item["event_key"] == "trace:child")

    assert child["parent_key"] == f"life_event:{parent_id}"
    assert child["cause"] == "跟着父事件发生"
    # 体力是这一笔自己的变化；后面几项是之前没过记账阈值、被这笔一起补齐的
    # 隐藏驱动漂移（账本按时间重放必须能回到当前值）。
    assert child["changes"] == ["体力 41→37", "社交 64→59", "好奇 44→47", "安心 31→32"]

    other = world_trace.collect_trace(
        config.companion_db_path, platform="other", chat_id="other-chat", user_id="other-user", day_key=DAY_KEY,
    )
    assert other["state"] == {}
    assert other["plan"] == []
    assert other["ledger"] == []


def test_collect_trace_is_read_only(tmp_path):
    config, store = _build_db(tmp_path)
    before = store.get_or_create_state(config.scope)["beans"]
    db_bytes = Path(config.companion_db_path).read_bytes()

    world_trace.collect_trace(config.companion_db_path, day_key=DAY_KEY)

    assert Path(config.companion_db_path).read_bytes() == db_bytes
    assert store.get_or_create_state(config.scope)["beans"] == before


def test_main_json_output(tmp_path, capsys):
    config, _store = _build_db(tmp_path)

    assert world_trace.main(["--db", str(config.companion_db_path), "--day", DAY_KEY, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["day_key"] == DAY_KEY
    assert payload["plan"]
    assert payload["ledger"]
    assert all(item["why_rule"] for item in payload["plan"])
    assert all("before" in item or item["changes"] for item in payload["ledger"])


def test_main_text_output(tmp_path, capsys):
    config, _store = _build_db(tmp_path)

    assert world_trace.main(["--db", str(config.companion_db_path), "--day", DAY_KEY]) == 0
    out = capsys.readouterr().out

    assert "世界模型追溯" in out
    assert "scope=" in out
    assert "== 世界账本流水 ==" in out
    assert "ledger.weather" in out


def test_main_reports_missing_database(tmp_path, capsys):
    assert world_trace.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert "数据库不存在" in capsys.readouterr().err


def test_main_reports_invalid_database(tmp_path, capsys):
    broken = tmp_path / "broken.db"
    broken.write_text("not a sqlite database", encoding="utf-8")

    assert world_trace.main(["--db", str(broken)]) == 3
    assert "打开数据库失败" in capsys.readouterr().err


def test_main_defaults_to_today(tmp_path, capsys):
    config, _store = _build_db(tmp_path)

    assert world_trace.main(["--db", str(config.companion_db_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    import datetime as dt

    assert payload["day_key"] == dt.date.today().isoformat()
    assert payload["plan"] == []


def test_script_entry_point_runs_as_a_subprocess(tmp_path):
    config, _store = _build_db(tmp_path)

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "world_trace.py"),
         "--db", str(config.companion_db_path), "--day", DAY_KEY, "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ledger"]


def test_day_plan_payloads_are_written_with_why(tmp_path):
    """落库的日程也要带 why，追溯工具才能读到理由。"""
    config, store = _build_db(tmp_path)
    rows = store.today_plan(config.scope, day_key=DAY_KEY)

    assert rows
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        why = payload.get("why") if isinstance(payload.get("why"), dict) else {}
        assert why.get("rule")
        assert why.get("text")


def test_same_seed_produces_the_same_stored_plan_and_why(tmp_path):
    """同种子同输入 → 落库的日程与 why 完全一致（追溯才有意义）。"""
    first_config, first_store = _build_db(tmp_path / "one")
    second_config, second_store = _build_db(tmp_path / "two")

    first = world_trace.collect_trace(first_config.companion_db_path, day_key=DAY_KEY)
    second = world_trace.collect_trace(second_config.companion_db_path, day_key=DAY_KEY)

    def rows(payload):
        return [
            (item["time"], item["slot_key"], item["title"], item["location"], item["why_rule"], item["why_text"])
            for item in payload["plan"]
        ]

    assert rows(first) == rows(second)
    assert len(rows(first)) >= 8
    assert first_store.get_or_create_state(first_config.scope)["beans"] == 40
    assert second_store.get_or_create_state(second_config.scope)["beans"] == 40


@pytest.mark.parametrize("flag", ["--help"])
def test_cli_exposes_the_documented_flags(flag):
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "world_trace.py"), flag],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )

    assert result.returncode == 0
    for expected in ("--db", "--scope-user", "--day", "--json"):
        assert expected in result.stdout
