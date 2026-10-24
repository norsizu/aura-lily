from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.state_rules import (
    apply_agent_reply_delta,
    apply_time_recovery,
    apply_user_interaction_delta,
)
from integrations.aura_persona_gateway.store import LilyPersonaStore, _life_event_from_row
from integrations.aura_persona_gateway.world import (
    DRIVE_BLEND_MIN_CHANGE,
    _settle_completed_world_state,
    _settle_temporary_care,
    _update_drives,
    build_world_snapshot,
    generate_day_plan,
)
from integrations.aura_persona_gateway.world_ledger import (
    HIDDEN_DRIVE_FIELDS,
    LEDGER_EVENT_PREFIX,
    apply_world_change,
    read_drive_baseline,
    replay_ledger,
    snapshot_state,
    state_delta,
)
from integrations.aura_persona_gateway.world_assets import default_world_canon


def _config(tmp_path: Path, *, aura_home_city: str = "上海") -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True)
    return PersonaGatewayConfig(
        enabled=True,
        persona_home=str(persona_home),
        companion_home=str(companion_home),
        hermes_home=str(tmp_path / "hermes-home"),
        aura_home_city=aura_home_city,
    )


def _seed_state(store: LilyPersonaStore, config: PersonaGatewayConfig, **overrides) -> dict:
    state = store.get_or_create_state(config.scope)
    state.update({"satiety": 30, "energy": 40, "mood": 60, "beans": 50, "trust": 50, "stress": 10})
    state["metadata"] = {"social_need": 70, "curiosity": 40, "privacy_sensitivity": 30, "resource_comfort": 30}
    state.update(overrides)
    store.save_state(config.scope, state)
    return state


