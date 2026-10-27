from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from .city_names import normalize_city_name


CITY_ALIASES = (
    "北京",
    "上海",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "成都",
    "重庆",
    "武汉",
    "西安",
    "苏州",
    "天津",
    "青岛",
    "厦门",
)
DEICTIC_PLACE_WORDS = {"你那边", "你那里", "你那儿", "你这边", "我这边", "我这里", "我这儿", "这边", "这里"}
TEMPORAL_LOCATION_SUFFIXES = ("今天", "现在", "当前", "明天", "这会儿", "这会")
NON_LOCATION_PREFIXES = (
    "我是问",
    "我想问",
    "我是说",
    "我不是",
    "想问",
    "不是问",
    "不是想问",
    "不是",
    "等一下",
    "等等",
    "不对",
    "刚才",
    "前面",
)
CORRECTION_FOCUS_MARKERS = (
    "我是问",
    "我想问",
    "我是说",
    "其实是",
    "应该是",
    "是想问",
    "是问",
    "问的是",
)
CORRECTION_START_MARKERS = (
    "等一下",
    "等等",
    "不对",
    "不是",
    "我不是",
    "我是说",
    "换个问题",
    "先别",
    "别急",
    "等下",
)
# 本地快答只接"明确在问现在几点/今天几号"的句子，其余一律交给主模型判断意图。
# 第一组本身就是问句；"时间/日期"是普通名词（休眠时间、有时间吗、花时间），必须带问法才算。
TIME_QUESTION_TOKENS = (
    "几点",
    "现在几时",
    "当前几时",
    "几号",
    "星期几",
    "周几",
    "礼拜几",
    "今天星期",
    "今天周",
    "今天礼拜",
)
TIME_NOUN_QUESTION_TOKENS = (
    "什么时间",
    "啥时间",
    "现在时间",
    "当前时间",
    "时间是多少",
    "时间多少",
    "几点钟",
    "什么日期",
    "今天日期",
    "日期是多少",
    "报时",
    "报个时",
    "报一下时",
)
TIME_TOKENS = TIME_QUESTION_TOKENS + TIME_NOUN_QUESTION_TOKENS
# 问的是哪一部分：只问日期就只答日期，只问几点就只答几点，两个都问才都答。
DATE_FOCUS_TOKENS = ("几号", "几月", "什么日期", "今天日期", "日期是多少", "什么日子", "哪天", "哪一天")
WEEKDAY_FOCUS_TOKENS = ("星期几", "周几", "礼拜几", "今天星期", "今天周", "今天礼拜")
CLOCK_FOCUS_TOKENS = ("几点", "几时", "什么时间", "啥时间", "现在时间", "当前时间", "时间是多少", "时间多少", "报时", "报个时", "报一下时")


def time_focus(clean: str) -> str:
    asks_date = any(token in clean for token in DATE_FOCUS_TOKENS)
    asks_weekday = any(token in clean for token in WEEKDAY_FOCUS_TOKENS)
    asks_clock = any(token in clean for token in CLOCK_FOCUS_TOKENS)
    if asks_clock and (asks_date or asks_weekday):
        return "both"
    if asks_clock:
        return "time"
    if asks_date:
        return "date"
    if asks_weekday:
        return "weekday"
    return "both"
# 句子是在下指令/改设置（调成、改到、设置、提醒我…），即使出现几点也不是在问时间。
COMMAND_TOKENS = (
    "调成", "调到", "调为", "调长", "调短", "调一下", "调整",
    "改成", "改到", "改为", "改一下", "改长", "改短",
    "设成", "设为", "设到", "设置", "设定", "设一下",
    "换成", "换到", "关掉", "关闭", "打开", "开启",
    "提醒我", "叫我", "闹钟", "定个", "定一个",
)
# 说的是设备本身的设置（"把休眠时间调成十分钟"）：快答照实说做不到，而不是报时。
DEVICE_SETTING_TOKENS = ("休眠", "息屏", "熄屏", "屏保", "待机", "锁屏")
DEVICE_SETTING_VERBS = COMMAND_TOKENS + ("长一点", "短一点", "久一点", "能不能改", "可以改", "多久休眠")
TEXT_NORMALIZE_TABLE = str.maketrans(
    {
        "氣": "气",
        "溫": "温",
        "麼": "么",
        "麽": "么",
        "樣": "样",
        "邊": "边",
        "裡": "里",
        "裏": "里",
        "兒": "儿",
        "幾": "几",
        "點": "点",
        "現": "现",
        "這": "这",
        "妳": "你",
    }
)


