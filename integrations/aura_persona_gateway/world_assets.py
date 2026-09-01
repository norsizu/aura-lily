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
        objects={
            "study.desk": {"label": "书桌", "location": "home.study", "state": "tidy"},
            "study.lamp": {"label": "桌灯", "location": "home.study", "state": "available"},
            "study.book": {"label": "看到一半的书", "location": "home.study", "state": "in_progress"},
            "living.window": {"label": "窗户", "location": "home.living_room", "state": "closed"},
            "kitchen.kettle": {"label": "烧水壶", "location": "home.kitchen", "state": "available"},
            "kitchen.cup": {"label": "杯子", "location": "home.kitchen", "state": "clean"},
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
