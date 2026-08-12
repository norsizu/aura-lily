from __future__ import annotations

import json
import os
import time
import hashlib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .city_names import normalize_city_name
from .config import FALSE_VALUES, TRUE_VALUES


CONFIGURED_VALUE_MARKER = "configured"
RUNTIME_CONFIG_ENV = "AURA_LILY_AURA_RUNTIME_CONFIG_PATH"

FAST_REPLY_MODES = {"local_rule", "hermes_main", "light_model"}
AURA_MODEL_MODES = {"hermes_main", "hermes_agent", "aura_model", "direct_llm"}
ASR_MODES = {"local", "api"}
HISTORY_LIMIT = 12
PROFILE_LIMIT = 24
WEATHER_CACHE_LIMIT = 12
LOCAL_ASR_HTTP_BASE_URL = "http://127.0.0.1:8766/v1"
AURA_MODEL_REASONING_EFFORTS = {"", "none", "low", "medium", "high"}
PROVIDER_OPTION_SECRET_KEYS = {
    "access_token",
    "api_key",
    "app_key",
    "authorization",
    "client_secret",
    "secret",
    "secret_key",
    "token",
}
TTS_PROVIDERS = [
    {
        "id": "none",
        "label": "暂不启用 TTS",
        "provider": "none",
        "base_url": "",
        "models": [],
        "voices": [],
        "requires_api_key": False,
        "requires_base_url": False,
    },
    {
        "id": "openai",
        "label": "OpenAI-compatible TTS",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini-tts", "tts-1"],
        "voices": ["alloy", "verse", "nova"],
        "group": "OpenAI-compatible",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "aliyun-nls",
        "label": "阿里云智能语音 NLS",
        "provider": "aliyun-nls",
        "base_url": "https://nls-gateway-cn-shanghai.aliyuncs.com",
        "models": ["nls-tts"],
        "voices": ["xiaoyun", "xiaogang", "ruoxi"],
        "group": "国内云服务",
        "description": "NLS HTTP TTS。Provider 选项填写 appkey 与 token；token 也可填在 API Key。输出使用 PCM。",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "volcengine",
        "label": "火山引擎语音合成",
        "provider": "volcengine",
        "base_url": "https://openspeech.bytedance.com/api/v1/tts",
        "models": ["volcano_tts"],
        "voices": [],
        "group": "国内云服务",
        "description": "火山引擎 HTTP TTS。Provider 选项填写 appid、cluster；API Key 填 access_token。",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "baidu",
        "label": "百度智能云语音合成",
        "provider": "baidu",
        "base_url": "https://tsn.baidu.com/text2audio",
        "models": ["baidu-tts"],
        "voices": [],
        "group": "国内云服务",
        "description": "百度 REST TTS。Provider 选项填写 client_id/client_secret，或直接填写 access_token。",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "minimax",
        "label": "MiniMax 语音合成",
        "provider": "minimax",
        "base_url": "https://api.minimaxi.com/v1/t2a_v2",
        "models": ["speech-2.6-hd", "speech-2.6-turbo"],
        "voices": [],
        "group": "国内云服务",
        "description": "MiniMax HTTP TTS。API Key 填 access token，Voice 填系统音色或 voice id。",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "tencent",
        "label": "腾讯云语音合成",
        "provider": "tencent",
        "base_url": "https://tts.tencentcloudapi.com",
        "models": ["tencent-tts"],
        "voices": ["101001"],
        "group": "国内云服务",
        "description": "腾讯云 TC3 TTS。Provider 选项填写 secret_id、secret_key、region；Voice 填数字音色 ID。",
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "stepfun-realtime",
        "label": "StepFun Realtime TTS",
        "provider": "stepfun",
        "base_url": "https://api.stepfun.com/v1",
        "models": ["stepaudio-2.5-tts"],
        "voices": [],
        "group": "实时语音（可选）",
        "description": "可选 WebSocket TTS 适配器。仅在已经拥有对应服务凭据时选择。",
        "route": "ws_tts",
        "streaming": True,
        "requires_api_key": True,
        "requires_base_url": False,
    },
    {
        "id": "voxcpm",
        "label": "VoxCPM (self-hosted)",
        "provider": "voxcpm",
        "base_url": "",
        "models": ["voxcpm2"],
        "voices": [],
        "group": "自托管",
        "requires_api_key": False,
        "requires_base_url": True,
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI-compatible TTS",
        "provider": "custom",
        "base_url": "",
        "models": [],
        "voices": [],
        "group": "自托管",
        "requires_api_key": True,
        "requires_base_url": True,
    },
    {
        "id": "custom-http",
        "label": "自定义 HTTP TTS endpoint",
        "provider": "custom-http",
        "base_url": "",
        "models": [],
        "voices": [],
        "group": "自托管",
        "requires_api_key": True,
        "requires_base_url": True,
    },
]
ASR_PROVIDERS = [
    {
        "id": "local-whisper-http",
        "label": "本机 Whisper HTTP ASR",
        "provider": "custom",
        "base_url": LOCAL_ASR_HTTP_BASE_URL,
        "models": ["whisper-base-local"],
        "group": "本地与自托管",
        "requires_api_key": False,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "local-whisper",
        "label": "本地 Whisper / faster-whisper",
        "provider": "local",
        "base_url": "",
        "models": ["whisper-large-v3", "faster-whisper-large-v3", "whisper-base"],
        "group": "本地与自托管",
        "requires_api_key": False,
        "requires_base_url": False,
        "mode": "local",
    },
    {
        "id": "openai",
        "label": "OpenAI-compatible ASR",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-transcribe", "whisper-1"],
        "group": "OpenAI-compatible",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "aliyun-nls",
        "label": "阿里云智能语音 NLS",
        "provider": "aliyun-nls",
        "base_url": "https://nls-gateway-cn-shanghai.aliyuncs.com",
        "models": ["nls-asr"],
        "group": "国内云服务",
        "description": "NLS REST ASR。Provider 选项填写 appkey 与 token；token 也可填在 API Key。",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "volcengine",
        "label": "火山引擎录音文件识别",
        "provider": "volcengine",
        "base_url": "wss://openspeech.bytedance.com/api/v2/asr",
        "models": ["volcengine-asr"],
        "group": "国内云服务",
        "description": "火山引擎非流式 ASR。Provider 选项填写 appid、cluster；API Key 填 access_token。",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "qwen3-asr",
        "label": "阿里云百炼 Qwen3-ASR-Flash",
        "provider": "qwen3-asr",
        "base_url": "https://dashscope.aliyuncs.com/api/v1",
        "models": ["qwen3-asr-flash"],
        "group": "国内云服务",
        "description": "通过 DashScope SDK 调用 Qwen3-ASR-Flash；需要额外安装 dashscope，API Key 填 DashScope key。",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "baidu",
        "label": "百度智能云语音识别",
        "provider": "baidu",
        "base_url": "https://vop.baidu.com/server_api",
        "models": ["baidu-asr"],
        "group": "国内云服务",
        "description": "百度 REST ASR。Provider 选项填写 client_id/client_secret，或直接填写 access_token。",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "tencent",
        "label": "腾讯云一句话识别",
        "provider": "tencent",
        "base_url": "https://asr.tencentcloudapi.com",
        "models": ["tencent-asr"],
        "group": "国内云服务",
        "description": "腾讯云 TC3 ASR。Provider 选项填写 secret_id、secret_key、region。",
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "stepfun-realtime",
        "label": "StepFun Realtime ASR",
        "provider": "stepfun",
        "base_url": "https://api.stepfun.com/v1",
        "models": ["stepaudio-2.5-asr-stream"],
        "group": "实时语音（可选）",
        "description": "可选实时 ASR 适配器。仅在已经拥有对应服务凭据时选择。",
        "route": "realtime_ws",
        "streaming": True,
        "requires_api_key": True,
        "requires_base_url": False,
        "mode": "api",
    },
    {
        "id": "custom",
        "label": "自定义 ASR API",
        "provider": "custom",
        "base_url": "",
        "models": [],
        "group": "自托管",
        "requires_api_key": True,
        "requires_base_url": True,
        "mode": "api",
    },
]


