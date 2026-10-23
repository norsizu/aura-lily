from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .pinned_notes import PINNED_NOTES_STATE_KEY, read_pinned_notes
from .self_why import SELF_WHY_STATE_KEY, read_self_why_state
from .world_ledger import (
    HIDDEN_DRIVE_FIELDS,
    LEDGER_EVENT_PREFIX,
    RECAP_FIELD_LABELS,
    TRACKED_FIELDS,
)


# ── X1 X-Ray 大屏的数据读取 ──────────────────────────────────
# 这里原来是 tools/world_trace.py 里的私有函数。追溯工具和大屏都要读同一份
# companion.db，各写一份必然漂移，所以读取逻辑集中到 gateway 侧，tool 只留
# 渲染和命令行。所有连接都用 mode=ro 打开，每次请求开一次、用完就关，不缓存。
LEDGER_AND_WORLD_PREFIXES = (LEDGER_EVENT_PREFIX, "world.")
DRIVE_LABELS = {field: RECAP_FIELD_LABELS.get(field, field) for field in HIDDEN_DRIVE_FIELDS}
DRIVE_LABELS["drives_snapshot_v1"] = "驱动快照"
# 大屏账本流水默认只显示最近 15 条，多了投屏上看不清。
SHOWCASE_LEDGER_LIMIT = 15


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fmt_time(value: Any, *, city: str = "") -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    # 没有城市时区信息时退回本地时区；日程行会单独带上 payload.city。
    try:
        tz = city_tzinfo(city)
        return dt.datetime.fromtimestamp(stamp, tz).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def city_tzinfo(city: str) -> dt.tzinfo:
    try:
        from .time_context import resolve_city_tzinfo

        return resolve_city_tzinfo(str(city or ""))
    except Exception:  # pragma: no cover - 时区表缺失时退回本地时间
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def like_clause(prefixes: tuple[str, ...]) -> str:
    return "(" + " OR ".join("event_type LIKE ?" for _ in prefixes) + ")"


def read_state(conn: sqlite3.Connection, *, platform: str, chat_id: str, user_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT mood, energy, affinity_xp, coins, metadata_json, created_at, updated_at,
               satiety, beans, trust, stress, outfit, scene
          FROM companion_state
         WHERE platform=? AND chat_id=? AND user_id=?
         ORDER BY id DESC LIMIT 1
        """,
        (platform, chat_id, user_id),
    ).fetchone()
    if row is None:
        return {}
    data = dict(zip(
        (
            "mood", "energy", "affinity_xp", "coins", "metadata", "created_at", "updated_at",
            "satiety", "beans", "trust", "stress", "outfit", "scene",
        ),
        row,
    ))
    data["metadata"] = json_object(data.get("metadata"))
    return data


def read_plan(conn: sqlite3.Connection, *, platform: str, chat_id: str, user_id: str, day_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT plan_date, slot_key, scheduled_at, activity_type, title, location,
               should_post, status, expected_delta_json, payload_json, executed_at
          FROM companion_day_plan
         WHERE platform=? AND chat_id=? AND user_id=? AND plan_date=?
         ORDER BY scheduled_at ASC, id ASC
        """,
        (platform, chat_id, user_id, day_key),
    ).fetchall()
    keys = (
        "plan_date", "slot_key", "scheduled_at", "activity_type", "title", "location",
        "should_post", "status", "expected_delta", "payload", "executed_at",
    )
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["expected_delta"] = json_object(item.get("expected_delta"))
        item["payload"] = json_object(item.get("payload"))
        out.append(item)
    return out


def read_ledger(
    conn: sqlite3.Connection,
    *,
    platform: str,
    chat_id: str,
    user_id: str,
    day_key: str,
) -> list[dict[str, Any]]:
    # 用 created_at 落在当天 00:00-24:00（本地时区）来筛，避免引入城市时区依赖。
    day_start = dt.datetime.fromisoformat(day_key).timestamp()
    day_end = day_start + 24 * 3600
    rows = conn.execute(
        f"""
        SELECT id, event_type, event_key, title, description, location, activity,
               visibility, intensity, delta_json, payload_json, created_at
          FROM companion_life_event
         WHERE platform=? AND chat_id=? AND user_id=? AND {like_clause(LEDGER_AND_WORLD_PREFIXES)}
           AND created_at >= ? AND created_at < ?
         ORDER BY created_at ASC, id ASC
         LIMIT 200
        """,
        (platform, chat_id, user_id, *[f"{prefix}%" for prefix in LEDGER_AND_WORLD_PREFIXES], day_start, day_end),
    ).fetchall()
    return [_ledger_item(row) for row in rows]