@dataclass(frozen=True)
class QueryContext:
    subject_entity: str
    target_location: str
    location_source: str
    confidence: float
    intent: str
    needs_clarification: bool = False
    boundary: str = ""
    timezone: str = ""
    local_answer_allowed: bool = True
    time_focus: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_query_context(
    text: str,
    *,
    aura_home_city: str,
    user_home_city: str = "",
    user_geo: dict[str, Any] | None = None,
) -> QueryContext:
    context = _resolve_query_context(text, aura_home_city=aura_home_city, user_home_city=user_home_city, user_geo=user_geo)
    if context.intent in {"time", "time_weather"}:
        clean = re.sub(r"[\s，。！？、,.!?~～:：；;（）()“”\"'`]+", "", correction_focus_text(str(text or "")).translate(TEXT_NORMALIZE_TABLE)).lower()
        context = replace(context, time_focus=time_focus(clean))
    return context


def _resolve_query_context(
    text: str,
    *,
    aura_home_city: str,
    user_home_city: str = "",
    user_geo: dict[str, Any] | None = None,
) -> QueryContext:
    raw = str(text or "")
    raw_for_intent = correction_focus_text(raw)
    normalized_raw = raw.translate(TEXT_NORMALIZE_TABLE)
    normalized_for_intent = raw_for_intent.translate(TEXT_NORMALIZE_TABLE)
    clean = re.sub(r"[\s，。！？、,.!?~～:：；;（）()“”\"'`]+", "", normalized_for_intent).lower()
    # "天怎么样"不算：会把"你今天怎么样"当成问天气。
    has_weather = any(token in clean for token in ("天气", "下雨", "气温", "温度", "多少度", "几度", "多少工", "冷不冷", "热不热", "带伞", "穿什么"))
    if "工资" in clean or "工时" in clean:
        has_weather = any(token in clean for token in ("天气", "下雨", "气温", "温度", "多少度", "几度", "冷不冷", "热不热", "带伞", "穿什么"))
    if has_weather and not is_weather_question(clean, raw_for_intent):
        # “今天天气有点冷，我好像感冒了”是在聊身体，不是在问天气：交给主模型判断。
        has_weather = False
    is_command = any(token in clean for token in COMMAND_TOKENS)
    has_time = not is_command and any(token in clean for token in TIME_TOKENS)
    if any(token in clean for token in DEVICE_SETTING_TOKENS) and any(token in clean for token in DEVICE_SETTING_VERBS):
        return QueryContext("device", "", "none", 0.9, "device_setting", local_answer_allowed=True)
    second_person = any(token in clean for token in ("你", "aura", "莉莉"))
    if has_time and has_weather:
        local_allowed = True
        explicit = extract_explicit_city(normalized_for_intent)
        if explicit:
            return QueryContext("location", explicit, "explicit_text", 0.96, "time_weather", timezone=_timezone_for_known_city(explicit), local_answer_allowed=local_allowed)
        if any(token in clean for token in ("你那里", "你那边", "你那儿", "你这边", "aura那里", "aura那边")):
            return QueryContext("aura", aura_home_city, "aura_home", 0.94, "time_weather", timezone=_timezone_for_known_city(aura_home_city), local_answer_allowed=local_allowed)
        city, source, timezone = _user_location(user_geo, user_home_city)
        if any(token in clean for token in ("我这里", "我这边", "我这儿", "我这")):
            if city or timezone:
                return QueryContext("user", city, source, 0.84 if source == "user_geo" else 0.72, "time_weather", timezone=timezone, local_answer_allowed=local_allowed)
            return QueryContext("user", "", "unknown", 0.35, "time_weather", needs_clarification=True, timezone=timezone, local_answer_allowed=local_allowed)
        if city or timezone:
            return QueryContext("user", city, source, 0.78 if source == "user_geo" else 0.68, "time_weather", timezone=timezone, local_answer_allowed=local_allowed)
        return QueryContext("user", "", "unknown", 0.35, "time_weather", needs_clarification=True, timezone=timezone, local_answer_allowed=local_allowed)
    if has_time:
        if any(token in clean for token in ("你那里", "你那边", "你那儿", "你这边", "aura那里", "aura那边")):
            return QueryContext("aura", aura_home_city, "aura_home", 0.9, "time")
        city, source, timezone = _user_location(user_geo, user_home_city)
        if any(token in clean for token in ("我这里", "我这边", "我这儿", "我这")):
            return QueryContext("user", city, source, 0.82 if city or timezone else 0.45, "time", needs_clarification=not bool(city or timezone), timezone=timezone)
        return QueryContext("user", city, source, 0.78 if city or timezone else 0.45, "time", needs_clarification=not bool(city or timezone), timezone=timezone)
    if has_weather:
        weather_intent = "weather_advice" if _is_weather_advice_or_reasoning(clean) else "weather"
        local_allowed = weather_intent == "weather"
        explicit = extract_explicit_city(normalized_for_intent)
        if explicit:
            return QueryContext("location", explicit, "explicit_text", 0.96, weather_intent, local_answer_allowed=local_allowed)
        if any(token in clean for token in ("你那里", "你那边", "你那儿", "你这边", "aura那里", "aura那边")):
            return QueryContext("aura", aura_home_city, "aura_home", 0.94, weather_intent, local_answer_allowed=local_allowed)
        city, source, timezone = _user_location(user_geo, user_home_city)
        if any(token in clean for token in ("我这里", "我这边", "我这儿", "我这")):
            if city:
                return QueryContext("user", city, source, 0.84 if source == "user_geo" else 0.72, weather_intent, timezone=timezone, local_answer_allowed=local_allowed)
            return QueryContext("user", "", "unknown", 0.35, weather_intent, needs_clarification=True, timezone=timezone, local_answer_allowed=local_allowed)
        if city:
            return QueryContext("user", city, source, 0.78 if source == "user_geo" else 0.68, weather_intent, timezone=timezone, local_answer_allowed=local_allowed)
        return QueryContext("user", "", "unknown", 0.35, weather_intent, needs_clarification=True, timezone=timezone, local_answer_allowed=local_allowed)
    if second_person and any(token in clean for token in ("你今天干什么", "你今天干嘛", "你今天做什么", "你今天做啥", "你今天有什么安排", "你今天去哪", "你今天去哪儿", "你今天出门吗")):
        return QueryContext("aura", "", "private", 0.9, "day_plan", boundary="whereabouts_soft")
    if second_person and any(token in clean for token in ("你在哪", "你在哪里", "你现在在哪", "你现在在哪里", "你在干嘛", "你现在干嘛", "你现在在干嘛", "你在做什么", "你现在做什么", "你现在在做什么")):
        return QueryContext("aura", "", "private", 0.9, "activity_or_location", boundary="whereabouts_soft")
    return QueryContext("unknown", "", "none", 0.0, "chat")


