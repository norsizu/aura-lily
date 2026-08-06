from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from .assets import PersonaAssets
from .config import PersonaGatewayConfig
from .outlets import OutletSignals
from .query_context import QueryContext, correction_focus_text
from .response_contract import SPOKEN_REPLY_INSTRUCTION, normalize_spoken_reply
from .state_rules import state_context_summary
from .time_context import current_time_prompt_text
from .world import render_world_prompt


@dataclass(frozen=True)
class PersonaContext:
    prompt: str
    state_summary: dict[str, Any]
    debug: dict[str, Any]


def build_persona_context(
    *,
    user_text: str,
    config: PersonaGatewayConfig,
    assets: PersonaAssets,
    state: dict[str, Any],
    recent_messages: list[dict[str, Any]],
    latest_moment: dict[str, Any] | None,
    today_plan: list[dict[str, Any]],
    query_context: QueryContext,
    outlet_signals: OutletSignals,
    local_cache: dict[str, Any] | None = None,
    world_snapshot: dict[str, Any] | None = None,
    compact_voice: bool = False,
) -> PersonaContext:
    state_summary = state_context_summary(state)
    world_enabled = bool(world_snapshot and world_snapshot.get("enabled"))
    world_prompt = render_world_prompt(world_snapshot)
    sections: list[str] = []
    if config.include_soul and assets.available:
        soul = assets.soul.strip()
        if compact_voice:
            # Keep enough room for a user-supplied persona while bounding the voice prompt.
            soul = _clip(soul, 2000)
        sections.append("## 主人格\n" + soul)
    if config.include_state:
        sections.append("## 当前状态\n" + _state_text(state_summary, hide_world_details=world_enabled))
    sections.append("## 当前时间\n" + current_time_prompt_text())
    if world_prompt:
        if compact_voice:
            world_prompt = _clip(world_prompt, 900)
        sections.append("## 世界状态\n" + world_prompt)
    if not compact_voice and not world_enabled and config.include_latest_moment and latest_moment:
        sections.append("## 最近动态\n" + _moment_text(latest_moment))
    if not compact_voice and not world_enabled and config.include_today_plan and today_plan:
        sections.append("## 今日生活线\n" + _plan_text(today_plan))
    if config.include_recent_messages and recent_messages:
        sections.append("## 最近对话\n" + _messages_text(recent_messages, compact=compact_voice))
    sections.append("## 指代解析\n" + json.dumps(query_context.to_dict(), ensure_ascii=False, sort_keys=True))
    relevant_local_cache = _local_cache_for_prompt(local_cache, query_context)
    focused_user_text = correction_focus_text(user_text)
    if relevant_local_cache:
        sections.append("## 内部缓存证据\n" + json.dumps(relevant_local_cache, ensure_ascii=False, sort_keys=True))
    sections.append("## 行为信号\n" + json.dumps(outlet_signals.to_dict(), ensure_ascii=False, sort_keys=True))
    sections.append(
        "## 本轮任务\n"
        "按主人格和当前状态自然回复用户。不要自称助手，不要解释这些上下文来源。"
        + SPOKEN_REPLY_INSTRUCTION
        + "用户本轮明确提出字数、格式或固定答复内容时，必须优先遵守；最近对话和内部背景不能覆盖用户本轮要求。"
        + (
            "这是实时语音低延迟回合；优先一句短答，必要时第二句只补最关键追问。"
            "第一句必须直接给实质答案，不要先铺垫“既然要X那肯定得Y”“那就不废话了”这类空话再展开。"
            "用户要求推荐几个/列举几个时，直接在第一句开始点名具体选项，一句话可以并列两三个名字；不要只答一个还留悬念。"
            "只引用本提示中明确出现的事实；没有事实依据时，不要编具体日期、次数、项目名、持续时长、最近发生的事件或用户心理状态。"
            "可以用“可能/像是/先从某一块看”做开放式承接，但不要断言“你最近一直/上周/这几天/连着几天/那个项目/你肯定”。"
            "用户要复盘状态或工作状态时，只给开放式切入问题，例如从工作节奏、事情太满、提不起劲、卡点这类维度问起；不要诊断用户紧绷、焦虑、疲惫、效率低或项目压力。"
            if compact_voice
            else ""
        )
        + "当前状态、最近动态和最近对话只作为内部背景；除非用户明确问你在哪、你那边、天气、行程或在干嘛，不要主动提具体地点名、商场、店铺或食物。"
        + "不要把最近对话里出现过的地点当成口头禅反复带进普通回复。"
        + "用户问“现在几点/现在多少度”这类无指代问题时，按用户所在地理解；只有问“你那边/你那里”时才按 Aura 所在地理解。"
        + "如果用户问你那边/你今天/你在哪/你在干嘛，优先使用世界状态里的当前状态、位置和日程；不要临场编造新的地点、食物、逛街或动作。"
        + "如果用户问天气且本地天气缓存不是 fresh，不要编造实时天气，也不要顺带汇报 Aura 的所在地或天气。"
        + "如果用户问天气建议、为什么带伞、怎么穿或要不要带伞，要先给建议结论，再用一句话说明依据；可以引用内部缓存证据，但不要只机械播报天气数据。"
        + "内部缓存证据不是用户原话，不要说用户发来数据、一串数据、气象站或类似调侃。"
        + "示例：用户说“我想复盘最近的状态”，可以答“从工作节奏说起：是事情太满，还是提不起劲？”；禁止只复读成“最近状态啊？”"
        + "用户说“回复速度/语音链路/首字/首包/首音频”时，语音链路特指本设备 ASR→模型首句→TTS 首音频→设备播放，不是手机基站、运营商网络或普通通信链路。"
        + "用户用“等一下/不是/我是问/我想问”纠正自己时，以纠正后的本轮重点为准，不要把被否定的前半句、被否定的地点或连接词当成问题。"
        + (
            "纠正后的本轮重点：\n" + focused_user_text.strip() + "\n"
            if focused_user_text.strip() and focused_user_text.strip() != str(user_text).strip()
            else ""
        )
        + "用户原话：\n"
        + str(user_text).strip()
    )
    prompt = "\n\n".join(part for part in sections if part.strip()).strip()
    prompt = (
        _clip_middle(prompt, min(config.max_context_chars, 3200))
        if compact_voice
        else _clip(prompt, config.max_context_chars)
    )
    debug = {
        "context_sections": [
            name
            for name, enabled in [
                ("soul", config.include_soul and assets.available),
                ("state", config.include_state),
                ("current_time", True),
                ("world", bool(world_prompt)),
                ("latest_moment", (not world_enabled) and config.include_latest_moment and bool(latest_moment)),
                ("today_plan", (not world_enabled) and config.include_today_plan and bool(today_plan)),
                ("recent_messages", config.include_recent_messages and bool(recent_messages)),
                ("query_context", True),
                ("local_cache", bool(relevant_local_cache)),
                ("outlet_signals", True),
            ]
            if enabled
        ],
        "prompt_chars": len(prompt),
        "soul_source": assets.source_path,
        "recent_message_count": len(recent_messages),
        "has_latest_moment": bool(latest_moment),
        "today_plan_count": len(today_plan),
        "query_context": query_context.to_dict(),
        "local_cache": relevant_local_cache,
        "outlet_signals": outlet_signals.to_dict(),
        "world_snapshot": world_snapshot or {},
        "compact_voice": bool(compact_voice),
        "user_text": str(user_text or "").strip(),
        "focused_user_text": focused_user_text if focused_user_text != str(user_text).strip() else "",
        "user_topic_keywords": _topic_keywords_for_voice_quality(user_text),
        "recent_aura_replies": _recent_aura_replies_for_voice_quality(recent_messages),
    }
    return PersonaContext(prompt=prompt, state_summary=state_summary, debug=debug)


