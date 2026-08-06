from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


@dataclass(frozen=True)
class PersonaScope:
    platform: str = "esp32"
    chat_id: str = "esp32-main"
    user_id: str = "default-user"

    def as_tuple(self) -> tuple[str, str, str]:
        return self.platform, self.chat_id, self.user_id


@dataclass(frozen=True)
class PersonaGatewayConfig:
    enabled: bool = False
    persona_home: str = "/data/aura-persona"
    companion_home: str = "/data/aura-companion"
    hermes_home: str = "/home/aura/.hermes"
    config_path: str = ""
    profile: str = "default"
    platform: str = "esp32"
    chat_id: str = "esp32-main"
    user_id: str = "default-user"
    aura_home_city: str = ""
    user_home_city: str = ""
    user_timezone: str = ""
    user_latitude: str = ""
    user_longitude: str = ""
    user_location_mode: str = "device_ip"
    include_soul: bool = True
    include_state: bool = True
    world_model_enabled: bool = True
    include_recent_messages: bool = True
    include_latest_moment: bool = True
    include_today_plan: bool = True
    include_debug_context: bool = True
    recent_message_limit: int = 10
    max_soul_chars: int = 6000
    max_context_chars: int = 9000
    proactive_enabled: bool = True
    spend_enabled: bool = True
    debug_enabled: bool = True
    admin_user: str = "admin"
    admin_password: str = ""
    admin_token: str = ""

    @property
    def scope(self) -> PersonaScope:
        return PersonaScope(
            platform=self.platform,
            chat_id=self.chat_id,
            user_id=self.user_id,
        )

    @property
    def companion_db_path(self) -> Path:
        return Path(self.companion_home).expanduser() / "companion.db"

    @property
    def runtime_config_path(self) -> Path:
        if self.config_path:
            return Path(self.config_path).expanduser()
        return Path(self.persona_home).expanduser() / "config" / "persona_gateway.json"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("admin_password", None)
        data.pop("admin_token", None)
        data["admin_password_configured"] = bool(self.admin_password)
        data["admin_token_configured"] = bool(self.admin_token)
        data["companion_db_path"] = str(self.companion_db_path)
        data["runtime_config_path"] = str(self.runtime_config_path)
        return data

    def configured_user_geo(self) -> dict[str, Any]:
        mode = str(self.user_location_mode or "").strip().lower()
        if mode in {"", "off", "disabled", "none"}:
            return {}
        city = str(self.user_home_city or "").strip()
        timezone = str(self.user_timezone or "").strip()
        latitude = _coerce_float_text(self.user_latitude)
        longitude = _coerce_float_text(self.user_longitude)
        if not (city or timezone or latitude or longitude):
            return {}
        geo = {
            "city": city,
            "timezone": timezone,
            "latitude": latitude,
            "longitude": longitude,
            "source": "manual",
        }
        return {key: value for key, value in geo.items() if value not in {"", None}}


def load_persona_config() -> PersonaGatewayConfig:
    env_config = _config_from_env()
    path = env_config.runtime_config_path
    if not path.exists():
        return env_config
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return env_config
    if not isinstance(payload, dict):
        return env_config
    merged = _merge_config(env_config, payload)
    # Keep admin credentials in env only.
    return _merge_config(
        merged,
        {
            "admin_user": env_config.admin_user,
            "admin_password": env_config.admin_password,
            "admin_token": env_config.admin_token,
        },
    )


def save_runtime_config(config: PersonaGatewayConfig, updates: dict[str, Any]) -> PersonaGatewayConfig:
    allowed = {item.name for item in fields(PersonaGatewayConfig)} - {
        "persona_home",
        "companion_home",
        "hermes_home",
        "config_path",
        "admin_user",
        "admin_password",
        "admin_token",
    }
    cleaned = {key: value for key, value in updates.items() if key in allowed}
    merged = _merge_config(config, cleaned)
    path = merged.runtime_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = merged.public_dict()
    for key in (
        "companion_db_path",
        "runtime_config_path",
        "admin_password_configured",
        "admin_token_configured",
    ):
        stored.pop(key, None)
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return load_persona_config()


def _config_from_env() -> PersonaGatewayConfig:
    legacy_admin_token = os.environ.get("AURA_LILY_ADMIN_TOKEN") or os.environ.get("AURA_PERSONA_ADMIN_TOKEN", "")
    return PersonaGatewayConfig(
        enabled=_env_bool("AURA_PERSONA_ENABLED", False),
        persona_home=os.environ.get("AURA_PERSONA_HOME", "/data/aura-persona"),
        companion_home=os.environ.get("AURA_COMPANION_HOME", "/data/aura-companion"),
        hermes_home=os.environ.get("HERMES_HOME", "/home/aura/.hermes"),
        config_path=os.environ.get("AURA_PERSONA_CONFIG_PATH", ""),
        platform=os.environ.get("AURA_PERSONA_PLATFORM", "esp32"),
        chat_id=os.environ.get("AURA_PERSONA_CHAT_ID", "esp32-main"),
        user_id=os.environ.get("AURA_PERSONA_USER_ID", "default-user"),
        aura_home_city=os.environ.get("AURA_LILY_HOME_CITY", ""),
        user_home_city=os.environ.get("AURA_USER_HOME_CITY", ""),
        user_timezone=os.environ.get("AURA_USER_TIMEZONE", ""),
        user_latitude=os.environ.get("AURA_USER_LATITUDE", ""),
        user_longitude=os.environ.get("AURA_USER_LONGITUDE", ""),
        user_location_mode=os.environ.get("AURA_USER_LOCATION_MODE", "device_ip"),
        world_model_enabled=_env_bool("AURA_LILY_WORLD_MODEL_ENABLED", True),
        admin_user=os.environ.get("AURA_LILY_ADMIN_USER") or os.environ.get("AURA_PERSONA_ADMIN_USER", "admin"),
        admin_password=(
            os.environ.get("AURA_LILY_ADMIN_PASSWORD")
            or os.environ.get("AURA_PERSONA_ADMIN_PASSWORD")
            or legacy_admin_token
        ),
        admin_token=legacy_admin_token,
        debug_enabled=_env_bool("AURA_PERSONA_DEBUG_ENABLED", True),
    )


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return default


def _merge_config(config: PersonaGatewayConfig, updates: dict[str, Any]) -> PersonaGatewayConfig:
    values = asdict(config)
    field_map = {item.name: item for item in fields(PersonaGatewayConfig)}
    for key, value in updates.items():
        if key not in field_map:
            continue
        current = values[key]
        if isinstance(current, bool):
            values[key] = _coerce_bool(value, current)
        elif isinstance(current, int):
            values[key] = _coerce_int(value, current)
        else:
            values[key] = "" if value is None else str(value)
    return PersonaGatewayConfig(**values)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(float(text))
    except (TypeError, ValueError):
        return ""