@dataclass(frozen=True)
class AuraRuntimeConfig:
    persona_home: str = ".aura/persona"
    config_path: str = ""
    aura_model_mode: str = "hermes_main"
    aura_model_provider: str = ""
    aura_model_model: str = ""
    aura_model_base_url: str = ""
    aura_model_api_key: str = ""
    aura_model_timeout_seconds: int = 90
    aura_model_max_tokens: int = 96
    aura_model_temperature: str = "0.4"
    aura_model_reasoning_effort: str = "none"
    fast_reply_enabled: bool = True
    fast_reply_mode: str = "hermes_main"
    fast_reply_provider: str = ""
    fast_reply_model: str = ""
    fast_reply_base_url: str = ""
    fast_reply_api_key: str = ""
    fast_reply_timeout_seconds: int = 8
    voice_turn_enabled: bool = True
    ack_and_enqueue_enabled: bool = True
    # 本地快捷应答（“测试一下”→“我在。”等模板回复）。关闭后这类话术会走真实模型。
    quick_ack_reply_enabled: bool = True
    greeting_reply: str = "嗯，在。"
    clarify_reply: str = "你刚才那句没说完整，再说一遍？"
    refuse_reply: str = "这个我不能帮你做。"
    background_ack_reply: str = "好，我去查，弄完马上告诉你。"
    cached_weather_enabled: bool = True
    cached_weather_city: str = ""
    cached_weather_temperature: str = ""
    cached_weather_condition: str = ""
    cached_weather_icon: int = 0
    cached_weather_humidity: str = ""
    cached_weather_source: str = ""
    cached_weather_observed_at: str = ""
    cached_weather_updated_at: int = 0
    cached_weather_ttl_seconds: int = 3600
    weather_provider: str = "open_meteo"
    weather_auto_refresh_enabled: bool = True
    weather_refresh_interval_seconds: int = 1800
    weather_request_timeout_seconds: int = 8
    weather_latitude: str = ""
    weather_longitude: str = ""
    weather_last_error: str = ""
    user_weather_cache: tuple[dict[str, Any], ...] = ()
    tts_enabled: bool = False
    tts_provider: str = "none"
    tts_model: str = ""
    tts_voice: str = ""
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_format: str = "pcm"
    tts_sample_rate: int = 24000
    tts_timeout_seconds: int = 15
    tts_provider_options: dict[str, Any] = field(default_factory=dict)
    tts_profiles: tuple[dict[str, Any], ...] = ()
    asr_enabled: bool = True
    asr_mode: str = "api"
    asr_provider: str = "custom"
    asr_model: str = "ggml-small.bin"
    asr_base_url: str = LOCAL_ASR_HTTP_BASE_URL
    asr_api_key: str = ""
    asr_language: str = "zh"
    asr_timeout_seconds: int = 30
    asr_provider_options: dict[str, Any] = field(default_factory=dict)
    asr_profiles: tuple[dict[str, Any], ...] = ()
    dialogue_quota_limit: int = 50
    dialogue_quota_window_seconds: int = 5 * 60 * 60
    config_history: tuple[dict[str, Any], ...] = ()
    model_profiles: tuple[dict[str, Any], ...] = ()
    profile_label: str = ""

    @property
    def runtime_config_path(self) -> Path:
        if self.config_path:
            return Path(self.config_path).expanduser()
        return Path(self.persona_home).expanduser() / "config" / "aura_runtime.json"

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        aura_key = bool(str(data.pop("aura_model_api_key", "")).strip())
        fast_key = bool(str(data.pop("fast_reply_api_key", "")).strip())
        tts_key = bool(str(data.pop("tts_api_key", "")).strip())
        asr_key = bool(str(data.pop("asr_api_key", "")).strip())
        tts_options = _public_provider_options(data.pop("tts_provider_options", {}))
        asr_options = _public_provider_options(data.pop("asr_provider_options", {}))
        data["aura_model_api_key_configured"] = aura_key
        data["fast_reply_api_key_configured"] = fast_key
        data["tts_api_key_configured"] = tts_key
        data["asr_api_key_configured"] = asr_key
        data["tts_provider_options"] = tts_options
        data["asr_provider_options"] = asr_options
        data["tts_provider_options_configured"] = _provider_option_secret_status(self.tts_provider_options)
        data["asr_provider_options_configured"] = _provider_option_secret_status(self.asr_provider_options)
        data["runtime_config_path"] = str(self.runtime_config_path)
        data["aura_model_modes"] = [
            {
                "id": "hermes_main",
                "label": "通过 Hermes CLI Agent",
                "description": "Aura 回复交给 Hermes CLI；适合需要工具、文件、网页和后台任务的回合。",
            },
            {
                "id": "aura_model",
                "label": "直接调用 Aura LLM",
                "description": "普通对话直接走 Aura 上游模型，不进入 Hermes Agent 执行层。",
            },
        ]
        data["fast_reply_modes"] = [
            {
                "id": "hermes_main",
                "label": "关闭快答，交给 Aura 对话模型",
                "description": "语音回合只做策略标记，回复仍由 Aura 对话模型生成。",
            },
            {
                "id": "local_rule",
                "label": "本地规则短答",
                "description": "打招呼、追问、拒绝等低风险短句直接返回，普通对话仍交给 Aura 对话模型。",
            },
            {
                "id": "light_model",
                "label": "旧轻量快答配置 [已并入 Aura 对话模型]",
                "description": "保留兼容旧配置；新配置请使用 Aura 对话模型。",
                "status": "deprecated",
            },
        ]
        data["tts_provider_presets"] = tts_provider_presets()
        data["asr_provider_presets"] = asr_provider_presets()
        data["tts_profiles"] = list(_profiles_with_defaults("tts", self.tts_profiles))
        data["asr_profiles"] = list(_profiles_with_defaults("asr", self.asr_profiles))
        data["cached_weather"] = cached_weather_snapshot(self)
        data["cached_weather_fresh"] = data["cached_weather"].get("status") == "fresh"
        data["cached_weather_age_seconds"] = data["cached_weather"].get("age_seconds")
        data["config_history"] = list(self.config_history or ())
        data["model_profiles"] = list(_model_profiles_with_active(self.model_profiles, self))
        data["notes"] = [
            "Aura runtime controls Aura main model selection, ASR, local voice shortcuts, and TTS/device behavior.",
            "Hermes remains the execution bridge; Aura can reuse the Hermes main model or call Hermes with an Aura-specific provider/model override.",
            "Fast reply is a local cached/ack layer; it is not Aura's base model.",
            "Runtime API keys live in the private runtime volume and are returned only by explicit admin reveal endpoints; history stores non-secret model settings only.",
        ]
        return data


