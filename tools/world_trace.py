#!/usr/bin/env python3
"""Alice Debug Console 风格的世界模型追溯工具（W5）。

只读打开 companion.db，把"今天 Lily 经历了什么"按时间摊开：
当前状态（含隐藏驱动）→ 今日日程（含 why）→ 世界账本流水（before→after、原因、因果链）。
不改库、不写任何文件。

读取逻辑在 ``integrations/aura_persona_gateway/trace_data.py``——X-Ray 大屏
（``GET /trace/data``）要读同一份数据，工具和大屏各写一份必然漂移。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.aura_persona_gateway.trace_data import (  # noqa: E402
    DRIVE_LABELS,
    LEDGER_AND_WORLD_PREFIXES,
    as_float,
    as_int,
    city_tzinfo,
    collect_trace,
    fmt_time,
    json_object,
    ledger_row,
    like_clause,
    plan_row,
    read_ledger,
    read_plan,
    read_state,
    state_row,
)
from integrations.aura_persona_gateway.world_ledger import (  # noqa: E402
    HIDDEN_DRIVE_FIELDS,
    RECAP_FIELD_LABELS,
    TRACKED_FIELDS,
)

__all__ = [
    "DRIVE_LABELS",
    "LEDGER_AND_WORLD_PREFIXES",
    "ROOT",
    "as_float",
    "as_int",
    "city_tzinfo",
    "collect_trace",
    "fmt_time",
    "json_object",
    "ledger_row",
    "like_clause",
    "main",
    "plan_row",
    "read_ledger",
    "read_plan",
    "read_state",
    "state_row",
]


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
            lines.append(f"  上次互动：{fmt_time(state['last_user_interaction_at'])}")

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
            lines.append(f"      " + "，".join(event["changes"]))
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
