from __future__ import annotations

import datetime as dt
import hashlib
import random
import re
import time
from typing import Any

from .time_context import resolve_city_tzinfo
from .world_assets import WorldCanon, load_world_canon
from .world_ledger import (
    HIDDEN_DRIVE_FIELDS,
    LEDGER_EVENT_PREFIX,
    RECAP_FIELD_LABELS,
    apply_world_change,
    format_recap_field,
    note_drive_baseline,
    read_drive_baseline,
)


WORLD_SCHEMA_VERSION = "aura_world_v2"
WORLD_DISABLED_REASON = "world_model_disabled"
ORDINARY_REPLY_REASON = "ordinary_reply"
TEMPORARY_CARE_WINDOW_SECONDS = 35 * 60.0
# metadata 里缓存的 generated current 只在这个窗口内可信，过期后回落到
# 当天计划重新推导（否则"吃早饭"会一直挂到晚上）。
GENERATED_CURRENT_TTL_SECONDS = 30 * 60.0
# W3：距上次互动超过这个间隔才生成"你不在时"的离线回顾。
OFFLINE_RECAP_MIN_GAP_SECONDS = 3600.0
OFFLINE_RECAP_LIMIT = 4
# W1：prompt 的"近期世界变化"和离线回顾都要同时读到旧 ``world.`` 与新
# ``ledger.`` 两种事件类型；``ledger.interaction`` 是每轮 ±1 的噪音，不进 prompt。
RECENT_EVENT_PREFIXES = ("world.", LEDGER_EVENT_PREFIX)
RECAP_EXCLUDED_EVENT_TYPES = (f"{LEDGER_EVENT_PREFIX}interaction",)
# 隐藏驱动漂移（"好奇变化"）对用户没有信息量，离线回顾里也跳过。
OFFLINE_RECAP_EXCLUDED_EVENT_TYPES = RECAP_EXCLUDED_EVENT_TYPES + (f"{LEDGER_EVENT_PREFIX}drive_blend",)
# 天气分类是内部枚举值，写进 why.text 前翻成中文，人话才好读。
WEATHER_CATEGORY_LABELS = {
    "clear": "晴",
    "cloudy": "多云",
    "mild": "温和",
    "rain": "下雨",
    "storm": "暴雨",
    "snow": "下雪",
    "heat": "高温",
    "cold": "寒冷",
    "unknown": "未知",
}


def _weather_label(category: str) -> str:
    key = _clean_text(category)
    return WEATHER_CATEGORY_LABELS.get(key, key or "未知")