def load_aura_runtime_config(*, persona_home: str = "") -> AuraRuntimeConfig:
    env_config = _config_from_env(persona_home=persona_home)
    path = env_config.runtime_config_path
    if not path.exists():
        return env_config
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return env_config
    if not isinstance(payload, dict):
        return env_config
    return _merge_config(env_config, payload, preserve_existing_secrets=True)


def save_aura_runtime_config(config: AuraRuntimeConfig, updates: dict[str, Any]) -> AuraRuntimeConfig:
    merged = _merge_config(config, updates, preserve_existing_secrets=True)
    path = merged.runtime_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = _stored_dict(merged)
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return load_aura_runtime_config(persona_home=merged.persona_home)


def tts_provider_presets() -> list[dict[str, Any]]:
    return [dict(item) for item in TTS_PROVIDERS]


def asr_provider_presets() -> list[dict[str, Any]]:
    return [dict(item) for item in ASR_PROVIDERS]


def voice_latency_path(config: AuraRuntimeConfig) -> dict[str, Any]:
    """Describe the selected voice pipeline without provider-specific billing logic.

    This is intentionally a configuration summary rather than a latency promise:
    actual latency is measured by the gateway and its benchmark tool.
    """

    asr_enabled = bool(config.asr_enabled)
    asr_api = asr_enabled and config.asr_mode == "api"
    asr_configured = bool(
        asr_enabled
        and (
            config.asr_mode == "local"
            or (str(config.asr_provider or "").strip() and str(config.asr_base_url or "").strip())
        )
    )
    tts_enabled = bool(config.tts_enabled)
    tts_configured = bool(
        tts_enabled
        and str(config.tts_provider or "").strip().lower() not in {"", "none"}
        and bool(str(config.tts_model or "").strip() or str(config.tts_base_url or "").strip())
    )
    llm_direct = config.aura_model_mode in {"aura_model", "direct_llm"}
    llm_configured = bool(
        not llm_direct
        or (str(config.aura_model_provider or "").strip() and str(config.aura_model_model or "").strip())
    )
    asr_label = "API ASR" if asr_api else "本地 ASR" if asr_enabled else "ASR 未启用"
    tts_label = "TTS 已配置" if tts_configured else "TTS 未启用" if not tts_enabled else "TTS 待补全"
    llm_label = "Aura 直接 LLM" if llm_direct else "Hermes CLI Agent"
    ready = bool(asr_configured and tts_configured and llm_configured)
    return {
        "ready": ready,
        "asr_enabled": asr_enabled,
        "asr_configured": asr_configured,
        "tts_enabled": tts_enabled,
        "tts_configured": tts_configured,
        "llm_direct": llm_direct,
        "llm_configured": llm_configured,
        "asr_label": asr_label,
        "llm_label": llm_label,
        "tts_label": tts_label,
        "summary": (
            "语音链路配置完整。"
            if ready
            else "语音链路尚未完整配置。"
        ),
    }


