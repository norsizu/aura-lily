from __future__ import annotations

import json
import sqlite3
import subprocess
import base64
import datetime as dt
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from integrations.aura_persona_gateway.assets import load_persona_assets
from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.context import build_persona_context
from integrations.aura_persona_gateway.grounded_intent import classify_grounded_current_intent
from integrations.aura_persona_gateway.outlets import OutletSignals
from integrations.aura_persona_gateway.query_context import correction_focus_text, resolve_query_context
from integrations.aura_persona_gateway.response_contract import SPOKEN_REPLY_INSTRUCTION
from integrations.aura_persona_gateway.reminders import parse_reminder_request
from integrations.aura_persona_gateway.runtime import (
    AuraRuntimeConfig,
    cached_weather_snapshot,
    load_aura_runtime_config,
    save_aura_runtime_config,
)
from integrations.aura_persona_gateway import llm as aura_llm_module
from integrations.aura_persona_gateway.state_rules import apply_user_interaction_delta, compute_affinity_level, state_context_summary
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.turn import (
    AuraPersonaGateway,
    _casual_continuation_is_unfounded_guess,
    _compact_streaming_voice_model_text,
    _dedupe_repeated_spoken_sentences,
    _is_detail_request,
    _is_enumeration_request,
    _prepare_streaming_voice_model_text,
    _streaming_voice_model_text_is_complete,
    _voice_reply_budget,
    _voice_stream_max_tokens,
)
from integrations.aura_persona_gateway.voice_turn import VoiceTurnVerdict, execute_voice_turn
from integrations.aura_persona_gateway.weather import fetch_current_weather, refresh_user_weather_if_needed, weather_snapshot_for_query
from integrations.aura_persona_gateway.world import (
    _settle_completed_world_state,
    build_world_snapshot,
    generate_day_plan,
    render_world_prompt,
)
from integrations.aura_persona_gateway.world_assets import load_world_canon
from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig, HermesLilyResult
from integrations.hermes_lily_cli.server import build_config, make_handler, parse_args


def _basic_auth(user: str = "admin", admin_pass: str = "unit-pass") -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{admin_pass}".encode("utf-8")).decode("ascii")
    return {"authorization": f"Basic {raw}"}


def _config(tmp_path: Path, *, enabled: bool = True, aura_home_city: str = "") -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True)
    (persona_home / "persona" / "soul.md").write_text("主 soul\n不要客服腔。", encoding="utf-8")
    return PersonaGatewayConfig(
        enabled=enabled,
        persona_home=str(persona_home),
        companion_home=str(companion_home),
        hermes_home=str(tmp_path / "hermes-home"),
        aura_home_city=aura_home_city,
        admin_token=("unit-test-token"),
        include_debug_context=True,
    )


def _im_message_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT count(*) FROM companion_im_message").fetchone()[0])


def _stream_direct_aura_model_text(
    tmp_path: Path,
    monkeypatch,
    model_text: str,
    *,
    user_text: str = "我今天想聊聊最近状态，你自然回应一句。",
    prior_aura_messages: tuple[str, ...] = (),
) -> list[dict]:
    class FakeStreamResponse:
        def __enter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": model_text}}]}, ensure_ascii=False)
            return iter([f"data: {payload}\n\n".encode("utf-8"), b"data: [DONE]\n\n"])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    for body in prior_aura_messages:
        store.save_im_message(config.scope, direction="aura", message_type="aura_text", body=body, status="sent")
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return list(
        gateway.run_direct_turn_stream(
            user_text,
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )


def _stream_direct_aura_model_chunks(
    tmp_path: Path,
    monkeypatch,
    chunks: tuple[str, ...],
    *,
    user_text: str = "我今天想聊聊最近状态，你自然回应一句。",
) -> list[dict]:
    class FakeStreamResponse:
        def __enter__(self):
            events = []
            for chunk in chunks:
                payload = json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
                events.append(f"data: {payload}\n\n".encode("utf-8"))
            events.append(b"data: [DONE]\n\n")
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return list(
        gateway.run_direct_turn_stream(
            user_text,
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )


def test_loads_single_main_soul_from_runtime(tmp_path):
    config = _config(tmp_path)

    assets = load_persona_assets(config)

    assert assets.available is True
    assert "主 soul" in assets.soul
    assert assets.source_path.endswith("soul.md")


def test_relationship_and_state_summary_keep_required_fields():
    state = {
        "mood": 90,
        "energy": 70,
        "satiety": 80,
        "beans": 120,
        "trust": 82,
        "stress": 5,
        "affinity_xp": 254,
        "scene": "study",
        "metadata": {
            "current_activity": "在书房看书",
            "current_location": "study",
            "location_label": "书房",
            "social_need": 48,
            "curiosity": 88,
            "relationship_flags": {"strained": False},
        },
    }

    summary = state_context_summary(state)

    assert compute_affinity_level(254) == 5
    assert summary["relationship"]["label"] == "信任"
    assert summary["current_activity"] == "在书房看书"
    assert summary["social_need"] == 48


def test_world_snapshot_generates_lily_plan_without_exposing_location_on_ordinary_reply(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["metadata"] = {
        "current_activity": "逛商场",
        "current_location": "mall",
        "location_label": "具体商场",
        "world_current_source": "manual",
        "world_manual_override": True,
    }
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context=resolve_query_context("你现在觉得回复慢吗？", aura_home_city="上海").to_dict(),
        now=1_782_880_000,
    )
    prompt = render_world_prompt(snapshot)

    assert snapshot["enabled"] is True
    assert len(snapshot["today_plan"]) >= 6
    assert snapshot["mention_policy"]["allow_location"] is False
    assert "具体商场" not in prompt
    assert "陪伴时光" not in json.dumps(snapshot["today_plan"], ensure_ascii=False)
    assert "具体商场" not in json.dumps(snapshot["today_plan"], ensure_ascii=False)


def test_world_snapshot_keeps_clock_state_for_ordinary_voice_turn(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context=resolve_query_context("测试一下", aura_home_city="上海").to_dict(),
        voice_low_latency=True,
        now=1_782_880_000,
    )

    assert snapshot["enabled"] is True
    assert snapshot["today_plan"]
    assert snapshot["debug"]["schema"] == "aura_world_v2"
    with sqlite3.connect(config.companion_db_path) as conn:
        count = conn.execute("SELECT count(*) FROM companion_day_plan").fetchone()[0]
    assert count == len(snapshot["today_plan"])


def test_world_clock_advances_at_each_scheduled_slot_and_sleeps_before_wake(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    initial = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context={"intent": "world_tick"},
        now=1_782_880_000,
    )
    plan = initial["today_plan"]
    before_wake = float(plan[0]["scheduled_at"]) - 60

    sleeping = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        now=before_wake,
    )
    assert sleeping["current"]["slot_key"] == "sleep"
    assert sleeping["current"]["location_key"] == "home.bedroom"

    for slot in plan:
        current = build_world_snapshot(
            config=config,
            store=store,
            state=store.get_or_create_state(config.scope),
            query_context={"intent": "world_tick"},
            now=float(slot["scheduled_at"]) + 1,
        )["current"]
        assert current["slot_key"] == slot["slot_key"]
        assert current["location_key"] == slot["payload"]["location_key"]

    dinner = next(item for item in plan if item["slot_key"] == "dinner")
    night = next(item for item in plan if item["slot_key"] == "night_settle")
    dinner_end = float(dinner["scheduled_at"]) + int(dinner["payload"]["duration_minutes"]) * 60
    between = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        now=min(dinner_end + 1, float(night["scheduled_at"]) - 1),
    )["current"]
    assert between["location_key"] == "home.living_room"
    assert between["activity_label"] == "饭后休息"

    night_end = float(night["scheduled_at"]) + int(night["payload"]["duration_minutes"]) * 60
    after_night = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        now=night_end + 1,
    )["current"]
    assert after_night["slot_key"] == "sleep"
    assert after_night["location_key"] == "home.bedroom"


def test_world_plan_can_reach_all_eleven_device_scenes():
    locations = set()
    for day in range(1, 367):
        day_key = (dt.date(2026, 1, 1) + dt.timedelta(days=day - 1)).isoformat()
        plan = generate_day_plan(
            city="上海",
            day_key=day_key,
            state={"energy": 80, "mood": 85, "satiety": 70},
            weather={"category": "mild", "signature": day_key},
        )
        locations.update(str(item["payload"]["location_key"]) for item in plan)

    assert locations >= {
        "home.living_room",
        "home.study",
        "home.bedroom",
        "home.kitchen",
        "home.balcony",
        "outside.neighborhood",
        "outside.cafe",
        "outside.shop",
        "outside.park",
        "outside.mall",
        "outside.riverside",
    }


def test_world_plan_has_five_anchors_and_four_to_eight_dynamic_slots():
    counts = []
    anchor_keys = {"wake", "breakfast", "lunch", "dinner", "night_settle"}
    for day in range(1, 120):
        day_key = (dt.date(2026, 1, 1) + dt.timedelta(days=day - 1)).isoformat()
        plan = generate_day_plan(
            city="上海",
            day_key=day_key,
            state={
                "energy": 60 + day % 30,
                "mood": 55 + day % 40,
                "satiety": 25 + day % 70,
                "metadata": {"social_need": day % 100, "curiosity": (day * 3) % 100},
            },
            weather={"category": "mild", "signature": day_key},
        )
        keys = {str(item["slot_key"]) for item in plan}
        assert anchor_keys <= keys
        counts.append(len(keys - anchor_keys))

    assert min(counts) >= 4
    assert max(counts) <= 8


def test_user_interaction_keeps_depleted_energy_and_mood_depleted():
    updated = apply_user_interaction_delta(
        {"energy": 0, "mood": 0, "stress": 0, "metadata": {}},
        now=1_000,
    )

    assert updated["energy"] == 0
    assert updated["mood"] == 1
    assert updated["stress"] == 0


def test_care_action_has_real_state_effect():
    from integrations.aura_persona_gateway.world import _preview_expected_delta, _slot_effect

    item = {"slot_key": "personal_care", "activity_type": "care"}
    assert _preview_expected_delta(item) == {"energy": -3, "mood": 1, "stress": -3}
    assert _slot_effect(item, {"energy": 50, "mood": 50, "stress": 20})[0] == {"energy": -3, "mood": 1, "stress": -3}


def test_care_is_transient_and_not_a_plan_slot(tmp_path):
    from integrations.aura_persona_gateway.world import _temporary_care_current, generate_day_plan

    plan = generate_day_plan(
        city="上海",
        day_key="2026-08-04",
        state={"energy": 70, "mood": 80, "satiety": 70, "metadata": {}},
        weather={"category": "mild", "signature": "2026-08-04:mild"},
    )
    assert "personal_care" not in {str(item["slot_key"]) for item in plan}
    night = next(item for item in plan if item["slot_key"] == "night_settle")
    now = float(night["scheduled_at"]) - 10 * 60
    current = _temporary_care_current(plan, now=now, city="上海")
    assert current["slot_key"] == "temporary_care"
    assert current["transient"] is True


