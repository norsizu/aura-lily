from __future__ import annotations

import time
from typing import Any, Iterable


# W1 世界账本：一切状态变化都有出处。
#
# 设计约束（见 lily-BuilderHub/DESIGN.md 第 2 节）：
#   * 不改数据库表结构，流水仍然写进现有的 ``companion_life_event``；
#   * ``before`` / ``after`` / ``cause`` / ``source`` / ``parent_key`` 全部放
#     ``payload_json``，老库直接兼容；
#   * 记录的是"截断后的真实变化"，所以按时间重放 delta 一定能回到当前值。
TRACKED_FIELDS = ("mood", "energy", "satiety", "stress", "trust", "beans", "affinity_xp")
# 隐藏驱动不进 UI，但仍然在动，因此单独放在 ``state["metadata"]`` 里记账。
HIDDEN_DRIVE_FIELDS = ("social_need", "curiosity", "privacy_sensitivity", "resource_comfort")
# ``coins`` 是 ``beans`` 的镜像列，只读快照里带上它，方便追溯工具展示。
SNAPSHOT_FIELDS = TRACKED_FIELDS + ("coins",)
LEDGER_EVENT_PREFIX = "ledger."
# X3 的便签钉上/清空也走账本（source="note"），数值不变，只为留一行可追溯记录。
LEDGER_SOURCE_NOTE = "note"
LEDGER_SOURCES = (
    "world_clock",
    "hunger_drive",
    "weather",
    "drive_blend",
    "dialogue",
    "emotion",
    "interaction",
    LEDGER_SOURCE_NOTE,
)
# 离线回顾里给用户看的中文字段名（W3）。
RECAP_FIELD_LABELS = {
    "mood": "心情",
    "energy": "体力",
    "satiety": "饱腹",
    "stress": "压力",
    "trust": "信任",
    "beans": "金豆",
    "affinity_xp": "好感",
    "social_need": "社交",
    "curiosity": "好奇",
    "privacy_sensitivity": "防备",
    "resource_comfort": "安心",
}
LEDGER_FIELDS = TRACKED_FIELDS + HIDDEN_DRIVE_FIELDS
# 隐藏驱动"最后一次被记账时的值"。驱动混合有 |change| >= 3 才记账的阈值
# （否则时间线会被 ±1 的漂移刷满），小于阈值的挪动会累积；基线让下一次记账
# 一次补齐这段差额，重放账本时才不会丢点数。
DRIVE_BASELINE_KEY = "drive_ledger_baseline"


