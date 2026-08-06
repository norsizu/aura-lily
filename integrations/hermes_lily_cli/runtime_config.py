from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .bridge import DEFAULT_TIMEOUT_SECONDS, HermesLilyConfig, scrub_text, tuple_from_csv


RUNTIME_CONFIG_ENV = "AURA_LILY_HERMES_CONFIG_PATH"
LEGACY_RUNTIME_CONFIG_ENV = "HERMES_LILY_CONFIG_PATH"
MODEL_OPTIONS_ENV = "HERMES_MODEL_OPTIONS"
LEGACY_MODEL_OPTIONS_ENV = "AURA_LILY_HERMES_MODEL_OPTIONS"
MODEL_PROFILES_ENV = "AURA_LILY_MODEL_PROFILES_PATH"
MODEL_PROFILES_FILENAME = "model_profiles.json"
CONFIGURED_KEY_MARKER = "configured"
SUPPORTED_PROVIDER_TRANSPORTS = {"openai_chat", "anthropic_messages"}
DOMESTIC_PROVIDER_IDS = {
    "alibaba",
    "alibaba-coding-plan",
    "deepseek",
    "kimi-for-coding",
    "minimax",
    "minimax-cn",
    "minimax-oauth",
    "qwen-oauth",
    "stepfun",
    "stepfun-open",
    "tencent-tokenhub",
    "xiaomi",
    "zai",
}
LOCAL_PROVIDER_IDS = {"custom", "lmstudio", "local"}
CODING_PLAN_PROVIDER_IDS = {
    "alibaba-coding-plan",
    "kimi-for-coding",
    "minimax-oauth",
}
HIDDEN_PROVIDER_IDS = {"kimi-coding", "kimi-coding-cn"}
LABEL_OVERRIDES = {
    "alibaba": "Alibaba Bailian / Qwen",
    "alibaba-coding-plan": "Alibaba Coding Plan",
    "kimi-for-coding": "Kimi Coding Plan",
    "minimax-oauth": "MiniMax Coding Plan / OAuth",
    "qwen-oauth": "Alibaba Qwen OAuth",
    "zai": "GLM / Z.AI",
}
ALIAS_OVERRIDES = {
    "openrouter": [],
}
CATALOG_MODEL_HINTS = {
    "alibaba": ["qwen-plus", "qwen-max", "qwen-turbo"],
    "alibaba-coding-plan": ["qwen3-coder-plus"],
    "anthropic": ["claude-sonnet-4.6", "claude-opus-4.6"],
    "deepseek": ["deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    "gemini": ["gemini-3-pro", "gemini-2.5-pro", "gemini-2.5-flash"],
    "kimi-for-coding": ["kimi-k2-0905-preview", "kimi-k2-turbo-preview"],
    "lmstudio": ["local-model"],
    "minimax": ["MiniMax-M2", "abab6.5s-chat"],
    "minimax-cn": ["MiniMax-M2", "abab6.5s-chat"],
    "minimax-oauth": ["MiniMax-M2", "abab6.5s-chat"],
    "openrouter": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.1", "google/gemini-3-pro"],
    "qwen-oauth": ["qwen3-coder-plus", "qwen-plus"],
    "stepfun": [
        "step-3.7-flash",
        "step-router-v1",
        "stepaudio-2.5-chat",
        "step-3.5-flash-2603",
        "step-3.5-flash",
    ],
    "stepfun-open": [
        "stepaudio-2.5-chat",
        "step-3.7-flash",
        "step-3.5-flash-2603",
        "step-3.5-flash",
    ],
    "tencent-tokenhub": ["deepseek-v3", "hunyuan-turbos-latest"],
    "xiaomi": ["mimo-vl-7b", "mimo-7b"],
    "zai": ["glm-5", "glm-4.6", "glm-4.5"],
}

UPDATE_FIELDS = {
    "provider",
    "model",
    "toolsets",
    "skills",
    "timeout_seconds",
    "profile_label",
}


def runtime_config_path() -> Path | None:
    raw = os.environ.get(RUNTIME_CONFIG_ENV) or os.environ.get(LEGACY_RUNTIME_CONFIG_ENV) or ""
    text = str(raw).strip()
    if not text:
        return None
    return Path(text).expanduser()


def load_runtime_bridge_config(base: HermesLilyConfig) -> HermesLilyConfig:
    path = runtime_config_path()
    loaded = base
    if not path or not path.exists():
        payload = {}
    else:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    if isinstance(payload, dict):
        loaded = merge_bridge_config(base, payload)
    hermes = read_hermes_provider_config(loaded)
    defaults: dict[str, Any] = {}
    if not loaded.provider and hermes.get("provider"):
        defaults["provider"] = hermes.get("provider")
    if not loaded.model and hermes.get("model"):
        defaults["model"] = hermes.get("model")
    return merge_bridge_config(loaded, defaults)


def save_runtime_bridge_config(base: HermesLilyConfig, updates: dict[str, Any]) -> HermesLilyConfig:
    merged = merge_bridge_config(base, updates)
    if _should_update_hermes_config(updates):
        write_hermes_provider_config(merged, updates)
    path = runtime_config_path()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_stored_config(merged), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    provider_config = read_hermes_provider_config(merged)
    _upsert_model_profile(
        "hermes",
        {
            "label": str(updates.get("profile_label") or "").strip(),
            "provider": merged.provider,
            "model": merged.model,
            "base_url": provider_config.get("base_url", ""),
            "api_key_configured": bool(provider_config.get("api_key_configured")),
        },
    )
    return merged


def merge_bridge_config(base: HermesLilyConfig, updates: dict[str, Any]) -> HermesLilyConfig:
    cleaned: dict[str, Any] = {}
    for key, value in dict(updates or {}).items():
        if key not in UPDATE_FIELDS:
            continue
        if key in {"provider", "model"}:
            cleaned[key] = "" if value is None else str(value).strip()
        elif key in {"toolsets", "skills"}:
            cleaned[key] = tuple_from_csv(value if isinstance(value, (str, list, tuple)) else str(value))
        elif key == "timeout_seconds":
            cleaned[key] = _coerce_timeout(value, base.timeout_seconds)
    if not cleaned:
        return base
    return replace(base, **cleaned)


def public_runtime_config(config: HermesLilyConfig) -> dict[str, Any]:
    path = runtime_config_path()
    hermes_provider = read_hermes_provider_config(config)
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": hermes_provider.get("base_url", ""),
        "api_key_configured": bool(hermes_provider.get("api_key_configured")),
        "hermes_config_path": hermes_provider.get("config_path", ""),
        "toolsets": list(config.toolsets),
        "skills": list(config.skills),
        "timeout_seconds": config.timeout_seconds,
        "command": [scrub_text(part, 180) for part in config.command],
        "hermes_home_configured": bool(config.hermes_home),
        "runtime_config_path": str(path) if path else "",
        "runtime_config_persistent": bool(path),
        "runtime_config_exists": bool(path and path.exists()),
        "provider_presets": provider_presets(),
        "model_options": load_model_options(),
        "model_options_source": _model_options_source(),
        "model_profiles": model_profiles(
            kind="hermes",
            active={
                "provider": config.provider,
                "model": config.model,
                "base_url": hermes_provider.get("base_url", ""),
                "api_key_configured": bool(hermes_provider.get("api_key_configured")),
            },
        ),
        "provider_keys_managed_by": "hermes_home_private_volume",
        "lily_stores_provider_keys": True,
        "lily_returns_provider_keys": False,
        "notes": [
            "AURA_LILY_ADMIN_USER/AURA_LILY_ADMIN_PASSWORD protect this local admin API only.",
            "AURA_LILY_ADMIN_TOKEN remains accepted only as a legacy compatibility credential.",
            "Provider API keys are written to the private Hermes home volume and never returned by this API.",
            "Use provider_presets for the admin UI dropdown; custom OpenAI-compatible endpoints are supported.",
        ],
    }