def cached_weather_snapshot(config: AuraRuntimeConfig, *, now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    enabled = bool(config.cached_weather_enabled)
    city = normalize_city_name(config.cached_weather_city)
    temperature = str(config.cached_weather_temperature or "").strip()
    condition = str(config.cached_weather_condition or "").strip()
    humidity = str(config.cached_weather_humidity or "").strip()
    updated_at = max(0, int(config.cached_weather_updated_at or 0))
    ttl_seconds = max(60, int(config.cached_weather_ttl_seconds or 3600))
    has_content = bool(temperature or condition)
    age_seconds = max(0, int(current - updated_at)) if updated_at else None
    if not enabled:
        status = "disabled"
    elif not has_content:
        status = "empty"
    elif not updated_at:
        status = "stale"
    elif age_seconds is not None and age_seconds > ttl_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "enabled": enabled,
        "status": status,
        "city": city,
        "temperature": temperature,
        "condition": condition,
        "weather_icon": max(0, min(3, int(config.cached_weather_icon or 0))),
        "humidity": humidity,
        "source": str(config.cached_weather_source or "").strip(),
        "observed_at": str(config.cached_weather_observed_at or "").strip(),
        "updated_at": updated_at,
        "ttl_seconds": ttl_seconds,
        "age_seconds": age_seconds,
        "has_content": has_content,
        "display": _weather_display(city=city, temperature=temperature, condition=condition, humidity=humidity),
    }


def _weather_display(*, city: str, temperature: str, condition: str, humidity: str = "") -> str:
    parts = []
    if city:
        parts.append(city)
    if temperature:
        suffix = "" if temperature.endswith(("度", "℃", "C", "c")) else "度"
        parts.append(f"{temperature}{suffix}")
    if condition:
        parts.append(condition)
    if humidity:
        suffix = "" if humidity.endswith("%") else "%"
        parts.append(f"湿度{humidity}{suffix}")
    return "，".join(parts)


def _config_from_env(*, persona_home: str = "") -> AuraRuntimeConfig:
    return AuraRuntimeConfig(
        persona_home=persona_home or os.environ.get("AURA_PERSONA_HOME", ".aura/persona"),
        config_path=os.environ.get(RUNTIME_CONFIG_ENV, ""),
        aura_model_mode=_env_choice("AURA_MODEL_MODE", "hermes_main", AURA_MODEL_MODES),
        aura_model_provider=os.environ.get("AURA_MODEL_PROVIDER", ""),
        aura_model_model=os.environ.get("AURA_MODEL_MODEL", ""),
        aura_model_base_url=os.environ.get("AURA_MODEL_BASE_URL", ""),
        aura_model_api_key=os.environ.get("AURA_MODEL_API_KEY", ""),
        aura_model_timeout_seconds=_env_int("AURA_MODEL_TIMEOUT_SECONDS", 90),
        aura_model_max_tokens=_env_int("AURA_MODEL_MAX_TOKENS", 96),
        aura_model_temperature=os.environ.get("AURA_MODEL_TEMPERATURE", "0.4"),
        aura_model_reasoning_effort=_env_choice("AURA_MODEL_REASONING_EFFORT", "none", AURA_MODEL_REASONING_EFFORTS),
        fast_reply_enabled=_env_bool("AURA_FAST_REPLY_ENABLED", True),
        fast_reply_mode=_env_choice("AURA_FAST_REPLY_MODE", "hermes_main", FAST_REPLY_MODES),
        fast_reply_provider=os.environ.get("AURA_FAST_REPLY_PROVIDER", ""),
        fast_reply_model=os.environ.get("AURA_FAST_REPLY_MODEL", ""),
        fast_reply_base_url=os.environ.get("AURA_FAST_REPLY_BASE_URL", ""),
        fast_reply_api_key=os.environ.get("AURA_FAST_REPLY_API_KEY", ""),
        voice_turn_enabled=_env_bool("AURA_VOICE_TURN_ENABLED", True),
        ack_and_enqueue_enabled=_env_bool("AURA_ACK_AND_ENQUEUE_ENABLED", True),
        quick_ack_reply_enabled=_env_bool("AURA_QUICK_ACK_REPLY_ENABLED", True),
        cached_weather_enabled=_env_bool("AURA_CACHED_WEATHER_ENABLED", True),
        cached_weather_city=os.environ.get("AURA_CACHED_WEATHER_CITY", ""),
        cached_weather_temperature=os.environ.get("AURA_CACHED_WEATHER_TEMPERATURE", ""),
        cached_weather_condition=os.environ.get("AURA_CACHED_WEATHER_CONDITION", ""),
        cached_weather_icon=_env_int("AURA_CACHED_WEATHER_ICON", 0),
        cached_weather_humidity=os.environ.get("AURA_CACHED_WEATHER_HUMIDITY", ""),
        cached_weather_source=os.environ.get("AURA_CACHED_WEATHER_SOURCE", ""),
        cached_weather_observed_at=os.environ.get("AURA_CACHED_WEATHER_OBSERVED_AT", ""),
        cached_weather_updated_at=_env_int("AURA_CACHED_WEATHER_UPDATED_AT", 0),
        cached_weather_ttl_seconds=_env_int("AURA_CACHED_WEATHER_TTL_SECONDS", 3600),
        weather_provider=os.environ.get("AURA_WEATHER_PROVIDER", "open_meteo"),
        weather_auto_refresh_enabled=_env_bool("AURA_WEATHER_AUTO_REFRESH_ENABLED", True),
        weather_refresh_interval_seconds=_env_int("AURA_WEATHER_REFRESH_INTERVAL_SECONDS", 1800),
        weather_request_timeout_seconds=_env_int("AURA_WEATHER_REQUEST_TIMEOUT_SECONDS", 8),
        weather_latitude=os.environ.get("AURA_WEATHER_LATITUDE", ""),
        weather_longitude=os.environ.get("AURA_WEATHER_LONGITUDE", ""),
        weather_last_error=os.environ.get("AURA_WEATHER_LAST_ERROR", ""),
        tts_enabled=_env_bool("AURA_TTS_ENABLED", False),
        tts_provider=os.environ.get("AURA_TTS_PROVIDER", "none"),
        tts_model=os.environ.get("AURA_TTS_MODEL", ""),
        tts_voice=os.environ.get("AURA_TTS_VOICE", ""),
        tts_base_url=os.environ.get("AURA_TTS_BASE_URL", ""),
        tts_api_key=os.environ.get("AURA_TTS_API_KEY", ""),
        tts_format=os.environ.get("AURA_TTS_FORMAT", "pcm"),
        tts_sample_rate=_env_int("AURA_TTS_SAMPLE_RATE", 24000),
        tts_timeout_seconds=_env_int("AURA_TTS_TIMEOUT_SECONDS", 15),
        tts_provider_options=_env_json_object("AURA_TTS_PROVIDER_OPTIONS"),
        tts_profiles=_default_audio_profiles("tts"),
        asr_enabled=_env_bool("AURA_ASR_ENABLED", True),
        asr_mode=_env_choice("AURA_ASR_MODE", "api", ASR_MODES),
        asr_provider=os.environ.get("AURA_ASR_PROVIDER", "custom"),
        asr_model=os.environ.get("AURA_ASR_MODEL", "whisper-base-local"),
        asr_base_url=os.environ.get("AURA_ASR_BASE_URL", LOCAL_ASR_HTTP_BASE_URL),
        asr_api_key=os.environ.get("AURA_ASR_API_KEY", ""),
        asr_language=os.environ.get("AURA_ASR_LANGUAGE", "zh"),
        asr_timeout_seconds=_env_int("AURA_ASR_TIMEOUT_SECONDS", 30),
        asr_provider_options=_env_json_object("AURA_ASR_PROVIDER_OPTIONS"),
        asr_profiles=_default_audio_profiles("asr"),
        dialogue_quota_limit=_env_int("AURA_DIALOGUE_QUOTA_LIMIT", 50),
        dialogue_quota_window_seconds=_env_int("AURA_DIALOGUE_QUOTA_WINDOW_SECONDS", 5 * 60 * 60),
    )


