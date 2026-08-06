"""语音闹钟/定时提醒的本地意图解析。

职责只有一件：把"定个11点10分的闹钟""5分钟后提醒我关火"这类话解析成
绝对触发时间（Asia/Shanghai）+ 确认话术 + 到点播报话术。
真正的定时调度在 WS 网关进程里做——那边握着设备连接，能到点主动推 TTS。

设计约束：
- 纯本地正则 + 时间计算，不碰模型，保证"说定好了"就真的定上了。
- 宁可漏判交给模型闲聊，也不误判劫持正常对话：
  只有出现"闹钟"，或"提醒/叫我"且带可解析时间时才认为是定时请求。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .time_context import DEFAULT_TIMEZONE

ALARM_HINT_RE = re.compile(r"闹钟")
REMIND_HINT_RE = re.compile(r"(提醒我|提醒一下|叫我|叫醒我)")
CANCEL_RE = re.compile(
    r"(取消|删掉|删除|不用).{0,6}(闹钟|提醒)|(闹钟|提醒).{0,6}(取消|删掉|删除|不用了|不要了)"
)

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_NUM_PATTERN = r"\d{1,3}|[零一二两三四五六七八九十]{1,3}"

RELATIVE_TIME_RE = re.compile(
    rf"(?P<num>{_NUM_PATTERN}|半)\s*(?P<half>个半)?\s*个?\s*(?P<unit>小时|分钟|秒钟|秒)\s*(?:之后|以后|过后|后)"
)
ABSOLUTE_TIME_RE = re.compile(
    rf"(?P<day>明天|明早|明晚|今晚|今天|后天)?\s*"
    rf"(?P<period>凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?\s*"
    rf"(?P<hour>{_NUM_PATTERN})\s*[点:：]\s*(?:(?P<minute>{_NUM_PATTERN})\s*分?|(?P<half>半))?"
)
_LABEL_STRIP_RE = re.compile(r"^(去|要|记得|说|一下|该)+")

_MIN_LEAD_SECONDS = 5
_MAX_LEAD_SECONDS = 7 * 24 * 3600


def _tz() -> dt.tzinfo:
    try:
        return ZoneInfo(DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return dt.timezone(dt.timedelta(hours=8), name=DEFAULT_TIMEZONE)


def _parse_int(text: str) -> int | None:
    value = str(text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    # 简体中文数字，支持 0-99：十、十一、二十、二十五……
    if value == "十":
        return 10
    if "十" in value:
        head, _, tail = value.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        if head and head not in _CN_DIGIT:
            return None
        if tail and tail not in _CN_DIGIT:
            return None
        return tens * 10 + ones
    if len(value) == 1 and value in _CN_DIGIT:
        return _CN_DIGIT[value]
    return None


def _relative_target(match: re.Match[str], now: dt.datetime) -> dt.datetime | None:
    num_text = match.group("num")
    unit = match.group("unit")
    if num_text == "半":
        minutes = 30 if unit == "小时" else 0
        if unit in {"分钟", "秒", "秒钟"}:
            return None
        return now + dt.timedelta(minutes=minutes)
    num = _parse_int(num_text)
    if num is None or num <= 0:
        return None
    if match.group("half"):  # "一个半小时后"
        if unit != "小时":
            return None
        return now + dt.timedelta(minutes=num * 60 + 30)
    if unit == "小时":
        return now + dt.timedelta(hours=num)
    if unit == "分钟":
        return now + dt.timedelta(minutes=num)
    return now + dt.timedelta(seconds=num)


def _absolute_target(match: re.Match[str], now: dt.datetime) -> dt.datetime | None:
    hour = _parse_int(match.group("hour"))
    if hour is None or hour > 24:
        return None
    if match.group("half"):
        minute = 30
    else:
        minute = _parse_int(match.group("minute") or "0")
    if minute is None or minute > 59:
        return None
    day = match.group("day") or ""
    period = match.group("period") or ""
    if day == "明早":
        day, period = "明天", period or "早上"
    elif day == "明晚":
        day, period = "明天", period or "晚上"
    elif day == "今晚":
        day, period = "今天", period or "晚上"
    day_offset = {"今天": 0, "明天": 1, "后天": 2}.get(day, 0)
    explicit_day = day in {"今天", "明天", "后天"}
    if period in {"下午", "傍晚", "晚上", "夜里"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 3:
        hour += 12
    if hour > 23:
        if hour == 24:
            hour = 0
            day_offset += 1
        else:
            return None
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + dt.timedelta(days=day_offset)
    if target <= now and not explicit_day:
        # 没说上午下午时，"3点"已过就先试当天下午 3 点，再不行滚到明天。
        if not period and hour < 12 and target + dt.timedelta(hours=12) > now:
            target += dt.timedelta(hours=12)
        else:
            target += dt.timedelta(days=1)
    return target


def _spoken_time(target: dt.datetime, now: dt.datetime) -> str:
    day_word = ""
    delta_days = (target.date() - now.date()).days
    if delta_days == 1:
        day_word = "明天"
    elif delta_days == 2:
        day_word = "后天"
    elif delta_days > 2:
        day_word = f"{target.month}月{target.day}日"
    if target.minute:
        return f"{day_word}{target.hour}点{target.minute:02d}分"
    return f"{day_word}{target.hour}点整"


def _extract_label(text: str) -> str:
    match = REMIND_HINT_RE.search(text)
    if not match:
        return ""
    tail = text[match.end():]
    # 时间短语可能跟在"提醒我"后面（"提醒我11点13分带小狗洗澡"），去掉再取事项。
    tail = ABSOLUTE_TIME_RE.sub("", tail)
    tail = RELATIVE_TIME_RE.sub("", tail)
    tail = re.sub(r"[，,。.!！?？、\s]+", "", tail)
    tail = _LABEL_STRIP_RE.sub("", tail)
    return tail[:30]


def parse_reminder_request(text: str, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """返回 None（不是定时请求）或 {"status": "cancel"|"unclear"|"ok", ...}。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if CANCEL_RE.search(raw):
        return {"status": "cancel"}
    is_alarm = bool(ALARM_HINT_RE.search(raw))
    is_remind = bool(REMIND_HINT_RE.search(raw))
    if not is_alarm and not is_remind:
        return None
    current = (now or dt.datetime.now(_tz())).astimezone(_tz())
    target: dt.datetime | None = None
    rel = RELATIVE_TIME_RE.search(raw)
    if rel:
        target = _relative_target(rel, current)
    if target is None:
        abs_match = ABSOLUTE_TIME_RE.search(raw)
        if abs_match:
            target = _absolute_target(abs_match, current)
    if target is None:
        # 只有明确说"闹钟"却给不出时间才追问；"提醒"无时间多半是闲聊，交回模型。
        return {"status": "unclear"} if is_alarm else None
    lead = (target - current).total_seconds()
    if lead < _MIN_LEAD_SECONDS or lead > _MAX_LEAD_SECONDS:
        return {"status": "unclear"}
    kind = "alarm" if is_alarm and not is_remind else "reminder"
    label = _extract_label(raw)
    spoken = _spoken_time(target, current)
    if kind == "alarm":
        confirm = f"好，{spoken}的闹钟定好了，到点我叫你。"
        announce = f"叮，{spoken}到了，闹钟时间。"
    elif label:
        confirm = f"好，{spoken}我提醒你{label}。"
        announce = f"叮，到点了：{label}。"
    else:
        confirm = f"好，{spoken}我会提醒你。"
        announce = "叮，到点了，你之前让我提醒的时间到了。"
    digest = hashlib.sha1(f"{raw}\0{target.isoformat()}\0{time.time_ns()}".encode("utf-8")).hexdigest()[:12]
    return {
        "status": "ok",
        "reminder_id": f"rem-{digest}",
        "kind": kind,
        "label": label,
        "fire_at_epoch": int(target.timestamp()),
        "fire_at_iso": target.isoformat(timespec="seconds"),
        "spoken_time": spoken,
        "confirm_text": confirm,
        "announce_text": announce,
        "created_at_epoch": int(current.timestamp()),
        "source_text": raw,
    }