def build_world_snapshot(
    *,
    config: Any,
    store: Any,
    state: dict[str, Any],
    query_context: dict[str, Any],
    user_geo: dict[str, Any] | None = None,
    voice_low_latency: bool = False,
    recent_messages: list[dict[str, Any]] | None = None,
    weather_snapshot: dict[str, Any] | None = None,
    now: float | None = None,
    persist: bool = True,
    offline_recap: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    enabled = bool(getattr(config, "world_model_enabled", True))
    city = _clean_text(getattr(config, "aura_home_city", ""))
    persona_name = _clean_text(getattr(config, "persona_name", "") or "Aura")
    canon = load_world_canon(config)
    ts = float(now if now is not None else time.time())
    # day_key 按 Lily 所在城市的当地时间计算，与容器时区无关。
    day_key = dt.datetime.fromtimestamp(ts, resolve_city_tzinfo(city)).date().isoformat()
    weather = _world_weather_snapshot(weather_snapshot, day_key=day_key)
    if not enabled:
        return {
            "enabled": False,
            "available": False,
            "persona_name": persona_name,
            "city": city,
            "day_key": day_key,
            "current": {},
            "today_plan": [],
            "next_plan": None,
            "weather": weather,
            "mention_policy": _mention_policy(
                query_context=query_context,
                state=state,
                current={},
                recent_messages=recent_messages or [],
                disabled=True,
            ),
            "prompt_lines": [],
            "canon": {},
            "scene": {},
            "recent_events": [],
            "offline_recap": [],
            "offline_recap_hours": 0.0,
            "debug": {
                "reason": WORLD_DISABLED_REASON,
                "build_ms": _elapsed_ms(started),
                "voice_low_latency": bool(voice_low_latency),
                "user_geo_source": str((user_geo or {}).get("source") or ""),
            },
        }

    scope = config.scope
    state = _apply_weather_influence(
        config=config,
        store=store,
        state=state,
        weather=weather,
        day_key=day_key,
        now=ts,
        persist=persist,
    )
    drive_metadata = dict(state.get("metadata") if isinstance(state.get("metadata"), dict) else {})
    # speculative 回合只读不写，因此也不该往账本里塞 drive_blend 流水。
    _update_drives(
        state,
        metadata=drive_metadata,
        now=ts,
        store=store if persist else None,
        scope=scope if persist else None,
    )
    if persist and drive_metadata != (state.get("metadata") if isinstance(state.get("metadata"), dict) else {}):
        state = dict(state)
        state["metadata"] = drive_metadata
        store.save_state(scope, state)
    plan = store.today_plan(scope, day_key=day_key)
    generated_new_plan = False
    plan_weather_changed = bool(weather) and not _plan_matches_weather(plan, weather)
    if not _is_aura_world_plan(plan) or plan_weather_changed or _has_legacy_temporary_slot(plan):
        plan = generate_day_plan(
            city=city,
            day_key=day_key,
            state=state,
            weather=weather,
            now=ts,
        )
        if persist and hasattr(store, "replace_day_plan"):
            store.replace_day_plan(scope, day_key=day_key, items=plan)
        generated_new_plan = True
    plan = _with_status(plan, now=ts)
    if persist and hasattr(store, "save_day_plan_statuses"):
        store.save_day_plan_statuses(scope, day_key=day_key, items=plan)
    if persist:
        state = _settle_completed_world_state(config=config, store=store, state=state, plan=plan, day_key=day_key, now=ts)
        state = _settle_temporary_care(config=config, store=store, state=state, plan=plan, day_key=day_key, now=ts)

    # W3：用户离开超过一小时，从账本里捞这段窗口的真实事件做离线回顾。
    recap_lines: list[str] = []
    offline_recap_hours = 0.0
    # 只有真正的对话回合才消费回顾；状态推送的 world_tick 和管理台视图
    # 也会持久化快照，不能让它们抢先把"这段离开"标记成已讲过。
    if persist and offline_recap:
        recap_lines, offline_recap_hours, state = _build_offline_recap_for_snapshot(
            store=store,
            scope=scope,
            state=state,
            city=city,
            now=ts,
        )

    current = _manual_current_from_state(state)
    current_source = "manual_state" if current else ""
    world_clock_tick = str((query_context or {}).get("intent") or "") == "world_tick"
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    preserve_conversation_override = str(metadata.get("world_current_source") or "") == "conversation"
    temporary_care = _temporary_care_current(plan, now=ts, city=city)
    # A recently persisted generated/conversation state is still the most
    # faithful current moment, even when today's plan had to be regenerated.
    # _generated_current_from_state applies the short TTL so an old meal or
    # location cannot pin the clock indefinitely.
    if not current and not preserve_conversation_override and temporary_care:
        current = temporary_care
        current_source = "temporary_care"
    if not current and (not world_clock_tick or preserve_conversation_override):
        current = _generated_current_from_state(state, city=city, now=ts)
        current_source = "generated_state" if current else "generated_plan"
    if not current:
        current = _current_from_plan(plan, now=ts, city=city)
    current = _canonicalize_current(current, canon=canon)
    scene = _scene_from_current(current, canon=canon, state=state)
    _persist_world_current(config, store, state, current, scene=scene, weather=weather, now=ts, persist=persist)
    recent_events = _recent_world_events(store, scope, limit=8)
    next_plan = _next_plan(plan, now=ts)
    policy = _mention_policy(
        query_context=query_context,
        state=state,
        current=current,
        recent_messages=recent_messages or [],
        disabled=False,
    )
    prompt_lines = render_world_prompt(
        {
            "enabled": True,
            "available": True,
            "persona_name": persona_name,
            "city": city,
            "day_key": day_key,
            "current": current,
            "today_plan": plan,
            "next_plan": next_plan,
            "weather": weather,
            "mention_policy": policy,
            "canon": canon.public_dict(),
            "scene": scene,
            "recent_events": recent_events,
            "offline_recap": recap_lines,
            "offline_recap_hours": offline_recap_hours,
            "debug": {},
        }
    ).splitlines()
    return {
        "enabled": True,
        "available": True,
        "persona_name": persona_name,
        "city": city,
        "day_key": day_key,
        "current": current,
        "today_plan": plan,
        "next_plan": next_plan,
        "weather": weather,
        "mention_policy": policy,
        "canon": canon.public_dict(),
        "scene": scene,
        "recent_events": recent_events,
        "offline_recap": recap_lines,
        "offline_recap_hours": offline_recap_hours,
        "prompt_lines": prompt_lines,
        "debug": {
            "schema": WORLD_SCHEMA_VERSION,
            "build_ms": _elapsed_ms(started),
            "generated_new_plan": generated_new_plan,
            "plan_weather_changed": plan_weather_changed,
            "weather_signature": weather.get("signature") or "",
            "plan_count": len(plan),
            "current_source": current_source,
            "canon_source": canon.source_path or "built_in_default",
            "scene_location": scene.get("location_key") or "",
            "recent_event_count": len(recent_events),
            "offline_recap_count": len(recap_lines),
            "offline_recap_hours": offline_recap_hours,
            "voice_low_latency": bool(voice_low_latency),
            "user_geo_source": str((user_geo or {}).get("source") or ""),
        },
    }


def _recent_world_events(store: Any, scope: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    """世界变化流水：旧 ``world.`` 和新 ``ledger.`` 前缀都要能读到（W1）。"""
    if not hasattr(store, "list_recent_life_events"):
        return []
    try:
        events = store.list_recent_life_events(
            scope,
            event_prefixes=RECENT_EVENT_PREFIXES,
            limit=limit,
        )
    except TypeError:
        # 老 store 只认单个 event_prefix。
        events = store.list_recent_life_events(scope, event_prefix="world.", limit=limit)
    return [
        event
        for event in events
        if str(event.get("event_type") or "") not in RECAP_EXCLUDED_EVENT_TYPES
    ]


def build_offline_recap(
    store: Any,
    scope: Any,
    *,
    since_ts: float,
    now: float,
    city: str,
    limit: int = OFFLINE_RECAP_LIMIT,
) -> list[str]:
    """W3：把"用户不在的这段时间"的账本流水压成几句可朗读的短句。

    只讲账本里真发生过的事，Lily 不能靠想象补内容。
    """
    since = float(since_ts or 0)
    until = float(now or 0)
    if since <= 0 or until <= since:
        return []
    rows: list[tuple[float, str]] = []
    for event in _recent_world_events(store, scope, limit=20):
        try:
            created_at = float(event.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if str(event.get("event_type") or "") in OFFLINE_RECAP_EXCLUDED_EVENT_TYPES:
            continue
        if created_at < since or created_at > until:
            continue
        line = _recap_line(event, city=city)
        if line:
            rows.append((created_at, line))
    rows.sort(key=lambda item: item[0])
    return [line for _created_at, line in rows[: max(1, int(limit or 1))]]


def _recap_line(event: dict[str, Any], *, city: str) -> str:
    when = _fmt_time(event.get("created_at"), city=city)
    title = _clean_text(event.get("title") or "")
    if not title:
        title = _clean_text(event.get("description") or "")
    if not title:
        return ""
    delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
    effects = [
        format_recap_field(field, _coerce_int(delta.get(field), 0))
        for field in RECAP_FIELD_LABELS
        if _coerce_int(delta.get(field), 0)
    ]
    body = title + (f"（{'，'.join(effects)}）" if effects else "")
    return " ".join(part for part in (when, body) if part)


def _offline_recap_anchor(metadata: dict[str, Any]) -> float:
    """上一次"用户真的在"的时刻。

    ``state_rules.apply_user_interaction_delta`` 每轮都会推进
    ``last_user_interaction_at``，所以要看它覆盖前保存下来的那一个。
    """
    for key in ("previous_user_interaction_at", "last_user_interaction_at", "last_interaction_at"):
        value = _coerce_float(metadata.get(key))
        if value and value > 0:
            return value
    return 0.0


def _build_offline_recap_for_snapshot(
    *,
    store: Any,
    scope: Any,
    state: dict[str, Any],
    city: str,
    now: float,
) -> tuple[list[str], float, dict[str, Any]]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    anchor = _offline_recap_anchor(metadata)
    if anchor <= 0 or now - anchor < OFFLINE_RECAP_MIN_GAP_SECONDS:
        return [], 0.0, state
    marker = f"{int(anchor)}"
    if _clean_text(metadata.get("offline_recap_for")) == marker:
        return [], 0.0, state
    recap = build_offline_recap(store, scope, since_ts=anchor, now=now, city=city)
    if not recap:
        return [], 0.0, state
    hours = max(1.0, round((now - anchor) / 3600.0, 1))
    updated = dict(state)
    next_metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    # 只注入一次：同一段"离开"不要反复讲。
    next_metadata["offline_recap_for"] = marker
    next_metadata["offline_recap_at"] = float(now)
    updated["metadata"] = next_metadata
    store.save_state(scope, updated)
    return recap, hours, updated


def _settle_completed_world_state(
    *, config: Any, store: Any, state: dict[str, Any], plan: list[dict[str, Any]], day_key: str, now: float,
) -> dict[str, Any]:
    """Apply completed schedule effects once, then persist a real dynamic state.

    The event key is the transaction id. This keeps repeated chat/world ticks
    from charging the same meal twice while still making old plans effective.
    """
    scope = config.scope
    metadata = dict(state.get("metadata") if isinstance(state.get("metadata"), dict) else {})
    applied = [str(item) for item in metadata.get("world_effects_applied", []) if str(item).strip()]
    applied_set = set(applied)
    updated = dict(state)
    changed = False
    for item in plan:
        if item.get("status") != "done":
            continue
        slot_key = _clean_text(item.get("slot_key") or "")
        if not slot_key:
            continue
        event_key = f"world.slot:{day_key}:{slot_key}"
        if event_key in applied_set:
            continue
        # Existing stores without has_life_event still use the metadata key.
        if hasattr(store, "has_life_event") and store.has_life_event(scope, event_key=event_key):
            applied_set.add(event_key)
            applied.append(event_key)
            continue
        delta, title, description, cost, method = _slot_effect(item, updated)
        if not delta and not cost:
            applied_set.add(event_key)
            applied.append(event_key)
            continue
        # W1：日程结算也走世界账本，事件类型从 world.slot_effect 变成
        # ledger.world_clock，before/after/cause 都落在 payload_json 里。
        updated, _summary = apply_world_change(
            store,
            scope,
            updated,
            {**delta, "beans": -cost} if cost else delta,
            source="world_clock",
            cause=f"按日程完成：{title}",
            title=title,
            event_key=event_key,
            location=_clean_text((item.get("payload") or {}).get("location_key")),
            activity=_clean_text((item.get("payload") or {}).get("activity_label") or item.get("title")),
            payload={
                "slot_key": slot_key,
                "meal_method": method,
                "source": "world_clock",
                "effect_note": description,
            },
        )
        if method in {"paid_snack", "cooked_snack"}:
            metadata["last_snack_at"] = float(now)
        applied_set.add(event_key)
        applied.append(event_key)
        changed = True

    snack = _maybe_settle_hunger_snack(config=config, store=store, state=updated, now=now, metadata=metadata)
    if snack:
        updated, snack_event = snack
        changed = True
        applied.append(snack_event)
    if len(applied) > 96:
        applied = applied[-96:]
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else metadata)
    metadata["world_effects_applied"] = applied
    # 结算完再混合一次驱动，这次也记账：否则状态变了、账本没有对应流水，
    # 重放账本就回不到当前值（W1 的不变量）。
    _update_drives(updated, metadata=metadata, now=now, store=store, scope=scope)
    updated["metadata"] = metadata
    if changed or updated.get("metadata") != state.get("metadata"):
        store.save_state(scope, updated)
    return updated


def _has_legacy_temporary_slot(plan: list[dict[str, Any]]) -> bool:
    return any(str(item.get("slot_key") or "") in {"personal_care", "bath", "shower"} for item in plan)


def _temporary_care_window(plan: list[dict[str, Any]], *, now: float) -> tuple[float, float] | None:
    night = next((item for item in plan if str(item.get("slot_key") or "") == "night_settle"), None)
    if not night:
        return None
    end = float(night.get("scheduled_at") or 0)
    if end <= 0:
        return None
    return end - TEMPORARY_CARE_WINDOW_SECONDS, end


def _temporary_care_current(plan: list[dict[str, Any]], *, now: float, city: str) -> dict[str, Any] | None:
    window = _temporary_care_window(plan, now=now)
    if not window or not (window[0] <= now < window[1]):
        return None
    return {
        "location_key": "home.bedroom",
        "location_label": "卧室",
        "activity_key": "care",
        "activity_label": "晚间整理",
        "title": "晚间整理",
        "slot_key": "temporary_care",
        "status": "active",
        "city": city,
        "source": "temporary_action",
        "transient": True,
        "available": True,
    }


def _settle_temporary_care(
    *, config: Any, store: Any, state: dict[str, Any], plan: list[dict[str, Any]], day_key: str, now: float,
) -> dict[str, Any]:
    window = _temporary_care_window(plan, now=now)
    if not window or now < window[0]:
        return state
    metadata = dict(state.get("metadata") if isinstance(state.get("metadata"), dict) else {})
    marker = f"world.temporary:care:{day_key}"
    applied = [str(item) for item in metadata.get("world_temporary_actions_applied", []) if str(item).strip()]
    if marker in applied:
        return state
    if hasattr(store, "has_life_event") and store.has_life_event(config.scope, event_key=marker):
        return state
    updated = dict(state)
    care_delta = {"energy": -3, "mood": 1, "stress": -3}
    # W1：短暂护理也过账本，cause 说明它为什么发生（不进日程槽位）。
    updated, _summary = apply_world_change(
        store,
        config.scope,
        updated,
        care_delta,
        source="world_clock",
        cause="洗澡、洗脸或护肤等短暂护理行为，不占用日程槽位",
        title="完成晚间整理",
        event_key=marker,
        location="home.bedroom",
        activity="晚间整理",
        payload={"source": "world_clock", "transient": True, "day_key": day_key},
    )
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else metadata)
    metadata["world_temporary_actions_applied"] = (applied + [marker])[-32:]
    updated["metadata"] = metadata
    store.save_state(config.scope, updated)
    return updated


def _slot_effect(item: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, int], str, str, int, str]:
    activity_type = _clean_text(item.get("activity_type") or "")
    slot_key = _clean_text(item.get("slot_key") or "")
    if slot_key == "snack":
        if _state_int(state, "beans", 0) >= 10:
            return ({"satiety": 22, "energy": 2, "mood": 6, "stress": -2}, "买了点零食", "按日程补充零食", 10, "paid_snack")
        return ({"satiety": 14, "energy": 2, "mood": 2}, "简单补充了一点食物", "余额不足，改为在家简单补充", 0, "cooked_snack")
    if activity_type == "meal" or slot_key in {"breakfast", "lunch", "dinner"}:
        beans = _state_int(state, "beans", 0)
        if beans >= 20:
            return ({"satiety": 38, "energy": 5, "mood": 3, "stress": -3}, "吃了一顿饭", "按日程完成进食（购买）", 20, "paid_meal")
        return ({"satiety": 30, "energy": 4, "mood": 2, "stress": -2}, "自己做了饭", "余额不足，改为在家做饭", 0, "cooked_meal")
    if activity_type == "rest" or slot_key == "wake":
        return ({"energy": 10 if activity_type == "rest" else -2, "stress": -5 if activity_type == "rest" else 0}, "休息恢复", "按日程休息，体力和压力得到调整", 0, "rest")
    if activity_type in {"walk", "browse", "errand"}:
        return ({"energy": -7, "mood": 5 if activity_type != "errand" else 2, "stress": -4, "social_need": -8}, "外出活动完成", "外出活动带来心情变化", 0, "outing")
    if activity_type == "care":
        return ({"energy": -3, "mood": 1, "stress": -3}, "做了晚间护理", "洗澡和护理让状态稍微放松", 0, "care")
    if activity_type in {"quiet", "home", "morning", "social"}:
        return ({"energy": -3, "mood": 2 if activity_type == "social" else 0, "stress": -1, "curiosity": 2 if activity_type == "quiet" else 0}, "日常活动完成", "日程中的日常活动已完成", 0, "routine")
    return ({}, "", "", 0, "")


