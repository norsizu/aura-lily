"""W3 离线回顾：离开超过一小时后，用账本流水生成可朗读的回顾。"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.state_rules import apply_user_interaction_delta
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.time_context import resolve_city_tzinfo
from integrations.aura_persona_gateway.world import (
    OFFLINE_RECAP_MIN_GAP_SECONDS,
    build_offline_recap,
    build_world_snapshot,
)
from integrations.aura_persona_gateway.world_ledger import LEDGER_EVENT_PREFIX, apply_world_change

CITY = "上海"
NOW = 1_785_800_000.0


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


def _seed_state(store: LilyPersonaStore, config: PersonaGatewayConfig, **overrides) -> dict:
    state = store.get_or_create_state(config.scope)
    state.update({"satiety": 30, "energy": 40, "mood": 60, "beans": 50, "trust": 50, "stress": 10})
    state["metadata"] = {"social_need": 70, "curiosity": 40, "privacy_sensitivity": 30, "resource_comfort": 30}
    state.update(overrides)
    store.save_state(config.scope, state)
    return state


def _ledger_event(store: LilyPersonaStore, config: PersonaGatewayConfig, state: dict, *, delta: dict, title: str):
    updated, summary = apply_world_change(
        store,
        config.scope,
        state,
        delta,
        source="world_clock",
        cause="测试流水",
        title=title,
        event_key=f"recap:{title}:{time.time_ns()}",
    )
    assert summary is not None
    return updated


def _force_created_at(db_path: Path, titles: dict[str, float]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        for title, created_at in titles.items():
            conn.execute(
                "UPDATE companion_life_event SET created_at=? WHERE title=?",
                (float(created_at), title),
            )
        conn.commit()
    finally:
        conn.close()


def _hhmm(stamp: float) -> str:
    return dt.datetime.fromtimestamp(float(stamp), resolve_city_tzinfo(CITY)).strftime("%H:%M")


def test_build_offline_recap_formats_time_title_and_effects(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    state = _ledger_event(
        store, config, state, delta={"mood": 5, "energy": -7}, title="在公园散了步",
    )
    _ledger_event(store, config, state, delta={"satiety": 12, "beans": -20}, title="吃了顿饭")

    now = time.time()
    recap = build_offline_recap(
        store, config.scope, since_ts=now - 3600, now=now + 1, city=CITY,
    )

    assert len(recap) == 2
    for line in recap:
        assert line.startswith(_hhmm(time.time())[:2]) or line[:2].isdigit()
        assert ":" in line[:3]
    assert "在公园散了步（心情+5，体力-7）" in recap[0]
    assert "吃了顿饭（饱腹+12，金币-20）" in recap[1]


def test_recap_only_covers_events_inside_the_window(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    state = _ledger_event(store, config, state, delta={"mood": 3}, title="窗口内")
    _ledger_event(store, config, state, delta={"mood": 9}, title="窗口外")
    _force_created_at(config.companion_db_path, {"窗口内": NOW - 600, "窗口外": NOW - 7200})

    recap = build_offline_recap(
        store, config.scope, since_ts=NOW - 3600, now=NOW, city=CITY,
    )

    assert [line.split(" ", 1)[1] for line in recap] == ["窗口内（心情+3）"]
    assert recap[0].startswith(_hhmm(NOW - 600))


def test_recap_respects_the_limit(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    for index in range(5):
        state = _ledger_event(store, config, state, delta={"energy": -1}, title=f"第{index}件事")
    _force_created_at(
        config.companion_db_path,
        {f"第{index}件事": NOW - 3000 + index for index in range(5)},
    )

    recap = build_offline_recap(
        store, config.scope, since_ts=NOW - 3600, now=NOW + 3600, city=CITY, limit=2,
    )

    assert len(recap) == 2
    assert "第0件事" in recap[0]
    assert "第1件事" in recap[1]


def test_recap_is_empty_when_the_window_has_no_events(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    _seed_state(store, config)

    assert build_offline_recap(store, config.scope, since_ts=NOW - 3600, now=NOW, city=CITY) == []
    # since >= now 或 since 非法都不做回顾。
    assert build_offline_recap(store, config.scope, since_ts=NOW, now=NOW, city=CITY) == []
    assert build_offline_recap(store, config.scope, since_ts=0, now=NOW, city=CITY) == []


def test_short_gap_does_not_inject_a_recap(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state["metadata"]["previous_user_interaction_at"] = NOW - OFFLINE_RECAP_MIN_GAP_SECONDS + 60
    store.save_state(config.scope, state)
    _ledger_event(store, config, state, delta={"mood": 5}, title="刚发生的事")

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW,
    )

    assert snapshot["offline_recap"] == []
    assert snapshot["offline_recap_hours"] == 0.0
    assert not any("你不在的这段时间" in line for line in snapshot["prompt_lines"])
    assert snapshot["debug"]["offline_recap_count"] == 0


def test_recap_is_injected_once_after_a_long_gap(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state["metadata"]["previous_user_interaction_at"] = NOW - 3 * 3600
    store.save_state(config.scope, state)
    state = _ledger_event(store, config, state, delta={"mood": 5, "energy": -7}, title="在公园散了步")
    _force_created_at(config.companion_db_path, {"在公园散了步": NOW - 1800})

    first = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW,
    )
    second = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW + 120,
    )

    assert len(first["offline_recap"]) == 1
    assert "在公园散了步（心情+5，体力-7）" in first["offline_recap"][0]
    assert first["offline_recap_hours"] == 3.0
    prompt = "\n".join(first["prompt_lines"])
    assert "你不在的这段时间（距上次聊天约 3 小时）" in prompt
    assert "只能讲以下真实发生过的事，不要编造" in prompt
    assert "- " in prompt

    # 同一段"离开"只讲一次，第二次不再注入。
    assert second["offline_recap"] == []
    assert not any("你不在的这段时间" in line for line in second["prompt_lines"])
    marker = store.get_or_create_state(config.scope)["metadata"]["offline_recap_for"]
    assert marker == str(int(NOW - 3 * 3600))


def test_empty_recap_is_not_injected_and_does_not_mark_the_window(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state["metadata"]["previous_user_interaction_at"] = NOW - 5 * 3600
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW,
    )
    metadata = store.get_or_create_state(config.scope)["metadata"]

    assert snapshot["offline_recap"] == []
    assert "offline_recap_for" not in metadata


def test_same_turn_interaction_does_not_count_as_time_away(tmp_path):
    """一轮对话先推进 last_user_interaction_at，快照不能把这段当离线窗口。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    updated = apply_user_interaction_delta(state, now=NOW - 10)
    store.save_state(config.scope, updated)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW,
    )

    assert snapshot["offline_recap"] == []