def correction_focus_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    normalized = raw.translate(TEXT_NORMALIZE_TABLE)
    compact = re.sub(r"\s+", "", normalized)
    if not any(marker in compact for marker in CORRECTION_START_MARKERS):
        return raw
    candidates = []
    for marker in CORRECTION_FOCUS_MARKERS:
        index = compact.rfind(marker)
        if index >= 0:
            candidates.append((index, marker))
    if not candidates:
        return raw
    index, marker = max(candidates, key=lambda item: item[0])
    tail = compact[index + len(marker):].strip("，,。.!！?？、:：；; ")
    return tail or raw


def extract_explicit_city(text: str) -> str:
    raw = str(text or "")
    for city in CITY_ALIASES:
        if city in raw and not _explicit_city_is_negated(raw, city):
            return city
    match = re.search(r"([\u4e00-\u9fff]{2,8})(?:市)?(?:今天|现在|明天|这会儿)?(?:天气|气温|温度|几度)", raw)
    if not match:
        return ""
    value = match.group(1).removesuffix("的")
    changed = True
    while changed:
        changed = False
        for prefix in NON_LOCATION_PREFIXES:
            if value.startswith(prefix):
                value = value[len(prefix):].removeprefix("的")
                changed = True
        for suffix in TEMPORAL_LOCATION_SUFFIXES:
            if value.endswith(suffix):
                value = value[: -len(suffix)].removesuffix("的")
                changed = True
    if (
        not value
        or value in DEICTIC_PLACE_WORDS
        or any(value.endswith(word) for word in DEICTIC_PLACE_WORDS)
        or any(value == prefix or value.endswith(prefix) for prefix in NON_LOCATION_PREFIXES)
        or _looks_like_non_location_fragment(value)
        or _explicit_city_is_negated(raw, value)
    ):
        return ""
    return value