def _stored_dict(config: AuraRuntimeConfig) -> dict[str, Any]:
    data = asdict(config)
    data.pop("persona_home", None)
    data.pop("config_path", None)
    return data


def _merge_config(
    config: AuraRuntimeConfig,
    updates: dict[str, Any],
    *,
    preserve_existing_secrets: bool,
) -> AuraRuntimeConfig:
    values = asdict(config)
    field_map = {item.name: item for item in fields(AuraRuntimeConfig)}
    allowed = set(field_map) - {"persona_home", "config_path"}
    weather_update_keys = {
        "cached_weather_city",
        "cached_weather_temperature",
        "cached_weather_condition",
        "cached_weather_icon",
        "cached_weather_humidity",
        "cached_weather_source",
        "cached_weather_observed_at",
    }
    weather_fields_seen = False
    weather_fields_changed = False
    for key, value in dict(updates or {}).items():
        if key == "profile_label":
            values["profile_label"] = str(value or "").strip()[:120]
            continue
        if key == "clear_aura_model_api_key":
            if _coerce_bool(value, False):
                values["aura_model_api_key"] = ""
            continue
        if key == "clear_fast_reply_api_key":
            if _coerce_bool(value, False):
                values["fast_reply_api_key"] = ""
            continue
        if key == "clear_tts_api_key":
            if _coerce_bool(value, False):
                values["tts_api_key"] = ""
            continue
        if key == "clear_asr_api_key":
            if _coerce_bool(value, False):
                values["asr_api_key"] = ""
            continue
        if key == "touch_cached_weather":
            if _coerce_bool(value, False):
                values["cached_weather_updated_at"] = int(time.time())
            continue
        if key == "clear_cached_weather":
            if _coerce_bool(value, False):
                values["cached_weather_city"] = ""
                values["cached_weather_temperature"] = ""
                values["cached_weather_condition"] = ""
                values["cached_weather_icon"] = 0
                values["cached_weather_humidity"] = ""
                values["cached_weather_source"] = ""
                values["cached_weather_observed_at"] = ""
                values["cached_weather_updated_at"] = 0
            continue
        if key not in allowed:
            continue
        if key in weather_update_keys:
            weather_fields_seen = True
            before = getattr(config, key, "")
            weather_fields_changed = weather_fields_changed or str(value or "").strip() != str(before or "").strip()
        if key in {"aura_model_api_key", "fast_reply_api_key", "tts_api_key", "asr_api_key"}:
            text = "" if value is None else str(value).strip()
            if preserve_existing_secrets and (not text or text == CONFIGURED_VALUE_MARKER):
                continue
            values[key] = text
            continue
        if key == "tts_profiles":
            values[key] = _coerce_audio_profiles("tts", value)
            continue
        if key == "asr_profiles":
            values[key] = _coerce_audio_profiles("asr", value)
            continue
        if key in {"tts_provider_options", "asr_provider_options"}:
            values[key] = _coerce_provider_options(
                value,
                existing=values.get(key),
                preserve_existing_secrets=preserve_existing_secrets,
            )
            continue
        if key == "user_weather_cache":
            values[key] = _coerce_weather_cache(value)
            continue
        if key == "config_history":
            values[key] = _coerce_history(value)
            continue
        if key == "model_profiles":
            values[key] = _coerce_model_profiles(value)
            continue
        current = values[key]
        if isinstance(current, bool):
            values[key] = _coerce_bool(value, current)
        elif isinstance(current, int):
            values[key] = _coerce_int(value, current)
        else:
            values[key] = "" if value is None else str(value).strip()
    values["aura_model_mode"] = _choice(values["aura_model_mode"], config.aura_model_mode, AURA_MODEL_MODES)
    values["aura_model_timeout_seconds"] = max(1, int(values["aura_model_timeout_seconds"] or 1))
    values["aura_model_max_tokens"] = max(16, min(1024, int(values["aura_model_max_tokens"] or 96)))
    values["aura_model_temperature"] = _temperature_text(values["aura_model_temperature"], config.aura_model_temperature)
    values["aura_model_reasoning_effort"] = _choice(
        values["aura_model_reasoning_effort"],
        config.aura_model_reasoning_effort,
        AURA_MODEL_REASONING_EFFORTS,
    )
    values["fast_reply_mode"] = _choice(values["fast_reply_mode"], config.fast_reply_mode, FAST_REPLY_MODES)
    values["fast_reply_timeout_seconds"] = max(1, int(values["fast_reply_timeout_seconds"] or 1))
    values["cached_weather_icon"] = max(0, min(3, int(values["cached_weather_icon"] or 0)))
    values["cached_weather_updated_at"] = max(0, int(values["cached_weather_updated_at"] or 0))
    values["cached_weather_ttl_seconds"] = max(60, int(values["cached_weather_ttl_seconds"] or 3600))
    values["weather_provider"] = str(values.get("weather_provider") or "open_meteo").strip() or "open_meteo"
    values["weather_refresh_interval_seconds"] = max(60, int(values["weather_refresh_interval_seconds"] or 1800))
    values["weather_request_timeout_seconds"] = max(1, int(values["weather_request_timeout_seconds"] or 8))
    values["user_weather_cache"] = _coerce_weather_cache(values.get("user_weather_cache"))
    if weather_fields_seen and (not values["cached_weather_updated_at"] or weather_fields_changed):
        has_weather_value = bool(
            str(values.get("cached_weather_temperature") or "").strip()
            or str(values.get("cached_weather_condition") or "").strip()
        )
        if values["cached_weather_enabled"] and has_weather_value:
            values["cached_weather_updated_at"] = int(time.time())
    values["tts_sample_rate"] = max(8000, int(values["tts_sample_rate"] or 24000))
    values["tts_timeout_seconds"] = max(1, int(values["tts_timeout_seconds"] or 15))
    values["tts_provider_options"] = _coerce_provider_options(values.get("tts_provider_options"))
    if not values["tts_provider"]:
        values["tts_provider"] = "none"
    values["asr_mode"] = _choice(values["asr_mode"], config.asr_mode, ASR_MODES)
    values["asr_timeout_seconds"] = max(1, int(values["asr_timeout_seconds"] or 30))
    values["asr_provider_options"] = _coerce_provider_options(values.get("asr_provider_options"))
    values["dialogue_quota_limit"] = max(
        1,
        min(100_000, int(values["dialogue_quota_limit"] or 50)),
    )
    values["dialogue_quota_window_seconds"] = max(
        60,
        min(7 * 24 * 60 * 60, int(values["dialogue_quota_window_seconds"] or 5 * 60 * 60)),
    )
    if not values["asr_provider"]:
        values["asr_provider"] = "local" if values["asr_mode"] == "local" else "custom"
    values["tts_profiles"] = _updated_audio_profiles("tts", values)
    values["asr_profiles"] = _updated_audio_profiles("asr", values)
    values["config_history"] = _updated_history(values)
    values["model_profiles"] = _updated_model_profiles(values)
    return AuraRuntimeConfig(**values)