def _local_cache_for_prompt(local_cache: dict[str, Any] | None, query_context: QueryContext) -> dict[str, Any]:
    if not isinstance(local_cache, dict) or not local_cache:
        return {}
    intent = str(query_context.intent or "")
    allowed_keys: set[str] = set()
    if intent in {"time", "time_weather"}:
        allowed_keys.add("current_time")
    if intent in {"weather", "weather_advice", "time_weather"}:
        allowed_keys.add("cached_weather")
    if not allowed_keys:
        return {}
    return {key: value for key, value in local_cache.items() if key in allowed_keys}


def _state_text(summary: dict[str, Any], *, hide_world_details: bool = False) -> str:
    relationship = summary.get("relationship") if isinstance(summary.get("relationship"), dict) else {}
    lines = [
        f"心情={summary.get('mood')} 能量={summary.get('energy')} 饱腹={summary.get('satiety')} 压力={summary.get('stress')}",
        f"信任={summary.get('trust')} 好感XP={summary.get('affinity_xp')} 关系={relationship.get('label')}({relationship.get('level')})",
        f"社交需求={summary.get('social_need')} 好奇心={summary.get('curiosity')} 边界紧张={relationship.get('strained')}",
    ]
    if hide_world_details:
        lines.append("位置和活动由世界状态模块按本轮边界决定是否可见。")
    else:
        lines.append(f"当前位置={summary.get('location_label') or summary.get('current_location') or summary.get('scene')} 当前活动={summary.get('current_activity') or '未知'}")
    return "\n".join(lines)


