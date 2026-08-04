from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from .city_names import normalize_city_name
from .reminders import parse_reminder_request
from .runtime import AuraRuntimeConfig, cached_weather_snapshot, load_aura_runtime_config
from .time_context import current_time_snapshot, current_time_spoken_reply


class VoiceTurnVerdict(str, Enum):
    SPEAK_NOW = "speak_now"
    ACK_AND_ENQUEUE = "ack_and_enqueue"
    CLARIFY_NOW = "clarify_now"
    REFUSE_NOW = "refuse_now"
    SILENT_DROP = "silent_drop"


@dataclass(frozen=True)
class BackgroundTask:
    task_id: str
    task_kind: str
    source_text: str
    fallback_text: str = "这件事没处理完，我在后台留了记录。"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VoiceTurnResult:
    verdict: VoiceTurnVerdict
    speak_text: str = ""
    emotion: str = "neutral"
    background_task: BackgroundTask | None = None
    continue_listening: bool = False
    debug: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "speak_text": self.speak_text,
            "emotion": self.emotion,
            "background_task": self.background_task.to_dict() if self.background_task else None,
            "continue_listening": self.continue_listening,
            "debug": dict(self.debug or {}),
        }


PUNCT_RE = re.compile(r"[\s,，。.!！?？、~～…·:：;；\"'“”‘’（）()【】\[\]{}<>《》]+")
TRIVIAL_NOISE_RE = re.compile(r"^(嗯+|呃+|啊+|额+|唔+|诶+|喂+)$")
GREETING_RE = re.compile(r"^(你好|嗨|哈喽|hello|hi|在吗|你在吗|早安|早上好|晚安|谢谢|谢啦|辛苦了)$", re.I)
QUICK_ACK_RE = re.compile(
    r"^(测试一下|测试下|测试|试一下|试下|简单回应(?:我)?一句|简单回复(?:我)?一句|随便说一句|听得到吗|能听到吗|还在吗|你还在吗)$",
    re.I,
)
CLARIFY_RE = re.compile(r"^(如果|假如|要是|那|这个|那个|然后|还有|刚才|前面)[，,。.\s]*$")
REFUSE_RE = re.compile(r"(删掉|删除|清空|格式化|rm\s+-rf|系统文件|越权|破解|偷|盗|密码|密钥|token)", re.I)
# 注意：这两个正则只是"兜底"路由（非流式 run_turn、hermes_main 等没有快模型
# 意图判断的场景）。语音流式路径上由前端快模型自己输出 [后台] 标记做意图判断，
# 见 llm.AGENT_TASK_STREAM_INSTRUCTION 与 turn.run_direct_turn_stream。
AGENT_LOOKUP_RE = re.compile(r"(查|搜|找|看一下|确认|比特币|股票|价格|新闻|资料|官网|天气预报)")
# 只有会产出“实体交付物”的重创作才排后台；写句诗、一句话总结这类轻创作直接让模型当场答。
AGENT_CREATE_RE = re.compile(r"(做一份|写一份|文档|报告|代码|脚本|表格|PPT|海报|简历|画.{0,4}图)")
# ── 本地能力边界判断 ─────────────────────────────────────────────
# 本地天气缓存只有一种能力：当前实况。判断是否超纲用两个正交维度：
#   1) 是不是天气类问题（优先用 query_context 解析出的 intent，兜底用主题词）；
#   2) 问的是不是非当前时段（通用的未来时间词，不跟天气词配对）。
# 两者都命中 = 超出本地能力 → 转后台 agent。不要再按"明天+天气"这种句式配对打补丁。
WEATHER_TOPIC_RE = re.compile(
    r"天气|预报|温度|气温|几度|多少度|下雨|降雨|降温|升温|下雪|刮风|冷不冷|热不热|带伞|穿什么"
)
FUTURE_TIME_RE = re.compile(
    r"明天|明早|明晚|后天|大后天|明后天|今晚|今夜|傍晚|晚上|夜里"
    r"|下周|下个月|未来|接下来|这几天|过几天|这周末|周末"
    r"|周[一二三四五六日天]|星期[一二三四五六日天]|礼拜[一二三四五六日天]"
)


