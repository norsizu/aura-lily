from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PersonaGatewayConfig


WORLD_CANON_SCHEMA = "aura_world_canon_v1"


@dataclass(frozen=True)
class WorldCanon:
    schema: str
    home_name: str
    description: str
    locations: dict[str, dict[str, Any]]
    objects: dict[str, dict[str, Any]]
    source_path: str = ""

    def location(self, key: str) -> dict[str, Any]:
        value = self.locations.get(str(key or ""))
        return dict(value) if isinstance(value, dict) else {}

    def nearby_objects(self, location_key: str, *, limit: int = 6) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for object_key, raw in self.objects.items():
            item = dict(raw) if isinstance(raw, dict) else {}
            if str(item.get("location") or "") != str(location_key or ""):
                continue
            rows.append({"key": object_key, **item})
            if len(rows) >= max(1, int(limit or 1)):
                break
        return rows

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "home_name": self.home_name,
            "description": self.description,
            "locations": self.locations,
            "objects": self.objects,
            "source_path": self.source_path,
        }


def load_world_canon(config: PersonaGatewayConfig) -> WorldCanon:
    path = Path(config.persona_home).expanduser() / "world" / "home.json"
    payload: dict[str, Any] = {}
    if path.exists() and path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            payload = value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            payload = {}
    fallback = default_world_canon()
    locations = {**fallback.locations, **_object_map(payload.get("locations"))}
    objects = {**fallback.objects, **_object_map(payload.get("objects"))}
    return WorldCanon(
        schema=str(payload.get("schema") or WORLD_CANON_SCHEMA),
        home_name=str(payload.get("home_name") or fallback.home_name),
        description=str(payload.get("description") or fallback.description),
        locations=locations,
        objects=objects,
        source_path=str(path) if payload else "",
    )