def _all_ledger_events(db_path, scope) -> list[dict]:
    """全量读账本（store 的 list_recent_life_events 最多只给 20 条）。"""
    import sqlite3

    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, event_type, event_key, title, description, location, activity,
                   visibility, intensity, delta_json, payload_json, created_at
              FROM companion_life_event
             WHERE platform=? AND chat_id=? AND user_id=? AND event_type LIKE ?
             ORDER BY created_at ASC, id ASC
            """,
            (*scope.as_tuple(), f"{LEDGER_EVENT_PREFIX}%"),
        ).fetchall()
    finally:
        conn.close()
    return [_life_event_from_row(row) for row in rows]


def _plan_rows(plan: list[dict]) -> list[tuple]:
    return [
        (
            str(item.get("slot_key") or ""),
            float(item.get("scheduled_at") or 0),
            str(item.get("title") or ""),
            str(item.get("location") or ""),
            str(item.get("activity_type") or ""),
            str(item.get("status") or ""),
            dict(item.get("expected_delta") or {}),
            # why 是 W2 新加的说明字段，比较时剥掉，其余 payload 必须一字不差。
            {
                key: value
                for key, value in (item.get("payload") if isinstance(item.get("payload"), dict) else {}).items()
                if key != "why"
            },
        )
        for item in plan
    ]

# 下面三组输入是“改动前”跑出来的期望值（W2 要求：同种子同输入必须生成完全相同的
# slot_key / 时间 / 地点 / 标题 / status / payload）。当时用旧版 generate_day_plan
# dump 后原样写死在这里，唯一允许的新差异是 payload 里多出来的 why 字段。
GOLDEN_DAY_PLANS = [
    {
        "input": dict(
            city="上海",
            day_key="2026-08-04",
            state={"energy": 70, "metadata": {"curiosity": 40, "social_need": 40}, "mood": 80, "satiety": 70},
            weather={"category": "mild", "signature": "2026-08-04:mild"},
            now=1785500000.0,
        ),
        "plan": [
            (
                "wake",
                1785800160.0,
                "慢慢起床",
                "卧室",
                "morning",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "刚起床", "city": "上海", "duration_minutes": 45, "life_axis": "home", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "breakfast",
                1785803160.0,
                "把早饭解决掉",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃早饭", "city": "上海", "duration_minutes": 35, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "morning_focus",
                1785807900.0,
                "上午安静处理点事情",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "看点内容", "city": "上海", "duration_minutes": 94, "life_axis": "quiet", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "lunch",
                1785818040.0,
                "午饭时间",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃午饭", "city": "上海", "duration_minutes": 55, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "curiosity_project",
                1785824760.0,
                "研究一点感兴趣的东西",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "满足一点好奇心", "city": "上海", "duration_minutes": 64, "life_axis": "curiosity", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "afternoon",
                1785830340.0,
                "沿着河岸吹会儿风",
                "河岸步道",
                "walk",
                "pending",
                {"energy": -7, "mood": 4, "stress": -3},
                {"activity_label": "在河边散步", "city": "上海", "duration_minutes": 87, "life_axis": "riverside_walk", "location_key": "outside.riverside", "location_label": "河岸步道", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "evening",
                1785837240.0,
                "回到家里缓一会儿",
                "客厅",
                "home",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "休息", "city": "上海", "duration_minutes": 75, "life_axis": "home", "location_key": "home.living_room", "location_label": "客厅", "requires_prior_outing": True, "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "dinner",
                1785841740.0,
                "晚饭",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃晚饭", "city": "上海", "duration_minutes": 45, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "personal_window",
                1785847380.0,
                "安静收拾一下心情",
                "客厅",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "听一会儿音乐", "city": "上海", "duration_minutes": 25, "life_axis": "personal", "location_key": "home.living_room", "location_label": "客厅", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
            (
                "night_settle",
                1785853860.0,
                "准备休息",
                "卧室",
                "rest",
                "pending",
                {"energy": 10, "stress": -5},
                {"activity_label": "睡前整理", "city": "上海", "duration_minutes": 80, "life_axis": "rest", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-04:mild", "world_schema": "aura_world_v2"},
            ),
        ],
    },
    {
        "input": dict(
            city="上海",
            day_key="2026-08-05",
            state={"energy": 30, "metadata": {"curiosity": 70, "social_need": 80}, "mood": 40, "satiety": 20},
            weather={"category": "rain", "signature": "2026-08-05:rain"},
            now=1785600000.0,
        ),
        "plan": [
            (
                "wake",
                1785885120.0,
                "慢慢起床",
                "卧室",
                "morning",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "刚起床", "city": "上海", "duration_minutes": 45, "life_axis": "home", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "breakfast",
                1785888540.0,
                "简单吃早饭",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃早饭", "city": "上海", "duration_minutes": 35, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "morning_focus",
                1785893880.0,
                "把上午留给安静的事",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "整理东西", "city": "上海", "duration_minutes": 129, "life_axis": "quiet", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "lunch",
                1785902580.0,
                "找点东西吃",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃午饭", "city": "上海", "duration_minutes": 55, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "curiosity_project",
                1785910620.0,
                "研究一点感兴趣的东西",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "满足一点好奇心", "city": "上海", "duration_minutes": 54, "life_axis": "curiosity", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "afternoon",
                1785912120.0,
                "下午先留在家里，等天气稳一点",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "在屋里处理安静的事", "city": "上海", "duration_minutes": 97, "life_axis": "weather_shelter", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "snack",
                1785918480.0,
                "补充一点能量",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 30, "stress": -2},
                {"activity_label": "吃点零食", "city": "上海", "duration_minutes": 20, "life_axis": "snack", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "recharge",
                1785919320.0,
                "找个时间缓一缓",
                "客厅",
                "rest",
                "pending",
                {"energy": 10, "stress": -5},
                {"activity_label": "短暂休息", "city": "上海", "duration_minutes": 45, "life_axis": "recovery", "location_key": "home.living_room", "location_label": "客厅", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "evening",
                1785924120.0,
                "回到家里缓一会儿",
                "客厅",
                "home",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "休息", "city": "上海", "duration_minutes": 75, "life_axis": "home", "location_key": "home.living_room", "location_label": "客厅", "requires_prior_outing": True, "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "dinner",
                1785927900.0,
                "吃晚饭",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃晚饭", "city": "上海", "duration_minutes": 45, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "personal_window",
                1785935880.0,
                "睡前做点喜欢的事",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "看点喜欢的内容", "city": "上海", "duration_minutes": 35, "life_axis": "personal", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
            (
                "night_settle",
                1785938700.0,
                "晚上慢慢收尾",
                "卧室",
                "rest",
                "pending",
                {"energy": 10, "stress": -5},
                {"activity_label": "睡前整理", "city": "上海", "duration_minutes": 80, "life_axis": "rest", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-05:rain", "world_schema": "aura_world_v2"},
            ),
        ],
    },
    {
        "input": dict(
            city="北京",
            day_key="2026-08-06",
            state={"energy": 95, "metadata": {"curiosity": 30, "social_need": 20}, "mood": 88, "satiety": 90},
            weather={"category": "clear", "signature": "2026-08-06:clear"},
            now=1785700000.0,
        ),
        "plan": [
            (
                "wake",
                1785973440.0,
                "醒来整理一下",
                "卧室",
                "morning",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "刚起床", "city": "北京", "duration_minutes": 45, "life_axis": "home", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "breakfast",
                1785974640.0,
                "简单吃早饭",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃早饭", "city": "北京", "duration_minutes": 35, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "morning_focus",
                1785982320.0,
                "把上午留给安静的事",
                "书房",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "安静待着", "city": "北京", "duration_minutes": 123, "life_axis": "quiet", "location_key": "home.study", "location_label": "书房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "lunch",
                1785988680.0,
                "找点东西吃",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃午饭", "city": "北京", "duration_minutes": 55, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "afternoon",
                1785997800.0,
                "找个安静地方待会儿",
                "附近咖啡店",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "安静停留", "city": "北京", "duration_minutes": 86, "life_axis": "quiet", "location_key": "outside.cafe", "location_label": "附近咖啡店", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "evening",
                1786012740.0,
                "傍晚回家休息",
                "客厅",
                "home",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "休息", "city": "北京", "duration_minutes": 75, "life_axis": "home", "location_key": "home.living_room", "location_label": "客厅", "requires_prior_outing": True, "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "dinner",
                1786013700.0,
                "吃晚饭",
                "厨房",
                "meal",
                "pending",
                {"energy": 4, "mood": 3, "satiety": 38, "stress": -2},
                {"activity_label": "吃晚饭", "city": "北京", "duration_minutes": 45, "life_axis": "meal", "location_key": "home.kitchen", "location_label": "厨房", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "personal_window",
                1786022640.0,
                "睡前做点喜欢的事",
                "客厅",
                "quiet",
                "pending",
                {"energy": -3, "stress": -1},
                {"activity_label": "看点喜欢的内容", "city": "北京", "duration_minutes": 36, "life_axis": "personal", "location_key": "home.living_room", "location_label": "客厅", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
            (
                "night_settle",
                1786025400.0,
                "准备休息",
                "卧室",
                "rest",
                "pending",
                {"energy": 10, "stress": -5},
                {"activity_label": "睡前整理", "city": "北京", "duration_minutes": 80, "life_axis": "rest", "location_key": "home.bedroom", "location_label": "卧室", "source": "aura_world_model", "weather_influence": "none", "weather_signature": "2026-08-06:clear", "world_schema": "aura_world_v2"},
            ),
        ],
    },
]


# ---------------------------------------------------------------------------
# W2 日程理由
# ---------------------------------------------------------------------------

def test_day_plan_output_is_unchanged_by_the_why_field():
    """加 why 不能改变任何槽位的生成结果。"""
    for case in GOLDEN_DAY_PLANS:
        assert _plan_rows(generate_day_plan(**case["input"])) == [tuple(row) for row in case["plan"]]


def test_every_slot_carries_a_why_rule_and_chinese_text():
    seen_rules: set[str] = set()
    for case in GOLDEN_DAY_PLANS:
        for item in generate_day_plan(**case["input"]):
            why = item["payload"]["why"]
            assert set(why) >= {"rule", "inputs", "text"}
            assert isinstance(why["rule"], str) and why["rule"]
            assert isinstance(why["text"], str) and why["text"]
            assert isinstance(why["inputs"], dict)
            seen_rules.add(why["rule"])
    assert "anchor" in seen_rules
    assert "weather_shelter" in seen_rules
    assert {"snack_low_satiety", "snack_seed_roll"} & seen_rules
    assert {"recharge_low_energy", "recharge_seed_roll"} & seen_rules
    assert {"curiosity_high", "curiosity_seed_roll"} & seen_rules


def test_why_inputs_expose_the_state_values_that_fired_the_rule():
    plan = generate_day_plan(
        city="上海",
        day_key="2026-08-05",
        state={"energy": 30, "mood": 40, "satiety": 20, "metadata": {"social_need": 80, "curiosity": 70}},
        weather={"category": "rain", "signature": "2026-08-05:rain"},
        now=1_785_600_000,
    )
    why = {str(item["slot_key"]): item["payload"]["why"] for item in plan}

    assert why["snack"]["rule"] == "snack_low_satiety"
    assert why["snack"]["inputs"]["satiety"] == 20
    assert "饱腹 20 ≤ 45" in why["snack"]["text"]
    assert why["recharge"]["rule"] == "recharge_low_energy"
    assert why["recharge"]["inputs"]["energy"] == 30
    assert "体力 30 < 48" in why["recharge"]["text"]
    assert why["afternoon"]["rule"] == "weather_shelter"
    assert why["afternoon"]["inputs"]["weather_category"] == "rain"
    assert why["curiosity_project"]["rule"] == "curiosity_high"
    assert why["curiosity_project"]["inputs"]["curiosity"] == 70
    assert why["lunch"]["rule"] == "anchor"
    assert why["lunch"]["inputs"]["lunch_out"] is False


def test_random_trigger_records_the_seed_roll():
    plan = generate_day_plan(
        city="上海",
        day_key="2026-08-04",
        state={"energy": 70, "mood": 80, "satiety": 70, "metadata": {"social_need": 40, "curiosity": 40}},
        weather={"category": "mild", "signature": "2026-08-04:mild"},
        now=1_785_500_000,
    )
    why = {str(item["slot_key"]): item["payload"]["why"] for item in plan}

    assert why["personal_window"]["rule"].startswith("personal_window_")
    assert isinstance(why["personal_window"]["inputs"]["seed_roll"], float)
    assert "日程种子抽中" in why["personal_window"]["text"]
    assert isinstance(why["afternoon"]["inputs"]["seed_roll"], int)
    assert "日程种子抽中了第" in why["afternoon"]["text"]


def test_social_pause_requires_high_social_need_and_an_outing():
    plan = generate_day_plan(
        city="上海",
        day_key="2026-08-06",
        state={
            "energy": 95,
            "mood": 88,
            "satiety": 90,
            "metadata": {"social_need": 80, "curiosity": 20},
        },
        weather={"category": "clear", "signature": "2026-08-06:clear"},
        now=1_785_700_000,
    )
    why = {str(item["slot_key"]): item["payload"]["why"] for item in plan}

    assert "social_pause" in why
    assert why["social_pause"]["inputs"]["social_need"] == 80
    assert why["social_pause"]["inputs"]["afternoon_out"] is True
    assert "社交需求 80 ≥ 65" in why["social_pause"]["text"]


def test_lunch_out_reason_names_energy_satiety_and_weather():
    plan = generate_day_plan(
        city="上海",
        day_key="2026-08-04",
        state={"energy": 90, "mood": 80, "satiety": 40, "metadata": {}},
        weather={"category": "mild", "signature": "2026-08-04:mild"},
        now=1_785_500_000,
    )
    lunch = next(item for item in plan if item["slot_key"] == "lunch")

    if lunch["payload"]["why"]["inputs"]["lunch_out"]:
        text = lunch["payload"]["why"]["text"]
        assert "午饭外出" in text
        assert "体力 90" in text and "饱腹 40" in text and "天气 温和" in text


# ---------------------------------------------------------------------------
# W1 世界账本
# ---------------------------------------------------------------------------

def test_apply_world_change_records_real_clamped_change(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = {"mood": 95, "energy": 5, "satiety": 10, "beans": 8, "metadata": {"social_need": 90}}

    updated, summary = apply_world_change(
        store,
        config.scope,
        state,
        {"mood": 20, "energy": -20, "satiety": -5, "beans": -20, "social_need": -30},
        source="world_clock",
        cause="测试截断",
        title="测试",
        event_key="ledger:test:clamp",
    )

    # 上限 100 / 下限 0 都截断过，账本记的是真实变化而不是原始 delta。
    assert updated["mood"] == 100
    assert updated["energy"] == 0
    assert updated["satiety"] == 5
    assert updated["beans"] == 0
    assert updated["metadata"]["social_need"] == 60
    assert summary["delta"] == {"mood": 5, "energy": -5, "satiety": -5, "beans": -8, "social_need": -30}
    assert summary["before"]["mood"] == 95
    assert summary["after"]["mood"] == 100

    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=5)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["before"]["mood"] == 95
    assert payload["after"]["mood"] == 100
    assert payload["cause"] == "测试截断"
    assert payload["source"] == "world_clock"
    assert events[0]["event_type"] == "ledger.world_clock"


def test_apply_world_change_skips_all_zero_changes(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = {"mood": 100, "energy": 0}

    updated, summary = apply_world_change(
        store,
        config.scope,
        state,
        {"mood": 5, "energy": -5},
        source="world_clock",
        cause="全部撞边界",
        title="测试",
    )

    assert summary is None
    assert updated is state
    assert store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=5) == []


def test_apply_world_change_is_idempotent_per_event_key(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = {"mood": 50, "energy": 50}

    first, first_summary = apply_world_change(
        store,
        config.scope,
        state,
        {"mood": 5},
        source="world_clock",
        cause="第一次",
        title="t",
        event_key="ledger:test:once",
    )
    second, second_summary = apply_world_change(
        store,
        config.scope,
        first,
        {"mood": 5},
        source="world_clock",
        cause="第二次",
        title="t",
        event_key="ledger:test:once",
    )

    assert first_summary is not None
    assert second_summary is None
    assert second is first
    assert first["mood"] == 55
    assert len(store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=5)) == 1


def test_beans_have_no_upper_bound_but_never_go_negative(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)

    rich, _ = apply_world_change(
        store, config.scope, {"beans": 90}, {"beans": 50}, source="interaction", cause="赚钱", title="t",
    )
    poor, _ = apply_world_change(
        store, config.scope, {"beans": 3, "coins": 3}, {"beans": -10}, source="interaction", cause="花钱", title="t",
    )

    assert rich["beans"] == 140
    assert rich["coins"] == 140
    assert poor["beans"] == 0
    assert poor["coins"] == 0


def test_state_delta_only_reports_tracked_and_hidden_fields():
    before = {"mood": 50, "energy": 50, "beans": 10, "metadata": {"social_need": 40}}
    after = {"mood": 55, "energy": 50, "beans": 10, "scene": "kitchen", "metadata": {"social_need": 44}}

    assert state_delta(before, after) == {"mood": 5, "social_need": 4}
    assert snapshot_state(after)["mood"] == 55


def test_completed_slot_effects_flow_through_the_ledger(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    plan = [
        {
            "slot_key": "lunch",
            "activity_type": "meal",
            "status": "done",
            "payload": {"location_key": "home.kitchen", "activity_label": "吃午饭"},
        },
        {
            "slot_key": "afternoon",
            "activity_type": "walk",
            "status": "done",
            "payload": {"location_key": "outside.park", "activity_label": "在公园散步"},
        },
    ]

    updated = _settle_completed_world_state(
        config=config, store=store, state=state, plan=plan, day_key="2026-08-04", now=1_785_800_000,
    )
    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=10)

    assert len(events) == 2
    assert {event["payload"]["slot_key"] for event in events} == {"lunch", "afternoon"}
    lunch = next(event for event in events if event["payload"]["slot_key"] == "lunch")
    assert lunch["payload"]["cause"] == "按日程完成：吃了一顿饭"
    assert lunch["delta"]["beans"] == -20
    assert lunch["payload"]["before"]["beans"] == 50
    assert lunch["payload"]["after"]["beans"] == 30
    walk = next(event for event in events if event["payload"]["slot_key"] == "afternoon")
    assert walk["delta"]["social_need"] == -8
    assert walk["payload"]["before"]["social_need"] == 70
    assert walk["payload"]["after"]["social_need"] == 62
    assert updated["beans"] == 30
    # 结算末尾还会再混合一次隐藏驱动，混合本身也记账：这次三个驱动都漂过了
    # 3 点阈值，只有挪了 1 点的 resource_comfort 安静不上账。
    blends = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}drive_blend", limit=10)
    assert [event["delta"] for event in blends] == [
        {"social_need": -5},
        {"curiosity": 4},
        {"privacy_sensitivity": 6},
    ]
    assert blends[0]["payload"]["cause"] == "社交从 62 漂到 57"
    # 基线上一次记到哪儿，下一次就从那儿接着算，重放账本不会漏点。
    assert updated["metadata"]["social_need"] == 57
    assert updated["metadata"]["drive_ledger_baseline"] == {
        "social_need": 57,
        "curiosity": 44,
        "privacy_sensitivity": 36,
    }


def test_hunger_snack_records_its_trigger_rule(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    plan = [{
        "slot_key": "wake",
        "activity_type": "morning",
        "status": "done",
        "payload": {"location_key": "home.bedroom", "activity_label": "刚起床"},
    }]

    updated = _settle_completed_world_state(
        config=config, store=store, state=state, plan=plan, day_key="2026-08-04", now=1_785_800_000,
    )
    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}hunger_drive", limit=5)

    assert len(events) == 1
    assert "饱腹 30 ≤ 30" in events[0]["payload"]["cause"]
    assert events[0]["payload"]["satiety"] == 30
    assert events[0]["delta"]["beans"] == -10
    assert updated["beans"] == 40

    # 同一个 4 小时窗口再结算一次：既不应用也不记账。
    repeated = _settle_completed_world_state(
        config=config, store=store, state=updated, plan=plan, day_key="2026-08-04", now=1_785_800_100,
    )
    assert repeated["beans"] == updated["beans"]
    assert len(store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}hunger_drive", limit=5)) == 1


def test_temporary_care_is_settled_once_through_the_ledger(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    night_at = dt.datetime(2026, 8, 4, 22, 35, tzinfo=dt.timezone.utc).timestamp()
    plan = [{"slot_key": "night_settle", "scheduled_at": night_at, "payload": {"duration_minutes": 80}}]

    updated = _settle_temporary_care(
        config=config, store=store, state=state, plan=plan, day_key="2026-08-04", now=night_at - 60,
    )
    repeated = _settle_temporary_care(
        config=config, store=store, state=updated, plan=plan, day_key="2026-08-04", now=night_at,
    )

    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=10)
    assert len(events) == 1
    assert events[0]["title"] == "完成晚间整理"
    assert events[0]["payload"]["transient"] is True
    assert "不占用日程槽位" in events[0]["payload"]["cause"]
    assert updated["energy"] == 37
    assert repeated["energy"] == 37


def test_drive_blend_is_only_recorded_when_the_move_is_meaningful(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    prefix = f"{LEDGER_EVENT_PREFIX}drive_blend"

    # 老库升级：驱动本来就有、但没有基线，按混合前的值起算，该记多少记多少。
    _update_drives(state, metadata=state["metadata"], now=1_785_800_000, store=store, scope=config.scope)
    events = store.list_recent_life_events(config.scope, event_prefix=prefix, limit=10)
    assert [event["delta"] for event in events] == [
        {"social_need": -6},
        {"curiosity": 4},
        {"privacy_sensitivity": 8},
    ]
    social = events[0]
    assert social["payload"]["before"]["social_need"] == 70
    assert social["payload"]["after"]["social_need"] == 64
    assert social["payload"]["cause"] == "社交从 70 漂到 64"
    assert str(social["event_key"]).startswith("drive:social_need:")
    assert state["metadata"]["social_need"] == 64
    # 只挪了 1 点的 resource_comfort 不上账本（|change| < 3 静默跳过）。
    assert all("resource_comfort" not in event["delta"] for event in events)

    # 再混合一次：相对基线（= 上一笔记账的值）漂了 5 点，超过阈值才记。
    _update_drives(state, metadata=state["metadata"], now=1_785_800_100, store=store, scope=config.scope)
    events = store.list_recent_life_events(config.scope, event_prefix=prefix, limit=10)
    fresh = events[3:]
    assert [event["delta"] for event in fresh] == [
        {"social_need": -5},
        {"curiosity": 3},
    ]
    assert fresh[0]["payload"]["before"]["social_need"] == 64
    assert fresh[0]["payload"]["after"]["social_need"] == 59
    assert fresh[0]["payload"]["cause"] == "社交从 64 漂到 59"
    assert state["metadata"]["social_need"] == 59

    # 同一个 5 分钟桶里再漂一次：event_key 已经存在，不重复记账。
    _update_drives(state, metadata=state["metadata"], now=1_785_800_200, store=store, scope=config.scope)
    assert len(store.list_recent_life_events(config.scope, event_prefix=prefix, limit=20)) == 5


def test_drive_blend_flushes_sub_threshold_drift_into_the_next_record(tmp_path):
    """小于阈值的漂移不单独记账，但要能被下一笔一次补齐（重放才不漏点）。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    # 先把基线立起来：社交当前 64，基线也是 64。
    _update_drives(state, metadata=state["metadata"], now=1_785_800_000, store=store, scope=config.scope)
    state["metadata"]["social_need"] = 64
    state["metadata"]["drive_ledger_baseline"]["social_need"] = 64

    # 手动把社交压到 62：这是"混合只挪了 2 点、不值得记账"的那类变化。
    state["metadata"]["social_need"] = 62
    apply_world_change(
        store, config.scope, state, {"mood": 2}, source="world_clock", cause="一件小事", title="小事",
    )

    events = store.list_recent_life_events(
        config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=5,
    )
    # 心情 +2 是这一次的；社交 -2 与安心 +1 是之前没过阈值的漂移，
    # 被这一笔一起补齐，重放账本时才不会丢掉这几点。
    assert events[-1]["delta"] == {"mood": 2, "social_need": -2, "resource_comfort": 1}
    assert events[-1]["payload"]["before"]["social_need"] == 64
    assert events[-1]["payload"]["after"]["social_need"] == 62
    assert events[-1]["payload"]["before"]["resource_comfort"] == 30
    assert events[-1]["payload"]["after"]["resource_comfort"] == 31
    # 补齐之后基线走到当前值，同一笔漂移不会记第二次。
    apply_world_change(
        store, config.scope, state, {"mood": 1}, source="world_clock", cause="又一件小事", title="小事",
    )
    assert store.list_recent_life_events(
        config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}world_clock", limit=5,
    )[-1]["delta"] == {"mood": 1}