def _weather_needs_forecast(raw: str, route: str) -> bool:
    """天气类问题指向非当前时段时返回 True（本地实况缓存答不了，要联网查预报）。"""
    value = str(raw or "")
    if "天气预报" in value:
        return True
    is_weather = route in {"weather", "weather_advice", "time_weather"} or bool(WEATHER_TOPIC_RE.search(value))
    return is_weather and bool(FUTURE_TIME_RE.search(value))
FIXED_REPLY_RE = re.compile(
    r"(?:回答|回复|答复|说)(?:我)?(?P<reply>我在|在|嗯|好|可以|收到|来了)"
)
BARE_FIXED_REPLY_RE = re.compile(r"(?:^|[，,。.\s])(?P<reply>我在)(?:$|[，,。.!！?？\s])")
SUPPORTIVE_RE = re.compile(
    r"(累|困|难受|不舒服|烦|焦虑|压力|低落|难过|委屈|睡不着|陪我|聊两句|说说话|安慰)"
)
VOICE_LATENCY_RE = re.compile(
    r"(回复速度|响应速度|反应速度|语音链路|首字|首包|首音频|哪里慢|慢在哪里|卡在哪里|耗时)"
)
CASUAL_CHAT_RE = re.compile(r"(聊聊|说说|最近状态|自然回应|陪我说|陪我聊|想跟你说|想找你聊)")
STATUS_REVIEW_RE = re.compile(
    r"(复盘.*(?:状态|工作|工作节奏)|(?:最近状态|工作状态|工作节奏).*(?:复盘|聊聊|说说|自然回应))"
)