def read_drive_baseline(metadata: Any) -> dict[str, int]:
    """隐藏驱动最后一次被记账时的值（没有基线时返回空 dict）。"""
    raw = metadata.get(DRIVE_BASELINE_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {field: _coerce_int(raw.get(field)) for field in HIDDEN_DRIVE_FIELDS if field in raw}


def note_drive_baseline(metadata: Any, values: dict[str, int]) -> None:
    """原地记下"这些驱动现在的值已经被账本覆盖到了"。"""
    if not isinstance(metadata, dict):
        return
    baseline = metadata.get(DRIVE_BASELINE_KEY)
    baseline = dict(baseline) if isinstance(baseline, dict) else {}
    for field, value in (values or {}).items():
        if field in HIDDEN_DRIVE_FIELDS:
            baseline[field] = _coerce_int(value)
    metadata[DRIVE_BASELINE_KEY] = baseline


def apply_world_change(
    store: Any,
    scope: Any,
    state: dict[str, Any],
    delta: dict[str, int],
    *,
    source: str,
    cause: str,
    title: str,
    event_key: str | None = None,
    parent_key: str | None = None,
    payload: dict[str, Any] | None = None,
    location: str = "",
    activity: str = "",
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Apply ``delta`` to ``state`` and write one ledger row explaining why.

    Returns ``(new_state, event_summary)``. ``event_summary`` is ``None`` when
    nothing was worth recording: either the change clamped down to zero, or the
    same ``event_key`` was already settled (idempotent world effects).
    """
    key = str(event_key or "").strip()
    if key and hasattr(store, "has_life_event") and store.has_life_event(scope, event_key=key):
        # 同一个事务 id：这件事已经结算过，既不重复应用也不重复记账。
        return state, None

    before = snapshot_state(state)
    # 隐藏驱动的"上一笔账"可能停在更早的值上：驱动混合有 |change| >= 3 的记账
    # 阈值，小于阈值的挪动不单独记。差额相对基线算，顺手把这段待记账漂移补齐，
    # 按时间重放 delta 才一定回到当前值。
    baseline = read_drive_baseline(state.get("metadata") if isinstance(state, dict) else None)
    accounted = {field: baseline.get(field, before[field]) for field in HIDDEN_DRIVE_FIELDS}
    updated = dict(state)
    _apply_ledger_delta(updated, delta or {})
    after = snapshot_state(updated)
    actual = {
        field: after[field] - (accounted[field] if field in HIDDEN_DRIVE_FIELDS else before[field])
        for field in LEDGER_FIELDS
        if after.get(field) != (accounted[field] if field in HIDDEN_DRIVE_FIELDS else before.get(field))
    }
    if not actual:
        # 全部撞到 0-100 边界（或者本来就是 0），记账只会污染时间线。
        return state, None

    record: dict[str, Any] = {
        "before": {
            field: (accounted[field] if field in HIDDEN_DRIVE_FIELDS else before[field])
            for field in LEDGER_FIELDS
        },
        "after": {field: after[field] for field in LEDGER_FIELDS},
        "delta": actual,
        "cause": str(cause or ""),
        "source": str(source or ""),
        "parent_key": str(parent_key or "") or None,
    }
    if payload:
        record.update(payload)
    if hasattr(store, "record_life_event"):
        store.record_life_event(
            scope,
            event_type=f"{LEDGER_EVENT_PREFIX}{source}",
            event_key=key or None,
            title=str(title or cause or source or "世界状态变化"),
            description=str(cause or ""),
            location=str(location or ""),
            activity=str(activity or ""),
            visibility="private",
            delta=actual,
            payload=record,
        )
    drives_changed = {field: after[field] for field in HIDDEN_DRIVE_FIELDS if field in actual}
    if drives_changed:
        note_drive_baseline(updated.get("metadata"), drives_changed)
    return updated, {
        "event_key": key,
        "source": str(source or ""),
        "title": str(title or ""),
        "cause": str(cause or ""),
        "parent_key": str(parent_key or "") or None,
        "delta": actual,
        "before": record["before"],
        "after": record["after"],
    }


def replay_ledger(events: Iterable[Any], initial: dict[str, Any] | None) -> dict[str, Any]:
    """Re-fold every ledger ``delta`` onto ``initial`` in time order.

    Used by tests to prove the ledger is complete: replaying it must reproduce
    the state the world model currently holds.
    """
    state = dict(initial or {})
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    state["metadata"] = dict(metadata)
    rows = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if not str(event.get("event_type") or "").startswith(LEDGER_EVENT_PREFIX):
            continue
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        try:
            created_at = float(event.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0.0
        rows.append((created_at, delta))
    rows.sort(key=lambda item: item[0])
    for _created_at, delta in rows:
        _apply_ledger_delta(state, delta)
    return state


def snapshot_state(state: dict[str, Any] | None) -> dict[str, int]:
    """Numeric view of a companion state (tracked fields + hidden drives)."""
    raw = state if isinstance(state, dict) else {}
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    values: dict[str, int] = {}
    for field in SNAPSHOT_FIELDS:
        fallback = _coerce_int(raw.get("coins"), 0) if field == "beans" else 0
        values[field] = _coerce_int(raw.get(field), fallback)
    for field in HIDDEN_DRIVE_FIELDS:
        values[field] = _coerce_int(metadata.get(field), 0)
    return values


def state_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int]:
    """Numeric diff between two states, limited to ledger-tracked fields."""
    left = snapshot_state(before)
    right = snapshot_state(after)
    return {
        field: right[field] - left[field]
        for field in LEDGER_FIELDS
        if right[field] != left[field]
    }


def record_state_change(
    store: Any,
    scope: Any,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    source: str,
    cause: str,
    title: str,
    event_key: str = "",
    parent_key: str | None = None,
    location: str = "",
    activity: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ledger the numeric difference between two already-computed states.

    ``apply_world_change`` mutates a state; this one is for call sites that
    already ran their own rules (``state_rules``) and only need the audit row.
    """
    delta = state_delta(before, after)
    if not delta and not read_drive_baseline(
        (before or {}).get("metadata") if isinstance(before, dict) else None
    ):
        # 一个数都没动，也没有待补齐的驱动漂移。
        return None
    _state, summary = apply_world_change(
        store,
        scope,
        before if isinstance(before, dict) else {},
        delta,
        source=source,
        cause=cause,
        title=title,
        event_key=event_key or None,
        parent_key=parent_key,
        location=location,
        activity=activity,
        payload=payload,
    )
    if summary and isinstance(after, dict):
        # 这条流水也改了隐藏驱动，基线要跟着走到 after，否则下次驱动混合
        # 会把这段差额重复记一遍。
        note_drive_baseline(after.get("metadata"), snapshot_state(after))
    return summary


def ledger_event_types(sources: Iterable[str] | None = None) -> tuple[str, ...]:
    """``event_type`` values the trace tool and prompt readers should look at."""
    names = [str(item or "").strip() for item in (sources if sources is not None else LEDGER_SOURCES)]
    names = [item for item in names if item]
    if not names:
        return (f"{LEDGER_EVENT_PREFIX}%",)
    return tuple(f"{LEDGER_EVENT_PREFIX}{item}" for item in names)


def is_ledger_event(event: Any) -> bool:
    return isinstance(event, dict) and str(event.get("event_type") or "").startswith(LEDGER_EVENT_PREFIX)


def _apply_ledger_delta(state: dict[str, Any], delta: dict[str, Any]) -> None:
    """Same clamping rules as ``world._apply_delta``: 0-100, beans floor only."""
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    metadata = dict(metadata)
    hidden_changed = False
    for raw_key, raw_value in (delta or {}).items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        try:
            value = int(round(float(raw_value)))
        except (TypeError, ValueError):
            continue
        if key in HIDDEN_DRIVE_FIELDS:
            metadata[key] = _clamp(_coerce_int(metadata.get(key), 0) + value)
            hidden_changed = True
            continue
        if key == "beans":
            fallback = _coerce_int(state.get("beans"), _coerce_int(state.get("coins"), 0))
            state["beans"] = max(0, fallback + value)
            state["coins"] = state["beans"]
            continue
        state[key] = _clamp(_coerce_int(state.get(key), 0) + value)
    if hidden_changed:
        state["metadata"] = metadata


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def format_recap_field(field: str, change: int) -> str:
    label = RECAP_FIELD_LABELS.get(str(field or ""), str(field or ""))
    if not label:
        return ""
    return f"{label}{change:+d}"


def now_ts() -> float:
    return float(time.time())