def _coerce_weather_cache(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {"key": key}
        for name in (
            "city",
            "temperature",
            "condition",
            "humidity",
            "source",
            "observed_at",
            "display",
            "latitude",
            "longitude",
        ):
            text = str(raw.get(name) or "").strip()
            if text:
                row[name] = normalize_city_name(text) if name == "city" else text
        row["weather_icon"] = max(0, min(3, _coerce_int(raw.get("weather_icon"), 0)))
        row["updated_at"] = max(0, _coerce_int(raw.get("updated_at"), 0))
        row["ttl_seconds"] = max(60, _coerce_int(raw.get("ttl_seconds"), 3600))
        rows.append(row)
        if len(rows) >= WEATHER_CACHE_LIMIT:
            break
    return tuple(rows)


def _default_audio_profiles(kind: str) -> tuple[dict[str, Any], ...]:
    if kind == "tts":
        return ()
    if kind == "asr":
        return tuple(
            _coerce_audio_profiles(
                "asr",
                [
                    {
	                        "id": "asr-local-whisper-http",
	                        "label": "本机 Whisper HTTP (small)",
	                        "enabled": True,
                        "mode": "api",
                        "provider": "custom",
                        "model": "ggml-small.bin",
                        "base_url": LOCAL_ASR_HTTP_BASE_URL,
                        "language": "zh",
                        "timeout_seconds": 60,
                        "builtin": True,
                    },
                    {
                        "id": "asr-local-whisper-command",
                        "label": "本地命令 Whisper",
                        "enabled": True,
                        "mode": "local",
                        "provider": "local",
                        "model": "whisper-large-v3",
                        "base_url": "",
                        "language": "zh",
                        "timeout_seconds": 30,
                        "builtin": True,
                    },
                ],
            )
        )
    return ()


def _profiles_with_defaults(kind: str, profiles: Any) -> tuple[dict[str, Any], ...]:
    return _merge_audio_profiles(kind, list(_default_audio_profiles(kind)) + list(_coerce_audio_profiles(kind, profiles)))


def _updated_audio_profiles(kind: str, values: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return _profiles_with_defaults(kind, values.get(f"{kind}_profiles"))


def _active_audio_profile(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    if kind == "tts":
        if str(values.get("tts_provider") or "").strip() in {"", "none"} and not str(values.get("tts_model") or "").strip():
            return {}
        profile = {
            "label": values.get("tts_profile_label") or "当前 TTS 配置",
            "enabled": values.get("tts_enabled"),
            "provider": values.get("tts_provider"),
            "model": values.get("tts_model"),
            "voice": values.get("tts_voice"),
            "base_url": values.get("tts_base_url"),
            "audio_format": values.get("tts_format"),
            "sample_rate": values.get("tts_sample_rate"),
            "timeout_seconds": values.get("tts_timeout_seconds"),
        }
        rows = _coerce_audio_profiles("tts", [profile])
        return rows[0] if rows else {}
    if kind == "asr":
        profile = {
            "label": values.get("asr_profile_label") or "当前 ASR 配置",
            "enabled": values.get("asr_enabled"),
            "mode": values.get("asr_mode"),
            "provider": values.get("asr_provider"),
            "model": values.get("asr_model"),
            "base_url": values.get("asr_base_url"),
            "language": values.get("asr_language"),
            "timeout_seconds": values.get("asr_timeout_seconds"),
        }
        rows = _coerce_audio_profiles("asr", [profile])
        return rows[0] if rows else {}
    return {}


def _merge_audio_profiles(kind: str, candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_signatures: set[tuple[str, ...]] = set()
    for item in _coerce_audio_profiles(kind, candidates):
        item_id = str(item.get("id") or "").strip()
        signature = _audio_profile_signature(kind, item)
        if item_id in seen_ids or signature in seen_signatures:
            continue
        seen_ids.add(item_id)
        seen_signatures.add(signature)
        merged.append(item)
        if len(merged) >= PROFILE_LIMIT:
            break
    return tuple(merged)


def _coerce_audio_profiles(kind: str, value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        if kind == "tts":
            for key in ("label", "provider", "model", "voice", "base_url", "audio_format"):
                text = str(raw.get(key) or "").strip()
                if text:
                    item[key] = text
            item["enabled"] = _coerce_bool(raw.get("enabled"), True)
            if "sample_rate" in raw:
                item["sample_rate"] = max(8000, _coerce_int(raw.get("sample_rate"), 24000))
            if "timeout_seconds" in raw:
                item["timeout_seconds"] = max(1, _coerce_int(raw.get("timeout_seconds"), 15))
            if not any(item.get(key) for key in ("provider", "model", "voice", "base_url")):
                continue
        elif kind == "asr":
            for key in ("label", "provider", "model", "base_url", "language"):
                text = str(raw.get(key) or "").strip()
                if text:
                    item[key] = text
            item["enabled"] = _coerce_bool(raw.get("enabled"), True)
            item["mode"] = _choice(raw.get("mode"), "api", ASR_MODES)
            if "timeout_seconds" in raw:
                item["timeout_seconds"] = max(1, _coerce_int(raw.get("timeout_seconds"), 30))
            if not any(item.get(key) for key in ("provider", "model", "base_url")):
                continue
        else:
            continue
        item["builtin"] = _coerce_bool(raw.get("builtin"), False)
        item["id"] = _clean_profile_id(raw.get("id")) or _audio_profile_id(kind, item)
        item.setdefault("label", _audio_profile_label(kind, item))
        rows.append(item)
        if len(rows) >= PROFILE_LIMIT:
            break
    return tuple(rows)


def _audio_profile_signature(kind: str, item: dict[str, Any]) -> tuple[str, ...]:
    if kind == "tts":
        keys = ("provider", "model", "voice", "base_url", "audio_format", "sample_rate")
    else:
        keys = ("mode", "provider", "model", "base_url", "language")
    return tuple(str(item.get(key) or "").strip().lower() for key in keys)


def _audio_profile_id(kind: str, item: dict[str, Any]) -> str:
    raw = "|".join(_audio_profile_signature(kind, item))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    provider = _slug_part(item.get("provider")) or "custom"
    model = _slug_part(item.get("model")) or "model"
    voice = _slug_part(item.get("voice")) if kind == "tts" else _slug_part(item.get("mode"))
    suffix = f"-{voice}" if voice else ""
    return f"{kind}-{provider}-{model}{suffix}-{digest}"


def _audio_profile_label(kind: str, item: dict[str, Any]) -> str:
    if kind == "tts":
        parts = [item.get("provider"), item.get("model"), item.get("voice")]
        return " / ".join(str(part) for part in parts if part) or "TTS 配置"
    parts = [item.get("provider"), item.get("model"), item.get("language")]
    return " / ".join(str(part) for part in parts if part) or "ASR 配置"


def _clean_profile_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in text)
    text = "-".join(part for part in text.split("-") if part)
    return text[:80]


def _slug_part(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch if ch.isalnum() else "-" for ch in text).strip("-")[:24]


def _updated_history(values: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    existing = _coerce_history(values.get("config_history"))
    candidates = [
        _history_item(
            kind="llm",
            label="Aura 对话模型",
            provider=values.get("aura_model_provider"),
            model=values.get("aura_model_model"),
            base_url=values.get("aura_model_base_url"),
            mode=values.get("aura_model_mode"),
            timeout_seconds=values.get("aura_model_timeout_seconds"),
            max_tokens=values.get("aura_model_max_tokens"),
            temperature=values.get("aura_model_temperature"),
            reasoning_effort=values.get("aura_model_reasoning_effort"),
        ),
        _history_item(
            kind="tts",
            label="TTS",
            provider=values.get("tts_provider"),
            model=values.get("tts_model"),
            base_url=values.get("tts_base_url"),
            voice=values.get("tts_voice"),
            audio_format=values.get("tts_format"),
            sample_rate=values.get("tts_sample_rate"),
            timeout_seconds=values.get("tts_timeout_seconds"),
        ),
        _history_item(
            kind="asr",
            label="ASR",
            provider=values.get("asr_provider"),
            model=values.get("asr_model"),
            base_url=values.get("asr_base_url"),
            mode=values.get("asr_mode"),
            language=values.get("asr_language"),
            timeout_seconds=values.get("asr_timeout_seconds"),
        ),
    ]
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for item in [candidate for candidate in candidates if candidate] + list(existing):
        key = (
            str(item.get("kind") or ""),
            str(item.get("provider") or ""),
            str(item.get("model") or ""),
            str(item.get("base_url") or ""),
            str(item.get("mode") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= HISTORY_LIMIT:
            break
    return tuple(merged)


def _updated_model_profiles(values: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    existing = list(_coerce_model_profiles(values.get("model_profiles")))
    active = _model_profile(
        kind="aura",
        label=values.get("profile_label") or "Aura 对话模型",
        provider=values.get("aura_model_provider"),
        model=values.get("aura_model_model"),
        base_url=values.get("aura_model_base_url"),
        api_key_configured=bool(str(values.get("aura_model_api_key") or "").strip()),
    )
    if active:
        signature = _model_profile_signature(active)
        existing = [row for row in existing if _model_profile_signature(row) != signature]
        existing.insert(0, active)
    return tuple(existing[:24])


def _model_profiles_with_active(profiles: Any, config: AuraRuntimeConfig) -> tuple[dict[str, Any], ...]:
    values = asdict(config)
    values["model_profiles"] = profiles
    return _updated_model_profiles(values)


def _coerce_model_profiles(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = _model_profile(
            kind=str(item.get("kind") or "aura"),
            label=item.get("label"),
            provider=item.get("provider"),
            model=item.get("model"),
            base_url=item.get("base_url"),
            api_key_configured=item.get("api_key_configured"),
            last_used_at=item.get("last_used_at"),
        )
        if not row:
            continue
        signature = _model_profile_signature(row)
        if signature in seen:
            continue
        seen.add(signature)
        rows.append(row)
        if len(rows) >= 24:
            break
    return tuple(rows)


def _model_profile(*, kind: str, label: Any, provider: Any, model: Any, base_url: Any,
                   api_key_configured: Any, last_used_at: Any = "") -> dict[str, Any]:
    kind_text = str(kind or "aura").strip()
    if kind_text not in {"aura", "hermes"}:
        kind_text = "aura"
    provider_text = str(provider or "").strip()
    model_text = str(model or "").strip()
    if not provider_text and not model_text:
        return {}
    return {
        "kind": kind_text,
        "label": str(label or f"{provider_text or '自定义'} / {model_text or '未命名'}").strip()[:120],
        "provider": provider_text,
        "model": model_text,
        "base_url": str(base_url or "").strip(),
        "api_key_configured": bool(api_key_configured),
        "last_used_at": str(last_used_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    }


def _model_profile_signature(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("kind") or ""),
        str(item.get("provider") or ""),
        str(item.get("model") or ""),
        str(item.get("base_url") or ""),
    )


def _history_item(kind: str, label: str, **raw: Any) -> dict[str, Any]:
    item = {"kind": kind, "label": label}
    for key, value in raw.items():
        text = "" if value is None else str(value).strip()
        if text:
            item[key] = text
    if not item.get("provider") and not item.get("model"):
        return {}
    if kind == "llm" and item.get("mode") != "aura_model":
        return {}
    if kind == "tts" and item.get("provider") in {"", "none"}:
        return {}
    return item


def _coerce_history(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    rows: list[dict[str, Any]] = []
    allowed_keys = {
        "kind",
        "label",
        "provider",
        "model",
        "base_url",
        "mode",
        "voice",
        "audio_format",
        "sample_rate",
        "language",
        "timeout_seconds",
        "max_tokens",
        "temperature",
        "reasoning_effort",
    }
    for item in value:
        if not isinstance(item, dict):
            continue
        clean = {key: str(item.get(key) or "").strip() for key in allowed_keys if str(item.get(key) or "").strip()}
        if clean.get("kind") in {"llm", "tts", "asr"} and (clean.get("provider") or clean.get("model")):
            rows.append(clean)
        if len(rows) >= HISTORY_LIMIT:
            break
    return tuple(rows)


def _env_bool(name: str, default: bool) -> bool:
    return _coerce_bool(os.environ.get(name, ""), default)


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    return _choice(os.environ.get(name, ""), default, allowed)


def _env_int(name: str, default: int) -> int:
    return _coerce_int(os.environ.get(name, ""), default)


def _env_json_object(name: str) -> dict[str, Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return _coerce_provider_options(value)


def _provider_option_is_secret(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized in PROVIDER_OPTION_SECRET_KEYS:
        return True
    return any(part in normalized for part in ("token", "secret", "password", "credential", "private_key"))


def _coerce_provider_options(
    value: Any,
    *,
    existing: Any = None,
    preserve_existing_secrets: bool = False,
) -> dict[str, Any]:
    """Keep provider options small and preserve masked secrets on an admin save."""

    if not isinstance(value, dict):
        return {}
    old = existing if isinstance(existing, dict) else {}
    clean: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:32]:
        key = str(raw_key or "").strip().lower().replace("-", "_")
        if not key or len(key) > 64 or not all(ch.isalnum() or ch == "_" for ch in key):
            continue
        secret = _provider_option_is_secret(key)
        if raw_value is None:
            # An explicit null clears a previously stored secret.
            continue
        if isinstance(raw_value, (dict, list, tuple, set)):
            continue
        text = str(raw_value).strip()
        if not text:
            if secret and preserve_existing_secrets and old.get(key):
                clean[key] = old[key]
            continue
        if secret and preserve_existing_secrets and text == CONFIGURED_VALUE_MARKER and old.get(key):
            clean[key] = old[key]
            continue
        clean[key] = text[:512]
    return clean


def _public_provider_options(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    public: dict[str, Any] = {}
    for key, raw_value in source.items():
        if _provider_option_is_secret(key):
            public[str(key)] = CONFIGURED_VALUE_MARKER
        else:
            public[str(key)] = raw_value
    return public


def _provider_option_secret_status(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {
        str(key): bool(str(raw_value or "").strip())
        for key, raw_value in source.items()
        if _provider_option_is_secret(key)
    }


def _choice(value: Any, default: str, allowed: set[str]) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _temperature_text(value: Any, default: Any) -> str:
    try:
        number = float(str(value if value is not None else default).strip())
    except (TypeError, ValueError):
        try:
            number = float(str(default or "0.4").strip())
        except (TypeError, ValueError):
            number = 0.4
    number = max(0.0, min(2.0, number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _ratio_text(value: Any, default: Any) -> str:
    try:
        number = float(str(value if value is not None else default).strip())
    except (TypeError, ValueError):
        try:
            number = float(str(default or "0.45").strip())
        except (TypeError, ValueError):
            number = 0.45
    number = max(0.0, min(1.0, number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


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
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default
