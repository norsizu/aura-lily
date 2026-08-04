from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


LEVEL_THRESHOLDS = (0, 30, 80, 150, 250, 400, 600, 850, 1200, 2000)
LEVEL_STAGE_LABELS = {
    1: "初识",
    2: "熟悉",
    3: "友好",
    4: "亲近",
    5: "信任",
    6: "依赖",
    7: "深厚",
    8: "默契",
    9: "心意相通",
    10: "唯一",
}
LEVEL_STAGE_DESCRIPTIONS = {
    1: "保持礼貌但适度的距离感。",
    2: "可以自然聊天，语气不用太拘谨。",
    3: "语气可以更放松自然。",
    4: "可以偶尔流露在乎。",
    5: "可以更自然地流露情感。",
    6: "说话更随意亲近。",
    7: "熟悉、自在，不用刻意表演。",
    8: "很多话不用说完就懂。",
    9: "带着只有彼此之间才有的默契感。",
    10: "有专属的唯一感。",
}


@dataclass(frozen=True)
class RelationshipState:
    level: int
    label: str
    description: str
    visibility: str
    strained: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "label": self.label,
            "description": self.description,
            "visibility": self.visibility,
            "strained": self.strained,
        }


def compute_affinity_level(affinity_xp: int) -> int:
    level = 1
    for idx, threshold in enumerate(LEVEL_THRESHOLDS, start=1):
        if int(affinity_xp or 0) >= threshold:
            level = idx
    return max(1, min(10, level))


def summarize_relationship(state: dict[str, Any]) -> RelationshipState:
    metadata = _metadata(state)
    flags = metadata.get("relationship_flags") if isinstance(metadata.get("relationship_flags"), dict) else {}
    strained = bool(flags.get("strained"))
    dynamic_level = compute_affinity_level(int(state.get("affinity_xp") or 0))
    baseline_level = max(1, min(10, _coerce_int(metadata.get("relationship_baseline_level"), 1)))
    level = max(dynamic_level, baseline_level)
    trust = max(
        _clamp(state.get("trust"), 50),
        _clamp(metadata.get("relationship_baseline_trust"), 0),
    )
    if strained:
        visibility = "public"
    elif level >= 5 and trust >= 70:
        visibility = "trusted"
    elif level >= 2:
        visibility = "friends"
    else:
        visibility = "public"
    return RelationshipState(
        level=level,
        label=LEVEL_STAGE_LABELS.get(level, "初识"),
        description=LEVEL_STAGE_DESCRIPTIONS.get(level, ""),
        visibility=visibility,
        strained=strained,
    )


# ── 闲置自动恢复 ──────────────────────────────────────────────
# 聊天每轮消耗体力（-1），休息时按小时回复；饱腹向日常水平回归
# （Aura 自己会按点吃饭，甜品店投喂只是额外加成）；压力随时间消散。
RECOVERY_MIN_INTERVAL_SECONDS = 300.0   # 5 分钟内不重复结算，对话中不回体力
RECOVERY_MAX_HOURS = 48.0               # 久未开机最多按 48h 结算，防溢出
ENERGY_RECOVERY_PER_HOUR = 10
SATIETY_BASELINE = 70
SATIETY_DRIFT_PER_HOUR = 5
STRESS_DECAY_PER_HOUR = 5