def read_recent_ledger(
    conn: sqlite3.Connection,
    *,
    platform: str,
    chat_id: str,
    user_id: str,
    limit: int = SHOWCASE_LEDGER_LIMIT,
) -> list[dict[str, Any]]:
    """最近 N 条账本事件（不按天切），新的在前——大屏要看"刚刚发生了什么"。"""
    rows = conn.execute(
        f"""
        SELECT id, event_type, event_key, title, description, location, activity,
               visibility, intensity, delta_json, payload_json, created_at
          FROM companion_life_event
         WHERE platform=? AND chat_id=? AND user_id=? AND {like_clause(LEDGER_AND_WORLD_PREFIXES)}
         ORDER BY created_at DESC, id DESC
         LIMIT ?
        """,
        (platform, chat_id, user_id, *[f"{prefix}%" for prefix in LEDGER_AND_WORLD_PREFIXES], max(1, int(limit or 1))),
    ).fetchall()
    return [_ledger_item(row) for row in rows]


def _ledger_item(row: Any) -> dict[str, Any]:
    keys = (
        "id", "event_type", "event_key", "title", "description", "location", "activity",
        "visibility", "intensity", "delta", "payload", "created_at",
    )
    item = dict(zip(keys, row))
    item["delta"] = json_object(item.get("delta"))
    item["payload"] = json_object(item.get("payload"))
    return item


def plan_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    why = payload.get("why") if isinstance(payload.get("why"), dict) else {}
    return {
        "time": fmt_time(item.get("scheduled_at"), city=str(payload.get("city") or "")),
        "scheduled_at": as_float(item.get("scheduled_at")),
        "slot_key": str(item.get("slot_key") or ""),
        "title": str(item.get("title") or ""),
        "location": str(item.get("location") or ""),
        "status": str(item.get("status") or ""),
        "activity_type": str(item.get("activity_type") or ""),
        "why_rule": str(why.get("rule") or ""),
        "why_text": str(why.get("text") or ""),
        "why_inputs": why.get("inputs") if isinstance(why.get("inputs"), dict) else {},
        "expected_delta": item.get("expected_delta") if isinstance(item.get("expected_delta"), dict) else {},
    }


def ledger_row(event: dict[str, Any], *, city: str) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    changes = [
        f"{RECAP_FIELD_LABELS.get(field, field)} {as_int(before.get(field), 0)}→{as_int(after.get(field), 0)}"
        for field in list(before) or list(after)
        if as_int(before.get(field), 0) != as_int(after.get(field), 0)
    ]
    return {
        "time": fmt_time(event.get("created_at"), city=city),
        "created_at": as_float(event.get("created_at")),
        "source": str(payload.get("source") or str(event.get("event_type") or "").replace(LEDGER_EVENT_PREFIX, "")),
        "event_type": str(event.get("event_type") or ""),
        "event_key": str(event.get("event_key") or ""),
        "title": str(event.get("title") or ""),
        "cause": str(payload.get("cause") or event.get("description") or ""),
        "parent_key": str(payload.get("parent_key") or "") or None,
        "changes": changes,
        "delta": delta,
        "location": str(event.get("location") or ""),
        "activity": str(event.get("activity") or ""),
    }