def test_recap_reads_the_ledger_prefix_even_without_world_events(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state = _ledger_event(store, config, state, delta={"stress": -4}, title="发了会儿呆")
    _force_created_at(config.companion_db_path, {"发了会儿呆": NOW - 1800})

    recap = build_offline_recap(store, config.scope, since_ts=NOW - 3600, now=NOW, city=CITY)

    assert recap == [f"{_hhmm(NOW - 1800)} 发了会儿呆（压力-4）"]
    event_type = store.list_recent_life_events(
        config.scope, event_prefix=LEDGER_EVENT_PREFIX, limit=1,
    )[0]["event_type"]
    assert event_type == f"{LEDGER_EVENT_PREFIX}world_clock"


def test_speculative_turn_writes_no_recap_marker(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state["metadata"]["previous_user_interaction_at"] = NOW - 4 * 3600
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW,
        persist=False,
    )

    assert snapshot["offline_recap"] == []
    assert "offline_recap_for" not in store.get_or_create_state(config.scope)["metadata"]


def test_background_world_tick_does_not_consume_the_recap(tmp_path):
    """状态推送的 world_tick 不能抢先把离线回顾标记成已讲过。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    state["metadata"]["previous_user_interaction_at"] = NOW - 3 * 3600
    store.save_state(config.scope, state)
    _ledger_event(store, config, state, delta={"mood": 5}, title="在公园散了步")
    _force_created_at(config.companion_db_path, {"在公园散了步": NOW - 1800})

    tick = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        now=NOW,
    )
    turn = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "chat"},
        offline_recap=True,
        now=NOW + 60,
    )

    assert tick["offline_recap"] == []
    assert len(turn["offline_recap"]) == 1