def _looks_like_non_location_fragment(value: str) -> bool:
    clean = re.sub(r"[\s，。！？、,.!?~～:：；;（）()“”\"'`]+", "", str(value or ""))
    if not clean:
        return True
    if clean in CITY_ALIASES:
        return False
    if clean[:1] in {"我", "你", "他", "她", "它", "这", "那"}:
        return True
    return any(token in clean for token in ("问", "说", "懂", "测试", "问题", "回复", "回答", "不是", "想"))


def _explicit_city_is_negated(raw: str, city: str) -> bool:
    value = str(raw or "")
    target = str(city or "").strip()
    if not value or not target:
        return False
    start = value.find(target)
    if start < 0:
        return False
    prefix = re.sub(r"[\s，。！？、,.!?~～:：；;（）()“”\"'`]+", "", value[max(0, start - 8):start])
    return any(marker in prefix for marker in ("不是问", "不问", "别问", "不是"))


def _user_location(user_geo: dict[str, Any] | None, user_home_city: str) -> tuple[str, str, str]:
    geo = user_geo or {}
    city = normalize_city_name(geo.get("city") or user_home_city or "")
    timezone = str(geo.get("timezone") or geo.get("time_zone") or "").strip()
    if geo.get("city") or geo.get("latitude") or geo.get("lat") or geo.get("longitude") or geo.get("lon") or geo.get("lng") or timezone:
        return city, "user_geo", timezone
    if city:
        return city, "user_home", timezone
    return "", "unknown", timezone


def _timezone_for_known_city(city: str) -> str:
    from .time_context import timezone_for_city

    return timezone_for_city(normalize_city_name(city))


WEATHER_QUESTION_TOKENS = (
    "怎么样", "怎样", "如何", "咋样", "啥样", "什么样",
    "多少度", "几度", "多少工", "冷不冷", "热不热", "下不下", "会不会", "有没有", "要不要",
    "吗", "嘛", "呢", "么",
    "查", "看看", "看一下", "看下", "说说", "说一下", "讲讲", "报一下", "播报", "告诉我", "问一下", "问问",
    "什么天气", "啥天气", "天气情况",
)
WEATHER_TAIL_NOUNS = ("天气", "天气预报", "气温", "温度")


def is_weather_question(clean: str, raw: str = "") -> bool:
    """只有真的在问天气（问句、查询、或者“北京天气”这种短查询）才走天气快路径。"""
    text = str(clean or "")
    if not text:
        return False
    if any(mark in str(raw or "") for mark in ("?", "？")):
        return True
    if any(token in text for token in WEATHER_QUESTION_TOKENS):
        return True
    if re.search(r"(.)[不没]\1", text):  # 好不好、晴不晴
        return True
    if _is_weather_advice_or_reasoning(text):
        return True
    tail = re.sub(r"[啊呀吧了哈]+$", "", text)
    return any(tail.endswith(noun) for noun in WEATHER_TAIL_NOUNS)


def _is_weather_advice_or_reasoning(clean: str) -> bool:
    text = str(clean or "")
    if not text:
        return False
    advice_tokens = (
        "为什么",
        "为啥",
        "原因",
        "建议",
        "要不要",
        "该不该",
        "需不需要",
        "需要带伞",
        "带伞吗",
        "带不带伞",
        "穿什么",
        "穿啥",
        "怎么穿",
        "适合穿",
        "合适穿",
    )
    return any(token in text for token in advice_tokens)
