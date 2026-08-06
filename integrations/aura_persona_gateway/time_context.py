from __future__ import annotations

import datetime as dt
import os
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
CITY_TIMEZONES = {
    "北京": "Asia/Shanghai",
    "上海": "Asia/Shanghai",
    "广州": "Asia/Shanghai",
    "深圳": "Asia/Shanghai",
    "杭州": "Asia/Shanghai",
    "南京": "Asia/Shanghai",
    "成都": "Asia/Shanghai",
    "重庆": "Asia/Shanghai",
    "武汉": "Asia/Shanghai",
    "西安": "Asia/Shanghai",
    "苏州": "Asia/Shanghai",
    "天津": "Asia/Shanghai",
    "青岛": "Asia/Shanghai",
    "厦门": "Asia/Shanghai",
}


def current_time_snapshot(
    *,
    now: dt.datetime | None = None,
    timezone_name: str = "",
    city: str = "",
) -> dict[str, Any]:
    timezone_name = (
        timezone_name
        or timezone_for_city(city)
        or os.environ.get("AURA_TIMEZONE")
        or os.environ.get("TZ")
        or DEFAULT_TIMEZONE
    ).strip()
    timezone = _load_timezone(timezone_name)
    current = now.astimezone(timezone) if now else dt.datetime.now(timezone)
    weekday = WEEKDAYS_ZH[current.weekday()]
    return {
        "status": "fresh",
        "iso": current.isoformat(timespec="seconds"),
        "date": current.strftime("%Y-%m-%d"),
        "time": current.strftime("%H:%M"),
        "hour": current.hour,
        "minute": current.minute,
        "weekday": weekday,
        "timezone": timezone_name or DEFAULT_TIMEZONE,
        "city": str(city or "").strip(),
        "display": f"{current.year}年{current.month}月{current.day}日，{weekday}，{current.hour:02d}点{current.minute:02d}分",
    }


def current_time_prompt_text(*, now: dt.datetime | None = None) -> str:
    snapshot = current_time_snapshot(now=now)
    return (
        f"当前日期={snapshot['date']} {snapshot['weekday']}\n"
        f"当前时间={snapshot['time']}\n"
        f"时区={snapshot['timezone']}"
    )


def current_time_spoken_reply(*, now: dt.datetime | None = None) -> str:
    snapshot = current_time_snapshot(now=now)
    return f"现在是{snapshot['display']}。"


def timezone_for_city(city: str) -> str:
    return CITY_TIMEZONES.get(str(city or "").strip(), "")


def resolve_city_tzinfo(city: str = "", timezone_name: str = "") -> dt.tzinfo:
    """按 城市 → AURA_TIMEZONE → TZ → 默认 的顺序解析 tzinfo。

    世界模型/日计划必须用所在城市的当地时间，不能依赖容器进程时区。
    """
    name = (
        timezone_name
        or timezone_for_city(city)
        or os.environ.get("AURA_TIMEZONE")
        or os.environ.get("TZ")
        or DEFAULT_TIMEZONE
    ).strip()
    return _load_timezone(name)


def unknown_time_snapshot(*, city: str = "") -> dict[str, Any]:
    return {
        "status": "unknown",
        "city": str(city or "").strip(),
        "timezone": "",
        "display": "",
    }


def _load_timezone(timezone_name: str) -> dt.tzinfo:
    name = timezone_name or DEFAULT_TIMEZONE
    if name.upper() in {"CST", "UTC+8", "GMT+8", "ASIA/SHANGHAI"}:
        try:
            return ZoneInfo(DEFAULT_TIMEZONE)
        except ZoneInfoNotFoundError:
            return dt.timezone(dt.timedelta(hours=8), name=DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return dt.timezone(dt.timedelta(hours=8), name=DEFAULT_TIMEZONE)
