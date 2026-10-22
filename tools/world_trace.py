#!/usr/bin/env python3
"""Alice Debug Console 风格的世界模型追溯工具（W5）。

只读打开 companion.db，把"今天 Lily 经历了什么"按时间摊开：
当前状态（含隐藏驱动）→ 今日日程（含 why）→ 世界账本流水（before→after、原因、因果链）。
不改库、不写任何文件。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.aura_persona_gateway.world_ledger import (  # noqa: E402
    HIDDEN_DRIVE_FIELDS,
    LEDGER_EVENT_PREFIX,
    RECAP_FIELD_LABELS,
    TRACKED_FIELDS,
)

LEDGER_AND_WORLD_PREFIXES = (LEDGER_EVENT_PREFIX, "world.")
DRIVE_LABELS = {field: RECAP_FIELD_LABELS.get(field, field) for field in HIDDEN_DRIVE_FIELDS}
DRIVE_LABELS["drives_snapshot_v1"] = "驱动快照"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fmt_time(value: Any, *, city: str = "") -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    # 没有城市时区信息时退回本地时区；日程行会单独带上 payload.city。
    try:
        tz = _city_tzinfo(city)
        return dt.datetime.fromtimestamp(stamp, tz).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _city_tzinfo(city: str) -> dt.tzinfo:
    try:
        from integrations.aura_persona_gateway.time_context import resolve_city_tzinfo

        return resolve_city_tzinfo(str(city or ""))
    except Exception:  # pragma: no cover - 时区表缺失时退回本地时间
        return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _like_clause(prefixes: tuple[str, ...]) -> str:
    return "(" + " OR ".join("event_type LIKE ?" for _ in prefixes) + ")"


def _read_state(conn: sqlite3.Connection, *, platform: str, chat_id: str, user_id: str) -> dict[str, Any]:
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
    data["metadata"] = _json_object(data.get("metadata"))
    return data


def _read_plan(conn: sqlite3.Connection, *, platform: str, chat_id: str, user_id: str, day_key: str) -> list[dict[str, Any]]:
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
        item["expected_delta"] = _json_object(item.get("expected_delta"))
        item["payload"] = _json_object(item.get("payload"))
        out.append(item)
    return out


def _read_ledger(
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
         WHERE platform=? AND chat_id=? AND user_id=? AND {_like_clause(LEDGER_AND_WORLD_PREFIXES)}
           AND created_at >= ? AND created_at < ?
         ORDER BY created_at ASC, id ASC
         LIMIT 200
        """,
        (platform, chat_id, user_id, *[f"{prefix}%" for prefix in LEDGER_AND_WORLD_PREFIXES], day_start, day_end),
    ).fetchall()
    keys = (
        "id", "event_type", "event_key", "title", "description", "location", "activity",
        "visibility", "intensity", "delta", "payload", "created_at",
    )
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["delta"] = _json_object(item.get("delta"))
        item["payload"] = _json_object(item.get("payload"))
        out.append(item)
    return out


def _plan_row(item: dict[str, Any]) -> dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    why = payload.get("why") if isinstance(payload.get("why"), dict) else {}
    return {
        "time": _fmt_time(item.get("scheduled_at"), city=str(payload.get("city") or "")),
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


def _ledger_row(event: dict[str, Any], *, city: str) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    changes = [
        f"{RECAP_FIELD_LABELS.get(field, field)} {_as_int(before.get(field), 0)}→{_as_int(after.get(field), 0)}"
        for field in list(before) or list(after)
        if _as_int(before.get(field), 0) != _as_int(after.get(field), 0)
    ]
    return {
        "time": _fmt_time(event.get("created_at"), city=city),
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


def _state_row(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    tracked = {field: _as_int(state.get(field), 0) for field in TRACKED_FIELDS}
    tracked["coins"] = _as_int(state.get("coins"), tracked.get("beans", 0))
    drives = {field: _as_int(metadata.get(field), 0) for field in HIDDEN_DRIVE_FIELDS}
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
        state = _read_state(conn, platform=platform, chat_id=chat_id, user_id=user_id)
        plan = _read_plan(conn, platform=platform, chat_id=chat_id, user_id=user_id, day_key=resolved_day)
        ledger = _read_ledger(
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
        "state": _state_row(state) if state else {},
        "plan": [_plan_row(item) for item in plan],
        "ledger": [_ledger_row(event, city=city) for event in ledger],
    }


def _render(payload: dict[str, Any]) -> str:
    scope = payload["scope"]
    lines = [
        f"世界模型追溯  db={payload['db']}",
        f"scope={scope['platform']}/{scope['chat_id']}/{scope['user_id']}  day={payload['day_key']}  city={payload['city'] or '未设置'}",
        "",
        "== 当前状态 ==",
    ]
    state = payload["state"]
    if not state:
        lines.append("（没有 companion_state 记录）")
    else:
        tracked = state["tracked"]
        lines.append(
            "  "
            + "  ".join(
                f"{RECAP_FIELD_LABELS.get(field, field)}={tracked.get(field, 0)}"
                for field in TRACKED_FIELDS
            )
            + f"  coins={tracked.get('coins', 0)}"
        )
        drives = state["hidden_drives"]
        lines.append("  隐藏驱动：" + "  ".join(f"{DRIVE_LABELS.get(f, f)}={drives.get(f, 0)}" for f in HIDDEN_DRIVE_FIELDS))
        if state.get("emotion_state"):
            lines.append(f"  情绪状态：{json.dumps(state['emotion_state'], ensure_ascii=False)}")
        if state.get("current_activity") or state.get("current_location"):
            lines.append(f"  当前：{state.get('current_activity') or '-'} @ {state.get('current_location') or '-'}")
        if state.get("last_user_interaction_at"):
            lines.append(f"  上次互动：{_fmt_time(state['last_user_interaction_at'])}")

    lines.append("")
    lines.append("== 今日日程 ==")
    if not payload["plan"]:
        lines.append("  （当天没有日程）")
    for item in payload["plan"]:
        lines.append(
            f"  {item['time'] or '--:--'}  [{item['status'] or '-'}] {item['title']}"
            + (f" @ {item['location']}" if item["location"] else "")
        )
        if item["why_text"]:
            lines.append(f"      why({item['why_rule']})：{item['why_text']}")

    lines.append("")
    lines.append("== 世界账本流水 ==")
    if not payload["ledger"]:
        lines.append("  （当天没有流水）")
    for event in payload["ledger"]:
        lines.append(
            f"  {event['time'] or '--:--'}  {event['event_type']}  {event['title']}"
            + (f"  [{event['event_key']}]" if event["event_key"] else "")
        )
        if event["cause"]:
            lines.append(f"      cause：{event['cause']}")
        if event["changes"]:
            lines.append("      " + "，".join(event["changes"]))
        if event["parent_key"]:
            lines.append(f"      parent_key：{event['parent_key']}")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读追溯 Lily 的世界模型状态与账本流水。")
    parser.add_argument("--db", required=True, help="companion.db 路径（只读打开）")
    parser.add_argument("--scope-user", default="default-user", help="scope 的 user_id")
    parser.add_argument("--scope-chat", default="esp32-main", help="scope 的 chat_id")
    parser.add_argument("--scope-platform", default="esp32", help="scope 的 platform")
    parser.add_argument("--day", default="", help="YYYY-MM-DD，默认今天")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    try:
        payload = collect_trace(
            args.db,
            platform=args.scope_platform,
            chat_id=args.scope_chat,
            user_id=args.scope_user,
            day_key=args.day,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"打开数据库失败：{exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