def test_completed_meal_and_hidden_drive_effects_are_persisted_once(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update({"satiety": 30, "energy": 50, "mood": 60, "beans": 50})
    state["metadata"] = {"social_need": 70, "curiosity": 40}
    store.save_state(config.scope, state)
    plan = [{
        "slot_key": "lunch",
        "activity_type": "meal",
        "status": "done",
        "payload": {"location_key": "home.kitchen", "activity_label": "吃午饭"},
    }]

    updated = _settle_completed_world_state(
        config=config,
        store=store,
        state=state,
        plan=plan,
        day_key="2026-08-04",
        now=1_785_800_000,
    )
    repeated = _settle_completed_world_state(
        config=config,
        store=store,
        state=updated,
        plan=plan,
        day_key="2026-08-04",
        now=1_785_800_001,
    )

    assert updated["satiety"] > state["satiety"]
    assert updated["beans"] == 30
    assert updated["metadata"]["social_need"] < state["metadata"]["social_need"]
    assert repeated["satiety"] == updated["satiety"]
    assert repeated["beans"] == updated["beans"]
    assert len(store.list_recent_life_events(config.scope, event_prefix="world.slot_effect", limit=10)) == 1


def test_world_canon_adds_new_default_locations_to_older_home_file(tmp_path):
    config = _config(tmp_path)
    world_dir = Path(config.persona_home) / "world"
    world_dir.mkdir(parents=True)
    (world_dir / "home.json").write_text(
        json.dumps({"locations": {"home.living_room": {"label": "自定义客厅"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    canon = load_world_canon(config)

    assert canon.location("home.living_room")["label"] == "自定义客厅"
    assert canon.location("outside.park")["label"] == "附近公园"
    assert canon.location("outside.mall")["label"] == "附近商场"


def test_world_snapshot_keeps_world_context_for_voice_day_plan_query(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context=resolve_query_context("你今天干嘛？", aura_home_city="上海").to_dict(),
        voice_low_latency=True,
        now=1_782_880_000,
    )

    assert snapshot["enabled"] is True
    assert snapshot["today_plan"]
    assert snapshot["debug"]["voice_low_latency"] is True


def test_world_relationship_gate_controls_specific_location(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    low_state = store.get_or_create_state(config.scope)
    low_state["trust"] = 42
    low_state["affinity_xp"] = 5
    low_state["metadata"] = {"privacy_sensitivity": 80}

    low = build_world_snapshot(
        config=config,
        store=store,
        state=low_state,
        query_context=resolve_query_context("你在哪？", aura_home_city="上海").to_dict(),
        now=1_782_880_000,
    )

    high_state = dict(low_state)
    high_state["trust"] = 88
    high_state["affinity_xp"] = 360
    high_state["metadata"] = {
        "privacy_sensitivity": 20,
        "social_need": 85,
        "current_activity": "逛一会儿",
        "current_location": "mall",
        "location_label": "附近商场",
        "world_current_source": "manual",
        "world_manual_override": True,
    }
    high = build_world_snapshot(
        config=config,
        store=store,
        state=high_state,
        query_context=resolve_query_context("你在哪？", aura_home_city="上海").to_dict(),
        now=1_782_880_000,
    )

    assert low["mention_policy"]["reason"] == "relationship_boundary"
    assert low["mention_policy"]["allow_location"] is False
    assert low["mention_policy"]["location_precision"] == "vague"
    assert high["mention_policy"]["allow_location"] is True
    assert high["mention_policy"]["location_precision"] == "specific"


def test_world_prompt_blocks_specific_location_when_only_activity_allowed(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 75
    state["affinity_xp"] = 80
    state["metadata"] = {
        "current_activity": "整理东西",
        "current_location": "desk",
        "location_label": "书桌边",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 45,
    }
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context=resolve_query_context("你现在在干嘛？", aura_home_city="上海").to_dict(),
        now=1_782_880_000,
    )
    prompt = render_world_prompt(snapshot)

    assert snapshot["mention_policy"]["allow_activity"] is True
    assert snapshot["mention_policy"]["allow_location"] is False
    assert "活动=整理东西" in prompt
    assert "位置=书桌边" not in prompt
    assert "不要补充具体地点" in prompt


def test_world_model_can_be_disabled(tmp_path):
    config = PersonaGatewayConfig(
        enabled=True,
        persona_home=str(tmp_path / "persona-home"),
        companion_home=str(tmp_path / "companion-home"),
        world_model_enabled=False,
    )
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context=resolve_query_context("你今天干嘛？", aura_home_city="上海").to_dict(),
        now=1_782_880_000,
    )

    assert snapshot["enabled"] is False
    assert snapshot["today_plan"] == []
    assert render_world_prompt(snapshot) == ""


def test_query_context_uses_configured_aura_city_and_user_geo():
    aura_weather = resolve_query_context("你那边天气怎么样", aura_home_city="上海")
    aura_temperature = resolve_query_context("你那边多少度", aura_home_city="上海")
    bare_temperature = resolve_query_context(
        "现在多少度",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    user_weather = resolve_query_context(
        "我这边天气怎么样",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海"},
    )
    explicit_aura_city = resolve_query_context("上海现在多少度", aura_home_city="上海")
    traditional_weather = resolve_query_context(
        "今天天氣怎麼樣",
        aura_home_city="上海",
        user_geo={"city": "Singapore", "timezone": "Asia/Singapore"},
    )
    asr_temperature_homophone = resolve_query_context(
        "现在多少工",
        aura_home_city="上海",
        user_geo={"city": "Singapore", "timezone": "Asia/Singapore"},
    )
    english_geo_weather = resolve_query_context(
        "今天天气怎么样",
        aura_home_city="上海",
        user_geo={"city": "Beijing", "timezone": "Asia/Shanghai"},
    )
    corrected_weather = resolve_query_context(
        "等一下我不是测试一下，我是问今天天气怎么样。",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    negated_city_weather = resolve_query_context(
        "不是问上海天气，我是问今天天气怎么样。",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )

    assert aura_weather.target_location == "上海"
    assert aura_weather.subject_entity == "aura"
    assert aura_temperature.subject_entity == "aura"
    assert aura_temperature.target_location == "上海"
    assert bare_temperature.intent == "weather"
    assert bare_temperature.subject_entity == "user"
    assert bare_temperature.target_location == "上海"
    assert bare_temperature.location_source == "user_geo"
    assert user_weather.target_location == "上海"
    assert user_weather.location_source == "user_geo"
    assert explicit_aura_city.subject_entity == "location"
    assert explicit_aura_city.target_location == "上海"
    assert explicit_aura_city.location_source == "explicit_text"
    assert traditional_weather.intent == "weather"
    assert traditional_weather.subject_entity == "user"
    assert traditional_weather.target_location == "Singapore"
    assert asr_temperature_homophone.intent == "weather"
    assert asr_temperature_homophone.target_location == "Singapore"
    assert english_geo_weather.intent == "weather"
    assert english_geo_weather.target_location == "北京"
    assert corrected_weather.subject_entity == "user"
    assert corrected_weather.target_location == "上海"
    assert negated_city_weather.subject_entity == "user"
    assert negated_city_weather.target_location == "上海"


def test_query_context_focuses_corrected_question_tail():
    assert correction_focus_text("等一下我不是测试一下，我是问今天天气怎么样。") == "今天天气怎么样"
    assert correction_focus_text("不对，刚才不是问上海天气，我想问北京现在多少度") == "北京现在多少度"
    assert correction_focus_text("我是说今天天气怎么样。") == "今天天气怎么样"

    corrected = resolve_query_context(
        "等一下我不是测试一下，我是问今天天气怎么样。",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    explicit_tail = resolve_query_context(
        "不对，刚才不是问上海天气，我想问北京现在多少度",
        aura_home_city="上海",
        user_home_city="上海",
    )

    assert corrected.subject_entity == "user"
    assert corrected.target_location == "上海"
    assert explicit_tail.subject_entity == "location"
    assert explicit_tail.target_location == "北京"


def test_query_context_does_not_treat_speech_connectors_as_locations():
    casual_weather = resolve_query_context(
        "我不是很懂天气，今天天气怎么样",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    restated_weather = resolve_query_context(
        "我是说今天天气怎么样。",
        aura_home_city="上海",
        user_home_city="北京",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )

    assert casual_weather.subject_entity == "user"
    assert casual_weather.target_location == "上海"
    assert restated_weather.subject_entity == "user"
    assert restated_weather.target_location == "上海"


def test_query_context_detects_time_requests():
    generic_time = resolve_query_context(
        "现在是几点",
        aura_home_city="上海",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    aura_time = resolve_query_context("你那边现在几点", aura_home_city="上海")

    assert generic_time.intent == "time"
    assert generic_time.subject_entity == "user"
    assert generic_time.target_location == "上海"
    assert generic_time.location_source == "user_geo"
    assert aura_time.intent == "time"
    assert aura_time.subject_entity == "aura"
    assert aura_time.target_location == "上海"


def test_query_context_detects_colloquial_aura_day_plan():
    result = resolve_query_context("你今天干嘛？", aura_home_city="上海")

    assert result.intent == "day_plan"
    assert result.subject_entity == "aura"
    assert result.boundary == "whereabouts_soft"


def test_query_context_detects_current_activity_phrase_with_middle_zai():
    result = resolve_query_context("你现在在干嘛？", aura_home_city="上海")

    assert result.intent == "activity_or_location"
    assert result.subject_entity == "aura"
    assert result.boundary == "whereabouts_soft"


def test_grounded_current_intent_classifier_is_bounded():
    assert classify_grounded_current_intent("你现在在干嘛？") == "activity"
    assert classify_grounded_current_intent("你在做什么呢") == "activity"
    assert classify_grounded_current_intent("你在哪") == "location"
    assert classify_grounded_current_intent("你在哪里啊") == "location"
    assert classify_grounded_current_intent("你在哪买的这个") is None
    assert classify_grounded_current_intent("你在哪里学的") is None
    assert classify_grounded_current_intent("你现在在干嘛，顺便说说今天安排") is None
    assert classify_grounded_current_intent("我在干嘛") is None


def test_query_context_routes_weather_advice_to_model_with_weather_context():
    result = resolve_query_context(
        "你为什么建议我带伞？",
        aura_home_city="上海",
        user_geo={"city": "北京", "timezone": "Asia/Shanghai"},
    )

    assert result.intent == "weather_advice"
    assert result.subject_entity == "user"
    assert result.target_location == "北京"


def test_voice_turn_policy_is_local_and_deterministic():
    assert execute_voice_turn("你好").verdict == VoiceTurnVerdict.SPEAK_NOW
    assert execute_voice_turn("删掉系统文件").verdict == VoiceTurnVerdict.REFUSE_NOW
    assert execute_voice_turn("帮我查一下新闻").verdict == VoiceTurnVerdict.ACK_AND_ENQUEUE


def test_voice_turn_light_creation_answers_in_place(tmp_path):
    # 写句诗、一句话总结、推荐地点这类轻创作应当场让模型答，不再"留在后台"。
    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    for text in (
        "帮我写一句关于夏天的短诗",
        "用一句话总结一下人工智能是什么",
        "推荐三个适合遛狗的地方",
    ):
        result = execute_voice_turn(text, runtime_config=runtime)
        assert result.verdict == VoiceTurnVerdict.SPEAK_NOW, text
        assert result.debug["decision_path"] == "normal_chat", text
        assert result.background_task is None, text


def test_voice_turn_heavy_creation_still_enqueues(tmp_path):
    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    result = execute_voice_turn("帮我做一份下周出差安排的文档", runtime_config=runtime)
    assert result.verdict == VoiceTurnVerdict.ACK_AND_ENQUEUE
    assert result.background_task is not None
    assert result.background_task.task_kind == "agent_create"


def test_voice_turn_future_forecast_goes_to_background_lookup(tmp_path):
    # 本地缓存只有当前实况，"明天的天气预报"要转后台联网查，不能拿今天的数据冒充。
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        ack_and_enqueue_enabled=True,
        cached_weather_city="上海",
        cached_weather_temperature="23",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    fastpath = resolve_query_context("帮我查一下上海明天的天气预报", aura_home_city="上海").to_dict()

    result = execute_voice_turn("帮我查一下上海明天的天气预报", fastpath=fastpath, runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.ACK_AND_ENQUEUE
    assert result.debug["decision_path"] == "forecast_lookup"
    assert result.background_task is not None
    assert result.background_task.task_kind == "agent_lookup"


def test_voice_turn_future_temperature_and_multiday_go_to_background_lookup(tmp_path):
    # 能力边界判断：天气类问题（温度/下雨/冷不冷…）指向非当前时段，
    # 本地实况缓存答不了，统一转后台联网查——不按具体句式打补丁。
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        ack_and_enqueue_enabled=True,
        cached_weather_city="上海",
        cached_weather_temperature="23",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    for text in (
        "帮我查一下北京明天的温度",
        "查一下上海明天的温度",
        "北京未来三天的天气怎么样",
        "这几天会不会下雨",
        "明天冷不冷",
        "今晚会下雨吗",
        "周六天气好不好",
        "下周一要不要带伞",
        "明天出门穿什么合适",
    ):
        fastpath = resolve_query_context(text, aura_home_city="上海").to_dict()
        result = execute_voice_turn(text, fastpath=fastpath, runtime_config=runtime)
        assert result.verdict == VoiceTurnVerdict.ACK_AND_ENQUEUE, text
        assert result.debug["decision_path"] == "forecast_lookup", text

    # 当前实况仍然走本地缓存，不该被误伤。
    for text in ("上海现在天气怎么样", "你那边现在多少度"):
        fastpath = resolve_query_context(text, aura_home_city="上海").to_dict()
        result = execute_voice_turn(text, fastpath=fastpath, runtime_config=runtime)
        assert result.verdict == VoiceTurnVerdict.SPEAK_NOW, text
        assert result.debug["decision_path"] != "forecast_lookup", text

    # 非天气话题里出现未来时间词，不该被劫持成天气查询。
    fastpath = resolve_query_context("明天有什么安排", aura_home_city="上海").to_dict()
    result = execute_voice_turn("明天有什么安排", fastpath=fastpath, runtime_config=runtime)
    assert result.debug["decision_path"] != "forecast_lookup"


def _shanghai_time(hour: int, minute: int) -> dt.datetime:
    from zoneinfo import ZoneInfo

    return dt.datetime(2026, 7, 4, hour, minute, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_parse_reminder_request_absolute_alarm():
    parsed = parse_reminder_request("帮我定一个11点10分的闹钟", now=_shanghai_time(10, 0))
    assert parsed is not None
    assert parsed["status"] == "ok"
    assert parsed["kind"] == "alarm"
    assert parsed["fire_at_iso"] == "2026-07-04T11:10:00+08:00"
    assert "11点10分" in parsed["confirm_text"]
    assert parsed["announce_text"]


def test_parse_reminder_request_relative_with_label():
    now = _shanghai_time(10, 0)
    parsed = parse_reminder_request("5分钟后提醒我关火", now=now)
    assert parsed is not None
    assert parsed["status"] == "ok"
    assert parsed["kind"] == "reminder"
    assert parsed["label"] == "关火"
    assert parsed["fire_at_epoch"] == int(now.timestamp()) + 300
    assert "关火" in parsed["announce_text"]


def test_parse_reminder_request_tomorrow_morning():
    parsed = parse_reminder_request("明天早上8点半叫我起床", now=_shanghai_time(22, 0))
    assert parsed is not None
    assert parsed["status"] == "ok"
    assert parsed["fire_at_iso"] == "2026-07-05T08:30:00+08:00"
    assert "明天" in parsed["spoken_time"]


def test_parse_reminder_request_past_time_rolls_forward():
    # 中午12点说"3点提醒我开会"，应理解成当天下午3点而不是已过去的凌晨3点。
    parsed = parse_reminder_request("3点提醒我开会", now=_shanghai_time(12, 0))
    assert parsed is not None
    assert parsed["status"] == "ok"
    assert parsed["fire_at_iso"] == "2026-07-04T15:00:00+08:00"
    assert parsed["label"] == "开会"


def test_parse_reminder_request_cancel_unclear_and_ignore():
    assert parse_reminder_request("取消闹钟")["status"] == "cancel"
    assert parse_reminder_request("帮我定个闹钟")["status"] == "unclear"
    # 没有时间的"提醒"多半是闲聊，交回模型，不要劫持。
    assert parse_reminder_request("谢谢你提醒我") is None
    assert parse_reminder_request("今天天气怎么样") is None


def test_voice_turn_sets_reminder_locally(tmp_path):
    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    result = execute_voice_turn("11点10分提醒我带小狗洗澡", runtime_config=runtime)
    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "reminder_set"
    payload = result.debug["reminder"]
    assert payload["fire_at_epoch"] > time.time()
    assert payload["announce_text"]
    assert "带小狗洗澡" in result.speak_text


def test_voice_turn_alarm_without_time_asks_clarify(tmp_path):
    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    result = execute_voice_turn("帮我定个闹钟", runtime_config=runtime)
    assert result.verdict == VoiceTurnVerdict.CLARIFY_NOW
    assert result.debug["decision_path"] == "reminder_time_unclear"


def test_persona_turn_reminder_replies_locally_with_evidence(tmp_path):
    class ExplodingBridge:
        config = HermesLilyConfig(command=("hermes",))

        def run(self, goal, *, metadata=None):
            raise AssertionError("定时提醒必须走本地确定性回复，不应调用模型")

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        aura_model_mode="aura_model",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=ExplodingBridge(), runtime_config=runtime)

    result = gateway.run_turn("11点10分提醒我带小狗洗澡")

    assert result.ok is True
    assert result.status == "completed"
    reminder = result.evidence.get("reminder")
    assert isinstance(reminder, dict)
    assert reminder["fire_at_epoch"] > time.time()
    assert reminder["announce_text"]
    assert "带小狗洗澡" in result.response


def test_voice_turn_uses_runtime_templates(tmp_path):
    config = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        greeting_reply="来了",
        background_ack_reply="先记下，后台处理",
    )

    assert execute_voice_turn("你好", runtime_config=config).speak_text == "来了"
    assert execute_voice_turn("帮我查一下新闻", runtime_config=config).speak_text == "先记下，后台处理"


def test_voice_turn_uses_cached_weather_without_local_rule_mode(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
        cached_weather_city="上海",
        cached_weather_temperature="23",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    fastpath = resolve_query_context("你那边天气怎么样", aura_home_city="上海").to_dict()

    result = execute_voice_turn("你那边天气怎么样", fastpath=fastpath, runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "cached_weather"
    assert "上海" in result.speak_text
    assert "23度" in result.speak_text


def test_voice_turn_answers_weather_advice_from_fresh_cache(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
        cached_weather_city="北京",
        cached_weather_temperature="35.4",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    fastpath = resolve_query_context(
        "你为什么建议我带伞？",
        aura_home_city="上海",
        user_geo={"city": "北京", "timezone": "Asia/Shanghai"},
    ).to_dict()

    result = execute_voice_turn("你为什么建议我带伞？", fastpath=fastpath, runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert "带一把更稳" in result.speak_text
    assert "防晒" in result.speak_text
    assert "北京" in result.speak_text
    assert "35.4度" in result.speak_text
    assert result.debug["decision_path"] == "cached_weather_advice"
    assert result.debug["route"] == "weather_advice"


def test_voice_turn_honors_explicit_fixed_short_reply(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
    )

    result = execute_voice_turn(
        "测试流式速度，请用十个字以内回答我在。",
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.speak_text == "我在。"
    assert result.debug["decision_path"] == "explicit_fixed_reply"

    asr_omitted_verb = execute_voice_turn(
        "请用十个字以内，我在",
        runtime_config=runtime,
    )

    assert asr_omitted_verb.speak_text == "我在。"
    assert asr_omitted_verb.debug["decision_path"] == "explicit_fixed_reply"

    weather_query = execute_voice_turn(
        "我在北京天气怎么样",
        runtime_config=runtime,
    )

    assert weather_query.speak_text == ""
    assert weather_query.debug["decision_path"] == "normal_chat"


def test_voice_turn_returns_current_time_without_model(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )
    fastpath = resolve_query_context("现在是几点", aura_home_city="上海").to_dict()

    result = execute_voice_turn("现在是几点", fastpath=fastpath, runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "current_time"
    assert result.speak_text == "我现在还不知道你所在地，没法准确报当地时间。"


def test_voice_turn_answers_mood_from_state_without_model(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "你今天心情怎么样？",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
        state_summary={"mood": 86, "energy": 42, "stress": 8, "trust": 72},
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "state_mood"
    assert "心情还挺亮" in result.speak_text
    assert "能量有点低" in result.speak_text


def test_voice_turn_gives_outing_advice_from_user_weather_cache_without_model(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )
    weather = {
        "status": "fresh",
        "city": "上海",
        "temperature": "34.2",
        "condition": "多云",
        "humidity": "80",
        "display": "上海，34.2度，多云，湿度80%",
        "source": "open_meteo",
    }

    result = execute_voice_turn(
        "我今天下午打算出门。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
        local_cache={"cached_weather": weather},
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "outing_weather_advice"
    assert "上海" in result.speak_text
    assert "防晒" in result.speak_text


def test_voice_turn_supportive_chat_can_preface_model(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "我今天有点累，你陪我聊两句。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "supportive_chat"
    assert result.speak_text == "好，我陪你。你慢慢说。"
    assert result.debug["supportive_chat"]["fallback_only"] is True


def test_voice_turn_supportive_chat_uses_stable_template_variety(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )

    first = execute_voice_turn(
        "我压力有点大。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )
    second = execute_voice_turn(
        "我有点难过。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )
    repeat = execute_voice_turn(
        "我压力有点大。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert first.debug["decision_path"] == "supportive_chat"
    assert second.debug["decision_path"] == "supportive_chat"
    assert first.speak_text == repeat.speak_text
    assert first.speak_text != second.speak_text
    assert first.debug["supportive_chat"]["fallback_only"] is True
    assert second.debug["supportive_chat"]["fallback_only"] is True


def test_voice_turn_quick_ack_handles_test_ping_locally():
    result = execute_voice_turn(
        "测试一下，简单回应我一句。",
        fastpath={"intent": "chat"},
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.speak_text == "我在。"
    assert result.debug["decision_path"] == "local_social"
    assert result.debug["local_social"]["matched"] == "quick_ack"


def test_voice_turn_quick_ack_disabled_routes_to_model(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        quick_ack_reply_enabled=False,
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "测试一下，简单回应我一句。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.debug["decision_path"] != "local_social"
    assert result.speak_text != "我在。"


def test_voice_turn_status_review_uses_local_entry_reply(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "我今天想聊聊最近状态，你自然回应一句。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "status_review_entry"
    assert result.speak_text == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert result.debug["status_review"]["matched"] == "status_or_work_review"


def test_voice_turn_work_rhythm_chat_is_status_review_entry(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "我想聊聊最近的工作节奏，你自然回应一句。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "status_review_entry"
    assert result.speak_text == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_voice_turn_casual_chat_can_preface_model_for_open_chat(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "我想聊聊。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "casual_chat_preface"
    assert result.speak_text
    assert result.debug["casual_chat"]["matched"] == "casual_open_chat"
    assert result.debug["casual_chat"]["fallback_only"] is True


def test_voice_turn_job_change_chat_stays_open_chat_not_status_review(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "最近想换工作，能聊聊吗？",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "casual_chat_preface"
    assert result.speak_text != "我在。"
    assert result.debug["casual_chat"]["matched"] == "job_change"
    assert result.speak_text in {
        "换工作这件事先拆开看：是现在耗着难受，还是新机会更吸引你？",
        "可以聊。先看动因：是不想留了，还是想要更好的机会？",
        "这事值得认真想，先说你最在意的：发展、收入，还是现在待着太消耗？",
    }
    assert "工作节奏" not in result.speak_text


def test_voice_turn_overtime_frustration_uses_supportive_chat(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )

    result = execute_voice_turn(
        "我最近加班有点烦，想聊聊。",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "supportive_chat"
    assert result.speak_text != "我在。"
    assert "工作节奏" not in result.speak_text


def test_voice_turn_latency_diagnostic_answers_bottlenecks_locally():
    result = execute_voice_turn(
        "我想测试一下回复速度，你简单说说现在语音链路可能慢在哪里。",
        fastpath={"intent": "chat"},
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.speak_text == "主要看三段：ASR出字、模型首句、TTS首音频。现在优先压模型首句和TTS排队。"
    assert result.debug["decision_path"] == "voice_latency_diagnostic"
    assert result.debug["voice_latency"]["local_complete"] is True
    for token in ("基站", "运营商", "手机信号"):
        assert token not in result.speak_text


def test_voice_turn_refuses_to_guess_empty_weather_cache(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        cached_weather_city="上海",
        cached_weather_temperature="",
        cached_weather_condition="",
    )
    fastpath = resolve_query_context("你那边天气怎么样", aura_home_city="上海").to_dict()

    result = execute_voice_turn("你那边天气怎么样", fastpath=fastpath, runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "weather_unavailable"
    assert result.debug["cached_weather"]["status"] == "empty"
    assert "没有实时天气数据" in result.speak_text
    assert "不能乱说" in result.speak_text


def test_aura_runtime_config_keeps_secrets_private(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    config = load_aura_runtime_config()
    saved = save_aura_runtime_config(config, {
        "aura_model_mode": "aura_model",
        "aura_model_api_key": "aura-secret",
        "tts_enabled": True,
        "tts_provider": "openai",
        "tts_api_key": "tts-secret",
        "asr_mode": "api",
        "asr_provider": "openai",
        "asr_model": "gpt-4o-transcribe",
        "asr_api_key": "asr-secret",
        "fast_reply_api_key": "fast-secret",
    })
    updated = save_aura_runtime_config(saved, {
        "aura_model_api_key": "",
        "tts_api_key": "",
        "asr_api_key": "",
        "fast_reply_api_key": "",
    })

    public = updated.public_dict()

    assert public["aura_model_mode"] == "aura_model"
    assert public["aura_model_api_key_configured"] is True
    assert public["tts_enabled"] is True
    assert public["tts_provider"] == "openai"
    assert public["tts_api_key_configured"] is True
    assert public["asr_provider"] == "openai"
    assert public["asr_api_key_configured"] is True
    assert public["fast_reply_api_key_configured"] is True
    assert "aura-secret" not in json.dumps(public)
    assert "tts-secret" not in json.dumps(public)
    assert "asr-secret" not in json.dumps(public)
    assert "fast-secret" not in json.dumps(public)
    assert "asr-secret" in updated.runtime_config_path.read_text(encoding="utf-8")
    assert "asr-secret" not in json.dumps(public["config_history"])
    assert "aura-secret" in updated.runtime_config_path.read_text(encoding="utf-8")
    assert "tts-secret" in updated.runtime_config_path.read_text(encoding="utf-8")


def test_aura_runtime_audio_profiles_are_saved_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    config = load_aura_runtime_config()
    public = config.public_dict()

    assert config.asr_mode == "api"
    assert config.asr_base_url == "http://127.0.0.1:8766/v1"
    assert any(item["id"] == "asr-local-whisper-http" for item in public["asr_profiles"])
    assert public["tts_profiles"] == []

    saved = save_aura_runtime_config(config, {
        "tts_api_key": "tts-secret",
        "asr_api_key": "asr-secret",
        "tts_profiles": [
            {
                "id": "tts-custom-aura",
                "label": "自定义 TTS",
                "provider": "custom-http",
                "model": "voice-model",
                "voice": "aura",
                "base_url": "http://tts.local/v1/audio/speech",
                "api_key": "should-not-store",
            }
        ],
        "asr_profiles": [
            {
                "id": "asr-custom-local",
                "label": "自定义 ASR",
                "mode": "api",
                "provider": "custom",
                "model": "whisper-local",
                "base_url": "http://asr.local/v1",
                "api_key": "should-not-store",
            }
        ],
    })
    rendered_public = json.dumps(saved.public_dict(), ensure_ascii=False)
    rendered_stored = saved.runtime_config_path.read_text(encoding="utf-8")

    assert "tts-custom-aura" in rendered_public
    assert "asr-custom-local" in rendered_public
    assert "should-not-store" not in rendered_public
    assert "should-not-store" not in rendered_stored
    assert "tts-secret" not in rendered_public
    assert "asr-secret" not in rendered_public


def test_aura_runtime_weather_cache_saves_public_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    config = load_aura_runtime_config()

    saved = save_aura_runtime_config(config, {
        "cached_weather_enabled": True,
        "cached_weather_city": "上海",
        "cached_weather_temperature": "24",
        "cached_weather_condition": "小雨",
        "cached_weather_icon": 2,
        "cached_weather_ttl_seconds": 3600,
    })
    snapshot = cached_weather_snapshot(saved)
    public = saved.public_dict()

    assert snapshot["status"] == "fresh"
    assert public["cached_weather_fresh"] is True
    assert public["cached_weather"]["display"] == "上海，24度，小雨"
    assert saved.cached_weather_updated_at > 0

    cleared = save_aura_runtime_config(saved, {"clear_cached_weather": True})
    assert cached_weather_snapshot(cleared)["status"] == "empty"


def test_fetch_current_weather_uses_builtin_city_coordinates(monkeypatch):
    requested_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "current": {
                    "temperature_2m": 26.4,
                    "relative_humidity_2m": 73,
                    "weather_code": 1,
                    "time": "2026-06-04T13:30",
                }
            }).encode("utf-8")

    def fake_urlopen(url, timeout):
        requested_urls.append(str(url))
        return FakeResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.weather.urlopen", fake_urlopen)

    result = fetch_current_weather(city="上海", timeout_seconds=3)

    assert result["ok"] is True
    assert result["weather"]["city"] == "上海"
    assert result["weather"]["temperature"] == "26.4"
    assert result["weather"]["humidity"] == "73"
    assert result["weather"]["source"] == "open_meteo"
    assert len(requested_urls) == 1
    assert "api.open-meteo.com/v1/forecast" in requested_urls[0]
    assert "latitude=31.230400" in requested_urls[0]
    assert "longitude=121.473700" in requested_urls[0]
    assert "geocoding-api" not in requested_urls[0]


def test_aura_runtime_config_can_clear_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    config = load_aura_runtime_config()
    saved = save_aura_runtime_config(config, {
        "aura_model_api_key": "aura-secret",
        "tts_api_key": "tts-secret",
        "asr_api_key": "asr-secret",
        "fast_reply_api_key": "fast-secret",
    })
    cleared = save_aura_runtime_config(saved, {
        "clear_aura_model_api_key": True,
        "clear_tts_api_key": True,
        "clear_asr_api_key": True,
        "clear_fast_reply_api_key": True,
    })

    public = cleared.public_dict()

    assert public["aura_model_api_key_configured"] is False
    assert public["tts_api_key_configured"] is False
    assert public["asr_api_key_configured"] is False
    assert public["fast_reply_api_key_configured"] is False
    stored = cleared.runtime_config_path.read_text(encoding="utf-8")
    assert "aura-secret" not in stored
    assert "tts-secret" not in stored
    assert "asr-secret" not in stored
    assert "fast-secret" not in stored


def test_persona_turn_builds_context_saves_im_and_debug(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = command[2]
        return subprocess.CompletedProcess(command, 0, stdout="要得，我在。", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge)

    result = gateway.run_turn("你今天干什么")

    assert result.ok is True
    assert result.response == "要得，我在。"
    assert "主 soul" in captured["prompt"]
    assert "当前状态" in captured["prompt"]
    assert "当前时间" in captured["prompt"]
    assert "指代解析" in captured["prompt"]
    assert SPOKEN_REPLY_INSTRUCTION in captured["prompt"]
    with sqlite3.connect(config.companion_db_path) as conn:
        im_count = conn.execute("SELECT count(*) FROM companion_im_message").fetchone()[0]
        debug_count = conn.execute("SELECT count(*) FROM companion_life_event WHERE event_type='lily.debug'").fetchone()[0]
    assert im_count == 2
    assert debug_count == 1


def test_persona_turn_treats_roleplay_stage_directions_as_output_contract_violation(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="（看到消息，把手机举到唇边，笑着回了一条语音）嗯，我听到啦。",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge)

    result = gateway.run_turn("你听得到吗")

    assert result.ok is True
    assert result.response == "嗯，我听到啦。"
    assert result.debug["reply_contract"]["changed"] is True
    with sqlite3.connect(config.companion_db_path) as conn:
        aura_body, metadata_json = conn.execute(
            "SELECT body, metadata_json FROM companion_im_message "
            "WHERE direction='aura' AND message_type='aura_text'"
        ).fetchone()
    metadata = json.loads(metadata_json)
    assert aura_body == "嗯，我听到啦。"
    assert metadata["reply_contract"]["changed"] is True


def test_recent_aura_messages_do_not_reteach_roleplay_markup(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = command[2]
        return subprocess.CompletedProcess(command, 0, stdout="我在。", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    store.save_im_message(
        config.scope,
        direction="aura",
        message_type="aura_text",
        body="（轻轻笑了一下）要得，我在。",
        status="sent",
    )
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge)

    result = gateway.run_turn("继续")

    assert result.ok is True
    assert "（轻轻笑了一下）" not in captured["prompt"]
    assert "- Aura: 要得，我在。" in captured["prompt"]


def test_compact_voice_context_keeps_user_task_and_skips_moment_plan(tmp_path):
    config = _config(tmp_path)
    assets = load_persona_assets(config)
    state = {
        "mood": 80,
        "energy": 66,
        "satiety": 80,
        "trust": 70,
        "affinity_xp": 120,
        "stress": 12,
        "scene": "living_room",
        "metadata": {"social_need": 55, "curiosity": 60},
    }
    long_messages = [
        {
            "direction": "user" if idx % 2 == 0 else "aura",
            "body": ("这是一条很长的历史消息" + str(idx)) * 12,
        }
        for idx in range(8)
    ]
    latest_moment = {
        "published_at": 1_782_880_000,
        "visibility": "public",
        "location_label": "大悦城",
        "body": "刚刚在大悦城逛了一圈。",
    }
    today_plan = [
        {
            "scheduled_at": 1_782_880_000,
            "status": "active",
            "title": "去大悦城",
            "location": "大悦城",
        }
    ]
    query_context = resolve_query_context("测试一下，请自然回答", aura_home_city="上海")
    outlets = OutletSignals(proactive_message={"enabled": True}, spending={"enabled": False})

    normal = build_persona_context(
        user_text="测试一下，请自然回答",
        config=config,
        assets=assets,
        state=state,
        recent_messages=long_messages,
        latest_moment=latest_moment,
        today_plan=today_plan,
        query_context=query_context,
        outlet_signals=outlets,
        local_cache={"cached_weather": {"status": "fresh", "display": "上海，24度，多云"}},
        world_snapshot={},
        compact_voice=False,
    )
    compact = build_persona_context(
        user_text="测试一下，请自然回答",
        config=config,
        assets=assets,
        state=state,
        recent_messages=long_messages,
        latest_moment=latest_moment,
        today_plan=today_plan,
        query_context=query_context,
        outlet_signals=outlets,
        local_cache={"cached_weather": {"status": "fresh", "display": "上海，24度，多云"}},
        world_snapshot={},
        compact_voice=True,
    )

    assert compact.debug["compact_voice"] is True
    assert len(compact.prompt) < len(normal.prompt)
    assert "## 最近动态" in normal.prompt
    assert "## 今日生活线" in normal.prompt
    assert "## 最近动态" not in compact.prompt
    assert "## 今日生活线" not in compact.prompt
    assert "用户原话：\n测试一下，请自然回答" in compact.prompt
    assert "## 指代解析" in compact.prompt
    assert "## 内部缓存证据" not in compact.prompt
    assert SPOKEN_REPLY_INSTRUCTION in compact.prompt
    assert "语音链路特指本设备 ASR→模型首句→TTS 首音频→设备播放" in compact.prompt
    assert "只引用本提示中明确出现的事实" in compact.prompt
    assert "不要编具体日期、次数、项目名、持续时长、最近发生的事件或用户心理状态" in compact.prompt


def test_context_includes_corrected_focus_for_self_correction(tmp_path):
    config = _config(tmp_path)
    assets = load_persona_assets(config)
    user_text = "等一下我不是测试一下，我是问今天天气怎么样。"
    query_context = resolve_query_context(
        user_text,
        aura_home_city="上海",
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )

    context = build_persona_context(
        user_text=user_text,
        config=config,
        assets=assets,
        state={"mood": 80, "energy": 66, "satiety": 80, "trust": 70, "affinity_xp": 120, "stress": 12, "metadata": {}},
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=query_context,
        outlet_signals=OutletSignals(proactive_message={"enabled": False}, spending={"enabled": False}),
        local_cache={},
        world_snapshot={},
        compact_voice=True,
    )

    assert context.debug["focused_user_text"] == "今天天气怎么样"
    assert "纠正后的本轮重点：\n今天天气怎么样" in context.prompt
    assert "不要把被否定的前半句" in context.prompt


def test_compact_voice_context_does_not_expose_weather_cache_for_ordinary_chat(tmp_path):
    config = _config(tmp_path)
    assets = load_persona_assets(config)
    query_context = resolve_query_context("你今天心情怎么样？", aura_home_city="上海")

    context = build_persona_context(
        user_text="你今天心情怎么样？",
        config=config,
        assets=assets,
        state={
            "mood": 80,
            "energy": 66,
            "satiety": 80,
            "trust": 70,
            "affinity_xp": 120,
            "stress": 12,
            "scene": "living_room",
            "metadata": {},
        },
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=query_context,
        outlet_signals=OutletSignals(proactive_message={"enabled": False}, spending={"enabled": False}),
        local_cache={"cached_weather": {"status": "fresh", "display": "北京，35.4度，多云"}},
        world_snapshot={},
        compact_voice=True,
    )

    assert context.debug["query_context"]["intent"] == "chat"
    assert context.debug["local_cache"] == {}
    assert "## 内部缓存证据" not in context.prompt
    assert "北京，35.4度" not in context.prompt


def test_weather_advice_context_marks_cache_as_internal_evidence(tmp_path):
    config = _config(tmp_path)
    assets = load_persona_assets(config)
    query_context = resolve_query_context(
        "你为什么建议我带伞？",
        aura_home_city="上海",
        user_geo={"city": "北京", "timezone": "Asia/Shanghai"},
    )

    context = build_persona_context(
        user_text="你为什么建议我带伞？",
        config=config,
        assets=assets,
        state={
            "mood": 80,
            "energy": 66,
            "satiety": 80,
            "trust": 70,
            "affinity_xp": 120,
            "stress": 12,
            "scene": "living_room",
            "metadata": {},
        },
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=query_context,
        outlet_signals=OutletSignals(proactive_message={"enabled": False}, spending={"enabled": False}),
        local_cache={"cached_weather": {"status": "fresh", "display": "北京，35.4度，多云，湿度36%"}},
        world_snapshot={},
        compact_voice=True,
    )

    assert context.debug["query_context"]["intent"] == "weather_advice"
    assert context.debug["local_cache"]["cached_weather"]["status"] == "fresh"
    assert "## 内部缓存证据" in context.prompt
    assert "先给建议结论" in context.prompt
    assert "不要只机械播报天气数据" in context.prompt
    assert "内部缓存证据不是用户原话" in context.prompt


def test_voice_gateway_turn_uses_compact_prompt_and_skips_moment_plan_reads(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = command[2]
        return subprocess.CompletedProcess(command, 0, stdout="我在，测试正常。", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    store.get_or_create_state(config.scope)
    store.save_im_message(config.scope, direction="user", message_type="user_text", body="第一条很长历史" * 20)
    store.save_im_message(config.scope, direction="aura", message_type="aura_text", body="第二条很长历史" * 20)
    with sqlite3.connect(config.companion_db_path) as conn:
        now = time.time()
        conn.execute(
            """
            INSERT INTO companion_moment_post
            (platform, chat_id, user_id, moment_type, visibility, title, body,
             location_label, activity_type, mood, energy, published_at, created_at, payload_json)
            VALUES (?, ?, ?, 'status', 'public', '逛街', '我刚刚在大悦城。',
                    '大悦城', 'walk', 80, 60, ?, ?, '{}')
            """,
            (*config.scope.as_tuple(), now, now),
        )
        conn.execute(
            """
            INSERT INTO companion_day_plan
            (platform, chat_id, user_id, plan_date, slot_key, scheduled_at, activity_type,
             title, location, should_post, status, expected_delta_json, payload_json)
            VALUES (?, ?, ?, ?, 'afternoon', ?, 'walk',
                    '去大悦城', '大悦城', 0, 'active', '{}', '{}')
            """,
            (*config.scope.as_tuple(), dt.datetime.now().date().isoformat(), now),
        )
        conn.commit()
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("请自然说一句今天状态。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    context_debug = result.debug["context"]
    assert context_debug["voice_low_latency"] is True
    assert context_debug["compact_voice"] is True
    assert context_debug["has_latest_moment"] is False
    assert context_debug["today_plan_count"] >= 6
    assert context_debug["world_snapshot"]["today_plan"]
    assert context_debug["world_snapshot"]["debug"]["schema"] == "aura_world_v2"
    assert "## 最近动态" not in captured["prompt"]
    assert "## 今日生活线" not in captured["prompt"]
    assert "用户原话：\n请自然说一句今天状态。" in captured["prompt"]
    assert "## 实时语音限制" in captured["prompt"]


def test_voice_gateway_skips_model_for_explicit_user_output_constraint(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Explicit fixed voice reply should skip the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn(
        "测试流式速度，请用十个字以内回答我在。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
    )

    assert result.ok is True
    assert result.response == "我在。"
    assert result.evidence["model_skipped"] is True
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "explicit_fixed_reply"


def test_voice_gateway_activity_query_exposes_stable_world_activity(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Current activity voice queries should skip the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "current_activity": "整理东西",
        "current_location": "desk",
        "location_label": "书桌边",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.response == "我在整理东西。"
    assert result.evidence["model_skipped"] is True
    assert result.debug["context"]["query_context"]["intent"] == "activity_or_location"
    assert result.debug["context"]["world_snapshot"]["current"]["activity_label"] == "整理东西"
    assert result.debug["context"]["world_snapshot"]["mention_policy"]["allow_activity"] is True
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "grounded_current_activity"
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["used_activity"] is True


def test_voice_gateway_activity_query_omits_stale_recent_activity_reply(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Stale recent activity should not be routed through the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    store.save_im_message(
        config.scope,
        direction="aura",
        message_type="aura_text",
        body="在家坐着呢，正打算去弄晚饭。",
    )
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "current_activity": "整理东西",
        "current_location": "desk",
        "location_label": "书桌边",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.debug["context"]["recent_message_count"] == 0
    assert result.response == "我在整理东西。"
    assert "在家" not in result.response
    assert "晚饭" not in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "grounded_current_activity"


def test_voice_gateway_activity_query_does_not_speak_generated_food_or_home_state(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Generated current state should not be treated as spoken fact")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "current_activity": "吃晚饭",
        "current_location": "home",
        "location_label": "家里",
        "world_current_source": "generated",
        "world_last_updated_at": time.time(),
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert "陪你说话" in result.response
    assert result.evidence["model_skipped"] is True
    assert "吃" not in result.response
    assert "家" not in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "grounded_current_activity"
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["source"] != "manual_state"
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["used_vague_activity"] is True
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["activity_category"] in {"rest", "quiet"}


def test_voice_gateway_location_query_uses_vague_life_reply_for_generated_state(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Generated current location should not require or reach the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    day_key = dt.datetime.fromtimestamp(time.time()).date().isoformat()
    store.replace_day_plan(
        config.scope,
        day_key=day_key,
        items=[
            {
                "slot_key": "afternoon",
                "scheduled_at": time.time() - 60,
                "activity_type": "browse",
                "title": "去附近商场逛一会儿",
                "location": "附近商场",
                "status": "active",
                "payload": {
                    "world_schema": "aura_world_v2",
                    "duration_minutes": 90,
                    "location_key": "outside.mall",
                    "location_label": "附近商场",
                    "activity_label": "随便逛逛",
                    "city": "上海",
                    "source": "aura_world_model",
                },
            }
        ],
    )
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你在哪？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.response == "具体位置先不说，刚活动了一下，现在正好陪你说话。"
    assert result.evidence["model_skipped"] is True
    assert "商场" not in result.response
    assert "附近" not in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "grounded_current_location"
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["used_location"] is False
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["used_vague_activity"] is True
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["activity_category"] == "outing"


def test_voice_gateway_activity_query_can_use_active_plan_activity(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Active plan activity should not require the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    day_key = dt.datetime.fromtimestamp(time.time()).date().isoformat()
    store.replace_day_plan(
        config.scope,
        day_key=day_key,
        items=[
            {
                "slot_key": "afternoon",
                "scheduled_at": time.time() - 60,
                "activity_type": "walk",
                "title": "出门走一小圈",
                "location": "住处附近",
                "status": "active",
                "payload": {
                    "world_schema": "aura_world_v2",
                    "duration_minutes": 90,
                    "location_key": "outside.park",
                    "location_label": "附近公园",
                    "activity_label": "散步",
                    "city": "上海",
                    "source": "aura_world_model",
                },
            }
        ],
    )
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.response == "我在散步。"
    assert result.evidence["model_skipped"] is True
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["source"] == "active_plan"
    assert result.debug["voice_turn"]["debug"]["grounded_current"]["used_activity"] is True


def test_voice_gateway_compound_activity_query_still_uses_model(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = command[2]
        return subprocess.CompletedProcess(command, 0, stdout="我在整理东西，今天安排得比较松。", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "current_activity": "整理东西",
        "current_location": "desk",
        "location_label": "书桌边",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你现在在干嘛，顺便说说今天安排。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.evidence.get("model_skipped") is not True
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "normal_chat"
    assert "今天安排" in captured["prompt"]


def test_persona_turn_answers_weather_advice_without_model_when_cache_is_fresh(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Fresh weather advice should skip Hermes and Aura LLM")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "35.4",
            "condition": "多云",
            "weather_icon": 1,
            "humidity": "36",
            "updated_at": int(time.time()),
            "ttl_seconds": 0,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，35.4度，多云，湿度36%",
            "source": "open_meteo",
            "observed_at": "2026-06-04T13:00",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="hermes_agent",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你为什么建议我带伞？", metadata={"user_geo": {"city": "北京"}})

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "带一把更稳" in result.response
    assert "防晒" in result.response
    assert "北京，35.4度，多云，湿度36%" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "cached_weather_advice"
    assert result.debug["context"]["query_context"]["intent"] == "weather_advice"


def test_persona_voice_turn_can_short_circuit_local_greeting_reply(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Hermes should not run for local greeting replies")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_mode="local_rule",
        greeting_reply="来了",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你好", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.response == "来了"
    assert result.evidence["model_skipped"] is True
    assert result.debug["hermes"]["status"] == "skipped"
    with sqlite3.connect(config.companion_db_path) as conn:
        im_count = conn.execute("SELECT count(*) FROM companion_im_message").fetchone()[0]
        debug_count = conn.execute("SELECT count(*) FROM companion_life_event WHERE event_type='lily.debug'").fetchone()[0]
    assert im_count == 2
    assert debug_count == 1


def test_persona_turn_short_circuits_recent_quality_examples(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("State and outing quality fast paths should skip Hermes")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        raise AssertionError("Outing chat fast path must not block on weather refresh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        user_weather_cache=(
            {
                "key": "open_meteo|上海||",
                "city": "上海",
                "temperature": "34.2",
                "condition": "多云",
                "weather_icon": 1,
                "humidity": "80",
                "updated_at": int(time.time()),
                "ttl_seconds": 3600,
                "display": "上海，34.2度，多云，湿度80%",
                "source": "open_meteo",
                "observed_at": "2026-07-02T15:00",
            },
        ),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    mood = gateway.run_turn("你今天心情怎么样？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})
    outing = gateway.run_turn(
        "我今天下午打算出门。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}},
    )

    assert mood.evidence["model_skipped"] is True
    assert mood.debug["voice_turn"]["debug"]["decision_path"] == "state_mood"
    assert "心情" in mood.response
    assert outing.evidence["model_skipped"] is True
    assert outing.debug["voice_turn"]["debug"]["decision_path"] == "outing_weather_advice"
    assert "上海" in outing.response
    assert "防晒" in outing.response


def test_persona_turn_uses_configured_user_geo_for_outing_fast_path(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Configured user geo outing fast path should skip Hermes")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        raise AssertionError("Outing fast path should use cache without blocking on weather refresh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    base_config = _config(tmp_path)
    config = PersonaGatewayConfig(
        **{
            **base_config.__dict__,
            "user_location_mode": "manual",
            "user_home_city": "上海",
            "user_timezone": "Asia/Shanghai",
        }
    )
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        user_weather_cache=(
            {
                "key": "open_meteo|上海||",
                "city": "上海",
                "temperature": "34.2",
                "condition": "多云",
                "weather_icon": 1,
                "humidity": "80",
                "updated_at": int(time.time()),
                "ttl_seconds": 3600,
                "display": "上海，34.2度，多云，湿度80%",
                "source": "open_meteo",
            },
        ),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("我今天下午打算出门。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.evidence["model_skipped"] is True
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "outing_weather_advice"
    assert "上海" in result.response
    assert "防晒" in result.response


def test_persona_turn_silently_drops_trivial_voice_noise(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Trivial ASR noise should not run Hermes or Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("嗯。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.status == "ignored"
    assert result.response == ""
    assert result.evidence["silent"] is True
    assert result.evidence["model_skipped"] is True
    assert result.debug["voice_turn"]["verdict"] == "silent_drop"
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "empty_or_noise"
    with sqlite3.connect(config.companion_db_path) as conn:
        messages = conn.execute("SELECT direction, body FROM companion_im_message ORDER BY id").fetchall()
        silent_events = conn.execute("SELECT count(*) FROM companion_life_event WHERE event_type='lily.voice.silent_drop'").fetchone()[0]
    assert messages == [("user", "嗯。")]
    assert silent_events == 1


def test_recent_bad_voice_examples_are_grounded_or_ignored(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Recent bad voice examples should not reach the model")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "35.4",
            "condition": "多云",
            "weather_icon": 1,
            "humidity": "36",
            "updated_at": int(time.time()),
            "ttl_seconds": 0,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，35.4度，多云，湿度36%",
            "source": "open_meteo",
            "observed_at": "2026-06-04T13:00",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    fixed = gateway.run_turn("测试流式速度，请用十个字以内回答我在。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})
    activity = gateway.run_turn("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})
    noise = gateway.run_turn("嗯。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})
    weather = gateway.run_turn("你为什么建议我带伞？", metadata={"user_geo": {"city": "北京"}, "source": "aura-lily-gateway", "audio_bytes": 1234})

    assert fixed.response == "我在。"
    assert fixed.evidence["model_skipped"] is True
    assert activity.evidence["model_skipped"] is True
    assert activity.debug["voice_turn"]["debug"]["decision_path"] == "grounded_current_activity"
    assert any(token in activity.response for token in ("听你说话", "陪你说话", "有空"))
    assert not any(token in activity.response for token in ("晚饭", "在家", "大悦城", "逛街", "商场"))
    assert noise.status == "ignored"
    assert noise.response == ""
    assert noise.evidence["silent"] is True
    assert weather.evidence["model_skipped"] is True
    assert "带一把更稳" in weather.response
    assert "依据是北京，35.4度，多云，湿度36%" in weather.response


def test_persona_turn_uses_cached_weather_without_model_even_in_main_mode(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Cached weather should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path, aura_home_city="上海")
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你那边天气怎么样")

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "上海" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "cached_weather"
    assert result.debug["context"]["local_cache"]["cached_weather"]["status"] == "fresh"


def test_persona_turn_answers_time_without_model_even_when_fast_reply_disabled(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Current time should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("现在是几点", metadata={"user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}})

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "上海现在是" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "current_time"
    assert result.debug["context"]["local_cache"]["current_time"]["time"]
    assert result.debug["context"]["query_context"]["subject_entity"] == "user"
    assert result.debug["context"]["query_context"]["target_location"] == "上海"


def test_persona_turn_answers_combined_aura_time_and_weather_without_model(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Combined time/weather should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path, aura_home_city="上海")
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_humidity="99",
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你那边几点了，天气怎么样？")

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "我这边现在是" in result.response
    assert "天气是上海，24度，多云，湿度99%" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "current_time_weather"
    assert result.debug["context"]["query_context"]["intent"] == "time_weather"
    assert result.debug["context"]["query_context"]["subject_entity"] == "aura"
    assert result.debug["context"]["local_cache"]["cached_weather"]["city"] == "上海"


def test_persona_turn_answers_combined_user_time_and_weather_without_model(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Combined user time/weather should skip Hermes and Aura LLM")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "18",
            "condition": "小雨",
            "weather_icon": 2,
            "humidity": "82",
            "updated_at": int(time.time()),
            "ttl_seconds": 3600,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，18度，小雨，湿度82%",
            "source": "open_meteo",
            "observed_at": "2026-06-04T13:00",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("现在几点，多少度？", metadata={"user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}})

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "上海现在是" in result.response
    assert "天气是上海，18度，小雨，湿度82%" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "current_time_weather"
    assert result.debug["context"]["query_context"]["intent"] == "time_weather"
    assert result.debug["context"]["query_context"]["subject_entity"] == "user"
    assert result.debug["context"]["local_cache"]["cached_weather"]["city"] == "上海"
    assert gateway.runtime_config.cached_weather_city == "上海"


def test_persona_turn_skips_model_for_explicit_fixed_short_reply(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Explicit fixed short replies should skip the model")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("测试流式速度，请用十个字以内回答我在。")

    assert result.ok is True
    assert result.response == "我在。"
    assert result.evidence["model_skipped"] is True
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "explicit_fixed_reply"


def test_persona_turn_refuses_to_guess_weather_without_cache(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Unavailable weather should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="",
        cached_weather_condition="",
        weather_auto_refresh_enabled=False,
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你那边天气怎么样")

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "没有实时天气数据" in result.response
    assert "不能乱说" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "weather_unavailable"


def test_persona_turn_uses_corrected_weather_focus_not_connector_as_location(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Corrected weather miss should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        weather_auto_refresh_enabled=False,
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn(
        "等一下我不是测试一下，我是问今天天气怎么样。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}},
    )

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "上海" in result.response
    assert "我是问" not in result.response
    assert result.debug["setup"]["focused_user_text"] == "今天天气怎么样"
    assert result.debug["context"]["query_context"]["subject_entity"] == "user"
    assert result.debug["context"]["query_context"]["target_location"] == "上海"
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "weather_unavailable"
    assert result.debug["context"]["local_cache"]["cached_weather"]["status"] in {"empty", "disabled"}


def test_persona_turn_refreshes_aura_weather_without_model(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Fresh weather should skip Hermes and Aura LLM")

    def fake_refresh(config, *, city="", force=False):
        updated = save_aura_runtime_config(config, {
            "cached_weather_enabled": True,
            "cached_weather_city": city or "上海",
            "cached_weather_temperature": "25",
            "cached_weather_condition": "晴",
            "cached_weather_icon": 0,
            "cached_weather_humidity": "61",
            "cached_weather_source": "open_meteo",
            "cached_weather_observed_at": "2026-06-04T13:00",
        })
        return updated, {"ok": True, "status": "refreshed", "weather": cached_weather_snapshot(updated)}

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_cached_weather_if_needed", fake_refresh)
    config = _config(tmp_path, aura_home_city="上海")
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="",
        cached_weather_condition="",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你那边多少度")

    assert result.ok is True
    assert result.evidence["model_skipped"] is True
    assert "上海" in result.response
    assert "25度" in result.response
    assert "湿度61%" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "cached_weather"
    assert result.debug["context"]["local_cache"]["cached_weather"]["source"] == "open_meteo"
    assert result.debug["context"]["query_context"]["subject_entity"] == "aura"


def test_persona_turn_uses_user_weather_for_bare_temperature(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("User weather should skip Hermes and Aura LLM")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "18",
            "condition": "小雨",
            "weather_icon": 2,
            "humidity": "82",
            "updated_at": int(time.time()),
            "ttl_seconds": 0,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，18度，小雨，湿度82%",
            "source": "open_meteo",
            "observed_at": "2026-06-04T13:00",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("现在多少度", metadata={"user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}})

    assert result.evidence["model_skipped"] is True
    assert result.response == "上海，18度，小雨，湿度82%。"
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "cached_weather"
    assert result.debug["context"]["query_context"]["subject_entity"] == "user"
    assert result.debug["context"]["query_context"]["target_location"] == "上海"
    assert gateway.runtime_config.cached_weather_city == "上海"
    assert gateway.runtime_config.cached_weather_temperature == "24"


def test_persona_turn_does_not_use_aura_weather_for_bare_temperature_without_user_location(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Unknown user weather should skip Hermes and Aura LLM")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("现在多少度")

    assert result.evidence["model_skipped"] is True
    assert "还不知道你那边的位置" in result.response
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "weather_unavailable"
    assert result.debug["context"]["query_context"]["subject_entity"] == "user"
    assert result.debug["context"]["local_cache"]["cached_weather"]["status"] == "unknown_location"


def test_persona_turn_uses_user_weather_without_overwriting_aura_cache(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("User-location weather must not reuse Aura weather cache")

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "18",
            "condition": "小雨",
            "weather_icon": 2,
            "humidity": "82",
            "updated_at": int(time.time()),
            "ttl_seconds": 0,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，18度，小雨，湿度82%",
            "source": "open_meteo",
            "observed_at": "2026-06-04T13:00",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="deepseek",
        aura_model_model="deepseek-chat",
        aura_model_base_url="https://api.deepseek.com",
        aura_model_api_key="unit-key",
        cached_weather_city="上海",
        cached_weather_temperature="24",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("我这边天气怎么样", metadata={"user_geo": {"city": "上海"}})

    assert result.evidence["model_skipped"] is True
    assert result.response == "上海，18度，小雨，湿度82%。"
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "cached_weather"
    assert result.debug["context"]["query_context"]["target_location"] == "上海"
    assert gateway.runtime_config.cached_weather_city == "上海"
    assert gateway.runtime_config.cached_weather_temperature == "24"


def test_user_weather_query_uses_memory_cache_between_refreshes(monkeypatch):
    from integrations.aura_persona_gateway import weather as weather_mod

    calls = []

    def fake_fetch_current_weather(*, city, provider="open_meteo", latitude="", longitude="", timeout_seconds=8.0):
        calls.append((city, latitude, longitude))
        return {
            "ok": True,
            "weather": {
                "enabled": True,
                "status": "fresh",
                "city": city,
                "temperature": "18",
                "condition": "小雨",
                "weather_icon": 2,
                "humidity": "82",
                "updated_at": int(time.time()),
                "ttl_seconds": 0,
                "age_seconds": 0,
                "has_content": True,
                "display": f"{city}，18度，小雨，湿度82%",
                "source": "open_meteo",
                "observed_at": "2026-06-04T13:00",
            },
        }

    weather_mod._QUERY_WEATHER_CACHE.clear()
    monkeypatch.setattr("integrations.aura_persona_gateway.weather.fetch_current_weather", fake_fetch_current_weather)
    runtime = AuraRuntimeConfig(weather_refresh_interval_seconds=1800, cached_weather_ttl_seconds=3600)

    first = weather_snapshot_for_query(runtime, city="上海")
    second = weather_snapshot_for_query(runtime, city="上海")

    assert len(calls) == 1
    assert first["display"] == "上海，18度，小雨，湿度82%"
    assert second["display"] == first["display"]
    assert second["ttl_seconds"] == 3600


def test_cached_user_weather_snapshot_is_read_only(monkeypatch):
    from integrations.aura_persona_gateway import weather as weather_mod
    from integrations.aura_persona_gateway.weather import cached_user_weather_snapshot

    def fail_fetch(*args, **kwargs):
        raise AssertionError("cached_user_weather_snapshot must not call external weather API")

    weather_mod._QUERY_WEATHER_CACHE.clear()
    monkeypatch.setattr("integrations.aura_persona_gateway.weather.fetch_current_weather", fail_fetch)
    runtime = AuraRuntimeConfig(
        weather_refresh_interval_seconds=1800,
        cached_weather_ttl_seconds=3600,
        user_weather_cache=(
            {
                "key": "open_meteo|上海||",
                "city": "上海",
                "temperature": "18",
                "condition": "小雨",
                "weather_icon": 2,
                "humidity": "82",
                "updated_at": int(time.time()),
                "ttl_seconds": 3600,
                "display": "上海，18度，小雨，湿度82%",
                "source": "open_meteo",
            },
        ),
    )

    hit = cached_user_weather_snapshot(runtime, city="上海")
    miss = cached_user_weather_snapshot(runtime, city="北京")

    assert hit["display"] == "上海，18度，小雨，湿度82%"
    assert miss == {}


def test_user_weather_query_persists_cache_to_runtime(tmp_path, monkeypatch):
    from integrations.aura_persona_gateway import weather as weather_mod

    calls = []

    def fake_fetch_current_weather(*, city, provider="open_meteo", latitude="", longitude="", timeout_seconds=8.0):
        calls.append((city, latitude, longitude))
        return {
            "ok": True,
            "weather": {
                "enabled": True,
                "status": "fresh",
                "city": city,
                "temperature": "19",
                "condition": "晴",
                "weather_icon": 0,
                "humidity": "41",
                "updated_at": int(time.time()),
                "ttl_seconds": 0,
                "age_seconds": 0,
                "has_content": True,
                "display": f"{city}，19度，晴，湿度41%",
                "source": "open_meteo",
                "observed_at": "2026-06-10T12:00",
                "latitude": 39.9042,
                "longitude": 116.4074,
            },
        }

    weather_mod._QUERY_WEATHER_CACHE.clear()
    monkeypatch.setattr("integrations.aura_persona_gateway.weather.fetch_current_weather", fake_fetch_current_weather)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        weather_refresh_interval_seconds=1800,
        cached_weather_ttl_seconds=3600,
    )

    updated, first = refresh_user_weather_if_needed(
        runtime,
        city="Beijing",
        latitude="39.9042",
        longitude="116.4074",
    )
    reloaded = load_aura_runtime_config(persona_home=runtime.persona_home)
    weather_mod._QUERY_WEATHER_CACHE.clear()
    second_updated, second = refresh_user_weather_if_needed(
        reloaded,
        city="北京",
        latitude="39.9042",
        longitude="116.4074",
    )

    assert len(calls) == 1
    assert first["city"] == "北京"
    assert first["display"] == "北京，19度，晴，湿度41%"
    assert updated.user_weather_cache[0]["city"] == "北京"
    assert reloaded.user_weather_cache[0]["city"] == "北京"
    assert second_updated.user_weather_cache[0]["city"] == "北京"
    assert second["display"] == "北京，19度，晴，湿度41%"


def test_persona_turn_acknowledges_and_runs_background_task(tmp_path):
    captured = {}

    class FakeBridge:
        config = HermesLilyConfig(command=("hermes",))

        def run(self, goal, *, metadata=None):
            captured["goal"] = goal
            captured["metadata"] = dict(metadata or {})
            return HermesLilyResult(
                ok=True,
                status="completed",
                response="后台查完了：今天适合测试 Lily。",
                request_id="bg-unit",
                latency_ms=9,
                evidence={"route": "hermes_agent"},
            )

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        fast_reply_mode="local_rule",
        ack_and_enqueue_enabled=True,
        background_ack_reply="好，我先处理，完成后告诉你。",
        aura_model_mode="aura_model",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=FakeBridge(), runtime_config=runtime)

    result = gateway.run_turn("帮我查一下今天的新闻")

    assert result.ok is True
    assert result.status == "deferred"
    assert result.response == "好，我先处理，完成后告诉你。"
    assert result.evidence["deferred"] is True
    assert result.evidence["task_kind"] == "agent_lookup"
    deadline = time.time() + 3
    rows = []
    events = []
    while time.time() < deadline:
        with sqlite3.connect(config.companion_db_path) as conn:
            rows = conn.execute(
                "SELECT direction, message_type, body, status, task_id, metadata_json "
                "FROM companion_im_message ORDER BY id"
            ).fetchall()
            events = conn.execute(
                "SELECT event_type, title FROM companion_life_event ORDER BY id"
            ).fetchall()
        if (
            "goal" in captured
            and any(row[1] == "background_task_result" for row in rows)
            and any(row[0] == "lily.background_task.completed" for row in events)
        ):
            break
        time.sleep(0.02)
    # goal 现在是"人设+最近对话+任务+输出要求"的组合 prompt，原始任务文本要在其中
    assert "帮我查一下今天的新闻" in captured["goal"]
    assert "你是 Lily" in captured["goal"]
    assert "最近对话" in captured["goal"]
    assert captured["metadata"]["aura_model_mode"] == "hermes_agent"
    assert captured["metadata"]["background_task"]["task_kind"] == "agent_lookup"
    assert [row[1] for row in rows] == ["user_text", "aura_text", "background_task_result"]
    assert rows[1][3] == "sent"
    assert rows[1][4]
    assert rows[2][2] == "后台查完了：今天适合测试 Lily。"
    assert rows[2][4] == rows[1][4]
    rendered_events = json.dumps(events, ensure_ascii=False)
    assert "lily.background_task.queued" in rendered_events
    assert "lily.background_task.completed" in rendered_events


def test_persona_turn_uses_direct_aura_model_without_hermes_agent(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        raise AssertionError("direct Aura LLM should not invoke Hermes CLI")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "独立模型回复"}}],
            }).encode("utf-8")

    def fake_urlopen(req: Request, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(**{
        "persona_home": config.persona_home,
        "aura_model_mode": "aura_model",
        "aura_model_provider": "deepseek",
        "aura_model_model": "deepseek-chat",
        "aura_model_base_url": "https://api.deepseek.com",
        "aura_model_api_key": "aura-unit-key",
        "aura_model_timeout_seconds": 33,
        "aura_model_max_tokens": 80,
        "aura_model_temperature": "0.3",
        "aura_model_reasoning_effort": "low",
    })
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你今天干什么")

    assert result.ok is True
    assert result.response == "独立模型回复"
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer aura-unit-key"
    assert captured["body"]["model"] == "deepseek-chat"
    assert captured["body"]["max_tokens"] == 80
    assert captured["body"]["temperature"] == 0.3
    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["messages"][0]["role"] == "system"
    assert SPOKEN_REPLY_INSTRUCTION in captured["body"]["messages"][0]["content"]
    assert captured["body"]["messages"][1]["role"] == "user"
    assert captured["timeout"] == 33
    assert result.debug["aura_runtime"]["aura_model_mode"] == "aura_model"
    assert result.debug["aura_runtime"]["model_route"] == "direct_llm"


def test_direct_aura_model_guard_blocks_world_location_in_ordinary_chat(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("direct Aura LLM should not invoke Hermes CLI")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "我刚刚在大悦城逛商场，顺手买了咖啡。"}}],
            }).encode("utf-8")

    def fake_urlopen(req: Request, timeout):
        return FakeResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["metadata"] = {
        "current_activity": "逛商场",
        "current_location": "mall",
        "location_label": "大悦城",
        "world_current_source": "manual",
        "world_manual_override": True,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="deepseek",
        aura_model_model="deepseek-chat",
        aura_model_base_url="https://api.deepseek.com",
        aura_model_api_key="aura-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("随便说一句。")

    assert result.ok is True
    assert result.response == "我在。"
    assert result.debug["reply_contract"]["quality_guard"]["reason"] == "blocked_world_background_leak"
    assert result.debug["context"]["world_snapshot"]["mention_policy"]["allow_location"] is False
    assert not any(token in result.response for token in ("大悦城", "商场", "咖啡"))
    with sqlite3.connect(config.companion_db_path) as conn:
        body, metadata_json = conn.execute(
            "SELECT body, metadata_json FROM companion_im_message "
            "WHERE direction='aura' AND message_type='aura_text'"
        ).fetchone()
    metadata = json.loads(metadata_json)
    assert body == "我在。"
    assert metadata["reply_contract"]["quality_guard"]["fallback_used"] is True


def test_direct_aura_model_guard_keeps_safe_sentence_before_world_location(tmp_path, monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "我在听你说。刚刚在大悦城逛商场。"}}],
            }).encode("utf-8")

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", lambda req, timeout: FakeResponse())
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["metadata"] = {
        "current_activity": "逛商场",
        "current_location": "mall",
        "location_label": "大悦城",
        "world_current_source": "manual",
        "world_manual_override": True,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="deepseek",
        aura_model_model="deepseek-chat",
        aura_model_base_url="https://api.deepseek.com",
        aura_model_api_key="aura-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("继续。")

    assert result.response == "我在。"
    assert result.debug["reply_contract"]["quality_guard"]["reason"] == "blocked_placeholder_reply"
    assert "大悦城" not in result.response
    assert "商场" not in result.response


def test_direct_aura_model_guard_does_not_block_explicit_location_answer(tmp_path, monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "我在大悦城。"}}],
            }).encode("utf-8")

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", lambda req, timeout: FakeResponse())
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 90
    state["affinity_xp"] = 320
    state["metadata"] = {
        "current_activity": "逛商场",
        "current_location": "mall",
        "location_label": "大悦城",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 10,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="deepseek",
        aura_model_model="deepseek-chat",
        aura_model_base_url="https://api.deepseek.com",
        aura_model_api_key="aura-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("你在哪？")

    assert result.response == "我在大悦城。"
    assert "quality_guard" not in result.debug["reply_contract"]


def test_direct_aura_model_uses_stepfun_endpoint(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        raise AssertionError("direct Aura LLM should not invoke Hermes CLI")

    monkeypatch.setattr(subprocess, "run", fake_run)

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"StepFun ok"}}]}'

    def fake_urlopen(req: Request, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.ai/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("测试一下")

    assert result.ok is True
    assert result.response == "StepFun ok"
    assert captured["url"] == "https://api.stepfun.ai/step_plan/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer stepfun-unit-key"
    assert captured["body"]["model"] == "step-3.5-flash"
    assert captured["body"]["max_tokens"] == 96
    assert captured["body"]["temperature"] == 0.4
    assert "reasoning_effort" not in captured["body"]


def test_direct_aura_model_stream_uses_low_latency_generation_params(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"好的"}}]}\n\n'.encode("utf-8")
            yield b'data: [DONE]\n\n'

    def fake_urlopen(req: Request, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_max_tokens=96,
        aura_model_temperature="0.2",
        aura_model_reasoning_effort="low",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("请自然说一句今天的状态。"))

    assert [item.get("type") for item in events] == ["final"]
    assert events[-1]["payload"]["response"] == "好的"
    assert captured["url"] == "https://api.stepfun.com/step_plan/v1/chat/completions"
    assert captured["body"]["stream"] is True
    assert captured["body"]["max_tokens"] == 384
    assert captured["body"]["temperature"] == 0.2
    assert captured["body"]["reasoning_effort"] == "low"
    assert "modalities" not in captured["body"]
    assert "实时语音对话" in captured["body"]["messages"][0]["content"]
    assert "不要用“嗯”“我想一下”“稍等”这类前导占位" in captured["body"]["messages"][0]["content"]
    assert "不是手机基站" in captured["body"]["messages"][0]["content"]


def test_direct_llm_stream_reuses_keepalive_connection(monkeypatch):
    created: list[int] = []

    class FakeHttpResponse:
        status = 200
        reason = "OK"
        headers = {}

        def __init__(self, index: int) -> None:
            self.index = index
            self.lines = iter([
                'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def readline(self):
            return next(self.lines, b"")

    class FakeConnection:
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.index = len(created) + 1
            self.closed = False
            self.requests: list[dict[str, object]] = []
            created.append(self.index)

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

        def getresponse(self) -> FakeHttpResponse:
            return FakeHttpResponse(self.index)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_KEEPALIVE_ENABLED", "1")
    monkeypatch.setattr(aura_llm_module, "HTTPSConnection", FakeConnection)
    aura_llm_module.close_direct_llm_http_pool()
    client = aura_llm_module.DirectLlmClient(
        aura_llm_module.DirectLlmConfig(
            provider="stepfun",
            model="step-3.5-flash",
            base_url="https://api.stepfun.com/step_plan/v1",
            api_key="unit-key",
            reasoning_effort="none",
        )
    )

    first = list(client.stream("第一句"))
    second = list(client.stream("第二句"))
    aura_llm_module.close_direct_llm_http_pool()

    assert [item.get("type") for item in first] == ["delta", "final"]
    assert [item.get("type") for item in second] == ["delta", "final"]
    assert len(created) == 1
    assert first[0]["timing"].get("aura_llm_http_keepalive") is False
    assert second[0]["timing"].get("aura_llm_http_keepalive") is True
    assert second[-1]["evidence"].get("aura_llm_http_keepalive") is True


def test_direct_llm_warm_preconnect_is_reused_by_first_stream(monkeypatch):
    created: list["FakeConnection"] = []

    class FakeHttpResponse:
        status = 200
        reason = "OK"
        headers = {}

        def readline(self):
            if not hasattr(self, "_lines"):
                self._lines = iter([
                    'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'.encode("utf-8"),
                    b"data: [DONE]\n\n",
                ])
            return next(self._lines, b"")

    class FakeConnection:
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.connected = False
            self.closed = False
            self.requests: list[dict[str, object]] = []
            created.append(self)

        def connect(self) -> None:
            self.connected = True

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            self.requests.append({"method": method, "path": path, "body": body, "headers": headers})

        def getresponse(self) -> FakeHttpResponse:
            return FakeHttpResponse()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_KEEPALIVE_ENABLED", "1")
    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_WARM_ENABLED", "1")
    monkeypatch.setattr(aura_llm_module, "HTTPSConnection", FakeConnection)
    aura_llm_module.close_direct_llm_http_pool()
    config = aura_llm_module.DirectLlmConfig(
        provider="stepfun",
        model="step-3.5-flash",
        base_url="https://api.stepfun.com/step_plan/v1",
        api_key="unit-key",
        reasoning_effort="none",
    )

    warm = aura_llm_module.warm_direct_llm_http_pool(config, timeout_seconds=0.5)
    client = aura_llm_module.DirectLlmClient(config)
    first = list(client.stream("第一句"))
    aura_llm_module.close_direct_llm_http_pool()

    assert warm["ok"] is True
    assert warm["status"] == "warmed"
    assert len(created) == 1
    assert created[0].connected is True
    assert len(created[0].requests) == 1
    assert first[0]["timing"].get("aura_llm_http_keepalive") is True
    assert first[-1]["evidence"].get("aura_llm_http_keepalive") is True


def test_direct_llm_warm_can_be_disabled(monkeypatch):
    created: list[object] = []

    class FakeConnection:
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            created.append(self)

    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_KEEPALIVE_ENABLED", "1")
    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_WARM_ENABLED", "0")
    monkeypatch.setattr(aura_llm_module, "HTTPSConnection", FakeConnection)
    aura_llm_module.close_direct_llm_http_pool()

    result = aura_llm_module.warm_direct_llm_http_pool(
        aura_llm_module.DirectLlmConfig(
            provider="stepfun",
            model="step-3.5-flash",
            base_url="https://api.stepfun.com/step_plan/v1",
            api_key="unit-key",
        ),
        timeout_seconds=0.5,
    )

    assert result["ok"] is False
    assert result["status"] == "warm_disabled"
    assert created == []


def test_direct_llm_empty_non_stream_response_uses_spoken_safe_fallback(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":""}}]}'

    monkeypatch.setattr(aura_llm_module, "urlopen", lambda req, timeout: FakeResponse())
    client = aura_llm_module.DirectLlmClient(
        aura_llm_module.DirectLlmConfig(
            provider="stepfun",
            model="step-3.5-flash",
            base_url="https://api.stepfun.com/step_plan/v1",
            api_key="unit-key",
        )
    )

    result = client.run("说一句")

    assert result.ok is False
    assert result.evidence["stop_reason"] == "empty_response"
    assert result.response == aura_llm_module.DIRECT_LLM_EMPTY_RESPONSE_FALLBACK
    assert "Aura direct LLM" not in result.response


def test_direct_llm_empty_stream_response_uses_spoken_safe_fallback(monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                b'data: {"choices":[{"delta":{"content":"","reasoning_content":"thinking"}}]}\n\n',
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(aura_llm_module, "urlopen", lambda req, timeout: FakeStreamResponse())
    client = aura_llm_module.DirectLlmClient(
        aura_llm_module.DirectLlmConfig(
            provider="stepfun",
            model="step-3.7-flash",
            base_url="https://api.stepfun.com/step_plan/v1",
            api_key="unit-key",
        )
    )

    events = list(client.stream("说一句"))

    assert [event["type"] for event in events] == ["final"]
    assert events[-1]["ok"] is False
    assert events[-1]["evidence"]["stop_reason"] == "empty_response"
    assert events[-1]["response"] == aura_llm_module.DIRECT_LLM_EMPTY_RESPONSE_FALLBACK
    assert "Aura direct LLM" not in events[-1]["response"]


def test_direct_llm_stream_retries_stale_keepalive_connection(monkeypatch):
    created: list["FakeConnection"] = []

    class FakeHttpResponse:
        status = 200
        reason = "OK"
        headers = {}

        def __init__(self, text: str) -> None:
            self.lines = iter([
                f'data: {{"choices":[{{"delta":{{"content":"{text}"}}}}]}}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def readline(self):
            return next(self.lines, b"")

    class FakeConnection:
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.index = len(created) + 1
            self.closed = False
            self._request_count = 0
            created.append(self)

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            self._request_count += 1
            if self.index == 1 and self._request_count == 2:
                raise OSError("stale socket")

        def getresponse(self) -> FakeHttpResponse:
            return FakeHttpResponse(f"好{self.index}")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_KEEPALIVE_ENABLED", "1")
    monkeypatch.setenv("AURA_DIRECT_LLM_HTTP_KEEPALIVE_RETRY_ONCE", "1")
    monkeypatch.setattr(aura_llm_module, "HTTPSConnection", FakeConnection)
    aura_llm_module.close_direct_llm_http_pool()
    client = aura_llm_module.DirectLlmClient(
        aura_llm_module.DirectLlmConfig(
            provider="stepfun",
            model="step-3.5-flash",
            base_url="https://api.stepfun.com/step_plan/v1",
            api_key="unit-key",
            reasoning_effort="none",
        )
    )

    list(client.stream("第一句"))
    second = list(client.stream("第二句"))
    aura_llm_module.close_direct_llm_http_pool()

    assert len(created) == 2
    assert created[0].closed is True
    assert second[0]["text"] == "好2"
    assert second[0]["timing"].get("aura_llm_http_keepalive_retry") is True
    assert second[-1]["evidence"].get("aura_llm_http_keepalive_retry") is True


def test_direct_aura_model_stream_adds_text_modalities_for_stepaudio_chat(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"从状态说起。"}}]}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("请自然说一句今天状态。"))

    assert events[0]["text"] == "从状态说起。"
    assert captured["body"]["modalities"] == ["text"]
    final = events[-1]["payload"]
    assert final["evidence"]["aura_llm_modalities"] == "text"


def test_direct_aura_model_stream_keeps_second_sentence_and_stops_before_third(tmp_path, monkeypatch):
    captured = {"lines": 0}

    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            lines = [
                'data: {"choices":[{"delta":{"content":"这块主要慢在模型生成和语音合成。"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"我会优先把回答压短。"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"第三句不应该继续消费，也不该送去语音。"}}]}\n\n',
                "data: [DONE]\n\n",
            ]
            for line in lines:
                captured["lines"] += 1
                yield line.encode("utf-8")

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("请自然说说实时语音回答要怎么保持简短。"))

    assert [item.get("type") for item in events] == ["delta", "delta", "final"]
    assert events[0]["text"] == "这块主要慢在模型生成和语音合成。"
    assert events[1]["text"] == "我会优先把回答压短。"
    final = events[2]["payload"]
    assert final["response"] == "这块主要慢在模型生成和语音合成。我会优先把回答压短。"
    assert final["evidence"]["voice_compacted"] is True
    assert final["evidence"]["stop_reason"] == "voice_compact_limit"
    assert "第三句" not in final["response"]
    assert captured["lines"] == 2


def _agent_marker_stream_gateway(tmp_path, monkeypatch, stream_lines, captured=None):
    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            for line in stream_lines:
                yield line.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req: Request, timeout):
        if captured is not None:
            captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)

    class FakeBridge:
        config = HermesLilyConfig(command=("hermes",))

        def __init__(self):
            self.calls = []

        def run(self, goal, *, metadata=None):
            self.calls.append({"goal": goal, "metadata": dict(metadata or {})})
            return HermesLilyResult(
                ok=True,
                status="completed",
                response="后台查完了。",
                request_id="bg-marker-unit",
                latency_ms=5,
                evidence={"route": "hermes_agent"},
            )

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = FakeBridge()
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        ack_and_enqueue_enabled=True,
        background_ack_reply="好，我去查，弄完马上告诉你。",
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return gateway, bridge


def test_stream_llm_agent_marker_defers_to_background(tmp_path, monkeypatch):
    # 正则命中"查/比特币/价格"的问题现在先交给快模型判断；模型输出 [后台] 标记
    # 才转后台，且任务描述用模型概括的文本，标记本身不进 TTS。
    captured = {}
    gateway, bridge = _agent_marker_stream_gateway(
        tmp_path,
        monkeypatch,
        [
            'data: {"choices":[{"delta":{"content":"[后"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"台]查比特币当前价格"}}]}\n\n',
        ],
        captured,
    )

    events = list(gateway.run_direct_turn_stream("比特币现在多少钱"))

    # 系统提示里要带意图判断指令
    assert "[后台]" in captured["body"]["messages"][0]["content"]
    deltas = [item for item in events if item.get("type") == "delta"]
    assert len(deltas) == 1
    assert deltas[0]["text"] == "好，我去查，弄完马上告诉你。"
    assert all("[后台]" not in str(item.get("text") or "") for item in deltas)
    final = events[-1]["payload"]
    assert final["status"] == "deferred"
    assert final["evidence"]["deferred"] is True
    assert final["voice_turn"]["debug"]["decision_path"] == "llm_agent_marker"
    assert final["voice_turn"]["background_task"]["source_text"] == "查比特币当前价格"
    deadline = time.time() + 3
    while time.time() < deadline and not bridge.calls:
        time.sleep(0.02)
    assert bridge.calls, "后台任务应当触发 hermes bridge"
    assert "查比特币当前价格" in bridge.calls[0]["goal"]


def test_stream_llm_answers_directly_even_when_regex_would_enqueue(tmp_path, monkeypatch):
    # "查"字命中旧正则，但模型自己能稳答（没输出标记）→ 当场回答，不再转后台。
    gateway, bridge = _agent_marker_stream_gateway(
        tmp_path,
        monkeypatch,
        ['data: {"choices":[{"delta":{"content":"水的沸点在标准大气压下是一百度。"}}]}\n\n'],
    )

    events = list(gateway.run_direct_turn_stream("帮我查一下水的沸点是多少"))

    final = events[-1]["payload"]
    assert final["status"] != "deferred"
    assert "一百度" in final["response"]
    time.sleep(0.1)
    assert not bridge.calls, "模型没打标记就不该起后台任务"


def test_direct_aura_model_stream_caps_long_two_sentence_reply_by_chars(tmp_path, monkeypatch):
    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"喔唷，一上来就开始搞技术调研啦？"}}]}\n\n'.encode("utf-8")
            yield 'data: {"choices":[{"delta":{"content":"其实最卡的地方多半就在网络跳转或者云端处理那一块儿，答案我待会儿慢慢跟你说。"}}]}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("请自然说一句今天的状态和安排。"))

    final = events[-1]["payload"]
    # 现在的口径：优先保完整句；两句共 54 字，在 80 字上限内就整句保留，不许腰斩。
    assert final["response"] == (
        "喔唷，一上来就开始搞技术调研啦？其实最卡的地方多半就在网络跳转或者云端处理那一块儿，答案我待会儿慢慢跟你说。"
    )
    assert final["evidence"]["voice_compacted"] is True


def test_direct_aura_model_stream_drops_unsafe_normal_reply_sentence(tmp_path, monkeypatch):
    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"闭上眼，我给你放首歌。"}}]}\n\n'.encode("utf-8")
            yield 'data: {"choices":[{"delta":{"content":"我就在这儿听你说。"}}]}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("测试一下普通回复过滤。"))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "我就在这儿听你说。"
    final = events[1]["payload"]
    assert final["response"] == "我就在这儿听你说。"
    assert "闭上眼" not in final["response"]
    assert "放首歌" not in final["response"]


def test_direct_aura_model_stream_uses_local_quick_ack_without_llm(tmp_path, monkeypatch):
    def fake_urlopen(req: Request, timeout):
        raise AssertionError("Quick ack voice turns should not call direct LLM")

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("测试一下，简单回应我一句。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["source"] == "local_voice_reply"
    assert events[0]["text"] == "我在。"
    final = events[1]["payload"]
    assert final["response"] == "我在。"
    assert final["evidence"]["model_skipped"] is True
    assert final["voice_turn"]["debug"]["local_social"]["matched"] == "quick_ack"
    stored = store.list_recent_messages(config.scope, limit=4)
    aura_messages = [item for item in stored if item["direction"] == "aura"]
    assert aura_messages[-1]["metadata"]["streamed"] is True
    assert aura_messages[-1]["metadata"]["local_voice_reply"] is True
    assert aura_messages[-1]["metadata"]["evidence"]["model_skipped"] is True


def test_non_chinese_voice_turn_bypasses_chinese_quick_reply_and_enforces_prompt(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        def __enter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": "I'm right here."}}]})
            return iter([f"data: {payload}\n\n".encode(), b"data: [DONE]\n\n"])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    metadata = {"source": "aura-lily-gateway", "audio_bytes": 1234, "language": "en-US"}
    events = list(gateway.run_direct_turn_stream("Are you there?", metadata=metadata))

    assert not any(event.get("source") == "local_voice_reply" for event in events)
    assert events[-1]["payload"]["response"] == "I'm right here."
    prompt = captured["body"]["messages"][-1]["content"]
    assert "Reply only in natural English" in prompt


def test_japanese_voice_context_has_high_priority_language_rule(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    gateway = AuraPersonaGateway(
        config=config,
        store=store,
        bridge=bridge,
        runtime_config=AuraRuntimeConfig(persona_home=config.persona_home),
    )

    context, _, _ = gateway.build_context(
        "今日はどうだった？",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "language": "ja-JP"},
    )

    assert "自然な日本語だけ" in context.prompt
    assert context.debug["conversation_language"] == "ja"


def test_direct_aura_model_stream_uses_status_review_entry_as_quality_fallback(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"先看睡眠还是工作节奏？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", lambda req, timeout: FakeStreamResponse())
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "先看睡眠还是工作节奏？"
    final = events[-1]["payload"]
    assert final["response"] == "先看睡眠还是工作节奏？"
    assert "model_skipped" not in final["evidence"]
    assert final["evidence"]["local_preface"] is False
    assert final["voice_turn"]["debug"]["decision_path"] == "status_review_entry"


def test_direct_aura_model_stream_speculative_local_reply_has_no_store_side_effects(tmp_path, monkeypatch):
    def fake_urlopen(req: Request, timeout):
        raise AssertionError("Speculative quick ack should not call direct LLM")

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    before_state = store.get_or_create_state(config.scope)
    before_messages = _im_message_count(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream(
        "测试一下，简单回应我一句。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "speculative": True},
    ))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "我在。"
    final = events[1]["payload"]
    assert final["response"] == "我在。"
    assert final["evidence"]["speculative"] is True
    assert final["evidence"]["model_skipped"] is True
    assert _im_message_count(config.companion_db_path) == before_messages
    after_state = store.get_or_create_state(config.scope)
    assert after_state["mood"] == before_state["mood"]
    assert after_state["energy"] == before_state["energy"]
    assert after_state["trust"] == before_state["trust"]


def test_direct_aura_model_stream_speculative_normal_reply_has_no_store_side_effects(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"今天状态还不错。"}}]}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    before_state = store.get_or_create_state(config.scope)
    before_messages = _im_message_count(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream(
        "请自然说一句今天的状态。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "speculative": True},
    ))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "今天状态还不错。"
    final = events[1]["payload"]
    assert final["response"] == "今天状态还不错。"
    assert final["evidence"]["speculative"] is True
    assert captured["body"]["stream"] is True
    assert _im_message_count(config.companion_db_path) == before_messages
    after_state = store.get_or_create_state(config.scope)
    assert after_state["mood"] == before_state["mood"]
    assert after_state["energy"] == before_state["energy"]
    assert after_state["trust"] == before_state["trust"]


def test_direct_aura_model_stream_sends_state_mood_as_local_delta(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"我也想把这点亮一点。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_max_tokens=64,
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("你今天心情怎么样？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "delta", "final"]
    assert events[0]["source"] == "local_preface"
    assert events[1]["text"] == "我也想把这点亮一点。"
    final = events[2]["payload"]
    assert final["response"] == events[0]["text"] + events[1]["text"]
    assert final["evidence"]["local_preface"] is True
    assert "model_skipped" not in final["evidence"]
    assert final["voice_turn"]["debug"]["decision_path"] == "state_mood"
    assert final["response"].startswith("心情")
    assert "已经先对用户说了" in captured["body"]["messages"][1]["content"]
    assert "不要把 mood/energy/stress 数值说出来" in captured["body"]["messages"][1]["content"]


def test_direct_aura_model_stream_dedupes_state_mood_preface_tail(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"跟你说话会放松一点。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update({"mood": 86, "energy": 42, "stress": 8, "trust": 72})
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_max_tokens=64,
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("你今天心情怎么样？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    final = events[1]["payload"]
    assert final["response"].count("跟你说话会放松一点") == 1
    assert final["response"] == events[0]["text"]
    assert final["voice_turn"]["debug"]["decision_path"] == "state_mood"


def test_direct_aura_model_stream_dedupes_repeated_final_sentence(tmp_path, monkeypatch):
    response = _dedupe_repeated_spoken_sentences(
        "心情还挺亮的，跟你说话会放松一点。跟你说话会放松一点。"
    )

    assert response == "心情还挺亮的，跟你说话会放松一点。"
    assert response.count("跟你说话会放松一点") == 1


def test_hermes_agent_stream_sends_state_mood_as_local_delta(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Hermes should be skipped for state mood local reply")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
        aura_model_mode="hermes_agent",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("你今天心情怎么样？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["source"] == "local_voice_reply"
    final = events[1]["payload"]
    assert events[0]["text"] == final["response"]
    assert final["evidence"]["model_skipped"] is True
    assert final["voice_turn"]["debug"]["decision_path"] == "state_mood"


def test_direct_aura_model_stream_uses_weather_advice_as_preface_then_model_followup(tmp_path, monkeypatch):
    captured = {}

    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"出门久的话再看一眼临近雨云。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeStreamResponse()

    def fake_user_weather(config, *, city, latitude="", longitude="", force=False):
        return config, {
            "enabled": True,
            "status": "fresh",
            "city": city,
            "temperature": "24.4",
            "condition": "多云",
            "weather_icon": 1,
            "humidity": "99",
            "updated_at": int(time.time()),
            "ttl_seconds": 3600,
            "age_seconds": 0,
            "has_content": True,
            "display": f"{city}，24.4度，多云，湿度99%",
            "source": "open_meteo",
        }

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    monkeypatch.setattr("integrations.aura_persona_gateway.turn.refresh_user_weather_if_needed", fake_user_weather)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "你为什么建议我带伞？",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "user_geo": {"city": "上海"}},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "delta", "final"]
    assert events[0]["source"] == "local_preface"
    assert "依据是" not in events[0]["text"]
    assert "上海，24.4度" not in events[0]["text"]
    assert events[1]["text"] == "出门久的话再看一眼临近雨云。"
    final = events[2]["payload"]
    assert final["evidence"]["local_preface"] is True
    assert final["voice_turn"]["debug"]["decision_path"] == "cached_weather_advice"
    assert final["response"] == events[0]["text"] + events[1]["text"]
    assert "不要再次播报温度、湿度、城市" in captured["body"]["messages"][1]["content"]


def test_direct_aura_model_stream_status_review_entry_uses_llm_directly(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"你想复盘最近状态，那我们就从最卡住你的地方开始。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "你想复盘最近状态，那我们就从最卡住你的地方开始。"
    final = events[-1]["payload"]
    assert final["response"] == "你想复盘最近状态，那我们就从最卡住你的地方开始。"
    assert "最卡住你的地方" in final["response"]
    assert "model_skipped" not in final["evidence"]
    assert final["evidence"]["local_preface"] is False
    assert final["voice_turn"]["debug"]["decision_path"] == "status_review_entry"
    assert final["voice_turn"]["debug"]["status_review"]["fallback_only"] is True
    assert "你说，我在听" not in final["response"]


def test_direct_aura_model_stream_open_chat_uses_llm_directly(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"先从你最想说的那件事开始。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    deltas = [item for item in events if item.get("type") == "delta"]
    assert deltas[0]["text"] == "先从你最想说的那件事开始。"
    assert "source" not in deltas[0]
    final = events[-1]["payload"]
    assert final["response"] == "先从你最想说的那件事开始。"
    assert final["evidence"]["local_preface"] is False
    assert "model_skipped" not in final["evidence"]
    assert final["voice_turn"]["debug"]["decision_path"] == "casual_chat_preface"


def test_direct_aura_model_stream_waits_out_split_low_value_openers(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"那"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"就聊先"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    deltas = [item["text"] for item in events if item.get("type") == "delta"]
    final = events[-1]["payload"]
    assert all(delta not in {"那", "就", "聊", "先", "聊先"} for delta in deltas)
    assert final["response"] in {
        "先说你最想聊的那一件。",
        "先从最挂心的地方说。",
        "先说最近最占心的那一块。",
    }
    assert final["evidence"]["local_preface"] is False
    assert "model_skipped" not in final["evidence"]
    assert "那就聊" not in final["response"]


def test_direct_aura_model_stream_blocks_vague_casual_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"那我就听着。你想从哪儿开始讲？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "final"]
    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] in {"blocked_vague_reply", "blocked_placeholder_reply"}
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "那我就听着" not in final["response"]
    assert "你想从哪儿开始讲" not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_placeholder_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"你想从哪儿开始讲都行。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_placeholder_reply"
    assert "model_skipped" not in final["evidence"]
    assert "从哪儿开始" not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_listening_placeholder(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"那我就在这儿听着。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    deltas = [item["text"] for item in events if item.get("type") == "delta"]
    final = events[-1]["payload"]
    assert "那我就在" not in "".join(deltas)
    assert final["response"] in {
        "先说你最想聊的那一件。",
        "先从最挂心的地方说。",
        "先说最近最占心的那一块。",
    }
    assert final["evidence"]["quality_guard"]["reason"] in {"blocked_vague_reply", "blocked_placeholder_reply"}
    assert "model_skipped" not in final["evidence"]


def test_direct_aura_model_stream_does_not_emit_open_chat_empty_companion_opening(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"那我就在这儿陪着你。"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"你想从哪儿"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    spoken_deltas = "".join(str(item.get("text") or "") for item in events if item.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["response"] in {
        "先说你最想聊的那一件。",
        "先从最挂心的地方说。",
        "先说最近最占心的那一块。",
    }
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_placeholder_reply"
    for token in ("陪着你", "你想从哪"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_waits_out_open_chat_filler_before_first_delta(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"好哒，"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"嘛。"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"先说你最想聊的那一件。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    deltas = [str(item.get("text") or "") for item in events if item.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas
    assert not deltas[0].startswith(("好哒", "嘛"))
    assert final["response"] == "先说你最想聊的那一件。"


def test_direct_aura_model_stream_blocks_status_topic_echo(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态啊？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "最近状态啊" not in final["response"]


def test_direct_aura_model_stream_status_fallback_avoids_recent_repeat(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "最近状态啊？",
        prior_aura_messages=("从工作节奏说起：是事情太满，还是提不起劲？",),
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert final["response"] == "先把状态拆小一点：工作量、睡眠，还是提不起劲？"
    assert final["response"] not in final["debug"]["context"]["recent_aura_replies"]


def test_direct_aura_model_stream_blocks_status_echo_even_with_anchored_tail(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态啊？"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"从工作节奏说起：是事情太满，还是提不起劲？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    deltas = [item["text"] for item in events if item.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "removed_low_value_status_opening"


def test_direct_aura_model_stream_blocks_weak_status_ack_even_with_anchor(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近确实得理理。"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"从工作节奏说起：是事情太满，还是提不起劲？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "removed_low_value_status_opening"


def test_direct_aura_model_stream_blocks_weak_status_ack_without_anchor(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近确实得理理。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_direct_aura_model_stream_rechecks_tail_after_stripping_status_echo(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态啊？我看你刚才盯着屏幕半天没动，是觉得事情"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    spoken_deltas = "".join(str(item.get("text") or "") for item in events if item.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    # 80 字口径下截断尾巴在句边界就被丢掉，剩下的“最近状态啊？”按空泛回复拦截。
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    for token in ("盯着屏幕", "半天没动", "是觉得事情"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_hides_provider_error_from_spoken_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            raise HTTPError(
                url="https://api.stepfun.com/step_plan/v1/chat/completions",
                code=400,
                msg="Bad Request",
                hdrs=None,
                fp=None,
            )

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["status"] == "failed"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_provider_error_reply"
    assert "HTTP" not in final["response"]


def test_direct_aura_model_stream_allows_status_reply_with_concrete_axis(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态先看睡眠还是工作？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "最近状态先看睡眠还是工作？"
    assert "quality_guard" not in final["evidence"]


def test_direct_aura_model_stream_blocks_aura_self_state_for_user_status(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态啊，其实我这几天一直盯着后台看呢，没敢睡死。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert "盯着后台" not in final["response"]
    assert "没敢睡" not in final["response"]


def test_direct_aura_model_stream_blocks_unfounded_user_state_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"其实看你这两天的状态，感觉你整个人绷得太紧了，是那种典型的隐形加班模式。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert "绷得太紧" not in final["response"]
    assert "隐形加班" not in final["response"]


def test_direct_aura_model_stream_supportive_chat_holds_low_value_and_incomplete_tail(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_chunks(
        tmp_path,
        monkeypatch,
        (
            "嗯，晓得。",
            "这种时候确实不想听大道理。",
            "是那种活儿干",
        ),
        user_text="我最近加班有点烦，想聊聊。",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]

    assert "晓得" not in spoken_deltas
    assert "活儿干" not in spoken_deltas
    assert "活儿干" not in final["response"]
    assert not final["response"].endswith("干")
    assert final["response"] == "加班这件事先别自己憋着，是累还是烦哪一点更多？"
    assert final["evidence"]["quality_guard"]["forced_terminal_fallback"] is True


def test_direct_aura_model_stream_blocks_unfounded_recent_work_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"其实你这几天一直在跟我念叨那些琐碎的事儿，听得出来，你心里那个结还没解开。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "念叨" not in final["response"]
    assert "没解开" not in final["response"]


def test_direct_aura_model_stream_blocks_unfounded_specific_work_history(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态确实该理理了。我看你上礼拜连着三天都在搞那个新项目，估计是累得够呛吧？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "上礼拜" not in final["response"]
    assert "三天" not in final["response"]
    assert "新项目" not in final["response"]


def test_direct_aura_model_stream_blocks_cutoff_work_rhythm_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"我也觉得这种节奏有点磨人。是事情堆得太满了让你觉得累，还是单纯提不起劲儿搞那些"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我想聊聊最近的工作节奏，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    # 新口径：被切断的尾句在句边界整句丢弃，留下的完整第一句可以直接说，不再触发守卫。
    assert final["response"] == "我也觉得这种节奏有点磨人。"
    assert "搞那些" not in final["response"]
    assert "堆得太满" not in final["response"]


def test_direct_aura_model_stream_blocks_unfounded_stress_and_project_guess(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近看你确实挺紧绷的，是觉得手头那几个项目压得太死，还是单纯觉得效率上不去？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "紧绷" not in final["response"]
    assert "几个项目" not in final["response"]
    assert "效率上不去" not in final["response"]


def test_direct_aura_model_stream_blocks_status_metaphor_guess(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "最近状态嘛，感觉是‘电量还剩一半，但不知道往哪儿充’？",
    )

    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "电量" not in final["response"]
    assert "充" not in final["response"]


def test_direct_aura_model_stream_blocks_unfounded_status_choice(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "最近状态嘛，是觉得事情太多有点乱，还是单纯想找个地方歇歇脚？",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "事情太多" not in final["response"]
    assert "歇歇脚" not in final["response"]


def test_direct_aura_model_stream_blocks_job_change_slang_guess(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "想跳槽啦？是觉得现在的坑待得憋屈，还是看到更好的机会手痒了？",
        user_text="最近想换工作，能聊聊吗？",
    )

    final = events[-1]["payload"]
    assert events[0]["source"] == "local_preface"
    assert final["evidence"]["stop_reason"] == "local_preface_unsafe_continuation"
    assert final["response"] == events[0]["text"]
    assert "坑" not in final["response"]
    assert "憋屈" not in final["response"]
    assert "手痒" not in final["response"]


def test_direct_aura_model_stream_blocks_job_change_unfounded_disappointment_claim(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "既然动了心思，那肯定是攒够了失望。是觉得现在的平台已经没法让你再往上爬了。",
        user_text="最近想换工作，能聊聊吗？",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]
    assert events[0]["source"] == "local_preface"
    assert final["evidence"]["stop_reason"] == "local_preface_unsafe_continuation"
    assert final["response"] == events[0]["text"]
    for token in ("攒够", "失望", "往上爬", "平台已经没法"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_job_change_patronizing_first_sentence(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "想换就换嘛，又不是小孩子过家家。是觉得现在的",
        user_text="最近想换工作，能聊聊吗？",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]
    assert events[0]["source"] == "local_preface"
    assert final["response"] == events[0]["text"]
    for token in ("想换就换", "过家家"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_incomplete_status_axis(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(tmp_path, monkeypatch, "是觉得生活节奏")

    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == []
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_direct_aura_model_stream_blocks_status_reply_without_review_anchor(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(tmp_path, monkeypatch, "周六凌晨三点。")

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "凌晨三点" not in final["response"]


def test_direct_aura_model_stream_blocks_unsolicited_time_and_state_guess(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "周六凌晨四点半就开始复盘工作节奏，看来是真闲不住。",
        user_text="我想聊聊最近的工作节奏，你自然回应一句。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "凌晨四点半" not in final["response"]
    assert "闲不住" not in final["response"]


def test_direct_aura_model_stream_blocks_malformed_status_punctuation(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(tmp_path, monkeypatch, "最近是事情太满，？")

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "，？" not in final["response"]


def test_direct_aura_model_stream_allows_status_terms_user_already_said(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "生活节奏有点乱的话，先看睡眠还是工作？",
        user_text="我最近状态生活节奏有点乱，想充电。",
    )

    final = events[-1]["payload"]
    assert final["response"] == "生活节奏有点乱的话，先看睡眠还是工作？"
    assert "quality_guard" not in final["evidence"]


def test_direct_aura_model_stream_blocks_first_person_self_state_even_if_user_said_it(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "我最近状态也不太好，刚好陪你聊两句。",
        user_text="我最近状态不太好，想聊聊。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert "我最近状态也不太好" not in final["response"]


def test_direct_aura_model_stream_blocks_contextual_self_state_with_first_person_anchor(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "巧了，我也还没睡，正好陪你聊聊。",
        user_text="我还没睡，睡不着。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert "我也还没睡" not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_aura_wakeup_state(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "反正我也刚醒，这会儿脑子最清醒。",
        user_text="我想聊聊。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert final["response"] == "先从最挂心的地方说。"
    assert "刚醒" not in final["response"]
    assert "清醒" not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_low_value_say_it_reply(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "既然你想聊，那就说呗。是想吐槽最近那些糟心事儿，还是单纯想找个人说说话解解闷？",
        user_text="我想聊聊。",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_placeholder_reply"
    assert final["response"] in {
        "先说你最想聊的那一件。",
        "先从最挂心的地方说。",
        "先说最近最占心的那一块。",
    }
    for token in ("说呗", "糟心事", "解闷"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_unfounded_morning_guess(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "不过这一大早的，你是突然想通了，还是又钻牛角尖里去了？",
        user_text="我想聊聊。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] in {
        "blocked_aura_self_state_hallucination",
        "blocked_unfounded_casual_guess",
    }
    assert final["response"] == "先从最挂心的地方说。"
    assert "一大早" not in final["response"]
    assert "钻牛角尖" not in final["response"]


def test_direct_aura_model_stream_blocks_open_chat_unsolicited_time_opening(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "不过先说好，今晚这时间点儿，你是想聊点正经的，还是单纯想找人说说话？",
        user_text="我想聊聊。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_aura_self_state_hallucination"
    assert final["response"] == "先从最挂心的地方说。"
    assert "时间点" not in final["response"]


def test_direct_aura_model_stream_blocks_incomplete_open_chat_clause(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "不过，",
        user_text="我想聊聊。",
    )

    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == []
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] == "先从最挂心的地方说。"


def test_direct_aura_model_stream_open_chat_fallback_avoids_recent_repeat(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "不过，",
        user_text="我想聊聊。",
        prior_aura_messages=("先从最挂心的地方说。",),
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] in {
        "先说你最想聊的那一件。",
        "先说最近最占心的那一块。",
    }
    assert final["response"] not in final["debug"]["context"]["recent_aura_replies"]


def test_direct_aura_model_stream_blocks_transition_and_unfounded_open_source_guess(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"行呀，那咱们就直奔主题。其实最近看你折腾那些开源项目，感觉你有点累。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "行呀" not in final["response"]
    assert "开源项目" not in final["response"]


def test_streaming_voice_completion_waits_for_incomplete_phrase():
    assert _streaming_voice_model_text_is_complete("最近状态啊... 其实我", raw_text="最近状态啊... 其实我") is False


def test_streaming_voice_compact_prefers_complete_sentence_over_comma_chop():
    # 用户实测“说话说一半就没了”的根因：第二句超限时被逗号腰斩再补假句号。
    # 现在超过 80 字上限时宁可整句丢掉第二句，只保完整第一句。
    s1 = "喔唷，大早上的就开始搞技术调研啦？"
    s2_long = "其实最卡的地方多半就在网络跳转或者云端处理那一块儿，尤其是回答一长就更明显，你可以先把问题拆小一点，一次只问一件事，看看会不会顺一些。"
    assert _compact_streaming_voice_model_text(s1 + s2_long) == s1


def test_streaming_voice_compact_short_two_sentences_kept_whole():
    text = "遛狗记得带水。要是嫌公园太挤，就沿江边走走。"
    assert _compact_streaming_voice_model_text(text) == text


def test_enumeration_request_detection():
    # 用户实测“推荐三个遛狗的地方”被两句/80字预算压没了，只剩铺垫句。
    assert _is_enumeration_request("推荐三个遛狗的好地方。") is True
    assert _is_enumeration_request("但我是让你推荐三个地方呀。") is True
    assert _is_enumeration_request("有哪些遛狗的好去处？") is True
    assert _is_enumeration_request("北京有什么好玩的地方推荐？") is True
    assert _is_enumeration_request("介绍两家好吃的店") is True
    assert _is_enumeration_request("现在几点了？") is False
    assert _is_enumeration_request("今天天气怎么样") is False
    assert _is_enumeration_request("我想复盘最近的状态") is False


def test_voice_reply_budget_expands_for_enumeration():
    assert _voice_reply_budget("推荐三个遛狗的好地方。") == (4, 160)
    assert _voice_reply_budget("今天天气怎么样") == (2, 80)


def test_detail_request_detection():
    # 用户反馈内容型问题（详细讲解/原因/步骤）被两句/80字预算压缩掉信息。
    assert _is_detail_request("详细讲讲年假制度") is True
    assert _is_detail_request("展开说说") is True
    assert _is_detail_request("具体介绍一下这个功能") is True
    assert _is_detail_request("为什么天会下雨") is True
    assert _is_detail_request("这个是怎么做到的") is True
    # 裸“怎么/如何”不算内容型：闲聊问句必须保持短预算。
    assert _is_detail_request("今天天气怎么样") is False
    assert _is_detail_request("现在几点了？") is False
    assert _is_detail_request("我想复盘最近的状态") is False


def test_voice_reply_budget_expands_for_detail():
    assert _voice_reply_budget("详细讲讲为什么冬天要给狗穿衣服") == (6, 320)
    # detail 和 enumeration 同时命中时 detail 胜出。
    assert _voice_reply_budget("详细介绍三个遛狗的好地方") == (6, 320)


def test_streaming_voice_compact_detail_budget_keeps_long_answer():
    text = (
        "冬天给狗穿衣服主要是保温。小型犬和短毛犬皮下脂肪薄，体表散热快，气温一低体温就跟着往下掉。"
        "老年犬和幼犬的体温调节能力也弱，出门吹风容易着凉拉肚子。"
        "衣服还能挡住雪水和融雪剂，回家不用整只擦一遍。"
        "不过双层毛的大型犬自带御寒能力，硬给它套衣服反而会捂出皮肤问题。"
        "所以要不要穿，先看体型、毛量和年龄，再看当天气温。"
    )
    # 内容型预算 (6, 320) 能完整保留讲解；默认预算会把后面几句裁掉。
    assert _compact_streaming_voice_model_text(text, max_sentences=6, limit=320) == text
    assert len(_compact_streaming_voice_model_text(text)) < len(text)


def test_streaming_voice_compact_enumeration_budget_keeps_list():
    text = "朝阳公园肯定算一个，草坪够大。奥森的南园也合适，跑道旁边有遛狗区。再就是通惠河边，人少还不用牵太紧。"
    # 默认预算只留得下前两句；列举预算能把三个地方都留住。
    assert _compact_streaming_voice_model_text(text, max_sentences=4, limit=160) == text
    assert len(_compact_streaming_voice_model_text(text)) < len(text)


def test_voice_stream_max_tokens_expands_for_enumeration():
    import types

    config = types.SimpleNamespace(aura_model_max_tokens=96, aura_model_model="stepaudio-2.5-chat")
    assert _voice_stream_max_tokens(config) == 96
    assert _voice_stream_max_tokens(config, enumeration=True) == 192
    assert _voice_stream_max_tokens(config, detail=True) == 384
    generous = types.SimpleNamespace(aura_model_max_tokens=512, aura_model_model="stepaudio-2.5-chat")
    assert _voice_stream_max_tokens(generous, detail=True) == 512


def test_voice_stream_max_tokens_reserves_visible_output_for_stepfun_reasoning_models():
    import types

    for model in ("step-3.7-flash", "step-3.5-flash", "step-3.5-flash-2603", "step-router-v1"):
        config = types.SimpleNamespace(aura_model_max_tokens=96, aura_model_model=model)
        assert _voice_stream_max_tokens(config) == 384

    generous = types.SimpleNamespace(aura_model_max_tokens=512, aura_model_model="step-3.7-flash")
    assert _voice_stream_max_tokens(generous) == 512


def test_streaming_voice_preparation_strips_low_value_chat_openers():
    assert _prepare_streaming_voice_model_text("好哒，我也觉得该歇会儿了。") == "我也觉得该歇会儿了。"
    assert _prepare_streaming_voice_model_text("好哒我就在这儿听着呢。") == "我就在这儿听着呢。"
    assert _prepare_streaming_voice_model_text("好哒咱们就从今天说起。") == "咱们就从今天说起。"
    assert _prepare_streaming_voice_model_text("是滴，先看工作节奏。") == "先看工作节奏。"
    assert _prepare_streaming_voice_model_text("那就聊。是想听听我的想法？") == "是想听听我的想法？"
    assert _prepare_streaming_voice_model_text("那就聊嘛。不过先说正事。") == "不过先说正事。"
    assert _prepare_streaming_voice_model_text("嘛。先说正事。") == "先说正事。"
    assert _prepare_streaming_voice_model_text("好哒。") == "好哒。"
    assert _prepare_streaming_voice_model_text("是滴，") == "是滴，"


def test_direct_aura_model_stream_blocks_incomplete_final_phrase(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态啊... 其实我"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "其实我" not in final["response"]


def test_direct_aura_model_stream_allows_anchored_casual_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"从工作节奏说起：是事情太满，还是提不起劲？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "quality_guard" not in final["evidence"]


def test_direct_aura_model_stream_falls_back_on_unfounded_casual_guess(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近是不是又熬大夜赶项目了？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天想聊聊最近状态，你自然回应一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "final"]
    final = events[1]["payload"]
    assert final["response"] == events[0]["text"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] in {
        "blocked_unfounded_casual_guess",
        "blocked_unfounded_user_state_claim",
    }
    assert "model_skipped" not in final["evidence"]
    assert "熬大夜" not in final["response"]


def test_direct_aura_model_stream_blocks_status_review_unfounded_slow_reply_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"是滴，最近看你消息回得慢，是进度压得太死，还是手头的事儿太杂？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    spoken_deltas = "".join(str(item.get("text") or "") for item in events if item.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    for token in ("消息回得慢", "进度压得太死", "手头的事儿太杂"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_status_review_unfounded_recent_pace_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近节奏蛮快的，感觉怎么样？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert "最近节奏" not in final["response"]


def test_direct_aura_model_stream_blocks_status_review_unfounded_messy_work_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近事情是不是挺杂的？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert "事情是不是挺杂" not in final["response"]


def test_direct_aura_model_stream_blocks_status_review_unfounded_hard_work_claim(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "正好我也觉得你这段时间挺拼的。是手头活儿太多压得慌，还是纯粹想找人吐吐槽？",
        user_text="我想聊聊最近的工作节奏，你自然回应一句。",
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert "挺拼" not in final["response"]
    assert "压得慌" not in final["response"]


def test_direct_aura_model_stream_blocks_incomplete_i_also_think_reply(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "刚好我也觉得",
        user_text="我想聊聊最近的工作节奏，你自然回应一句。",
    )

    final = events[-1]["payload"]
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_incomplete_streaming_reply"
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert "刚好我也觉得" not in final["response"]


def test_direct_aura_model_stream_repairs_quality_guard_after_partial_status_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"我也觉得该理理了。"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"是感觉事情堆得太"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream(
        "我想聊聊最近的工作节奏，你自然回应一句。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
    ))

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    # 半句尾巴在句边界被丢掉后，剩下的“我也觉得该理理了。”按空泛回复拦截。
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert final["evidence"]["quality_guard"]["final_response_repaired"] is True
    assert "堆得太" not in final["response"]


def test_direct_aura_model_stream_holds_incomplete_status_fragments(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近工作"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"节奏"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"啊"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"？"}}]}\n\n'.encode("utf-8"),
                'data: {"choices":[{"delta":{"content":"是"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream(
        "我想聊聊最近的工作节奏，你自然回应一句。",
        metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
    ))

    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == []
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert final["evidence"]["quality_guard"]["final_response_repaired"] is True


def test_direct_aura_model_stream_blocks_stripped_incomplete_status_tail(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "最近工作节奏啊？是觉得事情",
        user_text="我想聊聊最近的工作节奏，你自然回应一句。",
    )

    deltas = [event["text"] for event in events if event.get("type") == "delta"]
    final = events[-1]["payload"]
    assert deltas == []
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_vague_reply"
    assert "是觉得事情" not in final["response"]


def test_direct_aura_model_stream_blocks_status_review_unfounded_drift_claim(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"最近状态嘛，是觉得整个人都在飘，还是想找点重心？"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )

    final = events[-1]["payload"]
    assert final["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert "整个人都在飘" not in final["response"]
    assert "找点重心" not in final["response"]


def test_casual_unfounded_guess_filter_allows_open_followup():
    assert _casual_continuation_is_unfounded_guess("最近是不是又熬大夜了？") is True
    assert _casual_continuation_is_unfounded_guess("是不是又忙到没停下来了？") is True
    assert _casual_continuation_is_unfounded_guess("你想从哪儿开始聊？") is False


def test_direct_aura_model_stream_uses_supportive_llm_content(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"听起来你今天真的有点累，我们先把话说慢一点。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("我今天有点累，你陪我聊两句。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    final = events[1]["payload"]
    assert final["voice_turn"]["debug"]["decision_path"] == "supportive_chat"
    assert final["voice_turn"]["debug"]["supportive_chat"]["fallback_only"] is True
    assert final["response"] == "听起来你今天真的有点累，我们先把话说慢一点。"
    assert "model_skipped" not in final["evidence"]
    assert "睡吧" not in final["response"]
    assert "放歌" not in final["response"]


def test_direct_aura_model_stream_blocks_supportive_unfounded_overtime_guess(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "这种时候确实容易让人抓狂。是那种活儿干不完的烦，还是纯粹因为熬夜熬得整个人都不好了？",
        user_text="我最近加班有点烦，想聊聊。",
    )

    final = events[-1]["payload"]
    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    assert final["voice_turn"]["debug"]["decision_path"] == "supportive_chat"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] in {
        "加班这件事先别自己憋着，是累还是烦哪一点更多？",
        "我在，先把话说慢一点。",
        "好，我陪你。先说最难受的那一处。",
        "先缓一口气，我听你说。",
    }
    for token in ("抓狂", "活儿干不完", "熬夜", "整个人都不好"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_supportive_unfounded_consumption_claim(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "加班确实搞人心态，尤其是那种没意义的消耗。是活儿实在太多压不过来了，还是纯粹因为环境让人憋屈？",
        user_text="我最近加班有点烦，想聊聊。",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["voice_turn"]["debug"]["decision_path"] == "supportive_chat"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "加班这件事先别自己憋着，是累还是烦哪一点更多？"
    for token in ("没意义的消耗", "压不过来", "环境让人憋屈"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_blocks_supportive_unfounded_grind_claim(tmp_path, monkeypatch):
    events = _stream_direct_aura_model_text(
        tmp_path,
        monkeypatch,
        "加班确实蛮搞心态的。是那种活儿多到理不清，还是纯粹觉得没必要这么熬？",
        user_text="我最近加班有点烦，想聊聊。",
    )

    spoken_deltas = "".join(str(event.get("text") or "") for event in events if event.get("type") == "delta")
    final = events[-1]["payload"]
    assert final["voice_turn"]["debug"]["decision_path"] == "supportive_chat"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unfounded_user_state_claim"
    assert final["response"] == "加班这件事先别自己憋着，是累还是烦哪一点更多？"
    for token in ("搞心态", "活儿多到理不清", "没必要这么熬"):
        assert token not in spoken_deltas
        assert token not in final["response"]


def test_direct_aura_model_stream_falls_back_on_unsafe_supportive_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"你闭上眼睡吧，我给你放首老歌。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("我今天有点累，你陪我聊两句。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "好，我陪你。你慢慢说。"
    final = events[1]["payload"]
    assert final["response"] == "好，我陪你。你慢慢说。"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unsafe_supportive_reply"
    assert "model_skipped" not in final["evidence"]
    assert "闭上眼" not in final["response"]
    assert "老歌" not in final["response"]


def test_direct_aura_model_stream_falls_back_on_unsafe_supportive_colloquial_reply(tmp_path, monkeypatch):
    class FakeStreamResponse:
        def __enter__(self):
            return iter([
                'data: {"choices":[{"delta":{"content":"晓得哒，那些有的没的先不聊了。"}}]}\n\n'.encode("utf-8"),
                b"data: [DONE]\n\n",
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req: Request, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("我今天有点累，你陪我聊两句。", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["text"] == "好，我陪你。你慢慢说。"
    final = events[1]["payload"]
    assert final["response"] == "好，我陪你。你慢慢说。"
    assert final["evidence"]["quality_guard"]["reason"] == "blocked_unsafe_supportive_reply"
    assert "model_skipped" not in final["evidence"]
    assert "晓得哒" not in final["response"]
    assert "有的没的" not in final["response"]


def test_direct_aura_model_stream_sends_time_as_local_delta(tmp_path, monkeypatch):
    def fake_urlopen(req: Request, timeout):
        raise AssertionError("Local current-time answers should not call direct LLM")

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "现在几点？",
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234, "user_geo": {"city": "上海", "timezone": "Asia/Shanghai"}},
        )
    )

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["source"] == "local_voice_reply"
    final = events[1]["payload"]
    assert events[0]["text"] == final["response"]
    assert final["evidence"]["model_skipped"] is True
    assert final["voice_turn"]["debug"]["decision_path"] == "current_time"


def test_direct_stream_sends_grounded_current_as_local_delta(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise AssertionError("Grounded current local reply should not call Hermes")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["trust"] = 96
    state["affinity_xp"] = 280
    state["metadata"] = {
        "current_activity": "整理东西",
        "current_location": "desk",
        "location_label": "书桌边",
        "world_current_source": "manual",
        "world_manual_override": True,
        "privacy_sensitivity": 20,
    }
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
    runtime = AuraRuntimeConfig(persona_home=config.persona_home, aura_model_mode="hermes_agent")
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(gateway.run_direct_turn_stream("你现在在干嘛？", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}))

    assert [item.get("type") for item in events] == ["delta", "final"]
    assert events[0]["source"] == "local_voice_reply"
    final = events[1]["payload"]
    assert events[0]["text"] == "我在整理东西。"
    assert events[0]["text"] == final["response"]
    assert final["evidence"]["model_skipped"] is True
    assert final["voice_turn"]["debug"]["decision_path"] == "grounded_current_activity"


def test_direct_aura_model_omits_reasoning_effort_when_disabled(tmp_path, monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"No reasoning field"}}]}'

    def fake_urlopen(req: Request, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    result = gateway.run_turn("测试一下")

    assert result.ok is True
    assert result.response == "No reasoning field"
    assert "reasoning_effort" not in captured["body"]


def test_persona_store_does_not_create_cultural_chunk_tables(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)

    store.get_or_create_state(config.scope)

    with sqlite3.connect(config.companion_db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "companion_cultural_chunk" not in tables


def test_persona_store_retries_transient_sqlite_io_error(tmp_path, monkeypatch):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    calls = {"select": 0}
    original_select = LilyPersonaStore._select_state

    def flaky_select(conn, scope):
        calls["select"] += 1
        if calls["select"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return original_select(conn, scope)

    monkeypatch.setattr(LilyPersonaStore, "_select_state", staticmethod(flaky_select))

    state = store.get_or_create_state(config.scope)

    assert state["mood"] == 80
    assert calls["select"] >= 2


def test_http_persona_config_requires_admin_login(tmp_path, monkeypatch):
    config = _config(tmp_path, enabled=False)
    monkeypatch.setenv("AURA_PERSONA_HOME", config.persona_home)
    monkeypatch.setenv("AURA_COMPANION_HOME", config.companion_home)
    monkeypatch.setenv("AURA_LILY_ADMIN_PASSWORD", "unit-pass")

    handler = make_handler(build_config(parse_args([])))

    import threading
    from http.server import ThreadingHTTPServer
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            urlopen(f"{base}/persona/config", timeout=3)
        except HTTPError as exc:
            assert exc.code == 401
        else:  # pragma: no cover
            raise AssertionError("expected 401")

        req = Request(
            f"{base}/persona/config",
            data=json.dumps({
                "enabled": True,
                "user_location_mode": "manual",
                "user_home_city": "上海",
                "user_timezone": "Asia/Shanghai",
                "user_latitude": "31.2304",
                "user_longitude": "121.4737",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        payload = json.loads(urlopen(req, timeout=3).read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["config"]["enabled"] is True
        assert payload["config"]["user_location_mode"] == "manual"
        assert payload["config"]["user_home_city"] == "上海"
        assert payload["config"]["user_timezone"] == "Asia/Shanghai"
        assert payload["config"]["user_latitude"] == "31.2304"
        assert payload["config"]["user_longitude"] == "121.4737"
    finally:
        server.shutdown()
        thread.join(timeout=3)
