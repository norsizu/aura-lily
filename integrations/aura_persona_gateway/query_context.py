from __future__ import annotations

import re
from dataclasses import asdict, dataclass
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
TIME_TOKENS = (
    "几点",
    "时间",
    "现在几时",
    "当前几时",
    "几号",
    "日期",
    "星期几",
    "周几",
    "礼拜几",
    "今天星期",
    "今天周",
    "今天礼拜",
)
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_query_context(
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
    has_weather = any(token in clean for token in ("天气", "天是怎么样", "天怎么样", "下雨", "气温", "温度", "多少度", "几度", "多少工", "冷不冷", "热不热", "带伞", "穿什么"))
    if "工资" in clean or "工时" in clean:
        has_weather = any(token in clean for token in ("天气", "下雨", "气温", "温度", "多少度", "几度", "冷不冷", "热不热", "带伞", "穿什么"))
    has_time = any(token in clean for token in TIME_TOKENS)
    second_person = any(token in clean for token in ("你", "aura", "豆豆"))
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
