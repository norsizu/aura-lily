"""Small, deterministic dessert catalog and state effects.

Desserts are a transient world action rather than a scheduled life slot.  The
device owns the visible bean balance, while the server remains authoritative
for the companion stats and records the purchase in the life-event timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any


@dataclass(frozen=True)
class Dessert:
    item_id: str
    name: tuple[str, str, str]
    price: int
    effects: dict[str, int]
    effect_label: tuple[str, str, str]


DESSERT_CATALOG: tuple[Dessert, ...] = (
    # Price is not a total-power score; each item trades off four axes.
    Dessert("pudding", ("焦糖布丁", "Caramel pudding", "カスタードプリン"), 8,
            {"satiety": 8, "mood": 3, "stress": -1, "energy": 5},
            ("饱腹+8 心情+3\n压力-1 能量+5", "Sat+8 Mood+3\nStress-1 Energy+5", "満腹+8 気分+3\nストレス-1 元気+5")),
    Dessert("dorayaki", ("红豆铜锣烧", "Red bean dorayaki", "どら焼き"), 10,
            {"satiety": 11, "mood": 7, "stress": -3, "energy": 2},
            ("饱腹+11 心情+7\n压力-3 能量+2", "Sat+11 Mood+7\nStress-3 Energy+2", "満腹+11 気分+7\nストレス-3 元気+2")),
    Dessert("ice_cream", ("云朵冰淇淋", "Cloud ice cream", "雲のアイス"), 12,
            {"satiety": 5, "mood": 12, "stress": -8, "energy": -1},
            ("饱腹+5 心情+12\n压力-8 能量-1", "Sat+5 Mood+12\nStress-8 Energy-1", "満腹+5 気分+12\nストレス-8 元気-1")),
    Dessert("matcha_parfait", ("抹茶芭菲", "Matcha parfait", "抹茶パフェ"), 15,
            {"satiety": 15, "mood": 6, "stress": -4, "energy": 6},
            ("饱腹+15 心情+6\n压力-4 能量+6", "Sat+15 Mood+6\nStress-4 Energy+6", "満腹+15 気分+6\nストレス-4 元気+6")),
    Dessert("strawberry_cake", ("草莓蛋糕", "Strawberry cake", "いちごケーキ"), 18,
            {"satiety": 13, "mood": 16, "stress": -3, "energy": 0},
            ("饱腹+13 心情+16\n压力-3 能量+0", "Sat+13 Mood+16\nStress-3 Energy+0", "満腹+13 気分+16\nストレス-3 元気+0")),
    Dessert("strawberry_tart", ("莓果挞", "Berry tart", "ベリータルト"), 22,
            {"satiety": 9, "mood": 10, "stress": -12, "energy": 2},
            ("饱腹+9 心情+10\n压力-12 能量+2", "Sat+9 Mood+10\nStress-12 Energy+2", "満腹+9 気分+10\nストレス-12 元気+2")),
    Dessert("chocolate_sundae", ("巧克力圣代", "Chocolate sundae", "チョコサンデー"), 25,
            {"satiety": 22, "mood": 11, "stress": -5, "energy": -3},
            ("饱腹+22 心情+11\n压力-5 能量-3", "Sat+22 Mood+11\nStress-5 Energy-3", "満腹+22 気分+11\nストレス-5 元気-3")),
    Dessert("celebration_mille_crepe", ("庆祝千层", "Celebration mille crepe", "お祝いミルクレープ"), 30,
            {"satiety": 16, "mood": 22, "stress": -10, "energy": 4},
            ("饱腹+16 心情+22\n压力-10 能量+4", "Sat+16 Mood+22\nStress-10 Energy+4", "満腹+16 気分+22\nストレス-10 元気+4")),
)

DESSERT_BY_ID = {item.item_id: item for item in DESSERT_CATALOG}


def dessert_by_id(item_id: Any) -> Dessert | None:
    return DESSERT_BY_ID.get(str(item_id or "").strip().lower())


def apply_dessert_purchase(
    state: dict[str, Any],
    item_id: Any,
    *,
    device_beans: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Apply one purchase and return ``(updated_state, result_payload)``.

    ``device_beans`` is accepted because the device is the existing source of
    truth for its local bean wallet (clothing purchases are local too).  The
    value is only used when supplied; server-side callers can omit it.
    """
    item = dessert_by_id(item_id)
    if item is None:
        return None, {"ok": False, "status": "unknown_dessert", "error": "unknown dessert"}
    try:
        balance = int(round(float(device_beans))) if device_beans is not None else int(state.get("beans") or state.get("coins") or 0)
    except (TypeError, ValueError):
        balance = int(state.get("beans") or state.get("coins") or 0)
    balance = max(0, balance)
    if balance < item.price:
        return None, {
            "ok": False,
            "status": "insufficient_beans",
            "item_id": item.item_id,
            "price": item.price,
            "beans": balance,
        }

    updated = dict(state)
    updated["beans"] = balance - item.price
    updated["coins"] = updated["beans"]
    for key, delta in item.effects.items():
        try:
            current = int(round(float(updated.get(key, 0))))
        except (TypeError, ValueError):
            current = 0
        updated[key] = max(0, min(100, current + int(delta)))
    metadata = dict(updated.get("metadata") if isinstance(updated.get("metadata"), dict) else {})
    metadata["last_dessert_id"] = item.item_id
    metadata["last_dessert_at"] = time.time()
    updated["metadata"] = metadata
    return updated, {
        "ok": True,
        "status": "purchased",
        "item_id": item.item_id,
        "name": item.name[0],
        "price": item.price,
        "beans": updated["beans"],
        "effects": dict(item.effects),
    }