def state_row(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    tracked = {field: as_int(state.get(field), 0) for field in TRACKED_FIELDS}
    tracked["coins"] = as_int(state.get("coins"), tracked.get("beans", 0))
    drives = {field: as_int(metadata.get(field), 0) for field in HIDDEN_DRIVE_FIELDS}
    snapshot = metadata.get("drives_snapshot_v1") if isinstance(metadata.get("drives_snapshot_v1"), dict) else {}
    return {
        "tracked": tracked,
        "hidden_drives": drives,
        "drives_snapshot": snapshot.get("values") if isinstance(snapshot.get("values"), dict) else {},
        "drives_updated_at": snapshot.get("updated_at"),
        "emotion_state": metadata.get("emotion_state") if isinstance(metadata.get("emotion_state"), dict) else None,
        "outfit": str(state.get("outfit") or ""),
        "scene": str(state.get("scene") or ""),
        "current_activity": str(metadata.get("current_activity") or ""),
        "current_location": str(metadata.get("current_location") or ""),
        "last_user_interaction_at": metadata.get("last_user_interaction_at"),
        "previous_user_interaction_at": metadata.get("previous_user_interaction_at"),
        "offline_recap_for": metadata.get("offline_recap_for"),
        "updated_at": state.get("updated_at"),
    }


def read_state_at(
    db_path: str | Path,
    *,
    platform: str = "esp32",
    chat_id: str = "esp32-main",
    user_id: str = "default-user",
) -> dict[str, Any]:
    """只读打开一次 companion.db，取出某个 scope 的 state 行（没有就返回 {}）。

    给"另一个进程只想读一眼状态"的场景用（例如 WS 网关下发便签）：
    mode=ro 保证不建表、不改库、不触发 schema 迁移。
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return {}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return read_state(conn, platform=platform, chat_id=chat_id, user_id=user_id)
    finally:
        conn.close()


def collect_trace(
    db_path: str | Path,
    *,
    platform: str = "esp32",
    chat_id: str = "esp32-main",
    user_id: str = "default-user",
    day_key: str = "",
) -> dict[str, Any]:
    """Read-only walk over one scope's world model for a single day."""
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在：{path}")
    resolved_day = day_key or dt.date.today().isoformat()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        state = read_state(conn, platform=platform, chat_id=chat_id, user_id=user_id)
        plan = read_plan(conn, platform=platform, chat_id=chat_id, user_id=user_id, day_key=resolved_day)
        ledger = read_ledger(
            conn,
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            day_key=resolved_day,
        )
        city = ""
        for item in plan:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            city = str(payload.get("city") or "")
            if city:
                break
    finally:
        conn.close()
    return {
        "db": str(path),
        "scope": {"platform": platform, "chat_id": chat_id, "user_id": user_id},
        "day_key": resolved_day,
        "city": city,
        # 没有这一行的 scope 就返回空 dict，调用方可以直接判断"没见过这个人"。
        "state": state_row(state) if state else {},
        "plan": [plan_row(item) for item in plan],
        "ledger": [ledger_row(event, city=city) for event in ledger],
    }


# ── 大屏专用的三个附加读取 ──────────────────────────────────

def showcase_emotion_row(state: dict[str, Any]) -> dict[str, Any]:
    """``emotion``：当前情绪 + 最近一轮的评估链（LLM 自评→状态调制→惯性）。"""
    row = state_row(state)
    emotion_state = row.get("emotion_state") if isinstance(row.get("emotion_state"), dict) else {}
    resolution = emotion_state.get("last_resolution") if isinstance(emotion_state.get("last_resolution"), dict) else {}
    return {
        "emotion": str(emotion_state.get("emotion") or ""),
        "label": str(emotion_state.get("label") or ""),
        "intensity": as_int(emotion_state.get("intensity"), 0),
        "cause": str(emotion_state.get("cause") or ""),
        "at": as_float(emotion_state.get("at")),
        "at_text": fmt_time(emotion_state.get("at")),
        "last_resolution": {
            "source": str(resolution.get("source") or ""),
            "raw": str(resolution.get("raw") or ""),
            "modulated_from": str(resolution.get("modulated_from") or ""),
            "inertia_from": str(resolution.get("inertia_from") or ""),
            "reason": str(resolution.get("reason") or ""),
            "chain": format_resolution_chain(resolution, emotion=emotion_state.get("emotion")),
        },
    }


def format_resolution_chain(resolution: dict[str, Any], *, emotion: Any = "") -> str:
    """评估链一行，例：``LLM 自评 happy → 状态调制：心情 28<30 → neutral``。

    ``reason`` 里已经有引擎写好的中文解释，直接透出；只有它为空时才用
    结构化字段拼一行，保证大屏上永远有东西可看。
    """
    if not isinstance(resolution, dict) or not resolution:
        return ""
    reason = str(resolution.get("reason") or "").strip()
    if reason:
        return reason
    source = str(resolution.get("source") or "").strip()
    raw = str(resolution.get("raw") or "").strip()
    modulated = str(resolution.get("modulated_from") or "").strip()
    inertia = str(resolution.get("inertia_from") or "").strip()
    final = str(emotion or "").strip()
    parts: list[str] = []
    head = f"{source} 自评 {raw}" if source and raw else (source or raw)
    if head:
        parts.append(head)
    if modulated and modulated != raw:
        parts.append(f"状态调制：{modulated}")
    if inertia and inertia != modulated:
        parts.append(f"惯性：{inertia}")
    if final:
        parts.append(final)
    return " → ".join(parts)


def showcase_notes_row(state: dict[str, Any]) -> list[dict[str, Any]]:
    """``notes``：X3 钉在信息板上的便签，新的在前。"""
    return read_pinned_notes(state)


def showcase_why_row(state: dict[str, Any]) -> dict[str, Any]:
    """``why``：最近一次"问她为什么"引用的账本 event_key，大屏据此高亮流水行。"""
    stored = read_self_why_state(state)
    keys = [str(key or "").strip() for key in (stored.get("event_keys") or []) if str(key or "").strip()]
    return {
        "event_keys": keys,
        "question": str(stored.get("question") or ""),
        "at": as_float(stored.get("at")),
        "at_text": fmt_time(stored.get("at")),
    }


def collect_showcase_trace(
    db_path: str | Path,
    *,
    platform: str = "esp32",
    chat_id: str = "esp32-main",
    user_id: str = "default-user",
    day_key: str = "",
    ledger_limit: int = SHOWCASE_LEDGER_LIMIT,
    now: float | None = None,
) -> dict[str, Any]:
    """``GET /trace/data`` 的 payload：大屏要的 state/emotion/ledger/plan/notes/why。

    跟 ``collect_trace`` 的区别：账本取"最近 N 条"而不是"今天全部"，另外多带
    情绪评估链、便签和 why 高亮列表。SQLite 仍然 mode=ro 只读打开、每次开关。
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在：{path}")
    resolved_day = day_key or dt.date.today().isoformat()
    stamp = float(now if now is not None else time.time())
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        state = read_state(conn, platform=platform, chat_id=chat_id, user_id=user_id)
        plan = read_plan(conn, platform=platform, chat_id=chat_id, user_id=user_id, day_key=resolved_day)
        recent = read_recent_ledger(
            conn,
            platform=platform,
            chat_id=chat_id,
            user_id=user_id,
            limit=ledger_limit,
        )
        city = ""
        for item in plan:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            city = str(payload.get("city") or "")
            if city:
                break
    finally:
        conn.close()
    rows = state_row(state) if state else {}
    plan_rows = [plan_row(item) for item in plan]
    mark_current_plan_slot(plan_rows, now=stamp)
    return {
        "db": str(path),
        "scope": {"platform": platform, "chat_id": chat_id, "user_id": user_id},
        "day_key": resolved_day,
        "city": city,
        "server_time": stamp,
        "server_time_text": dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S"),
        "state": rows,
        "emotion": showcase_emotion_row(state) if state else {},
        "ledger": [ledger_row(event, city=city) for event in recent],
        "plan": plan_rows,
        "notes": showcase_notes_row(state),
        "why": showcase_why_row(state),
    }


def mark_current_plan_slot(plan_rows: list[dict[str, Any]], *, now: float) -> list[dict[str, Any]]:
    """把"当前进行中的槽位"标出来（最后一个 scheduled_at <= now 的槽）。"""
    if not plan_rows:
        return plan_rows
    current_index = -1
    for index, row in enumerate(plan_rows):
        scheduled = as_float(row.get("scheduled_at"))
        if scheduled > 0 and scheduled <= now:
            current_index = index
    for index, row in enumerate(plan_rows):
        row["current"] = index == current_index
    return plan_rows


__all__ = [
    "DRIVE_LABELS",
    "LEDGER_AND_WORLD_PREFIXES",
    "SHOWCASE_LEDGER_LIMIT",
    "as_float",
    "as_int",
    "city_tzinfo",
    "collect_showcase_trace",
    "collect_trace",
    "fmt_time",
    "format_resolution_chain",
    "json_object",
    "ledger_row",
    "like_clause",
    "mark_current_plan_slot",
    "plan_row",
    "read_ledger",
    "read_plan",
    "read_recent_ledger",
    "read_state",
    "read_state_at",
    "showcase_emotion_row",
    "showcase_notes_row",
    "showcase_why_row",
    "state_row",
    "PINNED_NOTES_STATE_KEY",
    "SELF_WHY_STATE_KEY",
]