def provider_presets() -> list[dict[str, Any]]:
    return [dict(item) for item in _build_provider_presets()]


@lru_cache(maxsize=1)
def load_hermes_provider_catalog() -> tuple[dict[str, Any], ...]:
    path = Path(__file__).with_name("provider_catalog.json")
    if not path.exists():
        raise FileNotFoundError(f"Hermes provider catalog is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Hermes provider catalog must be a JSON list: {path}")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get("id") or "").strip()
        transport = str(item.get("transport") or "").strip()
        if not provider_id or provider_id in seen or provider_id in HIDDEN_PROVIDER_IDS:
            continue
        if transport not in SUPPORTED_PROVIDER_TRANSPORTS:
            continue
        seen.add(provider_id)
        rows.append({
            "id": provider_id,
            "label": LABEL_OVERRIDES.get(provider_id, str(item.get("label") or provider_id).strip()),
            "source": str(item.get("source") or "").strip(),
            "transport": transport,
            "auth_type": str(item.get("auth_type") or "api_key").strip(),
            "base_url": str(item.get("base_url") or "").strip(),
            "base_url_env": str(item.get("base_url_env") or "").strip(),
            "billing_scope": str(item.get("billing_scope") or "").strip(),
            "api_key_env_vars": [str(value) for value in item.get("api_key_env_vars") or [] if str(value).strip()],
            "aliases": ALIAS_OVERRIDES.get(
                provider_id,
                [str(value) for value in item.get("aliases") or [] if str(value).strip()],
            ),
            "is_aggregator": bool(item.get("is_aggregator")),
            "region": _provider_region(provider_id, bool(item.get("is_aggregator"))),
        })
    return tuple(rows)