def execute_voice_turn(
    text: str,
    *,
    fastpath: dict[str, Any] | None = None,
    runtime_config: AuraRuntimeConfig | None = None,
    state_summary: dict[str, Any] | None = None,
    local_cache: dict[str, Any] | None = None,
) -> VoiceTurnResult:
    started = time.monotonic()
    runtime = runtime_config or load_aura_runtime_config()
    raw = str(text or "").strip()
    normalized = PUNCT_RE.sub("", raw).lower()
    route_context = fastpath or {}
    route_context = {**route_context, "raw_text": raw}
    route = str(route_context.get("intent") or "")
    if not runtime.voice_turn_enabled:
        return _result(VoiceTurnVerdict.SPEAK_NOW, started, "voice_turn_disabled", route=route, mode=runtime.fast_reply_mode)
    if not raw or not normalized or TRIVIAL_NOISE_RE.match(normalized):
        return _result(VoiceTurnVerdict.SILENT_DROP, started, "empty_or_noise", route=route, mode=runtime.fast_reply_mode)
    if REFUSE_RE.search(raw):
        return _result(
            VoiceTurnVerdict.REFUSE_NOW,
            started,
            "safety_refuse",
            speak=runtime.refuse_reply,
            emotion="firm",
            route=route,
            mode=runtime.fast_reply_mode,
        )
    if CLARIFY_RE.search(raw):
        return _result(
            VoiceTurnVerdict.CLARIFY_NOW,
            started,
            "incomplete_utterance",
            speak=runtime.clarify_reply,
            emotion="curious",
            route=route,
            continue_listening=True,
            mode=runtime.fast_reply_mode,
        )
    fixed_reply = _fixed_short_reply(raw)
    if fixed_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "explicit_fixed_reply",
            speak=fixed_reply,
            emotion="calm",
            route=route,
            mode=runtime.fast_reply_mode,
        )
    # 定闹钟/定时提醒：本地解析出绝对时间，网关到点主动推 TTS。
    # 必须走确定性路径——以前让模型口头答应"已设好"但没人真正调度，等于撒谎。
    reminder = parse_reminder_request(raw)
    if reminder is not None:
        reminder_status = str(reminder.get("status") or "")
        if reminder_status == "cancel":
            return _result(
                VoiceTurnVerdict.SPEAK_NOW,
                started,
                "reminder_cancel",
                speak="好，之前定的闹钟和提醒都取消了。",
                emotion="calm",
                route=route,
                mode=runtime.fast_reply_mode,
                extra_debug={"reminder": {"cancel_all": True}},
            )
        if reminder_status == "unclear":
            return _result(
                VoiceTurnVerdict.CLARIFY_NOW,
                started,
                "reminder_time_unclear",
                speak="要定几点的？说个具体时间，比如11点10分，或者5分钟后。",
                emotion="curious",
                route=route,
                continue_listening=True,
                mode=runtime.fast_reply_mode,
            )
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "reminder_set",
            speak=str(reminder.get("confirm_text") or ""),
            emotion="calm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"reminder": {k: v for k, v in reminder.items() if k != "status"}},
        )
    if route == "time_weather":
        time_weather_reply, time_weather_debug = _time_weather_reply(runtime, route_context)
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            time_weather_debug.get("decision_path") or "current_time_weather",
            speak=time_weather_reply,
            emotion="calm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"current_time_weather": time_weather_debug},
        )
    if route == "time":
        time_reply, time_debug = _time_reply(route_context)
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "current_time",
            speak=time_reply,
            emotion="calm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"current_time": time_debug},
        )
    # 能力边界：天气类问题指向非当前时段（明天/未来几天/周末…）时，
    # 本地缓存（只有当前实况）答不了，统一转后台联网查，避免拿今天的数据冒充预报。
    if _weather_needs_forecast(raw, route) and runtime.ack_and_enqueue_enabled:
        task = BackgroundTask(task_id=_task_id(raw, "agent_lookup"), task_kind="agent_lookup", source_text=raw)
        return _result(
            VoiceTurnVerdict.ACK_AND_ENQUEUE,
            started,
            "forecast_lookup",
            speak=runtime.background_ack_reply if runtime.fast_reply_enabled else "",
            emotion="focused",
            route=route,
            task=task,
            mode=runtime.fast_reply_mode,
        )
    weather_reply, weather_path, weather_debug = _weather_reply(runtime, route_context)
    if weather_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            weather_path,
            speak=weather_reply,
            emotion="calm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"cached_weather": weather_debug},
        )
    mood_reply, mood_debug = _state_mood_reply(raw, state_summary or {})
    if mood_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "state_mood",
            speak=mood_reply,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"state_mood": mood_debug},
        )
    outing_reply, outing_debug = _outing_weather_reply(raw, local_cache or {})
    if outing_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "outing_weather_advice",
            speak=outing_reply,
            emotion="attentive",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"outing_weather": outing_debug},
        )
    latency_reply, latency_debug = _voice_latency_reply(raw)
    if latency_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "voice_latency_diagnostic",
            speak=latency_reply,
            emotion="focused",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"voice_latency": latency_debug},
        )
    quick_ack = _quick_ack_reply(raw) if runtime.quick_ack_reply_enabled else ""
    if quick_ack:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "local_social",
            speak=quick_ack,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"local_social": {"matched": "quick_ack", "local_complete": True}},
        )
    if GREETING_RE.match(raw) or GREETING_RE.match(normalized):
        speak = runtime.greeting_reply if runtime.fast_reply_enabled else ""
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "local_social",
            speak=speak,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
        )
    supportive_reply, supportive_debug = _supportive_chat_reply(raw)
    if supportive_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "supportive_chat",
            speak=supportive_reply,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"supportive_chat": supportive_debug},
        )
    if runtime.ack_and_enqueue_enabled and (AGENT_LOOKUP_RE.search(raw) or AGENT_CREATE_RE.search(raw)):
        kind = "agent_lookup" if AGENT_LOOKUP_RE.search(raw) else "agent_create"
        task = BackgroundTask(task_id=_task_id(raw, kind), task_kind=kind, source_text=raw)
        speak = runtime.background_ack_reply if runtime.fast_reply_enabled else ""
        return _result(
            VoiceTurnVerdict.ACK_AND_ENQUEUE,
            started,
            kind,
            speak=speak,
            emotion="focused",
            route=route,
            task=task,
            mode=runtime.fast_reply_mode,
        )
    status_review_reply, status_review_debug = _status_review_entry_reply(raw)
    if status_review_reply:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "status_review_entry",
            speak=status_review_reply,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"status_review": status_review_debug},
        )
    casual_preface, casual_debug = _casual_chat_preface(raw)
    if casual_preface:
        return _result(
            VoiceTurnVerdict.SPEAK_NOW,
            started,
            "casual_chat_preface",
            speak=casual_preface,
            emotion="warm",
            route=route,
            mode=runtime.fast_reply_mode,
            extra_debug={"casual_chat": casual_debug},
        )
    return _result(VoiceTurnVerdict.SPEAK_NOW, started, "normal_chat", route=route, mode=runtime.fast_reply_mode)