def test_drive_blend_stays_quiet_when_the_drive_sits_on_its_target(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    # 四个驱动都正好落在各自的目标值上，混合结果不变。
    state["metadata"].update(
        {"social_need": 45, "curiosity": 55, "privacy_sensitivity": 38, "resource_comfort": 33}
    )
    store.save_state(config.scope, state)

    for step in range(3):
        _update_drives(state, metadata=state["metadata"], now=1_785_800_000 + step * 600, store=store, scope=config.scope)

    assert state["metadata"]["social_need"] == 45
    assert store.list_recent_life_events(
        config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}drive_blend", limit=10,
    ) == []


def test_drive_blend_skips_first_initialization(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state["metadata"] = {}

    _update_drives(state, metadata=state["metadata"], now=1_785_800_000, store=store, scope=config.scope)

    assert store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}drive_blend", limit=10) == []


def test_interaction_and_reply_deltas_are_ledgered(tmp_path):
    from integrations.aura_persona_gateway.turn import _record_interaction_ledger, _record_reply_ledger

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    now = 1_785_800_000.0
    # 上一次"真的在"是 4 小时前，这样 apply_time_recovery 才真的恢复体力。
    state["metadata"]["last_recovery_at"] = now - 4 * 3600
    store.save_state(config.scope, state)

    before = dict(state)
    recovered = apply_time_recovery(state, now=now)
    updated = apply_user_interaction_delta(state, now=now)
    assert state_delta(before, recovered) == {
        "energy": 40,
        "satiety": 20,
        "stress": -10,
    }
    assert state_delta(recovered, updated) == {
        "energy": -1,
        "mood": 1,
        "social_need": -4,
        "curiosity": 1,
    }

    _record_interaction_ledger(
        store=store, scope=config.scope, state=before, recovered=recovered, updated=updated, now=now,
    )
    replied = apply_agent_reply_delta(updated, ok=True, now=now)
    _record_reply_ledger(store=store, scope=config.scope, state=updated, updated=replied, ok=True)

    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}interaction", limit=10)
    stages = {str(event["payload"]["stage"]) for event in events}
    assert stages == {"time_recovery", "user_interaction", "agent_reply"}
    causes = {str(event["payload"]["cause"]) for event in events}
    assert "用户互动：陪你聊了一轮" in causes
    assert "完成一次回复" in causes
    assert updated["metadata"]["last_user_interaction_at"] == now