def _maybe_settle_hunger_snack(*, config: Any, store: Any, state: dict[str, Any], now: float, metadata: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    satiety = _state_int(state, "satiety", 70)
    if satiety > 30:
        return None
    try:
        last = float(metadata.get("last_snack_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and now - last < 4 * 3600:
        return None
    beans = _state_int(state, "beans", 0)
    paid = beans >= 10
    delta = {"satiety": 22 if paid else 14, "mood": 6 if paid else 2, "energy": 2, "stress": -2}
    if paid:
        delta["beans"] = -10
    event_key = f"world.snack:{int(now // (4 * 3600))}"
    if hasattr(store, "has_life_event") and store.has_life_event(config.scope, event_key=event_key):
        # W1：同一个 4 小时窗口只结算一次，之前是"每次都改状态、只记一次事件"。
        return None
    # W1：饥饿驱动的自主进食也走账本，cause 写清是哪条规则被触发。
    updated, _summary = apply_world_change(
        store,
        config.scope,
        state,
        delta,
        source="hunger_drive",
        cause=f"饱腹 {satiety} ≤ 30，自己补充食物",
        title="饿了，自己买了零食" if paid else "饿了，自己做了点吃的",
        event_key=event_key,
        location="home.kitchen",
        activity="吃零食" if paid else "简单做饭",
        payload={"source": "hunger_drive", "method": "paid_snack" if paid else "cooked_snack", "satiety": satiety},
    )
    next_metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else metadata)
    next_metadata["last_snack_at"] = float(now)
    updated["metadata"] = next_metadata
    return updated, event_key


def _update_drives(
    state: dict[str, Any],
    *,
    metadata: dict[str, Any],
    now: float,
    store: Any = None,
    scope: Any = None,
) -> None:
    """Derive hidden drives from the same state shown to the planner."""
    social_target = 35 + (_state_int(state, "stress", 0) // 2) + max(0, 65 - _state_int(state, "trust", 50)) // 3
    curiosity_target = 40 + _state_int(state, "mood", 70) // 4 + _state_int(state, "affinity_xp", 0) // 80
    social = _blend_drive(metadata.get("social_need"), social_target)
    curiosity = _blend_drive(metadata.get("curiosity"), curiosity_target)
    privacy_target = 35 + _state_int(state, "stress", 0) // 3
    comfort_target = 30 + _state_int(state, "beans", 0) // 8 - _state_int(state, "stress", 0) // 3
    privacy = max(0, min(100, max(_coerce_int(metadata.get("privacy_sensitivity"), 0), privacy_target)))
    comfort = max(0, min(100, _blend_drive(metadata.get("resource_comfort"), comfort_target)))
    # -1 表示"此前没有这个驱动"，首次初始化不算变化，不记账。
    before = {field: _coerce_int(metadata.get(field), -1) for field in HIDDEN_DRIVE_FIELDS}
    metadata["social_need"] = social
    metadata["curiosity"] = curiosity
    metadata["privacy_sensitivity"] = privacy
    metadata["resource_comfort"] = comfort
    metadata["drives_snapshot_v1"] = {"version": 1, "updated_at": now, "values": {"social_need": social, "curiosity": curiosity, "privacy_sensitivity": privacy, "resource_comfort": comfort}}
    if store is None or scope is None:
        return
    _record_drive_blend(
        store=store,
        scope=scope,
        state=state,
        metadata=metadata,
        before=before,
        values={"social_need": social, "curiosity": curiosity, "privacy_sensitivity": privacy, "resource_comfort": comfort},
        now=now,
    )


# 隐藏驱动一次 tick 只挪几个点，绝对值 < 3 的记录只会把时间线刷满，因此静默跳过。
DRIVE_BLEND_MIN_CHANGE = 3


def _record_drive_blend(
    *,
    store: Any,
    scope: Any,
    state: dict[str, Any],
    metadata: dict[str, Any],
    before: dict[str, int],
    values: dict[str, int],
    now: float,
) -> None:
    if not hasattr(store, "record_life_event"):
        return
    known = [field for field in HIDDEN_DRIVE_FIELDS if before.get(field, -1) >= 0]
    if not known:
        # 首次初始化：这些驱动此前不存在，没有什么"变化"可记，
        # 直接把当前值记成基线，后面从这儿开始算账。
        note_drive_baseline(metadata, {field: int(values.get(field, 0)) for field in HIDDEN_DRIVE_FIELDS})
        return
    stored = read_drive_baseline(metadata)
    if not stored:
        # 老库升级：驱动本来就有，只是没有基线。按混合前的值起算，
        # 这一次该记多少就记多少，不会凭空多出一笔巨差。
        note_drive_baseline(metadata, {field: before[field] for field in known})
        stored = read_drive_baseline(metadata)
    # 基线可能只覆盖一部分驱动（别的结算路径只记了自己动过的那几项），
    # 缺项按"没有待记账漂移"算，也就是混合前的值。
    baseline = {field: stored.get(field, before[field]) for field in known}
    # before/after 要能对上真实发生的那一步：把视图还原成"上次记账时"的驱动值，
    # 再让账本自己算截断后的差值，差额就是这段窗口里真正漂过去的点数。
    base_metadata = dict(metadata)
    for field in known:
        if field in baseline:
            base_metadata[field] = baseline[field]
    view = dict(state)
    view["metadata"] = base_metadata
    for field in known:
        label = RECAP_FIELD_LABELS.get(field, field)
        net = int(values.get(field, 0)) - baseline[field]
        if abs(net) < DRIVE_BLEND_MIN_CHANGE:
            continue
        _updated, summary = apply_world_change(
            store,
            scope,
            view,
            {field: net},
            source="drive_blend",
            cause=f"{label}从 {baseline[field]} 漂到 {values.get(field)}",
            title=f"{label}变化",
            event_key=f"drive:{field}:{int(now // 300)}",
            payload={"drive": field, "previous": baseline[field], "current": int(values.get(field, 0))},
        )
        if summary is not None:
            # 只有真记上了才推进基线；被 event_key 挡住说明这个 5 分钟桶里已经有了。
            note_drive_baseline(metadata, {field: int(values.get(field, 0))})


def _blend_drive(existing: Any, target: int) -> int:
    try:
        current = int(round(float(existing)))
    except (TypeError, ValueError):
        return max(0, min(100, int(target)))
    return max(0, min(100, int(round(current * 0.75 + target * 0.25))))


def _apply_delta(state: dict[str, Any], delta: dict[str, int]) -> None:
    metadata = dict(state.get("metadata") if isinstance(state.get("metadata"), dict) else {})
    hidden_changed = False
    for key, value in delta.items():
        if key in {"social_need", "curiosity", "privacy_sensitivity", "resource_comfort"}:
            metadata[key] = max(0, min(100, _state_int(metadata, key, 0) + int(value)))
            hidden_changed = True
            continue
        state[key] = max(0, min(100, _state_int(state, key, 0) + int(value)))
    if hidden_changed:
        state["metadata"] = metadata


def _state_int(state: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(round(float(state.get(key, default))))
    except (TypeError, ValueError):
        return int(default)


def reduce_world_turn(
    *,
    config: Any,
    store: Any,
    response: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply only explicit, low-risk first-person world actions from a reply.

    This reducer is deliberately deterministic. It adds no model call and ignores
    ambiguous prose, so streaming/TTS output remains plain speech.
    """
    body = _clean_text(response)
    if not body:
        return {"changed": False, "reason": "empty_response"}
    state = store.get_or_create_state(config.scope)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    previous = metadata.get("world_v2") if isinstance(metadata.get("world_v2"), dict) else {}
    if not previous:
        return {"changed": False, "reason": "world_not_initialized"}
    canon = load_world_canon(config)
    ts = float(now if now is not None else time.time())
    next_world = dict(previous)
    changes: dict[str, Any] = {}

    location_match = _explicit_location_from_reply(body, canon=canon)
    if location_match:
        location_key, location_label = location_match
        if _clean_text(previous.get("location_key")) != location_key:
            next_world["location_key"] = location_key
            next_world["location_label"] = location_label
            next_world["activity_key"] = "conversation"
            next_world["activity_label"] = f"在{location_label}待着"
            next_world["since"] = ts
            next_world["source"] = "conversation"
            changes["location"] = {"key": location_key, "label": location_label}

    object_states = dict(previous.get("object_states")) if isinstance(previous.get("object_states"), dict) else {}
    for object_key, item in canon.objects.items():
        label = _clean_text(item.get("label") or "")
        if not label or label not in body:
            continue
        new_state = _explicit_object_state(body, label=label)
        if new_state and _clean_text(object_states.get(object_key)) != new_state:
            object_states[object_key] = new_state
            changes.setdefault("objects", {})[object_key] = new_state
    next_world["object_states"] = object_states

    if not changes:
        return {"changed": False, "reason": "no_explicit_action"}
    updated = dict(state)
    next_metadata = dict(metadata)
    next_metadata["world_v2"] = next_world
    if "location" in changes:
        next_metadata["current_location"] = next_world["location_key"]
        next_metadata["location_label"] = next_world["location_label"]
        next_metadata["current_activity"] = next_world["activity_label"]
        next_metadata["world_current_source"] = "conversation"
        next_metadata["world_last_updated_at"] = ts
        updated["scene"] = next_world["location_key"]
    updated["metadata"] = next_metadata
    store.save_state(config.scope, updated)
    store.record_life_event(
        config.scope,
        event_type="world.dialogue_action",
        event_key=f"{int(ts)}:dialogue",
        title="Aura 的对话改变了当前场景",
        description=body[:160],
        location=_clean_text(next_world.get("location_key")),
        activity=_clean_text(next_world.get("activity_label")),
        visibility="private",
        delta=changes,
        payload={"source": "deterministic_reply_reducer"},
    )
    return {"changed": True, "changes": changes}


def generate_day_plan(
    *,
    city: str,
    day_key: str,
    state: dict[str, Any] | None = None,
    weather: dict[str, Any] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    seed = int(hashlib.sha256(f"{WORLD_SCHEMA_VERSION}:{city}:{day_key}".encode("utf-8")).hexdigest()[:12], 16)
    rng = random.Random(seed)
    energy = _coerce_int((state or {}).get("energy"), 70)
    mood = _coerce_int((state or {}).get("mood"), 80)
    satiety = _coerce_int((state or {}).get("satiety"), 80)
    base_date = dt.date.fromisoformat(day_key)
    weather = dict(weather or {})
    weather_category = _clean_text(weather.get("category") or "unknown")
    outing_axis = _choose_outing_axis(rng, energy=energy, mood=mood, weather=weather)
    # W2：这些随机判定的结果必须原样落进 payload.why，同种子同输入才可复现。
    # 注意保持原有 rng 消耗顺序，任何额外 draw 都会改变日程结果。
    morning_roll = None
    morning_on_balcony = False
    if weather_category in {"clear", "cloudy", "mild", "unknown"} and energy >= 45:
        morning_roll = rng.random()
        morning_on_balcony = morning_roll < 0.28
    lunch_roll = None
    lunch_out = False
    if energy >= 48 and satiety <= 88 and weather_category not in {"rain", "storm", "snow", "heat", "cold"}:
        lunch_roll = rng.random()
        lunch_out = lunch_roll > 0.35
    afternoon_out = outing_axis["location_key"] != "home"
    afternoon_why = dict(outing_axis.get("why") or {})
    slots = [
        _slot(
            base_date,
            slot_key="wake",
            minutes=7 * 60 + 35 + rng.randint(-25, 35),
            duration=45,
            activity_type="morning",
            title=_pick(rng, ["醒来整理一下", "慢慢起床", "洗漱换好衣服"]),
            location_key="home.bedroom",
            location_label="卧室",
            activity_label="刚起床",
            life_axis="home",
            city=city,
            why={
                "rule": "anchor",
                "inputs": {"energy": energy, "mood": mood},
                "text": "每日锚点：起床时间只做小幅抖动",
            },
        ),
        _slot(
            base_date,
            slot_key="breakfast",
            minutes=8 * 60 + 20 + rng.randint(-20, 25),
            duration=35,
            activity_type="meal",
            title=_pick(rng, ["吃点早饭", "简单吃早饭", "把早饭解决掉"]),
            location_key="home.kitchen",
            location_label="厨房",
            activity_label="吃早饭",
            life_axis="meal",
            city=city,
            why={
                "rule": "anchor",
                "inputs": {"satiety": satiety},
                "text": "每日锚点：早饭在家吃，顺手把昨晚的厨房收拾一下",
            },
        ),
        _slot(
            base_date,
            slot_key="morning_focus",
            minutes=10 * 60 + rng.randint(-35, 30),
            duration=105 + rng.randint(-15, 25),
            activity_type="quiet",
            title=_pick(rng, ["上午安静处理点事情", "上午在屋里待一会儿", "把上午留给安静的事"]),
            location_key="home.balcony" if morning_on_balcony else "home.study",
            location_label="阳台" if morning_on_balcony else "书房",
            activity_label=(
                _pick(rng, ["晒晒太阳", "看看外面的天气", "在阳台坐一会儿"])
                if morning_on_balcony
                else _pick(rng, ["整理东西", "看点内容", "安静待着"])
            ),
            life_axis="quiet",
            city=city,
            why={
                "rule": "morning_focus_balcony" if morning_on_balcony else "morning_focus_study",
                "inputs": {
                    "energy": energy,
                    "weather_category": weather_category,
                    "seed_roll": morning_roll,
                },
                "text": (
                    f"天气 {_weather_label(weather_category)}、体力 {energy} ≥ 45，日程种子抽中了上午在阳台待一会儿"
                    if morning_on_balcony
                    else f"天气 {_weather_label(weather_category)}、体力 {energy}，上午留在书房做安静的事"
                ),
            },
        ),
        _slot(
            base_date,
            slot_key="lunch",
            minutes=12 * 60 + 15 + rng.randint(-25, 45),
            duration=55,
            activity_type="meal",
            title=_pick(rng, ["吃午饭", "午饭时间", "找点东西吃"]),
            location_key="outside.neighborhood" if lunch_out else "home.kitchen",
            location_label="住处附近" if lunch_out else "厨房",
            activity_label="吃午饭",
            life_axis="meal",
            city=city,
            why={
                "rule": "anchor",
                "inputs": {
                    "energy": energy,
                    "satiety": satiety,
                    "weather_category": weather_category,
                    "seed_roll": lunch_roll,
                    "lunch_out": lunch_out,
                },
                "text": (
                    f"每日锚点：午饭外出（体力 {energy} ≥ 48、饱腹 {satiety} ≤ 88、天气 {_weather_label(weather_category)}）"
                    if lunch_out
                    else f"每日锚点：午饭在家做（体力 {energy}、饱腹 {satiety}、天气 {_weather_label(weather_category)}）"
                ),
            },
        ),
        _slot(
            base_date,
            slot_key="afternoon",
            minutes=15 * 60 + rng.randint(-30, 60),
            duration=80 + rng.randint(-10, 35),
            activity_type=outing_axis["activity_type"],
            title=outing_axis["title"],
            location_key=outing_axis["location_key"],
            location_label=outing_axis["location_label"],
            activity_label=outing_axis["activity_label"],
            life_axis=outing_axis["life_axis"],
            city=city,
            why=afternoon_why,
        ),
        _slot(
            base_date,
            slot_key="evening",
            minutes=18 * 60 + 10 + rng.randint(-20, 45),
            duration=75,
            activity_type="home" if afternoon_out else "quiet",
            title=_pick(rng, ["回到家里缓一会儿", "傍晚回家休息"]) if afternoon_out else _pick(rng, ["傍晚在家放松", "傍晚收拾一下"]),
            location_key="home.living_room",
            location_label="客厅",
            activity_label="休息",
            life_axis="home",
            city=city,
            extra={"requires_prior_outing": afternoon_out},
            why={
                "rule": "evening_after_outing" if afternoon_out else "evening_at_home",
                "inputs": {
                    "afternoon_out": afternoon_out,
                    "afternoon_location": _clean_text(outing_axis.get("location_key")),
                },
                "text": (
                    (
                        f"下午在{_clean_text(outing_axis.get('location_label'))}待着，傍晚回客厅缓一会儿"
                        if str(outing_axis.get("location_key") or "").startswith("home.")
                        else f"下午去了{_clean_text(outing_axis.get('location_label'))}，傍晚回客厅缓一会儿"
                    )
                    if afternoon_out
                    else "下午没出门，傍晚就在家放松"
                ),
            },
        ),
        _slot(
            base_date,
            slot_key="dinner",
            minutes=19 * 60 + 10 + rng.randint(-25, 35),
            duration=45,
            activity_type="meal",
            title=_pick(rng, ["吃晚饭", "晚饭", "晚上吃点东西"]),
            location_key="home.kitchen",
            location_label="厨房",
            activity_label="吃晚饭",
            life_axis="meal",
            city=city,
            why={
                "rule": "anchor",
                "inputs": {"satiety": satiety, "energy": energy},
                "text": "每日锚点：晚饭在家吃，饭后碗留到睡前再洗",
            },
        ),
        _slot(
            base_date,
            slot_key="night_settle",
            minutes=22 * 60 + 35 + rng.randint(-30, 45),
            duration=80,
            activity_type="rest",
            title=_pick(rng, ["睡前整理一天", "晚上慢慢收尾", "准备休息"]),
            location_key="home.bedroom",
            location_label="卧室",
            activity_label="睡前整理",
            life_axis="rest",
            city=city,
            why={
                "rule": "anchor",
                "inputs": {"energy": energy, "stress": _coerce_int((state or {}).get("stress"), 0)},
                "text": "每日锚点：睡前把一天收尾，准备休息",
            },
        ),
    ]
    # Five daily anchors remain stable (wake, three meals, night settle).
    # The three existing focus/outing/evening entries plus this personal
    # window form the minimum four dynamic slots. State and weather can add
    # up to four more, so the day has a 4-8 dynamic rhythm rather than a fixed
    # checklist.
    personal_in_study_roll = rng.random()
    personal_in_study = personal_in_study_roll < 0.55
    slots.append(
        _slot(
            base_date,
            slot_key="personal_window",
            minutes=21 * 60 + rng.randint(-20, 25),
            duration=35 + rng.randint(-10, 15),
            activity_type="quiet",
            title=_pick(rng, ["留一点自己的时间", "睡前做点喜欢的事", "安静收拾一下心情"]),
            location_key="home.study" if personal_in_study else "home.living_room",
            location_label="书房" if personal_in_study else "客厅",
            activity_label=_pick(rng, ["看点喜欢的内容", "听一会儿音乐", "安静待着"]),
            life_axis="personal",
            city=city,
            why={
                "rule": "personal_window_study" if personal_in_study else "personal_window_living_room",
                "inputs": {"seed_roll": personal_in_study_roll},
                "text": (
                    "今天的日程种子抽中了睡前在书房留一点自己的时间"
                    if personal_in_study
                    else "今天的日程种子抽中了睡前在客厅留一点自己的时间"
                ),
            },
        )
    )
    metadata = (state or {}).get("metadata") if isinstance((state or {}).get("metadata"), dict) else {}
    social_need = _coerce_int(metadata.get("social_need"), 40)
    # 短路的 or：satiety 已经很低时不再抽种子，保持和原来完全一致的 rng 消耗。
    snack_roll = None
    snack_triggered = satiety <= 45
    if not snack_triggered:
        snack_roll = rng.random()
        snack_triggered = snack_roll < 0.25
    if snack_triggered:
        slots.append(
            _slot(
                base_date,
                slot_key="snack",
                minutes=16 * 60 + rng.randint(-30, 45),
                duration=20,
                activity_type="meal",
                title=_pick(rng, ["下午找点零食", "补充一点能量", "吃点小东西"]),
                location_key="home.kitchen",
                location_label="厨房",
                activity_label="吃点零食",
                life_axis="snack",
                city=city,
                why={
                    "rule": "snack_low_satiety" if snack_roll is None else "snack_seed_roll",
                    "inputs": {"satiety": satiety, "seed_roll": snack_roll},
                    "text": (
                        f"饱腹 {satiety} ≤ 45，下午加一顿零食"
                        if snack_roll is None
                        else f"今天的日程种子抽中了下午加一顿零食（25% 概率，饱腹 {satiety}）"
                    ),
                },
            )
        )
    recharge_roll = None
    recharge_triggered = energy < 48
    if not recharge_triggered:
        recharge_roll = rng.random()
        recharge_triggered = recharge_roll < 0.22
    if recharge_triggered:
        slots.append(
            _slot(
                base_date,
                slot_key="recharge",
                minutes=17 * 60 + rng.randint(-25, 35),
                duration=45,
                activity_type="rest",
                title=_pick(rng, ["找个时间缓一缓", "下午补一点体力"]),
                location_key="home.living_room",
                location_label="客厅",
                activity_label="短暂休息",
                life_axis="recovery",
                city=city,
                why={
                    "rule": "recharge_low_energy" if recharge_roll is None else "recharge_seed_roll",
                    "inputs": {"energy": energy, "seed_roll": recharge_roll},
                    "text": (
                        f"体力 {energy} < 48，下午补一点体力"
                        if recharge_roll is None
                        else f"今天的日程种子抽中了下午缓一缓（22% 概率，体力 {energy}）"
                    ),
                },
            )
        )
    if social_need >= 65 and afternoon_out and weather_category not in {"rain", "storm", "snow", "heat"}:
        slots.append(
            _slot(
                base_date,
                slot_key="social_pause",
                minutes=20 * 60 + rng.randint(-20, 30),
                duration=45,
                activity_type="browse",
                title=_pick(rng, ["晚上在外面坐一会儿", "找个热闹一点的地方待会儿"]),
                location_key="outside.mall",
                location_label="附近商场",
                activity_label="在人多的地方待一会儿",
                life_axis="social",
                city=city,
                why={
                    "rule": "social_pause",
                    "inputs": {
                        "social_need": social_need,
                        "afternoon_out": afternoon_out,
                        "weather_category": weather_category,
                    },
                    "text": f"社交需求 {social_need} ≥ 65 且下午要出门，晚上去人多的地方待一会儿",
                },
            )
        )
    curiosity = _coerce_int(metadata.get("curiosity"), 40)
    curiosity_roll = None
    curiosity_triggered = curiosity >= 65
    if not curiosity_triggered and mood >= 78:
        curiosity_roll = rng.random()
        curiosity_triggered = curiosity_roll < 0.28
    if curiosity_triggered:
        slots.append(
            _slot(
                base_date,
                slot_key="curiosity_project",
                minutes=14 * 60 + rng.randint(-25, 35),
                duration=45 + rng.randint(-10, 20),
                activity_type="quiet",
                title=_pick(rng, ["研究一点感兴趣的东西", "做个小小的兴趣项目", "找点新鲜内容看看"]),
                location_key="home.study",
                location_label="书房",
                activity_label="满足一点好奇心",
                life_axis="curiosity",
                city=city,
                why={
                    "rule": "curiosity_high" if curiosity_roll is None else "curiosity_seed_roll",
                    "inputs": {"curiosity": curiosity, "mood": mood, "seed_roll": curiosity_roll},
                    "text": (
                        f"好奇心 {curiosity} ≥ 65，下午研究点感兴趣的东西"
                        if curiosity_roll is None
                        else f"心情 {mood} ≥ 78，今天的日程种子抽中了兴趣项目（28% 概率）"
                    ),
                },
            )
        )
    for item in slots:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        payload["weather_signature"] = _clean_text(weather.get("signature") or "unknown")
        payload["weather_influence"] = _clean_text(weather.get("plan_influence") or "none")
        item["expected_delta"] = _preview_expected_delta(item)
        item["payload"] = payload
    slots.sort(key=lambda item: float(item.get("scheduled_at") or 0))
    return _with_status(slots, now=float(now if now is not None else time.time()))


def _world_weather_snapshot(snapshot: dict[str, Any] | None, *, day_key: str) -> dict[str, Any]:
    raw = dict(snapshot or {})
    if str(raw.get("status") or "").strip() != "fresh":
        return {}
    forecast_date = _clean_text(raw.get("forecast_date") or "")
    forecast_is_today = not forecast_date or forecast_date == day_key
    current_condition = _clean_text(raw.get("condition") or "")
    forecast_condition = _clean_text(raw.get("forecast_condition") or "") if forecast_is_today else ""
    temperature = _coerce_float(raw.get("temperature"))
    temperature_min = _coerce_float(raw.get("temperature_min")) if forecast_is_today else None
    temperature_max = _coerce_float(raw.get("temperature_max")) if forecast_is_today else None
    humidity = _coerce_float(raw.get("humidity"))
    precipitation = _coerce_float(raw.get("precipitation_probability")) if forecast_is_today else None

    forecast_weather = forecast_condition or current_condition
    forecast_precipitation_likely = precipitation is None or precipitation >= 60
    if "雪" in current_condition or ("雪" in forecast_condition and forecast_precipitation_likely):
        category = "snow"
    elif "雨" in current_condition or (
        "雨" in forecast_condition and forecast_precipitation_likely
    ) or (precipitation is not None and precipitation >= 60):
        category = "rain"
    elif temperature_max is not None and temperature_max >= 33:
        category = "heat"
    elif temperature_max is not None and temperature_max <= 8:
        category = "cold"
    elif forecast_weather == "晴":
        category = "clear"
    elif forecast_weather in {"多云", "阴", "雾"}:
        category = "cloudy"
    else:
        category = "mild"

    plan_influence = {
        "rain": "indoor_flexible",
        "snow": "indoor_flexible",
        "heat": "avoid_peak_heat",
        "cold": "prefer_indoor",
        "clear": "normal_outing",
        "cloudy": "normal_outing",
        "mild": "normal_outing",
    }.get(category, "none")
    return {
        "status": "fresh",
        "city": _clean_text(raw.get("city") or ""),
        "condition": current_condition,
        "temperature": temperature,
        "humidity": humidity,
        "display": _clean_text(raw.get("display") or ""),
        "source": _clean_text(raw.get("source") or ""),
        "observed_at": _clean_text(raw.get("observed_at") or ""),
        "forecast_date": forecast_date,
        "forecast_condition": forecast_condition,
        "temperature_min": temperature_min,
        "temperature_max": temperature_max,
        "precipitation_probability": precipitation,
        "category": category,
        "signature": f"{day_key}:{category}",
        "plan_influence": plan_influence,
    }


def _apply_weather_influence(
    *,
    config: Any,
    store: Any,
    state: dict[str, Any],
    weather: dict[str, Any],
    day_key: str,
    now: float,
    persist: bool = True,
) -> dict[str, Any]:
    if not weather:
        return state
    signature = _clean_text(weather.get("signature") or f"{day_key}:unknown")
    metadata = dict(state.get("metadata")) if isinstance(state.get("metadata"), dict) else {}
    applied = [str(item) for item in metadata.get("world_weather_applied_signatures", []) if str(item).strip()]
    if signature in applied:
        return state

    category = _clean_text(weather.get("category") or "mild")
    mood_delta = {"clear": 1, "rain": -1, "snow": -2, "heat": -1, "cold": -1}.get(category, 0)
    energy_delta = {"rain": -1, "snow": -1, "heat": -2, "cold": -1}.get(category, 0)
    stress_delta = 1 if category in {"snow"} else 0
    humidity = _coerce_float(weather.get("humidity"))
    if humidity is not None and humidity >= 85 and category not in {"rain", "snow"}:
        energy_delta -= 1

    updated = dict(state)
    delta = {"mood": mood_delta, "energy": energy_delta, "stress": stress_delta}
    # 天气分类是内部枚举，写进 cause/title 前翻成中文，人话才好读。
    label = _weather_label(category)
    if persist:
        # W1：天气影响也走账本；speculative 回合不落库，也不该留下 event_key，
        # 否则真正那一轮会因为"已结算"而永远吃不到这次天气影响。
        updated, _summary = apply_world_change(
            store,
            config.scope,
            updated,
            delta,
            source="weather",
            cause=f"天气变成{label}，环境和体感影响了状态",
            title=f"天气环境更新为{label}",
            event_key=signature,
            payload={
                "weather_source": weather.get("source") or "weather_cache",
                "weather": _compact_world_weather(weather),
            },
        )
    else:
        _apply_delta(updated, delta)
    metadata["world_weather_applied_signatures"] = (applied + [signature])[-10:]
    metadata["world_weather_last_influence"] = {
        "signature": signature,
        "category": category,
        "mood_delta": mood_delta,
        "energy_delta": energy_delta,
        "stress_delta": stress_delta,
        "applied_at": float(now),
    }
    updated["metadata"] = metadata
    if persist:
        store.save_state(config.scope, updated)
    return updated


def _plan_matches_weather(plan: list[dict[str, Any]], weather: dict[str, Any]) -> bool:
    expected = _clean_text(weather.get("signature") or "")
    if not plan or not expected:
        return False
    signatures = {
        _clean_text((item.get("payload") or {}).get("weather_signature") or "")
        for item in plan
        if isinstance(item.get("payload"), dict)
    }
    signatures.discard("")
    return signatures == {expected}


def _compact_world_weather(weather: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(weather or {})
    keys = (
        "status",
        "city",
        "condition",
        "temperature",
        "humidity",
        "display",
        "source",
        "observed_at",
        "forecast_date",
        "forecast_condition",
        "temperature_min",
        "temperature_max",
        "precipitation_probability",
        "category",
        "signature",
        "plan_influence",
    )
    return {key: raw.get(key) for key in keys if raw.get(key) not in {None, ""}}


def _world_weather_prompt(weather: dict[str, Any]) -> str:
    if not weather:
        return ""
    current = _clean_text(weather.get("display") or "")
    forecast_parts: list[str] = []
    if weather.get("forecast_condition"):
        forecast_parts.append(str(weather["forecast_condition"]))
    if weather.get("temperature_min") is not None and weather.get("temperature_max") is not None:
        forecast_parts.append(f"{weather['temperature_min']}至{weather['temperature_max']}度")
    if weather.get("precipitation_probability") is not None:
        forecast_parts.append(f"最大降水概率{weather['precipitation_probability']}%")
    parts = []
    if current:
        parts.append(f"当前实况={current}")
    if forecast_parts:
        parts.append("当日预报=" + "，".join(forecast_parts))
    parts.append(f"计划影响={weather.get('plan_influence') or 'none'}")
    if weather.get("source"):
        parts.append(f"来源={weather['source']}")
    return "；".join(parts) + "。"


def render_world_prompt(snapshot: dict[str, Any] | None) -> str:
    if not snapshot or not snapshot.get("enabled"):
        return ""
    policy = snapshot.get("mention_policy") if isinstance(snapshot.get("mention_policy"), dict) else {}
    current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
    scene = snapshot.get("scene") if isinstance(snapshot.get("scene"), dict) else {}
    recent_events = snapshot.get("recent_events") if isinstance(snapshot.get("recent_events"), list) else []
    plan = snapshot.get("today_plan") if isinstance(snapshot.get("today_plan"), list) else []
    next_plan = snapshot.get("next_plan") if isinstance(snapshot.get("next_plan"), dict) else None
    weather = snapshot.get("weather") if isinstance(snapshot.get("weather"), dict) else {}
    allow_location = bool(policy.get("allow_location"))
    allow_activity = bool(policy.get("allow_activity"))
    allow_plan = bool(policy.get("allow_plan"))
    location_precision = str(policy.get("location_precision") or "none")
    persona_name = _clean_text(snapshot.get("persona_name") or "Aura")
    lines = [
        f"世界模型=启用；{persona_name} 的内部生活世界所在城市={snapshot.get('city') or '未设置'}。",
        "对外表达权限="
        + f"location:{allow_location} activity:{allow_activity} plan:{allow_plan}；"
        + f"precision={location_precision}；"
        + f"reason={policy.get('reason') or ORDINARY_REPLY_REASON}。",
    ]
    scene_text = _scene_prompt_text(scene)
    if scene_text:
        lines.append("内部当前场景：" + scene_text)
        lines.append(
            "内部场景是本轮事实依据，可以自然影响注意力、语气和动作选择；"
            "不要为了展示世界观而强行报位置或罗列陈设，也不要编造场景中没有的物品。"
        )
    weather_text = _world_weather_prompt(weather)
    if weather_text:
        lines.append("内部天气环境：" + weather_text)
        lines.append(
            "天气可以影响心情、体感和活动选择，但只能使用这里列出的当前实况与当日预报；"
            "不得据此补写刚下过雨、马上会下雨或其他未提供的天气经历。"
        )
    event_lines = [_recent_event_text(item) for item in recent_events[-3:]]
    event_lines = [item for item in event_lines if item]
    if event_lines:
        lines.append("近期世界变化：\n" + "\n".join(f"- {item}" for item in event_lines))
    recap_lines = [
        _clean_text(item)
        for item in (snapshot.get("offline_recap") if isinstance(snapshot.get("offline_recap"), list) else [])
    ]
    recap_lines = [item for item in recap_lines if item]
    if recap_lines:
        hours = snapshot.get("offline_recap_hours") or 1
        lines.append(
            f"你不在的这段时间（距上次聊天约 {_format_hours(hours)} 小时）。"
            "只能讲以下真实发生过的事，不要编造：\n"
            + "\n".join(f"- {item}" for item in recap_lines)
        )
    if allow_location or allow_activity:
        current_text = _current_text(current, expose_location=allow_location, expose_activity=allow_activity)
        if current_text:
            lines.append("当前状态：" + current_text)
        if allow_activity and not allow_location:
            lines.append("本轮只允许透露当前活动或模糊状态；不要补充具体地点、在家、商场、店铺、路线或食物。")
    else:
        lines.append(
            "用户没有直接追问位置或行程；不要生硬汇报具体地址、店铺和完整日程。"
            "场景相关细节只有与当前话题自然相关时才可轻带一句。"
        )
    if allow_plan:
        plan_lines = [_plan_line(item, expose_location=allow_location) for item in plan[:10]]
        if plan_lines:
            lines.append("今日计划：\n" + "\n".join(f"- {item}" for item in plan_lines if item))
    elif next_plan and policy.get("reason") != "relationship_boundary":
        lines.append("如果用户追问今天安排，可以优先用当前/下一项计划；未被问到时不要主动展开。")
    if policy.get("reason") == "relationship_boundary":
        lines.append("用户在问位置/行程，但当前关系边界不适合透露具体位置；可以自然、简短、模糊地回答，不要像客服式拒绝。")
    if policy.get("intent") in {"weather", "weather_advice"} and policy.get("subject_entity") == "aura":
        lines.append("天气回答必须优先使用本地天气缓存；没有 fresh 缓存时承认暂时没有实时天气，不要编造。")
    return "\n".join(line for line in lines if line).strip()


def world_debug_event(snapshot: dict[str, Any]) -> dict[str, Any]:
    policy = snapshot.get("mention_policy") if isinstance(snapshot.get("mention_policy"), dict) else {}
    return {
        "schema": WORLD_SCHEMA_VERSION,
        "enabled": bool(snapshot.get("enabled")),
        "available": bool(snapshot.get("available")),
        "city": snapshot.get("city"),
        "day_key": snapshot.get("day_key"),
        "current": snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {},
        "weather": snapshot.get("weather") if isinstance(snapshot.get("weather"), dict) else {},
        "next_plan": snapshot.get("next_plan") if isinstance(snapshot.get("next_plan"), dict) else None,
        "mention_policy": policy,
        "debug": snapshot.get("debug") if isinstance(snapshot.get("debug"), dict) else {},
    }


def _choose_outing_axis(
    rng: random.Random,
    *,
    energy: int,
    mood: int,
    weather: dict[str, Any] | None = None,
) -> dict[str, Any]:
    category = _clean_text((weather or {}).get("category") or "unknown")
    # W2：每个候选都带上"为什么是它"，inputs 只放真正参与判断的状态值。
    base_inputs = {"energy": energy, "mood": mood, "weather_category": category}
    if category in {"rain", "storm", "snow"}:
        return {
            "activity_type": "quiet",
            "title": "下午先留在家里，等天气稳一点",
            "location_key": "home.study",
            "location_label": "书房",
            "activity_label": "在屋里处理安静的事",
            "life_axis": "weather_shelter",
            "why": {
                "rule": "weather_shelter",
                "inputs": base_inputs,
                "text": f"天气是{_weather_label(category)}，下午先留在家里，等天气稳一点",
            },
        }
    if category == "heat":
        return {
            "activity_type": "quiet",
            "title": "避开最热的时候，在屋里待着",
            "location_key": "home.study",
            "location_label": "书房",
            "activity_label": "避暑休息",
            "life_axis": "weather_heat",
            "why": {
                "rule": "weather_heat",
                "inputs": base_inputs,
                "text": f"天气预报是{_weather_label(category)}，下午避开最热的时候在屋里待着",
            },
        }
    if category == "cold":
        return {
            "activity_type": "home",
            "title": "天气偏冷，下午在家做点事",
            "location_key": "home.living_room",
            "location_label": "客厅",
            "activity_label": "在屋里活动",
            "life_axis": "weather_cold",
            "why": {
                "rule": "weather_cold",
                "inputs": base_inputs,
                "text": f"天气是{_weather_label(category)}，下午在家做点事，不往外跑",
            },
        }
    if energy < 34:
        return {
            "activity_type": "rest",
            "title": _pick(rng, ["下午在家休息", "下午不出门，缓一缓"]),
            "location_key": "home.living_room",
            "location_label": "客厅",
            "activity_label": "休息",
            "life_axis": "rest",
            "why": {
                "rule": "low_energy_rest",
                "inputs": base_inputs,
                "text": f"体力 {energy} < 34，下午不出门，在家休息",
            },
        }
    options = [
        {
            "activity_type": "walk",
            "title": _pick(rng, ["去公园走一小圈", "去公园透透气"]),
            "location_key": "outside.park",
            "location_label": "附近公园",
            "activity_label": "在公园散步",
            "life_axis": "breath",
            "why": {
                "rule": "axis_park",
                "inputs": base_inputs,
                "text": "候选外出轴之一：去公园散步",
            },
        },
        {
            "activity_type": "quiet",
            "title": _pick(rng, ["找个安静地方待会儿", "下午去附近坐一会儿"]),
            "location_key": "outside.cafe",
            "location_label": "附近咖啡店",
            "activity_label": "安静停留",
            "life_axis": "quiet",
            "why": {
                "rule": "axis_cafe",
                "inputs": base_inputs,
                "text": "候选外出轴之一：找个安静地方坐一会儿",
            },
        },
        {
            "activity_type": "errand",
            "title": _pick(rng, ["顺路买点日用品", "去附近补点东西"]),
            "location_key": "outside.shop",
            "location_label": "社区小店",
            "activity_label": "日常购物",
            "life_axis": "daily_shopping",
            "why": {
                "rule": "axis_shop",
                "inputs": base_inputs,
                "text": "候选外出轴之一：顺路买点日用品",
            },
        },
    ]
    if mood >= 78 and energy >= 58:
        options.append(
            {
                "activity_type": "browse",
                "title": _pick(rng, ["去附近商场逛一会儿", "下午随便逛逛"]),
                "location_key": "outside.mall",
                "location_label": "附近商场",
                "activity_label": "在商场逛逛",
                "life_axis": "browse",
                "why": {
                    "rule": "axis_mall",
                    "inputs": {**base_inputs, "mood_gate": 78, "energy_gate": 58},
                    "text": f"心情 {mood} ≥ 78 且体力 {energy} ≥ 58，把逛商场加进了候选",
                },
            }
        )
    if mood >= 65 and energy >= 50:
        options.append(
            {
                "activity_type": "walk",
                "title": _pick(rng, ["去河边走走", "沿着河岸吹会儿风"]),
                "location_key": "outside.riverside",
                "location_label": "河岸步道",
                "activity_label": "在河边散步",
                "life_axis": "riverside_walk",
                "why": {
                    "rule": "axis_riverside",
                    "inputs": {**base_inputs, "mood_gate": 65, "energy_gate": 50},
                    "text": f"心情 {mood} ≥ 65 且体力 {energy} ≥ 50，把河边散步加进了候选",
                },
            }
        )
    chosen = dict(rng.choice(options))
    # 记下抽中的是第几个候选，保证同种子同输入可复现。
    chosen_index = next(
        (index for index, item in enumerate(options) if item.get("location_key") == chosen.get("location_key")),
        0,
    )
    why = dict(chosen.get("why") or {})
    inputs = dict(why.get("inputs") if isinstance(why.get("inputs"), dict) else base_inputs)
    inputs["seed_roll"] = chosen_index
    inputs["candidate_count"] = len(options)
    why["rule"] = str(why.get("rule") or "axis_random")
    why["inputs"] = inputs
    why["text"] = f"今天的日程种子抽中了第 {chosen_index + 1} 个外出候选：{_clean_text(why.get('text'))}"
    chosen["why"] = why
    return chosen


def _slot(
    base_date: dt.date,
    *,
    slot_key: str,
    minutes: int,
    duration: int,
    activity_type: str,
    title: str,
    location_key: str,
    location_label: str,
    activity_label: str,
    life_axis: str,
    city: str,
    extra: dict[str, Any] | None = None,
    why: dict[str, Any] | None = None,
) -> dict[str, Any]:
    minutes = max(0, min(23 * 60 + 55, int(minutes)))
    # 槽位时间是"城市当地时间"，转 epoch 必须带城市时区，不能吃容器时区。
    start = dt.datetime.combine(
        base_date,
        dt.time(hour=minutes // 60, minute=minutes % 60),
        tzinfo=resolve_city_tzinfo(city),
    )
    payload = {
        "world_schema": WORLD_SCHEMA_VERSION,
        "duration_minutes": max(15, int(duration)),
        "location_key": location_key,
        "location_label": location_label,
        "activity_label": activity_label,
        "life_axis": life_axis,
        "city": city,
        "source": "aura_world_model",
    }
    if extra:
        payload.update(extra)
    # W2：每个槽位都写清它是被哪条规则、哪几个状态值选中的。
    payload["why"] = dict(why or {"rule": "unspecified", "inputs": {}, "text": ""})
    return {
        "plan_date": base_date.isoformat(),
        "slot_key": slot_key,
        "scheduled_at": start.timestamp(),
        "activity_type": activity_type,
        "title": title,
        "location": location_label,
        "should_post": 0,
        "status": "pending",
        "expected_delta": _preview_expected_delta({"activity_type": activity_type, "slot_key": slot_key}),
        "payload": payload,
    }


def _preview_expected_delta(item: dict[str, Any]) -> dict[str, int]:
    activity_type = _clean_text(item.get("activity_type") or "")
    slot_key = _clean_text(item.get("slot_key") or "")
    if activity_type == "meal" or slot_key in {"breakfast", "lunch", "dinner", "snack"}:
        return {"satiety": 30 if slot_key == "snack" else 38, "energy": 4, "mood": 3, "stress": -2}
    if activity_type == "rest" or slot_key == "recharge":
        return {"energy": 10, "stress": -5}
    if activity_type in {"walk", "browse", "errand"}:
        return {"energy": -7, "mood": 4, "stress": -3}
    if activity_type == "care":
        return {"energy": -3, "mood": 1, "stress": -3}
    if activity_type in {"quiet", "home", "morning", "social"}:
        return {"energy": -3, "stress": -1}
    return {}


def _with_status(plan: list[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    out = []
    for item in sorted(plan, key=lambda row: float(row.get("scheduled_at") or 0)):
        row = dict(item)
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        duration = max(15, _coerce_int(payload.get("duration_minutes"), 45))
        start = float(row.get("scheduled_at") or 0)
        end = start + duration * 60
        if now < start:
            status = "pending"
        elif now <= end:
            status = "active"
        else:
            status = "done"
        row["status"] = status
        if status == "done" and not row.get("executed_at"):
            row["executed_at"] = end
        out.append(row)
    return out


def _current_from_plan(plan: list[dict[str, Any]], *, now: float, city: str) -> dict[str, Any]:
    if not plan:
        return {
            "location_key": "home",
            "location_label": "家里",
            "activity_key": "idle",
            "activity_label": "安静待着",
            "status": "fallback",
            "city": city,
            "source": "fallback_home",
            "available": True,
        }
    active = [item for item in plan if item.get("status") == "active"]
    if active:
        # Plans may intentionally overlap (for example wake-up and breakfast).
        # Once a newer slot starts it becomes the current activity immediately.
        return _current_from_slot(active[-1], city=city, source="active_plan")
    past = [item for item in plan if float(item.get("scheduled_at") or 0) <= now]
    if past:
        previous = past[-1]
        if str(previous.get("status") or "") == "done":
            if str(previous.get("slot_key") or "") == "night_settle":
                return _sleep_current(city=city, source="overnight_sleep")
            future = [item for item in plan if float(item.get("scheduled_at") or 0) > now]
            return _interlude_from_slots(previous, future[0] if future else None, city=city)
        return _current_from_slot(previous, city=city, source="nearest_plan")
    first = plan[0]
    if str(first.get("slot_key") or "") == "wake":
        return _sleep_current(city=city, source="overnight_sleep")
    return _current_from_slot(plan[0], city=city, source="next_plan")


def _sleep_current(*, city: str, source: str) -> dict[str, Any]:
    return {
        "location_key": "home.bedroom",
        "location_label": "卧室",
        "activity_key": "rest",
        "activity_label": "睡觉",
        "title": "还在睡觉",
        "slot_key": "sleep",
        "status": "active",
        "city": city,
        "source": source,
        "available": True,
    }


def _interlude_from_slots(
    previous: dict[str, Any],
    upcoming: dict[str, Any] | None,
    *,
    city: str,
) -> dict[str, Any]:
    previous_payload = previous.get("payload") if isinstance(previous.get("payload"), dict) else {}
    previous_type = _clean_text(previous.get("activity_type") or "")
    previous_slot = _clean_text(previous.get("slot_key") or "")
    upcoming_slot = _clean_text((upcoming or {}).get("slot_key") or "")
    location_key = _clean_text(previous_payload.get("location_key") or "home.living_room")
    location_label = _clean_text(previous_payload.get("location_label") or "客厅")
    activity_label = "休息一下"

    if previous_type == "meal" or upcoming_slot == "night_settle":
        location_key = "home.living_room"
        location_label = "客厅"
        activity_label = "饭后休息" if previous_type == "meal" else "晚上放松"
    elif location_key.startswith("outside."):
        activity_label = "在外面待一会儿"

    return {
        "location_key": location_key,
        "location_label": location_label,
        "activity_key": "interlude",
        "activity_label": activity_label,
        "title": activity_label,
        "slot_key": f"between_{previous_slot}_{upcoming_slot or 'end'}",
        "status": "active",
        "city": city,
        "source": "plan_interlude",
        "available": True,
    }


def _current_from_slot(slot: dict[str, Any], *, city: str, source: str) -> dict[str, Any]:
    payload = slot.get("payload") if isinstance(slot.get("payload"), dict) else {}
    return {
        "location_key": _clean_text(payload.get("location_key") or slot.get("activity_type") or "home"),
        "location_label": _clean_text(payload.get("location_label") or slot.get("location") or "家里"),
        "activity_key": _clean_text(slot.get("activity_type") or "idle"),
        "activity_label": _clean_text(payload.get("activity_label") or slot.get("title") or "安静待着"),
        "title": _clean_text(slot.get("title") or ""),
        "slot_key": _clean_text(slot.get("slot_key") or ""),
        "status": _clean_text(slot.get("status") or ""),
        "city": city,
        "source": source,
        "available": True,
    }


def _next_plan(plan: list[dict[str, Any]], *, now: float) -> dict[str, Any] | None:
    for item in sorted(plan, key=lambda row: float(row.get("scheduled_at") or 0)):
        if float(item.get("scheduled_at") or 0) > now:
            return dict(item)
    return None


def _query_needs_world_context(query_context: dict[str, Any]) -> bool:
    intent = str((query_context or {}).get("intent") or "").strip()
    subject = str((query_context or {}).get("subject_entity") or "").strip()
    boundary = str((query_context or {}).get("boundary") or "").strip()
    if intent in {"whereabouts", "day_plan", "weather", "weather_advice", "time", "time_weather"}:
        return True
    if subject == "aura" and boundary:
        return True
    return False


def _manual_current_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    if str(metadata.get("world_current_source") or "") != "manual" and not metadata.get("world_manual_override"):
        return None
    activity = _clean_text(metadata.get("current_activity") or "")
    location_key = _clean_text(metadata.get("current_location") or state.get("scene") or "")
    location_label = _clean_text(metadata.get("location_label") or location_key)
    if not (activity or location_label):
        return None
    return {
        "location_key": location_key or "manual",
        "location_label": location_label or location_key or "手动位置",
        "activity_key": "manual",
        "activity_label": activity or "手动状态",
        "title": activity or "手动状态",
        "slot_key": "manual_override",
        "status": "manual",
        "city": "",
        "source": "manual_state",
        "available": True,
    }


def _generated_current_from_state(state: dict[str, Any], *, city: str, now: float) -> dict[str, Any] | None:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    if str(metadata.get("world_current_source") or "") not in {"generated", "conversation"}:
        return None
    # 过期检查：缓存的 current 只是当时计划槽位的快照，随时间必须失效，
    # 否则会一直卡在旧活动上（时区修复前生成的"吃早饭"就是这样残留的）。
    try:
        age = float(now) - float(metadata.get("world_last_updated_at"))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > GENERATED_CURRENT_TTL_SECONDS:
        return None
    activity = _clean_text(metadata.get("current_activity") or "")
    location_key = _clean_text(metadata.get("current_location") or state.get("scene") or "")
    location_label = _clean_text(metadata.get("location_label") or location_key)
    if not (activity or location_label):
        return None
    return {
        "location_key": location_key or "generated",
        "location_label": location_label or location_key or "生成位置",
        "activity_key": "generated",
        "activity_label": activity or "生成状态",
        "title": activity or "生成状态",
        "slot_key": "generated_state",
        "status": "generated",
        "city": city,
        "source": str(metadata.get("world_current_source") or "generated_state"),
        "available": True,
    }


def _canonicalize_current(current: dict[str, Any], *, canon: WorldCanon) -> dict[str, Any]:
    row = dict(current or {})
    raw_key = _clean_text(row.get("location_key") or "home.living_room")
    activity_key = _clean_text(row.get("activity_key") or "")
    legacy_map = {
        "desk": "home.study",
        "living_room": "home.living_room",
        "neighborhood_food": "outside.neighborhood",
        "nearby_walk": "outside.park",
        "quiet_stop": "outside.cafe",
        "daily_shop": "outside.shop",
        "park": "outside.park",
        "mall": "outside.mall",
        "riverside": "outside.riverside",
    }
    if raw_key == "home":
        if activity_key == "meal":
            raw_key = "home.kitchen"
        elif activity_key in {"rest", "morning"}:
            raw_key = "home.bedroom"
        elif activity_key == "quiet":
            raw_key = "home.study"
        else:
            raw_key = "home.living_room"
    key = legacy_map.get(raw_key, raw_key)
    location = canon.location(key)
    row["location_key"] = key
    if location.get("label"):
        row["location_label"] = str(location.get("label"))
    return row


def _scene_from_current(
    current: dict[str, Any],
    *,
    canon: WorldCanon,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    location_key = _clean_text(current.get("location_key") or "")
    location = canon.location(location_key)
    nearby = canon.nearby_objects(location_key, limit=6)
    metadata = (state or {}).get("metadata") if isinstance((state or {}).get("metadata"), dict) else {}
    world_state = metadata.get("world_v2") if isinstance(metadata.get("world_v2"), dict) else {}
    dynamic_states = world_state.get("object_states") if isinstance(world_state.get("object_states"), dict) else {}
    for item in nearby:
        key = _clean_text(item.get("key") or "")
        if key and key in dynamic_states:
            item["state"] = _clean_text(dynamic_states.get(key))
    return {
        "location_key": location_key,
        "location_label": _clean_text(location.get("label") or current.get("location_label") or location_key),
        "location_kind": _clean_text(location.get("kind") or ""),
        "description": _clean_text(location.get("description") or ""),
        "connections": list(location.get("connections") or [])[:6],
        "activity_key": _clean_text(current.get("activity_key") or ""),
        "activity_label": _clean_text(current.get("activity_label") or "安静待着"),
        "nearby_objects": nearby,
    }


def _persist_world_current(
    config: Any,
    store: Any,
    state: dict[str, Any],
    current: dict[str, Any],
    *,
    scene: dict[str, Any],
    weather: dict[str, Any] | None = None,
    now: float,
    persist: bool = True,
) -> None:
    if not current:
        return
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    previous = metadata.get("world_v2") if isinstance(metadata.get("world_v2"), dict) else {}
    location_key = _clean_text(current.get("location_key") or "")
    activity_label = _clean_text(current.get("activity_label") or "")
    changed = bool(previous) and (
        _clean_text(previous.get("location_key")) != location_key
        or _clean_text(previous.get("activity_label")) != activity_label
    )
    since = float(now) if changed or not previous else float(previous.get("since") or now)
    nearby_states = {
        _clean_text(item.get("key")): _clean_text(item.get("state"))
        for item in scene.get("nearby_objects", [])
        if isinstance(item, dict) and _clean_text(item.get("key"))
    }
    object_states = dict(previous.get("object_states")) if isinstance(previous.get("object_states"), dict) else {}
    object_states.update(nearby_states)
    world_state = {
        "schema": WORLD_SCHEMA_VERSION,
        "location_key": location_key,
        "location_label": _clean_text(scene.get("location_label") or current.get("location_label") or ""),
        "activity_key": _clean_text(current.get("activity_key") or ""),
        "activity_label": activity_label,
        "since": since,
        "nearby_objects": list(nearby_states),
        "object_states": object_states,
        "weather": _compact_world_weather(weather),
        "source": _clean_text(current.get("source") or "generated"),
    }
    updated = dict(state)
    next_metadata = dict(metadata)
    next_metadata["current_activity"] = activity_label
    next_metadata["current_location"] = location_key
    next_metadata["location_label"] = world_state["location_label"]
    next_metadata["world_current_source"] = "manual" if current.get("source") == "manual_state" else "generated"
    # Keep the scene start as the cache anchor. Refreshing it on every chat turn
    # would prevent the schedule from ever advancing during an active conversation.
    next_metadata["world_last_updated_at"] = since
    next_metadata["world_schema"] = WORLD_SCHEMA_VERSION
    next_metadata["world_v2"] = world_state
    updated["scene"] = location_key or str(updated.get("scene") or "living_room")
    updated["metadata"] = next_metadata
    if persist:
        store.save_state(config.scope, updated)
    if persist and changed and hasattr(store, "record_life_event"):
        previous_label = _clean_text(previous.get("location_label") or previous.get("location_key") or "")
        current_label = world_state["location_label"] or location_key
        title = (
            f"从{previous_label or '上一处'}转到{current_label}"
            if _clean_text(previous.get("location_key")) != location_key
            else f"在{current_label}开始{activity_label or '新的活动'}"
        )
        store.record_life_event(
            config.scope,
            event_type="world.transition",
            event_key=f"{int(now)}:{location_key}:{activity_label}",
            title=title,
            description=activity_label,
            location=location_key,
            activity=activity_label,
            visibility="private",
            delta={"before": previous, "after": world_state},
            payload={"source": "world_clock"},
        )


def _mention_policy(
    *,
    query_context: dict[str, Any],
    state: dict[str, Any],
    current: dict[str, Any],
    recent_messages: list[dict[str, Any]],
    disabled: bool,
) -> dict[str, Any]:
    intent = str(query_context.get("intent") or "chat")
    subject = str(query_context.get("subject_entity") or "unknown")
    query_boundary = str(query_context.get("boundary") or "")
    if disabled:
        return {
            "allow_location": False,
            "allow_activity": False,
            "allow_moment": False,
            "allow_plan": False,
            "location_precision": "none",
            "reason": WORLD_DISABLED_REASON,
            "intent": intent,
            "subject_entity": subject,
            "boundary": query_boundary,
        }
    asks_current = intent == "activity_or_location" and subject == "aura"
    asks_plan = intent == "day_plan" and subject == "aura"
    asks_aura_weather_or_time = intent in {"weather", "weather_advice", "time", "time_weather"} and subject == "aura"
    relationship_gate = _relationship_boundary(state)
    if asks_aura_weather_or_time:
        allow_location = False
        allow_activity = False
        allow_plan = False
        location_precision = "city"
        reason = "asked_aura_weather_or_time"
    elif asks_current or asks_plan:
        allow_location = relationship_gate["allow_specific_location"]
        allow_activity = relationship_gate["allow_activity"]
        allow_plan = asks_plan and relationship_gate["allow_plan"]
        location_precision = "specific" if allow_location else "vague"
        reason = "asked_world" if asks_current else "asked_day_plan"
        if not (allow_location or allow_activity or allow_plan):
            reason = "relationship_boundary"
    else:
        allow_location = False
        allow_activity = False
        allow_plan = False
        location_precision = "none"
        reason = ORDINARY_REPLY_REASON
    if not (asks_current or asks_plan) and _recently_mentioned(current, recent_messages):
        allow_location = False
        allow_activity = False
        allow_plan = False
        location_precision = "none"
        reason = "recent_location_cooldown"
    return {
        "allow_location": allow_location,
        "allow_activity": allow_activity,
        "allow_moment": False,
        "allow_plan": allow_plan,
        "location_precision": location_precision,
        "reason": reason,
        "intent": intent,
        "subject_entity": subject,
        "boundary": query_boundary,
        "relationship_gate": relationship_gate,
    }


def _relationship_boundary(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    flags = metadata.get("relationship_flags") if isinstance(metadata.get("relationship_flags"), dict) else {}
    trust = max(
        _coerce_int(state.get("trust"), 50),
        _coerce_int(metadata.get("relationship_baseline_trust"), 0),
    )
    affinity_xp = _coerce_int(state.get("affinity_xp"), 0)
    baseline_level = max(1, min(10, _coerce_int(metadata.get("relationship_baseline_level"), 1)))
    social_need = _coerce_int(metadata.get("social_need"), 0)
    privacy = _coerce_int(metadata.get("privacy_sensitivity"), 35)
    strained = bool(flags.get("strained"))
    baseline_closeness = max(0, (baseline_level - 1) * 12)
    closeness = trust + max(baseline_closeness, min(70, affinity_xp // 4)) + min(30, social_need // 2) - privacy
    allow_plan = not strained and closeness >= 40
    allow_activity = not strained and closeness >= 48
    allow_specific_location = not strained and closeness >= 70 and trust >= 58 and affinity_xp >= 80 and privacy <= 72
    return {
        "trust": trust,
        "affinity_xp": affinity_xp,
        "baseline_level": baseline_level,
        "social_need": social_need,
        "privacy_sensitivity": privacy,
        "strained": strained,
        "closeness": closeness,
        "allow_plan": allow_plan,
        "allow_activity": allow_activity,
        "allow_specific_location": allow_specific_location,
    }


def _recently_mentioned(current: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
    location = _clean_text(current.get("location_label") or "")
    if not location or len(location) < 2:
        return False
    recent_aura = [
        str(item.get("body") or "")
        for item in messages[-6:]
        if str(item.get("direction") or "") == "aura"
    ]
    return any(location in body for body in recent_aura)


def _is_aura_world_plan(plan: list[dict[str, Any]]) -> bool:
    if not plan:
        return False
    aura_rows = 0
    for item in plan:
        title = _clean_text(item.get("title") or "")
        if "陪伴时光" in title:
            return False
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if payload.get("world_schema") == WORLD_SCHEMA_VERSION:
            aura_rows += 1
    return aura_rows >= max(1, len(plan) // 2)


def _current_text(current: dict[str, Any], *, expose_location: bool, expose_activity: bool) -> str:
    parts = []
    if expose_location and current.get("location_label"):
        parts.append(f"位置={current.get('location_label')}")
    if expose_activity and current.get("activity_label"):
        parts.append(f"活动={current.get('activity_label')}")
    if current.get("status"):
        parts.append(f"状态={current.get('status')}")
    return " ".join(str(item) for item in parts if item)


def _scene_prompt_text(scene: dict[str, Any]) -> str:
    if not scene:
        return ""
    parts = []
    location = _clean_text(scene.get("location_label") or scene.get("location_key") or "")
    activity = _clean_text(scene.get("activity_label") or "")
    description = _clean_text(scene.get("description") or "")
    if location:
        parts.append(f"位置={location}")
    if activity:
        parts.append(f"正在={activity}")
    if description:
        parts.append(f"环境={description}")
    objects = []
    # W4：只渲染当前场景最多 3 件物件的细节，密度给足但不涨 prompt 体积。
    for item in scene.get("nearby_objects", [])[:3]:
        if not isinstance(item, dict):
            continue
        label = _clean_text(item.get("label") or item.get("key") or "")
        if not label:
            continue
        state = _clean_text(item.get("state") or "")
        detail = _object_detail_text(item)
        rendered = [part for part in (state, detail) if part]
        objects.append(f"{label}（{'，'.join(rendered)}）" if rendered else label)
    if objects:
        parts.append("附近物品=" + "、".join(objects))
    return "；".join(parts)


def _object_detail_text(item: dict[str, Any]) -> str:
    """habit / sense / story 里挑最有用的一段，压成一句人话锚点。"""
    for field in ("habit", "sense", "story"):
        text = _clean_text(item.get(field))
        if text:
            return text[:60]
    return ""


def _recent_event_text(event: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        return ""
    when = _fmt_time(event.get("created_at"))
    title = _clean_text(event.get("title") or "")
    description = _clean_text(event.get("description") or "")
    body = "，".join(item for item in (title, description) if item)
    return " ".join(item for item in (when, body) if item)


def _explicit_location_from_reply(body: str, *, canon: WorldCanon) -> tuple[str, str] | None:
    locations = sorted(
        canon.locations.items(),
        key=lambda item: len(_clean_text(item[1].get("label") if isinstance(item[1], dict) else "")),
        reverse=True,
    )
    for key, item in locations:
        label = _clean_text(item.get("label") if isinstance(item, dict) else "")
        if not label:
            continue
        pattern = re.compile(rf"(?:我(?:现在|这会儿|刚刚)?(?:在|到了|到|去|回到)|(?:去|到|回到))\s*{re.escape(label)}")
        match = pattern.search(body)
        if not match:
            continue
        prefix = body[max(0, match.start() - 3) : match.start()]
        if "不" in prefix or "没" in prefix:
            continue
        return str(key), label
    return None


def _explicit_object_state(body: str, *, label: str) -> str:
    index = body.find(label)
    if index < 0:
        return ""
    window = body[max(0, index - 8) : index + len(label) + 10]
    if any(token in window for token in ("没关", "不关", "没开", "不开")):
        return ""
    if any(token in window for token in ("关小", "开一点", "留条缝", "半开")):
        return "半开"
    if any(token in window for token in ("关上", "关掉", "关了", "合上")):
        return "关闭"
    if any(token in window for token in ("打开", "开着", "开了", "亮着", "点亮")):
        return "开启"
    if any(token in window for token in ("烧水", "加热")):
        return "工作中"
    if any(token in window for token in ("看书", "翻书", "读书")):
        return "使用中"
    return ""


def _plan_line(item: dict[str, Any], *, expose_location: bool) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    when = _fmt_time(item.get("scheduled_at"), city=str(payload.get("city") or ""))
    title = _clean_text(item.get("title") or "")
    location = _clean_text(item.get("location") or "") if expose_location else ""
    status = _clean_text(item.get("status") or "")
    return " ".join(part for part in (when, status, title, f"@{location}" if location else "") if part)


def _format_hours(hours: Any) -> str:
    """3.0 → "3"，3.5 → "3.5"：回顾句是要念出来的，别带小数点尾巴。"""
    try:
        value = round(float(hours), 1)
    except (TypeError, ValueError):
        return "1"
    if value <= 0:
        return "1"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _fmt_time(value: Any, *, city: str = "") -> str:
    try:
        return dt.datetime.fromtimestamp(float(value), resolve_city_tzinfo(city)).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _pick(rng: random.Random, items: list[str]) -> str:
    return str(rng.choice(items))


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
