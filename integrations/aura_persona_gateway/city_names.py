from __future__ import annotations

from typing import Any


CITY_NAME_ALIASES: dict[str, str] = {
    "beijing": "北京",
    "peking": "北京",
    "北京市": "北京",
    "北京": "北京",
    "shanghai": "上海",
    "上海市": "上海",
    "上海": "上海",
    "guangzhou": "广州",
    "canton": "广州",
    "广州市": "广州",
    "广州": "广州",
    "shenzhen": "深圳",
    "深圳市": "深圳",
    "深圳": "深圳",
    "hangzhou": "杭州",
    "杭州市": "杭州",
    "杭州": "杭州",
    "nanjing": "南京",
    "南京市": "南京",
    "南京": "南京",
    "chengdu": "成都",
    "成都市": "成都",
    "成都": "成都",
    "chongqing": "重庆",
    "重庆市": "重庆",
    "重慶": "重庆",
    "重庆": "重庆",
    "wuhan": "武汉",
    "武汉市": "武汉",
    "武漢": "武汉",
    "武汉": "武汉",
    "xian": "西安",
    "xi'an": "西安",
    "xi an": "西安",
    "西安市": "西安",
    "西安": "西安",
    "suzhou": "苏州",
    "苏州市": "苏州",
    "蘇州": "苏州",
    "苏州": "苏州",
    "tianjin": "天津",
    "天津市": "天津",
    "天津": "天津",
    "qingdao": "青岛",
    "青岛市": "青岛",
    "青島": "青岛",
    "青岛": "青岛",
    "xiamen": "厦门",
    "厦门市": "厦门",
    "廈門": "厦门",
    "厦门": "厦门",
}


def normalize_city_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = " ".join(text.replace("_", " ").replace("-", " ").split()).lower()
    compact = key.replace(" ", "")
    return CITY_NAME_ALIASES.get(key) or CITY_NAME_ALIASES.get(compact) or CITY_NAME_ALIASES.get(text) or text
