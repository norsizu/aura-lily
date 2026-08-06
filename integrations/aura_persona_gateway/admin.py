from __future__ import annotations

import base64
import hmac
from pathlib import Path
from typing import Any

from .assets import load_persona_assets
from .config import PersonaGatewayConfig, save_runtime_config
from .runtime import AuraRuntimeConfig, save_aura_runtime_config
from .state_rules import state_context_summary
from .store import LilyPersonaStore
from .weather import refresh_cached_weather_if_needed
from .world import build_world_snapshot


def check_admin_token(config: PersonaGatewayConfig, headers: dict[str, str]) -> bool:
    return check_admin_auth(config, headers)


def check_admin_auth(config: PersonaGatewayConfig, headers: dict[str, str]) -> bool:
    if _basic_auth_matches(config, headers):
        return True
    if not config.admin_token:
        return False
    supplied = headers.get("x-aura-admin-token") or _bearer_token(headers)
    return hmac.compare_digest(supplied, config.admin_token)


def _basic_auth_matches(config: PersonaGatewayConfig, headers: dict[str, str]) -> bool:
    if not config.admin_password:
        return False
    authorization = headers.get("authorization", "").strip()
    if not authorization.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(authorization.split(" ", 1)[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, sep, password = raw.partition(":")
    if not sep:
        return False
    expected_user = config.admin_user or "admin"
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, config.admin_password)


def _bearer_token(headers: dict[str, str]) -> str:
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


def persona_health(config: PersonaGatewayConfig, store: LilyPersonaStore) -> dict[str, Any]:
    db_health = store.health()
    return {
        "ok": bool(db_health.get("ok")),
        "enabled": config.enabled,
        "config": config.public_dict(),
        "db": db_health,
    }


def update_persona_config(config: PersonaGatewayConfig, updates: dict[str, Any]) -> PersonaGatewayConfig:
    return save_runtime_config(config, updates)


def aura_runtime(config: AuraRuntimeConfig) -> dict[str, Any]:
    return {"ok": True, "config": config.public_dict()}


def aura_runtime_secret(config: AuraRuntimeConfig, key: str) -> dict[str, Any]:
    fields = {
        "aura_model_api_key": config.aura_model_api_key,
        "fast_reply_api_key": config.fast_reply_api_key,
        "tts_api_key": config.tts_api_key,
        "asr_api_key": config.asr_api_key,
    }
    if key not in fields:
        return {"ok": False, "error": "unknown secret"}
    return {"ok": True, "key": key, "value": fields[key]}


def update_aura_runtime(config: AuraRuntimeConfig, updates: dict[str, Any]) -> AuraRuntimeConfig:
    return save_aura_runtime_config(config, updates)


def refresh_aura_weather(config: AuraRuntimeConfig, *, city: str = "", force: bool = True) -> tuple[AuraRuntimeConfig, dict[str, Any]]:
    return refresh_cached_weather_if_needed(config, city=city, force=force)


def persona_assets(config: PersonaGatewayConfig) -> dict[str, Any]:
    assets = load_persona_assets(config)
    editable_path = Path(config.persona_home).expanduser() / "persona" / "soul.md"
    return {
        "ok": True,
        "available": assets.available,
        "source_path": assets.source_path,
        "editable_path": str(editable_path),
        "soul": assets.soul,
    }


def update_persona_assets(config: PersonaGatewayConfig, updates: dict[str, Any]) -> dict[str, Any]:
    soul = str(updates.get("soul") or "").strip()
    if len(soul) > 20_000:
        return {"ok": False, "error": "soul is too large; max 20000 chars"}
    editable_path = Path(config.persona_home).expanduser() / "persona" / "soul.md"
    if not soul:
        try:
            editable_path.unlink()
        except FileNotFoundError:
            pass
        return persona_assets(config)
    editable_path.parent.mkdir(parents=True, exist_ok=True)
    editable_path.write_text(soul.rstrip() + "\n", encoding="utf-8")
    return persona_assets(config)


def persona_state(config: PersonaGatewayConfig, store: LilyPersonaStore) -> dict[str, Any]:
    state = store.get_or_create_state(config.scope)
    return {
        "ok": True,
        "state": _editable_state(state),
        "summary": state_context_summary(state),
    }


def persona_world(config: PersonaGatewayConfig, store: LilyPersonaStore) -> dict[str, Any]:
    state = store.get_or_create_state(config.scope)
    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=state,
        query_context={
            "subject_entity": "aura",
            "target_location": "",
            "location_source": "admin",
            "confidence": 1.0,
            "intent": "day_plan",
            "boundary": "admin_world_view",
        },
        user_geo={},
        voice_low_latency=False,
        recent_messages=[],
    )
    return {"ok": True, "world": snapshot}


def update_persona_state(
    config: PersonaGatewayConfig,
    store: LilyPersonaStore,
    updates: dict[str, Any],
) -> dict[str, Any]:
    state = store.get_or_create_state(config.scope)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    for key in ("mood", "energy", "satiety", "trust", "stress", "affinity_xp", "beans"):
        if key in updates:
            state[key] = _coerce_int(updates.get(key), int(state.get(key) or 0))
    for key in ("scene", "outfit"):
        if key in updates:
            state[key] = str(updates.get(key) or "").strip()
    for key in (
        "current_activity",
        "current_location",
        "location_label",
        "social_need",
        "curiosity",
        "privacy_sensitivity",
    ):
        if key in updates:
            value = updates.get(key)
            metadata[key] = _coerce_int(value, 0) if key in {"social_need", "curiosity", "privacy_sensitivity"} else str(value or "").strip()
    if any(key in updates for key in ("current_activity", "current_location", "location_label")):
        metadata["world_current_source"] = "manual"
        metadata["world_manual_override"] = True
    if "relationship_strained" in updates:
        flags = metadata.get("relationship_flags") if isinstance(metadata.get("relationship_flags"), dict) else {}
        flags["strained"] = bool(updates.get("relationship_strained"))
        metadata["relationship_flags"] = flags
    state["metadata"] = metadata
    store.save_state(config.scope, state)
    return persona_state(config, store)


def _editable_state(state: dict[str, Any]) -> dict[str, Any]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    flags = metadata.get("relationship_flags") if isinstance(metadata.get("relationship_flags"), dict) else {}
    return {
        "mood": int(state.get("mood") or 0),
        "energy": int(state.get("energy") or 0),
        "satiety": int(state.get("satiety") or 0),
        "trust": int(state.get("trust") or 0),
        "stress": int(state.get("stress") or 0),
        "affinity_xp": int(state.get("affinity_xp") or 0),
        "beans": int(state.get("beans") or state.get("coins") or 0),
        "scene": str(state.get("scene") or ""),
        "outfit": str(state.get("outfit") or ""),
        "current_activity": str(metadata.get("current_activity") or ""),
        "current_location": str(metadata.get("current_location") or ""),
        "location_label": str(metadata.get("location_label") or ""),
        "world_current_source": str(metadata.get("world_current_source") or ""),
        "world_manual_override": bool(metadata.get("world_manual_override")),
        "social_need": int(metadata.get("social_need") or 0),
        "curiosity": int(metadata.get("curiosity") or 0),
        "privacy_sensitivity": int(metadata.get("privacy_sensitivity") or 0),
        "relationship_strained": bool(flags.get("strained")),
    }


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default