def _moment_text(moment: dict[str, Any]) -> str:
    ts = _fmt_ts(moment.get("published_at"))
    body = str(moment.get("body") or "").strip()
    location = str(moment.get("location_label") or "").strip()
    visibility = str(moment.get("visibility") or "").strip()
    return f"{ts} [{visibility}] {location}：{body}".strip()


def _plan_text(plan: list[dict[str, Any]]) -> str:
    rows = []
    for item in plan[-10:]:
        when = _fmt_ts(item.get("scheduled_at"), time_only=True)
        status = str(item.get("status") or "")
        title = str(item.get("title") or "")
        location = str(item.get("location") or "")
        rows.append(f"- {when} {status} {title} @ {location}".rstrip())
    return "\n".join(rows)


def _messages_text(messages: list[dict[str, Any]], *, compact: bool = False) -> str:
    rows = []
    limit = 3 if compact else 12
    clip_limit = 80 if compact else 180
    for item in messages[-limit:]:
        is_user = item.get("direction") == "user"
        direction = "用户" if is_user else "Aura"
        body = str(item.get("body") or "").replace("\n", " ").strip()
        if not is_user:
            body = normalize_spoken_reply(body).text
        if body:
            rows.append(f"- {direction}: {_clip(body, clip_limit)}")
    return "\n".join(rows)


def _recent_aura_replies_for_voice_quality(messages: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    rows: list[str] = []
    for item in reversed(messages):
        if item.get("direction") != "aura":
            continue
        body = normalize_spoken_reply(str(item.get("body") or "")).text.strip()
        if body:
            rows.append(_clip(body, 80))
        if len(rows) >= limit:
            break
    return rows


def _fmt_ts(value: Any, *, time_only: bool = False) -> str:
    try:
        parsed = dt.datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return ""
    return parsed.strftime("%H:%M") if time_only else parsed.strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _clip_middle(text: str, limit: int) -> str:
    clean = str(text or "")
    if limit <= 0 or len(clean) <= limit:
        return clean
    marker = "\n\n[...语音低延迟模式已省略中间背景...]\n\n"
    if limit <= len(marker) + 80:
        return clean[:limit].rstrip()
    head_len = max(200, int((limit - len(marker)) * 0.45))
    tail_len = max(200, limit - len(marker) - head_len)
    return (clean[:head_len].rstrip() + marker + clean[-tail_len:].lstrip()).strip()


def _topic_keywords_for_voice_quality(text: str) -> list[str]:
    value = str(text or "")
    candidates = (
        "最近状态",
        "状态",
        "复盘",
        "工作",
        "天气",
        "速度",
        "响应",
        "时间",
        "位置",
        "心情",
        "计划",
    )
    return [token for token in candidates if token in value]