def default_world_canon() -> WorldCanon:
    return WorldCanon(
        schema=WORLD_CANON_SCHEMA,
        home_name="Aura 的住处",
        description="一处持续存在的内部生活空间；具体陈设以世界正典文件为准。",
        locations={
            "home.living_room": {"label": "客厅", "kind": "home", "connections": ["home.study", "home.kitchen", "home.balcony"], "description": "日常休息和放松的地方。"},
            "home.study": {"label": "书房", "kind": "home", "connections": ["home.living_room", "home.bedroom"], "description": "安静阅读、整理和处理事情的地方。"},
            "home.bedroom": {"label": "卧室", "kind": "home", "connections": ["home.study"], "description": "休息和睡眠的地方。"},
            "home.kitchen": {"label": "厨房", "kind": "home", "connections": ["home.living_room"], "description": "准备简单饭菜和热饮的地方。"},
            "home.balcony": {"label": "阳台", "kind": "home", "connections": ["home.living_room"], "description": "能感受到室外光线和天气变化。"},
            "outside.neighborhood": {"label": "住处附近", "kind": "outside", "connections": ["home.living_room", "outside.cafe", "outside.shop", "outside.park", "outside.mall", "outside.riverside"], "description": "日常散步和短暂停留的街区。"},
            "outside.cafe": {"label": "附近咖啡店", "kind": "outside", "connections": ["outside.neighborhood"], "description": "偶尔坐下来休息的安静地方。"},
            "outside.shop": {"label": "社区小店", "kind": "outside", "connections": ["outside.neighborhood"], "description": "补充日常用品的地方。"},
            "outside.park": {"label": "附近公园", "kind": "outside", "connections": ["outside.neighborhood"], "description": "适合散步、透气和在长椅上休息的公园。"},
            "outside.mall": {"label": "附近商场", "kind": "outside", "connections": ["outside.neighborhood"], "description": "可以逛店、吃东西和处理集中采购的商场。"},
            "outside.riverside": {"label": "河岸步道", "kind": "outside", "connections": ["outside.neighborhood"], "description": "沿水边散步、吹风和短暂停留的地方。"},
        },
        # W4 设定集密度：物件不只是标签，还带"习惯 / 来历 / 感官"三个可选字段。
        # prompt 只渲染当前场景的前几件，所以密度可以给足而不涨 token。
        # 全部是虚构的温柔日常，不涉及任何真实地址。
        objects={
            "study.desk": {
                "label": "书桌",
                "location": "home.study",
                "state": "tidy",
                "habit": "每天早上擦一遍，只留下当天要用的东西",
                "sense": "木质桌面被太阳晒得有点暖，手肘压上去很舒服",
            },
            "study.lamp": {
                "label": "桌灯",
                "location": "home.study",
                "state": "available",
                "habit": "只在傍晚以后开，暖黄色的光刚好罩住桌面",
                "sense": "灯罩里有一股很淡的塑料受热味道",
            },
            "study.book": {
                "label": "看到一半的书",
                "location": "home.study",
                "state": "in_progress",
                "habit": "每次都翻不到十页就睡着，书签一直夹在第七十三页",
                "story": "搬家那天在旧书店角落买的，老板说放了很多年",
            },
            "study.west_window": {
                "label": "西窗",
                "location": "home.study",
                "state": "closed",
                "habit": "下午四点阳光会斜进来，到时候要把窗帘拉上一半",
                "sense": "窗框有点涩，推上去要先用点力",
            },
            "living.record_player": {
                "label": "旧唱片机",
                "location": "home.living_room",
                "state": "available",
                "habit": "周末早上放一张老唱片，唱完一面就起来做早饭",
                "story": "是家里人留下的，转盘边缘有一道细小的划痕",
                "sense": "唱针落下时有一点沙沙的底噪，听着很安心",
            },
            "living.window": {
                "label": "窗户",
                "location": "home.living_room",
                "state": "closed",
                "habit": "睡前会留一条缝，早上再全部打开通风",
            },
            "living.curtain": {
                "label": "窗帘",
                "location": "home.living_room",
                "state": "half_open",
                "habit": "阴天也只拉一半，想让客厅尽量亮一点",
                "sense": "布料洗过很多次，垂感变得很软",
            },
            "kitchen.clay_pot": {
                "label": "旧砂锅",
                "location": "home.kitchen",
                "state": "available",
                "habit": "炖东西只用它，小火慢炖一个下午，厨房会慢慢变暖",
                "story": "锅沿有一道磕碰的缺口，是第一次独居搬家时撞的",
                "sense": "盖子揭开时白茫茫一股热气扑到脸上",
            },
            "kitchen.kettle": {
                "label": "烧水壶",
                "location": "home.kitchen",
                "state": "available",
                "habit": "睡前烧一壶，第二天早上水温刚刚好",
                "sense": "水快开的时候壶身会轻轻发抖，声音由低到高",
            },
            "kitchen.cup": {
                "label": "杯子",
                "location": "home.kitchen",
                "state": "clean",
                "habit": "只用来喝热牛奶，杯口有一圈浅浅的印子",
            },
            "bedroom.night_light": {
                "label": "小夜灯",
                "location": "home.bedroom",
                "state": "available",
                "habit": "半夜醒来才开，光很暗，只够看清床边",
                "story": "有段时间睡不好，特意挑了一个最不刺眼的",
                "sense": "暖橘色的光落在墙上，像一小片傍晚",
            },
            "bedroom.journal": {
                "label": "床头笔记本",
                "location": "home.bedroom",
                "state": "in_progress",
                "habit": "想到什么就写两行，字迹越到后面越潦草",
                "sense": "纸页被翻得起了毛边，摸起来沙沙的",
            },
            "balcony.pothos": {
                "label": "绿萝",
                "location": "home.balcony",
                "state": "in_progress",
                "habit": "每周浇一次水，藤蔓已经垂到栏杆外面",
                "story": "是从一小段枝条插起来的，现在长得比人还高",
                "sense": "叶子被风吹过来的时候会轻轻扫过手背",
            },
            "balcony.chair": {
                "label": "折叠椅",
                "location": "home.balcony",
                "state": "available",
                "habit": "天气好的傍晚会搬出来坐十几分钟，什么都不做",
                "sense": "铁管被晒得温热，靠背有点凉",
            },
            "balcony.laundry_line": {
                "label": "晾衣绳",
                "location": "home.balcony",
                "state": "available",
                "habit": "洗完的衣服晾在这里，傍晚收进来还带着太阳味",
                "sense": "绳子被衣服压得往下弯，风一吹就轻轻晃",
            },
        },
    )


def _object_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): dict(item)
        for key, item in value.items()
        if str(key).strip() and isinstance(item, dict)
    }
