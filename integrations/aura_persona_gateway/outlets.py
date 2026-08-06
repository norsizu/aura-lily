from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OutletSignals:
    proactive_message: dict[str, Any]
    spending: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proactive_message": dict(self.proactive_message),
            "spending": dict(self.spending),
        }


def evaluate_lily_outlets(
    state_summary: dict[str, Any],
    *,
    proactive_enabled: bool,
    spend_enabled: bool,
    now: float | None = None,
) -> OutletSignals:
    ts = float(now if now is not None else time.time())
    relationship = state_summary.get("relationship") if isinstance(state_summary.get("relationship"), dict) else {}
    strained = bool(relationship.get("strained"))
    social_need = _int(state_summary.get("social_need"))
    trust = _int(state_summary.get("trust"))
    energy = _int(state_summary.get("energy"))
    stress = _int(state_summary.get("stress"))
    beans = _int(state_summary.get("beans"))
    resource_comfort = _int(state_summary.get("resource_comfort"), 50)

    proactive_score = 0
    proactive_reason = "disabled"
    if proactive_enabled and not strained:
        proactive_score = 15 + max(0, social_need - 45) + max(0, trust - 60) // 2 - max(0, stress - 45)
        proactive_reason = "social_need+trust-stress"
    proactive = {
        "enabled": bool(proactive_enabled),
        "candidate": bool(proactive_enabled and not strained and proactive_score >= 45 and energy >= 35),
        "score": max(0, min(100, proactive_score)),
        "reason": proactive_reason,
        "evaluated_at": ts,
    }

    spend_score = 0
    spend_reason = "disabled"
    if spend_enabled:
        spend_score = 20 + max(0, resource_comfort - 45) + min(25, beans // 20) - max(0, stress - 65)
        spend_reason = "resource_comfort+beans-stress"
    spending = {
        "enabled": bool(spend_enabled),
        "candidate": bool(spend_enabled and beans >= 30 and spend_score >= 45),
        "score": max(0, min(100, spend_score)),
        "budget_beans": beans,
        "reason": spend_reason,
        "evaluated_at": ts,
    }
    return OutletSignals(proactive_message=proactive, spending=spending)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default