def _fixed_short_reply(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if not any(token in value for token in ("回答", "回复", "答复", "说")):
        match = BARE_FIXED_REPLY_RE.search(value)
        if not match:
            return ""
        if not any(token in value for token in ("字以内", "个字", "固定", "直接", "测试流式", "测试速度")):
            return ""
        reply = match.group("reply")
        return reply if reply.endswith(("。", "！", "？")) else f"{reply}。"
    if not any(token in value for token in ("字以内", "个字", "只", "就", "固定", "直接", "测试")):
        return ""
    match = FIXED_REPLY_RE.search(value)
    if not match:
        return ""
    reply = match.group("reply")
    if reply == "在":
        reply = "我在"
    return reply if reply.endswith(("。", "！", "？")) else f"{reply}。"


def _quick_ack_reply(text: str) -> str:
    value = PUNCT_RE.sub("", str(text or "")).lower()
    if not value:
        return ""
    if any(token in value for token in ("为什么", "怎么", "如何", "多少", "几点", "天气", "在哪", "干嘛", "查", "写", "生成", "总结")):
        return ""
    if QUICK_ACK_RE.match(value):
        return "我在。"
    if "测试" in value and any(token in value for token in ("简单回应", "简单回复", "一句")):
        return "我在。"
    return ""


def _voice_latency_reply(text: str) -> tuple[str, dict[str, Any]]:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    if not VOICE_LATENCY_RE.search(str(text or "")) and not VOICE_LATENCY_RE.search(clean):
        return "", {}
    if not any(token in clean for token in ("慢", "卡", "耗时", "速度", "响应", "反应", "首字", "首包", "链路")):
        return "", {}
    return (
        "主要看三段：ASR出字、模型首句、TTS首音频。现在优先压模型首句和TTS排队。",
        {"matched": "voice_latency_pipeline", "local_complete": True},
    )


def _result(
    verdict: VoiceTurnVerdict,
    started: float,
    path: str,
    *,
    speak: str = "",
    emotion: str = "neutral",
    route: str = "",
    continue_listening: bool = False,
    task: BackgroundTask | None = None,
    mode: str = "local_rule",
    extra_debug: dict[str, Any] | None = None,
) -> VoiceTurnResult:
    return VoiceTurnResult(
        verdict=verdict,
        speak_text=speak,
        emotion=emotion,
        background_task=task,
        continue_listening=continue_listening,
        debug={
            "decision_path": path,
            "route": route,
            "fast_reply_mode": mode,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            **(extra_debug or {}),
        },
    )


def _weather_reply(runtime: AuraRuntimeConfig, context: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    intent = str(context.get("intent") or "")
    if intent not in {"weather", "weather_advice"}:
        return "", "", {}
    if context.get("local_answer_allowed") is False and intent != "weather_advice":
        return "", "", {
            "skipped": "local_answer_not_allowed",
            "intent": intent,
        }
    provided = context.get("weather_snapshot") if isinstance(context.get("weather_snapshot"), dict) else None
    snapshot = provided or cached_weather_snapshot(runtime)
    debug = {
        "status": snapshot.get("status"),
        "city": snapshot.get("city"),
        "target_location": str(context.get("target_location") or "").strip(),
        "subject_entity": str(context.get("subject_entity") or "").strip(),
        "location_source": str(context.get("location_source") or "").strip(),
        "age_seconds": snapshot.get("age_seconds"),
        "source": snapshot.get("source"),
        "provided_snapshot": bool(provided),
    }
    if snapshot.get("status") == "fresh" and _weather_cache_matches_query(snapshot, context):
        debug["matched"] = True
        display = str(snapshot.get("display") or "").strip()
        if display:
            if intent == "weather_advice":
                return _fresh_weather_advice_reply(snapshot, context), "cached_weather_advice", debug
            return _fresh_weather_reply(display, context), "cached_weather", debug
    if snapshot.get("status") == "fresh":
        debug["matched"] = False
    if intent == "weather_advice":
        return _weather_advice_unavailable_reply(snapshot, context), "weather_advice_unavailable", debug
    return _weather_unavailable_reply(snapshot, context), "weather_unavailable", debug


def _weather_unavailable_reply(snapshot: dict[str, Any], context: dict[str, Any]) -> str:
    subject = str(context.get("subject_entity") or "").strip()
    target = str(context.get("target_location") or "").strip()
    status = str(snapshot.get("status") or "").strip()
    if subject == "user":
        if target:
            return f"我现在没有{target}的实时天气数据，不能乱说。"
        return "我现在还不知道你那边的位置，也没有实时天气数据，不能乱说。"
    if subject == "aura":
        if status == "stale":
            return "我这边的天气数据有点旧了，不能当实时天气说。"
        if status == "disabled":
            return "我这边现在没有启用实时天气数据，不能乱说天气。"
        return "我这边现在没有实时天气数据，不能乱说天气。"
    if target:
        return f"我现在没有{target}的实时天气数据，不能乱说。"
    return "我现在没有实时天气数据，不能乱说天气。"


def _time_reply(context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    snapshot = context.get("time_snapshot") if isinstance(context.get("time_snapshot"), dict) else {}
    subject = str(context.get("subject_entity") or "").strip()
    city = str(context.get("target_location") or "").strip()
    timezone = str(context.get("timezone") or "").strip()
    debug = {
        "status": snapshot.get("status") if snapshot else "generated",
        "city": snapshot.get("city") if snapshot else "",
        "timezone": snapshot.get("timezone") if snapshot else "",
        "subject_entity": subject,
        "location_source": str(context.get("location_source") or "").strip(),
        "provided_snapshot": bool(snapshot),
    }
    if snapshot.get("status") == "unknown":
        return "我现在还不知道你所在地，没法准确报当地时间。", debug
    if snapshot.get("display"):
        city = str(snapshot.get("city") or "").strip()
        prefix = "我这边" if subject == "aura" else (f"{city}" if city else "你那边")
        return f"{prefix}现在是{snapshot['display']}。", debug
    if subject == "user":
        if not (city or timezone):
            return "我现在还不知道你所在地，没法准确报当地时间。", debug
        snapshot = current_time_snapshot(city=city, timezone_name=timezone)
        debug.update({"city": snapshot.get("city"), "timezone": snapshot.get("timezone"), "status": "generated"})
        prefix = f"{city}" if city else "你那边"
        return f"{prefix}现在是{snapshot['display']}。", debug
    if subject == "aura":
        snapshot = current_time_snapshot(city=city)
        debug.update({"city": snapshot.get("city"), "timezone": snapshot.get("timezone"), "status": "generated"})
        return f"我这边现在是{snapshot['display']}。", debug
    return current_time_spoken_reply(), debug


def _time_weather_reply(runtime: AuraRuntimeConfig, context: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    time_reply, time_debug = _time_reply(context)
    weather_reply, weather_path, weather_debug = _weather_reply(runtime, {**context, "intent": "weather"})
    debug = {
        "decision_path": "current_time_weather",
        "time": time_debug,
        "weather": weather_debug,
        "weather_path": weather_path,
    }
    if weather_path in {"weather_unavailable", "weather_advice_unavailable"}:
        debug["decision_path"] = weather_path
    if not weather_reply:
        weather_reply = _weather_unavailable_reply({}, context)
        debug["decision_path"] = "weather_unavailable"
    return _join_time_weather_reply(time_reply, weather_reply), debug


def _join_time_weather_reply(time_reply: str, weather_reply: str) -> str:
    time_text = str(time_reply or "").strip().rstrip("。")
    weather_text = _compact_weather_clause(weather_reply)
    if not time_text:
        return weather_text + "。" if weather_text else ""
    if not weather_text:
        return time_text + "。"
    return f"{time_text}；天气是{weather_text}。"


def _compact_weather_clause(weather_reply: str) -> str:
    value = str(weather_reply or "").strip().rstrip("。")
    if value.startswith("我这边现在是："):
        value = value.removeprefix("我这边现在是：").strip()
    if value.startswith("我这边现在是:"):
        value = value.removeprefix("我这边现在是:").strip()
    if value.startswith("我这边现在"):
        value = value.removeprefix("我这边现在").strip("是：: ")
    return value


def _fresh_weather_reply(display: str, context: dict[str, Any]) -> str:
    subject = str(context.get("subject_entity") or "").strip()
    if subject == "aura":
        return f"我这边现在是：{display}。"
    return f"{display}。"


def _fresh_weather_advice_reply(snapshot: dict[str, Any], context: dict[str, Any]) -> str:
    display = str(snapshot.get("display") or "").strip()
    condition = str(snapshot.get("condition") or "").strip()
    temperature = _parse_number(snapshot.get("temperature"))
    humidity = _parse_number(snapshot.get("humidity"))
    subject = str(context.get("subject_entity") or "").strip()
    prefix = "我这边" if subject == "aura" else ""
    rainy = any(token in condition for token in ("雨", "雷", "雪", "雹"))
    if rainy:
        advice = "带一把更稳"
        reason = "主要是防雨，别淋到"
    elif temperature is not None and temperature >= 32:
        advice = "带一把更稳"
        reason = "主要是遮阳防晒"
    elif humidity is not None and humidity >= 85 and condition in {"阴", "多云", "雾"}:
        advice = "可以带一把小伞"
        reason = "湿度高，天气容易闷着变天"
    else:
        advice = "不用特意带伞"
        reason = "看起来不像马上会下雨，出门久的话再备一把"
    if prefix:
        return f"{prefix}{advice}，{reason}；依据是{display}。"
    return f"{advice}，{reason}；依据是{display}。"


def _weather_advice_unavailable_reply(snapshot: dict[str, Any], context: dict[str, Any]) -> str:
    base = _weather_unavailable_reply(snapshot, context)
    return base.replace("不能乱说天气", "不能乱给带伞或穿搭建议")


def _state_mood_reply(text: str, state_summary: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    value = PUNCT_RE.sub("", str(text or "")).lower()
    if not any(token in value for token in ("心情怎么样", "心情好吗", "开心吗", "高兴吗", "今天心情")):
        return "", {}
    mood = _as_int(state_summary.get("mood"), 80)
    energy = _as_int(state_summary.get("energy"), 65)
    stress = _as_int(state_summary.get("stress"), 0)
    trust = _as_int(state_summary.get("trust"), 50)
    if mood >= 82:
        head = "心情还挺亮的"
    elif mood >= 60:
        head = "心情还算稳"
    else:
        head = "心情有点低"
    details: list[str] = []
    if energy <= 45:
        details.append("就是能量有点低")
    elif energy >= 78:
        details.append("人也挺有劲")
    if stress >= 45:
        details.append("压力稍微有点在")
    if trust >= 70:
        details.append("跟你说话会放松一点")
    tail = "，".join(details[:2])
    reply = f"{head}，{tail}。" if tail else f"{head}。"
    return reply, {"mood": mood, "energy": energy, "stress": stress, "trust": trust}


def _outing_weather_reply(text: str, local_cache: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    if not any(token in clean for token in ("出门", "出去", "外出", "去外面", "下午出去", "下午出门")):
        return "", {}
    if not any(token in clean for token in ("今天", "下午", "一会", "等会", "现在", "待会", "打算", "准备")):
        return "", {}
    snapshot = local_cache.get("cached_weather") if isinstance(local_cache.get("cached_weather"), dict) else {}
    if snapshot.get("status") != "fresh":
        return "", {"status": snapshot.get("status") or "missing"}
    display = str(snapshot.get("display") or "").strip()
    city = str(snapshot.get("city") or "").strip()
    condition = str(snapshot.get("condition") or "").strip()
    temperature = _parse_number(snapshot.get("temperature"))
    humidity = _parse_number(snapshot.get("humidity"))
    hints: list[str] = []
    if any(token in condition for token in ("雨", "雷", "雪", "雹")):
        hints.append("带伞")
    if temperature is not None and temperature >= 30:
        hints.append("防晒")
    if humidity is not None and humidity >= 85:
        hints.append("别闷太久")
    if temperature is not None and temperature <= 8:
        hints.append("多穿一点")
    if not hints:
        hints.append("路上慢点")
    place = city or "你那边"
    if display:
        reply = f"可以呀，{place}现在{_compact_weather_display(display, place)}，出门记得{hints[0]}。"
    else:
        reply = f"可以呀，出门记得{hints[0]}。"
    return reply, {
        "status": "fresh",
        "city": city,
        "condition": condition,
        "temperature": snapshot.get("temperature"),
        "humidity": snapshot.get("humidity"),
        "hint": hints[0],
    }


def _supportive_chat_reply(text: str) -> tuple[str, dict[str, Any]]:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    if not SUPPORTIVE_RE.search(clean):
        return "", {}
    if AGENT_LOOKUP_RE.search(text) or AGENT_CREATE_RE.search(text):
        return "", {"skipped": "agent_intent"}
    if any(token in clean for token in ("为什么", "怎么", "如何", "多少", "几点", "天气", "在哪", "干嘛")):
        return "", {"skipped": "question_intent"}
    job_reply, job_debug = _job_change_chat_reply(clean)
    if job_reply:
        return job_reply, job_debug
    if "加班" in clean:
        return "加班这件事先别自己憋着，是累还是烦哪一点更多？", {
            "matched": "overtime_support",
            "fallback_only": True,
        }
    if any(token in clean for token in ("陪我", "聊两句", "说说话", "安慰")):
        reply = _stable_supportive_reply(
            clean,
            [
                "好，我陪你。你慢慢说。",
                "我在，慢慢讲给我听。",
                "好，我们先慢慢聊。",
            ],
            keep_first_when=("陪我聊两句",),
        )
        return reply, {"matched": "companionship", "fallback_only": True}
    if any(token in clean for token in ("累", "困", "压力", "焦虑", "睡不着")):
        reply = _stable_supportive_reply(
            clean,
            [
                "辛苦了，先慢一点。我在听。",
                "先缓一口气，我听着。",
                "别急，慢慢说给我听。",
            ],
        )
        return reply, {"matched": "tired_or_stressed", "fallback_only": True}
    reply = _stable_supportive_reply(
        clean,
        [
            "我在这儿陪你。你慢慢说。",
            "我听着，你可以慢慢讲。",
            "先别一个人扛着，我在。",
        ],
    )
    return reply, {"matched": "emotion", "fallback_only": True}


def _casual_chat_preface(text: str) -> tuple[str, dict[str, Any]]:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    if not clean or not CASUAL_CHAT_RE.search(clean):
        return "", {}
    if SUPPORTIVE_RE.search(clean):
        return "", {"skipped": "supportive_chat"}
    if AGENT_LOOKUP_RE.search(text) or AGENT_CREATE_RE.search(text):
        return "", {"skipped": "agent_intent"}
    if any(token in clean for token in ("为什么", "怎么", "如何", "多少", "几点", "天气", "在哪", "干嘛", "帮我", "给我")):
        return "", {"skipped": "question_or_task"}
    job_reply, job_debug = _job_change_chat_reply(clean)
    if job_reply:
        return job_reply, job_debug
    if any(token in clean for token in ("最近状态", "自然回应")):
        options = [
            "好呀，我陪你聊一会儿。你想从哪儿开始都行。",
            "可以，我们慢慢聊。你先说最近最挂心的那一点。",
            "好，我在这儿。你不用整理好再说，想到哪儿说哪儿。",
        ]
    else:
        options = [
            "先说你最想聊的那一件。",
            "先从最挂心的地方说。",
            "先说最近最占心的那一块。",
        ]
    reply = _stable_supportive_reply(clean, options)
    return reply, {"matched": "casual_open_chat", "fallback_only": True}


def _job_change_chat_reply(clean: str) -> tuple[str, dict[str, Any]]:
    if any(token in clean for token in ("换工作", "跳槽", "离职", "找工作", "新工作")):
        options = [
            "换工作这件事先拆开看：是现在耗着难受，还是新机会更吸引你？",
            "可以聊。先看动因：是不想留了，还是想要更好的机会？",
            "这事值得认真想，先说你最在意的：发展、收入，还是现在待着太消耗？",
        ]
        reply = _stable_supportive_reply(clean, options)
        return reply, {"matched": "job_change", "fallback_only": True}
    return "", {}


def _status_review_entry_reply(text: str) -> tuple[str, dict[str, Any]]:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    if not clean or not STATUS_REVIEW_RE.search(clean):
        return "", {}
    if AGENT_LOOKUP_RE.search(text) or AGENT_CREATE_RE.search(text):
        return "", {"skipped": "agent_intent"}
    if any(token in clean for token in ("为什么", "怎么", "如何", "多少", "几点", "天气", "在哪", "干嘛", "帮我", "给我")):
        return "", {"skipped": "question_or_task"}
    return "从工作节奏说起：是事情太满，还是提不起劲？", {
        "matched": "status_or_work_review",
        "fallback_only": True,
    }


def _stable_supportive_reply(text: str, options: list[str], *, keep_first_when: tuple[str, ...] = ()) -> str:
    if not options:
        return ""
    value = str(text or "")
    if any(token in value for token in keep_first_when):
        return options[0]
    digest = hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()
    index = int(digest[:8], 16) % len(options)
    return options[index]


def _asks_weather_reason(text: str) -> bool:
    clean = PUNCT_RE.sub("", str(text or "")).lower()
    return any(token in clean for token in ("为什么", "原因", "依据", "怎么判断"))


def _compact_weather_display(display: str, place: str) -> str:
    value = str(display or "").strip()
    place_text = str(place or "").strip()
    if place_text and value.startswith(place_text + "，"):
        value = value[len(place_text) + 1:]
    parts = [part.strip() for part in re.split(r"[，,]", value) if part.strip()]
    if len(parts) <= 2:
        return "，".join(parts) or value
    return "，".join(parts[:2])


def _parse_number(value: Any) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _as_int(value: Any, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, min(100, parsed))


def _weather_cache_matches_query(snapshot: dict[str, Any], context: dict[str, Any]) -> bool:
    city = normalize_city_name(snapshot.get("city") or "")
    target = normalize_city_name(context.get("target_location") or "")
    subject = str(context.get("subject_entity") or "").strip()
    source = str(context.get("location_source") or "").strip()
    if city and target:
        return city == target
    if city and not target:
        return False
    if not city and subject == "aura":
        return source in {"aura_home", "explicit_text", ""}
    return False


def _task_id(text: str, kind: str) -> str:
    digest = hashlib.sha1(f"{kind}\0{text}\0{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
    return f"voice-{digest}"


def background_task_from_llm_marker(task_text: str, *, source_text: str) -> BackgroundTask:
    """把快模型输出的 [后台] 标记转成后台任务。

    task_text 是模型自己概括的任务描述（如"查比特币当前价格"），比原话更适合
    直接交给 agent；source_text 保留用户原话做兜底。kind 沿用 agent_lookup——
    后台 goal 组装（_background_task_goal）不区分 lookup/create。
    """
    task = str(task_text or "").strip() or str(source_text or "").strip()
    kind = "agent_create" if AGENT_CREATE_RE.search(task) else "agent_lookup"
    return BackgroundTask(task_id=_task_id(task, kind), task_kind=kind, source_text=task)