def _build_provider_presets() -> list[dict[str, Any]]:
    presets = [_preset_from_catalog_item(item) for item in load_hermes_provider_catalog()]
    presets.append({
        "id": "openai-compatible",
        "label": "OpenAI-compatible / 自定义 API 池",
        "provider": "custom",
        "base_url": "",
        "models": [],
        "requires_base_url": True,
        "requires_api_key": True,
        "transport": "openai_chat",
        "auth_type": "api_key",
        "aliases": ["custom", "api-pool", "openai-compatible"],
        "group": "本地/自定义",
        "is_aggregator": False,
        "api_key_env_vars": ["OPENAI_API_KEY"],
    })
    order = {"Coding Plan": 0, "国内主流": 1, "国际主流": 2, "聚合器": 3, "本地/自定义": 4}
    return sorted(presets, key=lambda item: (order.get(str(item.get("group")), 9), str(item.get("label") or "")))


def _preset_from_catalog_item(item: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(item["id"])
    auth_type = str(item.get("auth_type") or "api_key")
    base_url = str(item.get("base_url") or "")
    aliases = list(item.get("aliases") or [])
    api_key_env_vars = list(item.get("api_key_env_vars") or [])
    return {
        "id": provider_id,
        "label": str(item.get("label") or provider_id),
        "provider": provider_id,
        "base_url": base_url,
        "models": CATALOG_MODEL_HINTS.get(provider_id, []),
        "requires_base_url": not bool(base_url) and provider_id not in {"custom", "local"},
        "requires_api_key": auth_type == "api_key" and provider_id not in {"lmstudio", "local", "custom"},
        "transport": str(item.get("transport") or ""),
        "auth_type": auth_type,
        "aliases": aliases,
        "group": _provider_group(provider_id, bool(item.get("is_aggregator"))),
        "is_aggregator": bool(item.get("is_aggregator")),
        "api_key_env_vars": api_key_env_vars,
        "base_url_env": str(item.get("base_url_env") or ""),
        "billing_scope": str(item.get("billing_scope") or ""),
    }


def _provider_region(provider_id: str, is_aggregator: bool) -> str:
    if provider_id in DOMESTIC_PROVIDER_IDS:
        return "cn"
    if provider_id in LOCAL_PROVIDER_IDS:
        return "local"
    if is_aggregator:
        return "aggregator"
    return "global"


def _provider_group(provider_id: str, is_aggregator: bool) -> str:
    if provider_id in CODING_PLAN_PROVIDER_IDS:
        return "Coding Plan"
    if provider_id in DOMESTIC_PROVIDER_IDS:
        return "国内主流"
    if provider_id in LOCAL_PROVIDER_IDS:
        return "本地/自定义"
    if is_aggregator:
        return "聚合器"
    return "国际主流"


def hermes_config_path(config: HermesLilyConfig) -> Path | None:
    raw_home = config.hermes_home or os.environ.get("HERMES_HOME", "")
    if not raw_home:
        raw_home = str(Path.home() / ".hermes")
    return Path(raw_home).expanduser() / "config.yaml"


def hermes_env_path(config: HermesLilyConfig) -> Path | None:
    """hermes v0.14 起 provider key 的权威来源：HERMES_HOME/.env（优先于环境变量）。"""
    raw_home = config.hermes_home or os.environ.get("HERMES_HOME", "")
    if not raw_home:
        raw_home = str(Path.home() / ".hermes")
    return Path(raw_home).expanduser() / ".env"


def provider_api_key_env_var(provider: str) -> str:
    """按 provider 查它在 hermes 里对应的 API key 环境变量名（如 KIMI_API_KEY）。"""
    pid = str(provider or "").strip()
    if not pid:
        return ""
    for item in load_hermes_provider_catalog():
        if item["id"] == pid or pid in (item.get("aliases") or []):
            env_vars = item.get("api_key_env_vars") or []
            return str(env_vars[0]) if env_vars else ""
    return ""


def _read_env_file_var(path: Path | None, var: str) -> str:
    if not path or not var or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    prefix = f"{var}="
    value = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip().strip("\"'")
    return value


def _upsert_env_file_var(path: Path | None, var: str, value: str) -> None:
    """把 VAR=value 写入 .env：已有则原位替换（保留注释与其它行），没有则追加。"""
    if not path or not var:
        return
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    prefix = f"{var}="
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(prefix):
            if not replaced:
                out.append(f"{var}={value}")
                replaced = True
            continue  # 丢弃重复行
        out.append(line)
    if not replaced:
        out.append(f"{var}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def _remove_env_file_var(path: Path | None, var: str) -> None:
    if not path or not var or not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    prefix = f"{var}="
    out = [line for line in lines if not line.strip().startswith(prefix)]
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")


def read_hermes_provider_config(config: HermesLilyConfig) -> dict[str, Any]:
    path = hermes_config_path(config)
    if not path:
        return {"config_path": ""}
    payload = _read_yaml(path)
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    api_key = str(model.get("api_key") or "").strip()
    # key 的实际生效来源是 HERMES_HOME/.env；config.yaml 只是历史遗留。
    env_key = _read_env_file_var(
        hermes_env_path(config),
        provider_api_key_env_var(str(model.get("provider") or "")),
    )
    return {
        "config_path": str(path),
        "provider": str(model.get("provider") or "").strip(),
        "model": str(model.get("default") or model.get("model") or "").strip(),
        "base_url": str(model.get("base_url") or "").strip(),
        "api_key_configured": bool(env_key or api_key),
    }


def read_hermes_provider_secret(config: HermesLilyConfig, key: str) -> dict[str, Any]:
    if key != "api_key":
        return {"ok": False, "error": "unknown secret"}
    path = hermes_config_path(config)
    if not path:
        return {"ok": False, "error": "Hermes config path is unavailable"}
    payload = _read_yaml(path)
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    # 优先返回 .env 里实际生效的 key，config.yaml 只作回退。
    env_key = _read_env_file_var(
        hermes_env_path(config),
        provider_api_key_env_var(str(model.get("provider") or "")),
    )
    return {"ok": True, "key": key, "value": env_key or str(model.get("api_key") or "")}


def write_hermes_provider_config(config: HermesLilyConfig, updates: dict[str, Any]) -> None:
    path = hermes_config_path(config)
    if not path:
        return
    payload = _read_yaml(path)
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    provider = str(updates.get("provider") or config.provider or model.get("provider") or "").strip()
    default_model = str(updates.get("model") or config.model or model.get("default") or model.get("model") or "").strip()
    base_url = str(updates.get("base_url") if updates.get("base_url") is not None else model.get("base_url") or "").strip()
    api_key = str(updates.get("api_key") or "").strip()
    clear_api_key = bool(updates.get("clear_api_key"))
    if provider:
        model["provider"] = provider
    if default_model:
        model["default"] = default_model
    if base_url:
        model["base_url"] = base_url
    elif "base_url" in updates:
        model.pop("base_url", None)
    if clear_api_key:
        model.pop("api_key", None)
    elif api_key and api_key != CONFIGURED_KEY_MARKER:
        model["api_key"] = api_key
    payload["model"] = model
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(path, payload)
    # hermes v0.14 只从 HERMES_HOME/.env（或环境变量）读 provider key，
    # config.yaml 里的 api_key 已经不生效——后台填的 key 必须同步写进 .env，
    # 否则会出现“后台配置看着没问题、实际调用报缺 key”的错位。
    env_var = provider_api_key_env_var(provider)
    env_path = hermes_env_path(config)
    if env_var and env_path:
        if clear_api_key:
            _remove_env_file_var(env_path, env_var)
        elif api_key and api_key != CONFIGURED_KEY_MARKER:
            _upsert_env_file_var(env_path, env_var, api_key)


def load_model_options() -> list[dict[str, str]]:
    raw = os.environ.get(MODEL_OPTIONS_ENV) or os.environ.get(LEGACY_MODEL_OPTIONS_ENV) or ""
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        return _parse_json_options(text)
    return _parse_csv_options(text)


def _stored_config(config: HermesLilyConfig) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "model": config.model,
        "toolsets": list(config.toolsets),
        "skills": list(config.skills),
        "timeout_seconds": config.timeout_seconds,
    }


def model_profiles_path() -> Path:
    configured = str(os.environ.get(MODEL_PROFILES_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    runtime = runtime_config_path()
    if runtime:
        return runtime.parent / MODEL_PROFILES_FILENAME
    return Path.home() / ".aura-lily" / MODEL_PROFILES_FILENAME


def model_profiles(*, kind: str, active: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return safe saved model identities; never persist or expose API key text."""
    path = model_profiles_path()
    rows: list[dict[str, Any]] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, json.JSONDecodeError):
        payload = []
    if isinstance(payload, dict):
        payload = payload.get("profiles", [])
    if isinstance(payload, list):
        rows.extend(_clean_model_profile(item, kind=kind) for item in payload)
    rows = [row for row in rows if row]
    if active and (str(active.get("provider") or "").strip() or str(active.get("model") or "").strip()):
        prior = next((row for row in rows if _profile_signature(row) == _profile_signature({**active, "kind": kind})), {})
        current = _clean_model_profile({**prior, **active, "kind": kind}, kind=kind)
        if current:
            signature = _profile_signature(current)
            rows = [row for row in rows if _profile_signature(row) != signature]
            rows.insert(0, current)
    return rows[:24]


def _upsert_model_profile(kind: str, raw: dict[str, Any]) -> None:
    profile = _clean_model_profile({**raw, "kind": kind}, kind=kind)
    if not profile:
        return
    path = model_profiles_path()
    existing = model_profiles(kind=kind)
    signature = _profile_signature(profile)
    existing = [row for row in existing if _profile_signature(row) != signature]
    existing.insert(0, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing[:24], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clean_model_profile(raw: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or str(raw.get("kind") or kind) != kind:
        return {}
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not provider and not model:
        return {}
    return {
        "kind": kind,
        "label": str(raw.get("label") or f"{provider or '自定义'} / {model or '未命名'}").strip()[:120],
        "provider": provider,
        "model": model,
        "base_url": str(raw.get("base_url") or "").strip(),
        "api_key_configured": bool(raw.get("api_key_configured")),
        "last_used_at": str(raw.get("last_used_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    }


def _profile_signature(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("kind") or ""),
        str(item.get("provider") or ""),
        str(item.get("model") or ""),
        str(item.get("base_url") or ""),
    )


def _should_update_hermes_config(updates: dict[str, Any]) -> bool:
    return any(key in updates for key in ("api_key", "base_url", "clear_api_key", "provider", "model"))


def _parse_json_options(text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    options: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        label = str(item.get("label") or "").strip() or _default_label(provider, model)
        options.append({
            "provider": provider,
            "model": model,
            "label": label,
        })
    return options


def _parse_csv_options(text: str) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for raw_item in text.replace("\n", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        label = ""
        if "|" in item:
            item, label = [part.strip() for part in item.split("|", 1)]
        if ":" in item:
            provider, model = [part.strip() for part in item.split(":", 1)]
        else:
            provider, model = "", item
        if not model:
            continue
        options.append({
            "provider": provider,
            "model": model,
            "label": label or _default_label(provider, model),
        })
    return options


def _default_label(provider: str, model: str) -> str:
    return f"{provider} / {model}" if provider else model


def _model_options_source() -> str:
    if os.environ.get(MODEL_OPTIONS_ENV):
        return MODEL_OPTIONS_ENV
    if os.environ.get(LEGACY_MODEL_OPTIONS_ENV):
        return LEGACY_MODEL_OPTIONS_ENV
    return ""


def _coerce_timeout(value: Any, default: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    return max(1.0, timeout or DEFAULT_TIMEOUT_SECONDS)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _read_minimal_yaml(path)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        path.write_text(_dump_minimal_yaml(payload), encoding="utf-8")
        return
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _read_minimal_yaml(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    payload: dict[str, Any] = {}
    current_section = ""
    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith(" ") and raw_line.endswith(":"):
            current_section = raw_line[:-1].strip()
            payload.setdefault(current_section, {})
            continue
        if current_section == "model" and ":" in raw_line:
            key, value = raw_line.split(":", 1)
            payload.setdefault("model", {})[key.strip()] = value.strip().strip("\"'")
    return payload


def _dump_minimal_yaml(payload: dict[str, Any]) -> str:
    rows: list[str] = []
    model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
    if model:
        rows.append("model:")
        for key in ("default", "provider", "api_key", "base_url"):
            if model.get(key):
                rows.append(f"  {key}: {json.dumps(str(model[key]), ensure_ascii=False)}")
    return "\n".join(rows).rstrip() + "\n"