def test_weather_influence_is_ledgered_once_per_signature(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    _seed_state(store, config)
    weather = {"category": "rain", "signature": "2026-08-04:rain", "plan_influence": "indoor_flexible"}
    day_key = "2026-08-04"

    from integrations.aura_persona_gateway.world import _apply_weather_influence

    updated = _apply_weather_influence(
        config=config, store=store, state=store.get_or_create_state(config.scope),
        weather=weather, day_key=day_key, now=1_785_800_000,
    )
    repeated = _apply_weather_influence(
        config=config, store=store, state=updated,
        weather=weather, day_key=day_key, now=1_785_800_100,
    )

    events = store.list_recent_life_events(config.scope, event_prefix=f"{LEDGER_EVENT_PREFIX}weather", limit=5)
    assert len(events) == 1
    assert events[0]["event_key"] == "2026-08-04:rain"
    assert events[0]["payload"]["before"]["mood"] == 60
    assert events[0]["payload"]["after"]["mood"] == 59
    # 天气分类翻成中文再写进 cause/title，读账本的人才不用猜 rain 是什么。
    assert events[0]["payload"]["cause"] == "天气变成下雨，环境和体感影响了状态"
    assert events[0]["title"] == "天气环境更新为下雨"
    assert repeated["mood"] == 59


def test_recent_events_read_both_world_and_ledger_prefixes(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    store.record_life_event(
        config.scope, event_type="world.transition", event_key="w:1", title="旧前缀事件", visibility="private",
    )
    store.record_life_event(
        config.scope,
        event_type=f"{LEDGER_EVENT_PREFIX}world_clock",
        event_key="l:1",
        title="新前缀事件",
        visibility="private",
    )
    store.record_life_event(
        config.scope,
        event_type=f"{LEDGER_EVENT_PREFIX}interaction",
        event_key="l:2",
        title="每轮噪音",
        visibility="private",
    )

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        now=1_785_800_000,
    )

    titles = [str(event["title"]) for event in snapshot["recent_events"]]
    assert "旧前缀事件" in titles
    assert "新前缀事件" in titles
    # ledger.interaction 是每轮 ±1 的噪音，不进 prompt。
    assert "每轮噪音" not in titles
    assert "近期世界变化" in "\n".join(snapshot["prompt_lines"])


def test_replay_ledger_reproduces_the_current_state_after_a_full_day(tmp_path):
    """W1 不变量：按时间重放账本 delta 必须回到当前值。"""
    from integrations.aura_persona_gateway.turn import _record_interaction_ledger, _record_reply_ledger

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    initial = {
        "mood": 60,
        "energy": 40,
        "satiety": 30,
        "stress": 10,
        "trust": 50,
        "beans": 50,
        "affinity_xp": 0,
        "coins": 50,
        "metadata": {"social_need": 70, "curiosity": 40, "privacy_sensitivity": 30, "resource_comfort": 30},
    }
    store.save_state(config.scope, dict(initial))
    # save_state 只 UPDATE 不 INSERT，先确保这一行存在。
    store.get_or_create_state(config.scope)
    store.save_state(config.scope, dict(initial))
    # 天气快照要带 status=fresh + 中文 condition 才会被认成实况。
    weather = {"status": "fresh", "city": "上海", "condition": "小雨", "temperature": 21.0, "humidity": 88.0}
    base = dt.datetime(2026, 8, 4, 6, 0, tzinfo=dt.timezone.utc).timestamp()
    # 一整天按 20 分钟一跳：先跑世界 tick（结算日程 / 驱动混合 / 天气），
    # 再跑一轮真实对话（恢复 + 互动 + 回复），全部走 turn.py 的记账入口。
    for step in range(72):
        now = base + step * 20 * 60
        build_world_snapshot(
            config=config,
            store=store,
            state=store.get_or_create_state(config.scope),
            query_context={"intent": "world_tick"},
            weather_snapshot=weather,
            now=now,
        )
        state = store.get_or_create_state(config.scope)
        recovered = apply_time_recovery(state, now=now)
        updated = apply_user_interaction_delta(state, now=now)
        _record_interaction_ledger(
            store=store, scope=config.scope, state=state, recovered=recovered, updated=updated, now=now,
        )
        ok = bool(step % 3)
        replied = apply_agent_reply_delta(updated, ok=ok, now=now)
        _record_reply_ledger(store=store, scope=config.scope, state=updated, updated=replied, ok=ok)
        store.save_state(config.scope, replied)

    current = store.get_or_create_state(config.scope)
    # list_recent_life_events 是给 prompt 用的，最多只回 20 条；这里要全量，
    # 按只读方式把账本整段读出来（不改库）。
    events = _all_ledger_events(config.companion_db_path, config.scope)
    assert events, "一整天应该留下账本流水"
    assert all(str(event["event_type"]).startswith(LEDGER_EVENT_PREFIX) for event in events)

    replayed = replay_ledger(events, initial)
    # 记录在案的都是"截断后的真实变化"，所以跟踪字段必须分毫不差。
    for field in ("mood", "energy", "satiety", "beans"):
        assert replayed[field] == current[field], field
    # 隐藏驱动有 |change| >= 3 才记账的阈值，账本末尾最多残留（阈值-1）点漂移；
    # 残差必须正好等于"当前值 - 基线"，而不是随便对不上。
    baseline = read_drive_baseline(current["metadata"])
    for field in HIDDEN_DRIVE_FIELDS:
        slack = current["metadata"][field] - baseline.get(field, current["metadata"][field])
        assert abs(slack) < DRIVE_BLEND_MIN_CHANGE, (field, slack)
        assert replayed["metadata"][field] == current["metadata"][field] - slack, field
    assert {
        f"{LEDGER_EVENT_PREFIX}world_clock",
        f"{LEDGER_EVENT_PREFIX}drive_blend",
        f"{LEDGER_EVENT_PREFIX}weather",
        f"{LEDGER_EVENT_PREFIX}interaction",
    } <= {str(event["event_type"]) for event in events}


def test_replay_ledger_ignores_non_ledger_events():
    initial = {"mood": 50, "beans": 10, "metadata": {}}
    events = [
        {"event_type": "world.transition", "created_at": 3, "delta": {"mood": 99}},
        {"event_type": f"{LEDGER_EVENT_PREFIX}world_clock", "created_at": 1, "delta": {"mood": 5, "beans": -3}},
        {"event_type": f"{LEDGER_EVENT_PREFIX}weather", "created_at": 2, "delta": {"mood": -2}},
    ]

    replayed = replay_ledger(events, initial)

    assert replayed["mood"] == 53
    assert replayed["beans"] == 7


# ---------------------------------------------------------------------------
# W4 设定集密度
# ---------------------------------------------------------------------------

def test_default_canon_has_detailed_objects_across_home_scenes():
    canon = default_world_canon()
    objects = canon.objects

    assert 12 <= len(objects) <= 16
    home_scenes = {str(item.get("location") or "") for item in objects.values()}
    assert home_scenes == {
        "home.study",
        "home.living_room",
        "home.kitchen",
        "home.bedroom",
        "home.balcony",
    }
    detailed = [
        key
        for key, item in objects.items()
        if any(_clean(item.get(field)) for field in ("habit", "story", "sense"))
    ]
    assert len(detailed) == len(objects)
    for key in ("study.west_window", "balcony.pothos", "kitchen.clay_pot", "bedroom.night_light", "living.record_player"):
        assert key in objects
        assert _clean(objects[key].get("habit"))


def _clean(value) -> str:
    return str(value or "").strip()


def test_scene_prompt_renders_at_most_three_objects_with_details():
    from integrations.aura_persona_gateway.world import _scene_prompt_text

    canon = default_world_canon()
    scene = {
        "location_key": "home.study",
        "location_label": "书房",
        "activity_label": "整理东西",
        "nearby_objects": canon.nearby_objects("home.study", limit=6),
    }

    text = _scene_prompt_text(scene)

    assert text.count("、") <= 3  # 最多三件物件
    assert "书桌" in text
    assert "每天早上擦一遍" in text
    assert "西窗" not in text  # 第 4 件不渲染
    assert "附近物品=" in text


def test_reminder_ledger_rows_reach_the_trace_page(tmp_path):
    import sqlite3

    from integrations.aura_persona_gateway.trace_data import ledger_row, read_recent_ledger
    from integrations.aura_persona_gateway.world_ledger import record_reminder_ledger

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    now = time.time()
    record_reminder_ledger(store, config.scope, action="set", label="去散步", fire_at=now + 600, reminder_id="r1", at=now)
    record_reminder_ledger(store, config.scope, action="fire", label="去散步", fire_at=now + 600, reminder_id="r1", at=now + 600)

    scope = config.scope
    platform, chat_id, user_id = scope.platform, scope.chat_id, scope.user_id
    with sqlite3.connect(config.companion_db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [ledger_row(item, city="上海") for item in read_recent_ledger(conn, platform=platform, chat_id=chat_id, user_id=user_id)]

    reminder_rows = [row for row in rows if row["source"] == "reminder"]
    assert [row["action"] for row in reminder_rows] == ["fire", "set"]  # 新的在前
    assert reminder_rows[1]["title"] == "定了提醒：去散步"
    assert abs(reminder_rows[1]["fire_at"] - (now + 600)) < 1