def apply_time_recovery(state: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """按距上次结算的闲置时长恢复体力/回归饱腹/消散压力。

    无事可做时原样返回传入的 state（调用方可用 `is` 判断是否需要落库）。
    """
    ts = float(now if now is not None else time.time())
    metadata = _metadata(state)
    anchor = metadata.get("last_recovery_at") or metadata.get("last_interaction_at")
    try:
        anchor_ts = float(anchor)
    except (TypeError, ValueError):
        anchor_ts = 0.0
    if anchor_ts <= 0.0:
        # 首次见到该状态：只记锚点，不补历史时长。
        updated = dict(state)
        metadata["last_recovery_at"] = ts
        updated["metadata"] = metadata
        return updated
    elapsed = ts - anchor_ts
    if elapsed < RECOVERY_MIN_INTERVAL_SECONDS:
        return state
    hours = min(elapsed / 3600.0, RECOVERY_MAX_HOURS)
    updated = dict(state)
    energy = _clamp(updated.get("energy"), 100)
    updated["energy"] = min(100, energy + int(round(hours * ENERGY_RECOVERY_PER_HOUR)))
    satiety = _clamp(updated.get("satiety"), SATIETY_BASELINE)
    drift = int(round(hours * SATIETY_DRIFT_PER_HOUR))
    if satiety > SATIETY_BASELINE:
        satiety = max(SATIETY_BASELINE, satiety - drift)
    else:
        satiety = min(SATIETY_BASELINE, satiety + drift)
    updated["satiety"] = satiety
    stress = _clamp(updated.get("stress"), 0)
    updated["stress"] = max(0, stress - int(round(hours * STRESS_DECAY_PER_HOUR)))
    metadata["last_recovery_at"] = ts
    updated["metadata"] = metadata
    return updated


def apply_user_interaction_delta(state: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    # 先结算闲置恢复，再扣本轮消耗，保证长时间离开后回来体力是满的。
    updated = dict(apply_time_recovery(state, now=now))
    metadata = _metadata(updated)
    ts = float(now if now is not None else time.time())
    metadata["last_interaction_at"] = ts
    # 恢复锚点跟着每轮推进：正在对话的时段不算“闲置”，不回体力。
    metadata["last_recovery_at"] = ts
    metadata["recent_message_times"] = _recent_times(metadata, ts)
    # Do not use ``or`` here: a real zero is a valid depleted state and must
    # not silently jump back to the default value before applying the delta.
    updated["energy"] = _clamp(_coerce_int(updated.get("energy"), 100) - 1, 100)
    updated["mood"] = _clamp(_coerce_int(updated.get("mood"), 80) + 1, 80)
    updated["stress"] = _clamp(_coerce_int(updated.get("stress"), 0) - 1, 0)
    # Conversation is a real social action: it costs a little energy while
    # satisfying the social drive and giving curiosity a small nudge.
    metadata["social_need"] = max(0, min(100, _coerce_int(metadata.get("social_need"), 40) - 4))
    metadata["curiosity"] = max(0, min(100, _coerce_int(metadata.get("curiosity"), 40) + 1))
    updated["metadata"] = metadata
    return updated


def apply_agent_reply_delta(state: dict[str, Any], *, ok: bool, now: float | None = None) -> dict[str, Any]:
    updated = dict(state)
    metadata = _metadata(updated)
    metadata["last_reply_at"] = float(now if now is not None else time.time())
    if ok:
        updated["mood"] = _clamp(int(updated.get("mood") or 80) + 1, 80)
        updated["stress"] = _clamp(int(updated.get("stress") or 0) - 1, 0)
    else:
        updated["stress"] = _clamp(int(updated.get("stress") or 0) + 3, 0)
    updated["metadata"] = metadata
    return updated


def state_context_summary(state: dict[str, Any]) -> dict[str, Any]:
    metadata = _metadata(state)
    drives = metadata.get("drives_snapshot_v1") if isinstance(metadata.get("drives_snapshot_v1"), dict) else {}
    drive_values = drives.get("values") if isinstance(drives.get("values"), dict) else {}
    relationship = summarize_relationship(state)
    return {
        "mood": int(state.get("mood") or 0),
        "energy": int(state.get("energy") or 0),
        "satiety": int(state.get("satiety") or 0),
        "beans": int(state.get("beans") or state.get("coins") or 0),
        "trust": max(
            int(state.get("trust") or 0),
            _coerce_int(metadata.get("relationship_baseline_trust"), 0),
        ),
        "stress": int(state.get("stress") or 0),
        "affinity_xp": int(state.get("affinity_xp") or 0),
        "relationship": relationship.to_dict(),
        "scene": str(state.get("scene") or ""),
        "outfit": str(state.get("outfit") or ""),
        "current_activity": str(metadata.get("current_activity") or ""),
        "current_location": str(metadata.get("current_location") or state.get("scene") or ""),
        "location_label": str(metadata.get("location_label") or ""),
        "social_need": int(metadata.get("social_need") or drive_values.get("social_need") or 0),
        "curiosity": int(metadata.get("curiosity") or drive_values.get("curiosity") or 0),
        "privacy_sensitivity": int(drive_values.get("privacy_sensitivity") or metadata.get("privacy_sensitivity") or 0),
        "resource_comfort": int(drive_values.get("resource_comfort") or 0),
    }


def apply_relationship_baseline(
    state: dict[str, Any],
    *,
    level: int,
    trust: int,
) -> dict[str, Any]:
    """Persist canonical relationship history separately from dynamic interaction state."""
    baseline_level = max(1, min(10, _coerce_int(level, 1)))
    baseline_trust = max(0, min(100, _coerce_int(trust, 50)))
    metadata = _metadata(state)
    if (
        _coerce_int(metadata.get("relationship_baseline_level"), 1) == baseline_level
        and _coerce_int(metadata.get("relationship_baseline_trust"), 50) == baseline_trust
    ):
        return state
    updated = dict(state)
    metadata["relationship_baseline_level"] = baseline_level
    metadata["relationship_baseline_trust"] = baseline_trust
    updated["metadata"] = metadata
    return updated


def _recent_times(metadata: dict[str, Any], ts: float) -> list[float]:
    raw = metadata.get("recent_message_times")
    values = raw if isinstance(raw, list) else []
    recent: list[float] = []
    for item in values:
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        if ts - value <= 3600:
            recent.append(value)
    recent.append(ts)
    return recent[-20:]


def _metadata(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _clamp(value: Any, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, min(100, parsed))


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
