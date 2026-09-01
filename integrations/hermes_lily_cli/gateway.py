from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import ctypes.util
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Lock, Thread
from typing import Any, AsyncIterator, Iterator
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed, InvalidStatus
except ImportError as exc:  # pragma: no cover - exercised in minimal images
    raise SystemExit("The gateway requires the 'websockets' package. Install requirements.txt.") from exc

from integrations.hermes_lily_cli.bridge import HermesLilyConfig, scrub_text
from integrations.hermes_lily_cli.dialogue_quota import DEFAULT_LIMIT, DEFAULT_WINDOW_SECONDS, DialogueQuota
from integrations.hermes_lily_cli.local_audio import match_local_audio
from integrations.hermes_lily_cli.quota import query_quota
from integrations.hermes_lily_cli.runtime_config import read_hermes_provider_config, read_hermes_provider_secret
from integrations.hermes_lily_cli.voice_providers import (
    VoiceProviderError,
    synthesize_aliyun_nls,
    synthesize_baidu,
    synthesize_minimax,
    synthesize_tencent,
    synthesize_volcengine,
    transcribe_aliyun_nls,
    transcribe_baidu,
    transcribe_qwen3_asr,
    transcribe_tencent,
    transcribe_volcengine,
)

try:
    from integrations.aura_persona_gateway.config import load_persona_config
    from integrations.aura_persona_gateway.grounded_intent import classify_grounded_current_intent
    from integrations.aura_persona_gateway.llm import open_pooled_http_request, warm_pooled_http_url
    from integrations.aura_persona_gateway.query_context import resolve_query_context
    from integrations.aura_persona_gateway.response_contract import normalize_spoken_reply
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig, cached_weather_snapshot, load_aura_runtime_config
    from integrations.aura_persona_gateway.desserts import apply_dessert_purchase
    from integrations.aura_persona_gateway.state_rules import apply_time_recovery, compute_affinity_level
    from integrations.aura_persona_gateway.store import LilyPersonaStore
    from integrations.aura_persona_gateway.weather import refresh_cached_weather_if_needed, refresh_user_weather_if_needed
    from integrations.aura_persona_gateway.world import build_world_snapshot
except ImportError:  # pragma: no cover - tiny builds can run the HTTP bridge only
    AuraRuntimeConfig = None
    apply_dessert_purchase = None
    apply_time_recovery = None
    classify_grounded_current_intent = None
    cached_weather_snapshot = None
    compute_affinity_level = None
    LilyPersonaStore = None
    load_aura_runtime_config = None
    load_persona_config = None
    normalize_spoken_reply = None
    open_pooled_http_request = None
    resolve_query_context = None
    refresh_cached_weather_if_needed = None
    refresh_user_weather_if_needed = None
    warm_pooled_http_url = None
    build_world_snapshot = None


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, "") or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = str(os.environ.get(name, "") or "").strip().lower()
    return value if value in choices else default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765/turn"
DEFAULT_PLACEHOLDER_GOAL = "Aura Lily device voice turn reached the local gateway, but ASR did not produce text."
MAX_AUDIO_BYTES = 4 * 1024 * 1024
DEVICE_SAMPLE_RATE = 16_000
DEVICE_CHANNELS = 1
DEVICE_SAMPLE_WIDTH = 2
QUOTA_CACHE_TTL_SECONDS = _env_float("AURA_QUOTA_CACHE_TTL_SECONDS", 1800.0, minimum=60.0, maximum=3600.0)
_QUOTA_CACHE_LOCK = Lock()
_MODEL_QUOTA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_HERMES_QUOTA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
ASR_HTTP_KEEPALIVE_ENABLED = _env_bool("AURA_ASR_HTTP_KEEPALIVE_ENABLED", True)
ASR_HTTP_WARM_ENABLED = _env_bool("AURA_ASR_HTTP_WARM_ENABLED", True)
# 误触护栏：解码音频短于该阈值且 ASR 无文本时，跳过“没听清楚”的 TTS 兜底回复（0 关闭）。
ASR_TAP_GUARD_MS = _env_int("AURA_ASR_TAP_GUARD_MS", 500, minimum=0)
TTS_BINARY_MAGIC = b"ATTS"
TTS_BINARY_HEADER_SIZE = 16
TTS_BINARY_FLAG_FINAL = 0x01
TTS_AUDIO_CHUNK_STALL_MS = 300
TTS_CHUNK_BYTES = _env_int("AURA_TTS_WS_CHUNK_BYTES", 2048, minimum=512)
TTS_AUDIO_SEND_PACING_ENABLED = _env_bool("AURA_TTS_AUDIO_SEND_PACING_ENABLED", False)
TTS_AUDIO_SEND_PACING_RATE = _env_float("AURA_TTS_AUDIO_SEND_PACING_RATE", 1.35, minimum=1.0, maximum=4.0)
TTS_AUDIO_SEND_PACING_PREFILL_MS = _env_int("AURA_TTS_AUDIO_SEND_PACING_PREFILL_MS", 240, minimum=0)
TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS = _env_int("AURA_TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS", 60, minimum=0)
TTS_AUDIO_SEND_DIRECT_PACKETS = _env_int("AURA_TTS_AUDIO_SEND_DIRECT_PACKETS", 5, minimum=0)
TTS_TEXT_CHUNK_CHARS = _env_int("AURA_TTS_TEXT_CHUNK_CHARS", 18, minimum=8)
TTS_FIRST_CHUNK_CHARS = _env_int("AURA_TTS_FIRST_CHUNK_CHARS", 8, minimum=4)
TTS_FIRST_CHUNK_MIN_CHARS = _env_int("AURA_TTS_FIRST_CHUNK_MIN_CHARS", 5, minimum=2)
TTS_PREFETCH_CONCURRENCY = _env_int("AURA_TTS_PREFETCH_CONCURRENCY", 2, minimum=1)
TTS_PREFACE_ENABLED = _env_bool("AURA_TTS_PREFACE_ENABLED", False)
TTS_PREFACE_DELAY_MS = _env_int("AURA_TTS_PREFACE_DELAY_MS", 700, minimum=0)
TTS_PREFACE_MAX_WAIT_MS = _env_int("AURA_TTS_PREFACE_MAX_WAIT_MS", 200, minimum=0)
TTS_PREFACE_MIN_DEVICE_BYTES = _env_int("AURA_TTS_PREFACE_MIN_DEVICE_BYTES", 8192, minimum=0)
TTS_PREFACE_TEXT = os.environ.get("AURA_TTS_PREFACE_TEXT", "嗯。").strip() or "嗯。"
STEPFUN_WS_TTS_ENABLED = _env_bool("AURA_TTS_STEPFUN_WS_ENABLED", True)
STEPFUN_WS_TTS_FALLBACK_HTTP = _env_bool("AURA_TTS_STEPFUN_WS_FALLBACK_HTTP", True)
STEPFUN_WS_TTS_WARM_ENABLED = _env_bool("AURA_TTS_STEPFUN_WS_WARM_ENABLED", True)
STEPFUN_WS_TTS_FLUSH_AFTER_DELTA = _env_bool("AURA_TTS_STEPFUN_WS_FLUSH_AFTER_DELTA", True)
STEPFUN_WS_TTS_FLUSH_EACH_DELTA = _env_bool("AURA_TTS_STEPFUN_WS_FLUSH_EACH_DELTA", False)
STEPFUN_WS_TTS_MODE = os.environ.get("AURA_TTS_STEPFUN_WS_MODE", "default").strip() or "default"
STEPFUN_WS_TTS_TEXT_NORMALIZATION = os.environ.get("AURA_TTS_STEPFUN_WS_TEXT_NORMALIZATION", "standard").strip() or "standard"
STEPFUN_WS_TTS_INSTRUCTION = os.environ.get("AURA_TTS_INSTRUCTION", "").strip()[:200]
STEPFUN_WS_TTS_SPEED_RATIO = _env_float("AURA_TTS_SPEED_RATIO", 1.0, minimum=0.5, maximum=2.0)
STEPFUN_WS_TTS_VOLUME_RATIO = _env_float("AURA_TTS_VOLUME_RATIO", 1.0, minimum=0.1, maximum=2.0)
STEPFUN_WS_TTS_SAMPLE_RATE = _env_int("AURA_TTS_STEPFUN_WS_SAMPLE_RATE", DEVICE_SAMPLE_RATE, minimum=8000)
STEPFUN_WS_TTS_PROXY = os.environ.get("AURA_TTS_STEPFUN_WS_PROXY", "").strip()
STEPFUN_WS_TTS_OPEN_TIMEOUT_SECONDS = _env_float("AURA_TTS_STEPFUN_WS_OPEN_TIMEOUT_SECONDS", 2.0, minimum=0.2, maximum=10.0)
STEPFUN_WS_TTS_MAX_SESSIONS = _env_int("AURA_TTS_STEPFUN_WS_MAX_SESSIONS", 2, minimum=1)
STEPFUN_WS_TTS_ACQUIRE_TIMEOUT_SECONDS = _env_float(
    "AURA_TTS_STEPFUN_WS_ACQUIRE_TIMEOUT_SECONDS",
    0.8,
    minimum=0.0,
    maximum=10.0,
)
STEPFUN_WS_TTS_WARM_ACQUIRE_TIMEOUT_SECONDS = _env_float(
    "AURA_TTS_STEPFUN_WS_WARM_ACQUIRE_TIMEOUT_SECONDS",
    0.05,
    minimum=0.0,
    maximum=2.0,
)
STEPFUN_WS_TTS_429_COOLDOWN_SECONDS = _env_float(
    "AURA_TTS_STEPFUN_WS_429_COOLDOWN_SECONDS",
    20.0,
    minimum=0.0,
    maximum=300.0,
)
STEPFUN_WS_TTS_WAIT_FIRST_AUDIO = _env_bool("AURA_TTS_STEPFUN_WS_WAIT_FIRST_AUDIO", False)
STEPFUN_WS_TTS_FIRST_AUDIO_WAIT_MS = _env_int("AURA_TTS_STEPFUN_WS_FIRST_AUDIO_WAIT_MS", 1200, minimum=0)
STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS = _env_int("AURA_TTS_STEPFUN_WS_FIRST_SEGMENT_READY_WAIT_MS", 300, minimum=0)
STEPFUN_WS_TTS_FIRST_SEGMENT_HTTP_POLICY = _env_choice(
    "AURA_TTS_STEPFUN_WS_FIRST_SEGMENT_HTTP_POLICY",
    "auto",
    {"auto", "always", "off"},
)
STEPFUN_WS_ASR_ENABLED = _env_bool("AURA_ASR_STEPFUN_WS_ENABLED", True)
STEPFUN_WS_ASR_SERVER_VAD = _env_bool("AURA_ASR_STEPFUN_WS_SERVER_VAD", True)
STEPFUN_WS_ASR_SILENCE_MS = _env_int("AURA_ASR_STEPFUN_WS_SILENCE_MS", 360, minimum=100)
STEPFUN_WS_ASR_THRESHOLD = _env_float("AURA_ASR_STEPFUN_WS_THRESHOLD", 0.5, minimum=0.0, maximum=1.0)
STEPFUN_WS_ASR_FINAL_WAIT_MS = _env_int("AURA_ASR_STEPFUN_WS_FINAL_WAIT_MS", 1200, minimum=100)
STEPFUN_WS_ASR_COMMIT_WAIT_MS = _env_int("AURA_ASR_STEPFUN_WS_COMMIT_WAIT_MS", 1200, minimum=100)
STEPFUN_WS_ASR_FINISH_QUEUE_TIMEOUT_MS = _env_int("AURA_ASR_STEPFUN_WS_FINISH_QUEUE_TIMEOUT_MS", 1000, minimum=100)
STEPFUN_WS_ASR_QUEUE_FRAMES = _env_int("AURA_ASR_STEPFUN_WS_QUEUE_FRAMES", 96, minimum=8)
STEPFUN_WS_ASR_PROMPT = os.environ.get("AURA_ASR_STEPFUN_WS_PROMPT", "请记录下你所听到的语音内容。").strip()
STEPFUN_WS_ASR_EARLY_TURN_ENABLED = _env_bool("AURA_ASR_STEPFUN_WS_EARLY_TURN_ENABLED", False)
STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL = _env_bool("AURA_ASR_STEPFUN_WS_EARLY_TURN_ALLOW_PARTIAL", False)
STEPFUN_WS_ASR_USE_PARTIAL_AS_FINAL = _env_bool("AURA_ASR_STEPFUN_WS_USE_PARTIAL_AS_FINAL", False)
STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS = _env_int("AURA_ASR_STEPFUN_WS_EARLY_TURN_MIN_CHARS", 4, minimum=1)
STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS = _env_int("AURA_ASR_STEPFUN_WS_EARLY_TURN_MIN_AUDIO_MS", 700, minimum=0)
STREAMING_ASR_PREFETCH_ENABLED = _env_bool("AURA_STREAMING_ASR_PREFETCH_ENABLED", True)
STREAMING_ASR_PREFETCH_MIN_CHARS = _env_int("AURA_STREAMING_ASR_PREFETCH_MIN_CHARS", 5, minimum=2)
STREAMING_ASR_PREFETCH_WAIT_MS = _env_int("AURA_STREAMING_ASR_PREFETCH_WAIT_MS", 150, minimum=0)
STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED = _env_bool("AURA_STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", False)
STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_CHARS = _env_int("AURA_STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_CHARS", 7, minimum=4)
STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS = _env_int("AURA_STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS", 900, minimum=0)
STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS = tuple(
    item.strip()
    for item in os.environ.get(
        "AURA_STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS",
        "weather,time,activity_or_location,local_quality",
    ).split(",")
    if item.strip()
)
STREAMING_ASR_GROUNDED_CURRENT_MIN_CHARS = _env_int("AURA_STREAMING_ASR_GROUNDED_CURRENT_MIN_CHARS", 3, minimum=3)
STREAMING_ASR_GROUNDED_CURRENT_MIN_AUDIO_MS = _env_int("AURA_STREAMING_ASR_GROUNDED_CURRENT_MIN_AUDIO_MS", 900, minimum=0)
STREAMING_ASR_LOCAL_QUALITY_MIN_AUDIO_MS = _env_int("AURA_STREAMING_ASR_LOCAL_QUALITY_MIN_AUDIO_MS", 900, minimum=0)
STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED = _env_bool("AURA_STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED = _env_bool("AURA_STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED", False)
STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS = _env_int("AURA_STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS", 6, minimum=4)
STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS = _env_int("AURA_STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 700, minimum=0)
STREAMING_ASR_LOCAL_VAD_SILENCE_MS = _env_int("AURA_STREAMING_ASR_LOCAL_VAD_SILENCE_MS", 360, minimum=100)
STEPFUN_WS_ASR_STOP_SILENCE_MS = _env_int("AURA_ASR_STEPFUN_WS_STOP_SILENCE_MS", 260, minimum=0)
STEPFUN_REALTIME_DIRECT_REPLY_ENABLED = _env_bool("AURA_STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", False)
STEPFUN_REALTIME_STOP_SILENCE_MS = _env_int("AURA_ASR_STEPFUN_REALTIME_STOP_SILENCE_MS", 900, minimum=0)
RECORDING_NO_AUDIO_TIMEOUT_SECONDS = _env_float(
    "AURA_RECORDING_NO_AUDIO_TIMEOUT_SECONDS",
    2.5,
    minimum=0.5,
    maximum=30.0,
)
RECORDING_MAX_SECONDS = _env_float(
    "AURA_RECORDING_MAX_SECONDS",
    8.0,
    minimum=1.0,
    maximum=60.0,
)
# 音频断流护栏：已收到音频、但超过该秒数没有新包（设备上行掉线且没发 stop）时提前收尾，
# 避免傻等到 RECORDING_MAX_SECONDS。0 表示关闭。
RECORDING_STALL_TIMEOUT_SECONDS = _env_float(
    "AURA_RECORDING_STALL_TIMEOUT_SECONDS",
    1.5,
    minimum=0.0,
    maximum=30.0,
)
ASR_LOW_CONFIDENCE_MAX_CHARS = _env_int("AURA_ASR_LOW_CONFIDENCE_MAX_CHARS", 2, minimum=1)
BRIDGE_STREAM_ENABLED = _env_bool("AURA_BRIDGE_STREAM_ENABLED", True)
BRIDGE_STREAM_MIN_CHARS = _env_int("AURA_BRIDGE_STREAM_MIN_CHARS", 4, minimum=2)
BRIDGE_STREAM_FIRST_SEGMENT_CHARS = _env_int("AURA_BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6, minimum=4)
BRIDGE_STREAM_WS_TTS_CHUNK_CHARS = _env_int("AURA_BRIDGE_STREAM_WS_TTS_CHUNK_CHARS", 8, minimum=4)
BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS = _env_int("AURA_BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS", 4, minimum=0)
BRIDGE_SPECULATIVE_ENABLED = _env_bool("AURA_BRIDGE_SPECULATIVE_ENABLED", True)
BRIDGE_SPECULATIVE_REUSE_ENABLED = _env_bool("AURA_BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
BRIDGE_SPECULATIVE_MIN_CHARS = _env_int("AURA_BRIDGE_SPECULATIVE_MIN_CHARS", 10, minimum=4)
BRIDGE_SPECULATIVE_MIN_AUDIO_MS = _env_int("AURA_BRIDGE_SPECULATIVE_MIN_AUDIO_MS", 700, minimum=0)
BRIDGE_SPECULATIVE_MAX_EVENTS = _env_int("AURA_BRIDGE_SPECULATIVE_MAX_EVENTS", 12, minimum=1)
BRIDGE_SPECULATIVE_MAX_CHARS = _env_int("AURA_BRIDGE_SPECULATIVE_MAX_CHARS", 160, minimum=32)
BRIDGE_SPECULATIVE_MIN_PREFIX_RATIO = _env_float("AURA_BRIDGE_SPECULATIVE_MIN_PREFIX_RATIO", 0.9, minimum=0.0, maximum=1.0)
SERVER_VAD_ENABLED = _env_bool("AURA_SERVER_VAD_ENABLED", True)
SERVER_VAD_SPEECH_RMS = _env_int("AURA_SERVER_VAD_SPEECH_RMS", 280, minimum=1)
SERVER_VAD_SILENCE_RMS = _env_int("AURA_SERVER_VAD_SILENCE_RMS", 180, minimum=1)
SERVER_VAD_MIN_SPEECH_MS = _env_int("AURA_SERVER_VAD_MIN_SPEECH_MS", 240, minimum=1)
SERVER_VAD_SILENCE_MS = _env_int("AURA_SERVER_VAD_SILENCE_MS", 900, minimum=1)
REALTIME_LOCAL_VAD_SILENCE_MS = _env_int("AURA_REALTIME_LOCAL_VAD_SILENCE_MS", 600, minimum=100)
SERVER_VAD_MIN_AUDIO_MS = _env_int("AURA_SERVER_VAD_MIN_AUDIO_MS", 700, minimum=1)
GATEWAY_STATUS_PATH = os.environ.get("AURA_LILY_GATEWAY_STATUS_PATH", ".aura/persona/config/gateway_status.json")
GATEWAY_LOG_TRANSCRIPT_PREVIEW = _env_bool("AURA_GATEWAY_LOG_TRANSCRIPT_PREVIEW", True)
BACKGROUND_POLL_INTERVAL_SECONDS = 1.0
BACKGROUND_POLL_TIMEOUT_SECONDS = 90.0
BACKGROUND_PROGRESS_INTERVAL_SECONDS = 3.0
WORLD_SYNC_POLL_INTERVAL_SECONDS = _env_float(
    "AURA_WORLD_SYNC_POLL_INTERVAL_SECONDS",
    60.0,
    minimum=10.0,
    maximum=600.0,
)
STAGE_DIRECTION_HINTS = (
    "看到",
    "看见",
    "消息",
    "手机",
    "语音",
    "回复",
    "回了",
    "笑",
    "眨",
    "叹",
    "点头",
    "摇头",
    "抬",
    "低",
    "靠",
    "凑",
    "抱",
    "摸",
    "挥",
    "停",
    "沉默",
    "语气",
    "表情",
    "神情",
    "动作",
    "旁白",
    "心理",
    "心里",
    "开心",
    "高兴",
    "难过",
    "不高兴",
    "生气",
    "委屈",
    "害羞",
    "撒娇",
    "惊讶",
    "犹豫",
    "哭",
    "哄",
    "轻声",
    "小声",
    "认真",
    "温柔",
    "呼吸",
    "停顿",
    "语速",
    "语调",
)
_TTS_PREFACE_CACHE: dict[tuple[str, str, str, str, int, str], TtsResult] = {}
_TTS_PREFACE_TASKS: dict[tuple[str, str, str, str, int, str], asyncio.Task[TtsResult]] = {}
_STEPFUN_WS_TTS_SEMAPHORES: dict[int, asyncio.Semaphore] = {}
_STEPFUN_WS_TTS_SEMAPHORE_LIMITS: dict[int, int] = {}
_STEPFUN_WS_TTS_COOLDOWN_UNTIL = 0.0
_STEPFUN_WS_TTS_COOLDOWN_DETAIL = ""


def log_gateway(message: str, **fields: Any) -> None:
    rendered = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in fields.items()
    )
    suffix = f" {rendered}" if rendered else ""
    print(f"aura-lily-gateway {message}{suffix}", file=sys.stderr, flush=True)


def _transcript_preview(text: str, *, limit: int = 80) -> str:
    if not GATEWAY_LOG_TRANSCRIPT_PREVIEW:
        return ""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _compact_transcript_for_confidence(text: str) -> str:
    value = str(text or "").strip().lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def transcript_is_low_confidence_fragment(text: str) -> bool:
    compact = _compact_transcript_for_confidence(text)
    if not compact:
        return False
    meaningful_short = {
        "天气",
        "温度",
        "几点",
        "几度",
        "测试",
        "你好",
        "在吗",
        "早安",
        "晚安",
        "停",
        "暂停",
        "继续",
        "大声点",
        "小声点",
    }
    if compact in meaningful_short:
        return False
    filler_fragments = {
        "这",
        "嗯",
        "嗯嗯",
        "啊",
        "呀",
        "呃",
        "额",
        "哦",
        "喂",
        "诶",
        "哎",
        "唉",
        "好",
        "是",
        "对",
        "行",
        "这个",
        "那个",
        "就是",
    }
    return len(compact) <= ASR_LOW_CONFIDENCE_MAX_CHARS and compact in filler_fragments


def pcm16_mono_stats(pcm: bytes) -> dict[str, int]:
    sample_count = len(pcm or b"") // DEVICE_SAMPLE_WIDTH
    if sample_count <= 0:
        return {
            "decoded_pcm_ms": 0,
            "decoded_samples": 0,
            "rms": 0,
            "peak": 0,
            "clipping_samples": 0,
            "clipping_ratio_x10000": 0,
        }

    total_sq = 0
    peak = 0
    clipping = 0
    view = memoryview(pcm)
    for offset in range(0, sample_count * DEVICE_SAMPLE_WIDTH, DEVICE_SAMPLE_WIDTH):
        sample = int.from_bytes(view[offset:offset + DEVICE_SAMPLE_WIDTH], "little", signed=True)
        abs_sample = abs(sample)
        if abs_sample > peak:
            peak = abs_sample
        if abs_sample >= 32760:
            clipping += 1
        total_sq += sample * sample

    rms = int((total_sq / sample_count) ** 0.5)
    return {
        "decoded_pcm_ms": int(sample_count * 1000 / DEVICE_SAMPLE_RATE),
        "decoded_samples": sample_count,
        "rms": rms,
        "peak": peak,
        "clipping_samples": clipping,
        "clipping_ratio_x10000": int(clipping * 10000 / sample_count),
    }


@dataclass
class GatewayConfig:
    host: str
    port: int
    bridge_url: str
    placeholder_goal: str = DEFAULT_PLACEHOLDER_GOAL
    bridge_timeout_seconds: float = 180.0


@dataclass
class TurnState:
    turn_id: int = 0
    device_id: str = ""
    boot_id: str = ""
    fw_version: str = ""
    supports_local_audio: bool = False
    language: str = "zh"
    started_at: float = 0.0
    audio_bytes: int = 0
    audio_packet_count: int = 0
    audio_first_packet_ms: int = 0
    audio_last_packet_ms: int = 0
    audio_stop_received_ms: int = 0
    sample_rate: int = DEVICE_SAMPLE_RATE
    audio_format: str = "opus"
    frame_duration_ms: int = 60
    metadata: dict[str, Any] = field(default_factory=dict)
    audio_chunks: list[bytes] = field(default_factory=list)
    client_ip: str = ""
    device_public_ip: str = ""
    processing_started_at: float = 0.0
    turn_trigger_reason: str = ""
    turn_trigger_detail: str = ""
    turn_triggered_at: float = 0.0
    turn_trigger_ms: int = 0
    turn_trigger_audio_ms: int = 0
    turn_trigger_silence_ms: int = 0
    asr_latency_ms: int = 0
    asr_decode_ms: int = 0
    asr_backend_ms: int = 0
    asr_wav_bytes: int = 0
    asr_pcm_ms: int = 0
    asr_pcm_rms: int = 0
    asr_pcm_peak: int = 0
    asr_pcm_clipping_ratio_x10000: int = 0
    bridge_latency_ms: int = 0
    server_vad_enabled: bool = False
    server_vad_triggered: bool = False
    processing_task: asyncio.Task[None] | None = None
    recording_watchdog_task: asyncio.Task[None] | None = None
    vad_seen_speech: bool = False
    vad_speech_ms: int = 0
    vad_silence_ms: int = 0
    vad_audio_ms: int = 0
    vad_opus_decoder: Any = None
    streaming_asr_session: Any = None
    stepfun_realtime_session: Any = None
    stepfun_realtime_enabled: bool = False
    stepfun_realtime_triggered: bool = False
    stepfun_realtime_started_at: float = 0.0
    stepfun_realtime_first_audio_ms: int = 0
    stepfun_realtime_first_audio_after_response_ms: int = 0
    stepfun_realtime_total_ms: int = 0
    stepfun_realtime_audio_bytes: int = 0
    stepfun_realtime_forwarded_frames: int = 0
    stepfun_realtime_text: str = ""
    stepfun_realtime_monitor_task: asyncio.Task[None] | None = None
    streaming_asr_opus_decoder: Any = None
    streaming_asr_started_at: float = 0.0
    streaming_asr_first_delta_ms: int = 0
    streaming_asr_final_ms: int = 0
    streaming_asr_audio_bytes: int = 0
    streaming_asr_forwarded_frames: int = 0
    streaming_asr_queue_drops: int = 0
    streaming_asr_finish_qsize_at_stop: int = 0
    streaming_asr_finish_queue_ms: int = 0
    streaming_asr_finish_queue_timeout: bool = False
    streaming_asr_sender_drain_ms: int = 0
    streaming_asr_receiver_wait_ms: int = 0
    streaming_asr_commit_sent: bool = False
    streaming_asr_commit_to_final_ms: int = 0
    streaming_asr_final_ready: bool = False
    streaming_asr_final_text: str = ""
    streaming_asr_final_reason: str = ""
    streaming_asr_early_turn_triggered: bool = False
    streaming_asr_early_turn_blocked: bool = False
    streaming_asr_monitor_task: asyncio.Task[None] | None = None
    streaming_asr_prefetch_task: asyncio.Task[None] | None = None
    streaming_asr_prefetch_text: str = ""
    streaming_asr_prefetch_intent: str = ""
    streaming_asr_prefetch_subject: str = ""
    streaming_asr_prefetch_location: str = ""
    streaming_asr_prefetch_status: str = ""
    streaming_asr_prefetch_started_ms: int = 0
    streaming_asr_prefetch_done_ms: int = 0
    streaming_asr_prefetch_wait_ms: int = 0
    streaming_asr_prefetch_error: str = ""
    bridge_speculative_task: asyncio.Task[Any] | None = None
    bridge_speculative_text: str = ""
    bridge_speculative_started_ms: int = 0
    bridge_speculative_status: str = ""
    bridge_speculative_decision: str = ""
    bridge_speculative_reason: str = ""
    bridge_speculative_event_count: int = 0
    bridge_speculative_delta_chars: int = 0
    bridge_speculative_queue: asyncio.Queue[dict[str, Any] | None] | None = None
    bridge_speculative_adopted: bool = False
    stepfun_tts_warm_task: asyncio.Task[Any] | None = None
    stepfun_tts_warm_started_at: float = 0.0
    stream_tts_turn_id: int = 0
    stream_tts_sender_task: asyncio.Task[None] | None = None
    stream_tts_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    stream_tts_stepfun_task: asyncio.Task[Any] | None = None
    stream_tts_stepfun_session: Any = None


@dataclass(frozen=True)
class AsrResult:
    ok: bool
    text: str = ""
    status: str = "ok"
    detail: str = ""


@dataclass
class BridgeSpeculativeResult:
    text: str
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"
    error: str = ""
    started_ms: int = 0
    completed_ms: int = 0
    delta_chars: int = 0


@dataclass(frozen=True)
class BridgeSpeculativeReuse:
    events: list[dict[str, Any]] | None
    decision: str
    reason: str
    event_source: AsyncIterator[dict[str, Any]] | None = None


@dataclass(frozen=True)
class TtsResult:
    ok: bool
    audio: bytes = b""
    detail: str = ""
    chunk_count: int = 0
    audio_chunk_count: int = 0
    audio_bytes: int = 0
    latency_ms: int = 0
    first_chunk_ms: int = 0
    first_audio_ms: int = 0
    source_sample_rate: int = DEVICE_SAMPLE_RATE
    device_sample_rate: int = DEVICE_SAMPLE_RATE
    streamed: bool = False
    audio_chunk_gap_count: int = 0
    audio_chunk_gap_p50_ms: int = 0
    audio_chunk_gap_p95_ms: int = 0
    audio_chunk_gap_max_ms: int = 0
    audio_chunk_stall_count: int = 0
    audio_chunk_gaps_ms: tuple[int, ...] = ()
    audio_buffer_leads_ms: tuple[int, ...] = ()
    audio_send_bytes: int = 0
    audio_send_ms: int = 0
    audio_send_realtime_x100: int = 0
    audio_send_pacing_enabled: bool = False
    audio_send_pacing_rate_x100: int = 0
    audio_send_pacing_prefill_ms: int = 0
    audio_send_pacing_sleep_count: int = 0
    audio_send_pacing_sleep_ms: int = 0
    audio_buffer_lead_min_ms: int = 0
    audio_buffer_lead_p50_ms: int = 0
    audio_buffer_lead_final_ms: int = 0


@dataclass
class AudioChunkTiming:
    started_at: float
    stall_threshold_ms: int = TTS_AUDIO_CHUNK_STALL_MS
    chunk_count: int = 0
    first_audio_ms: int = 0
    send_started_at: float = 0.0
    finished_at: float = 0.0
    last_sent_at: float = 0.0
    audio_bytes: int = 0
    gaps_ms: list[int] = field(default_factory=list)
    pacing_enabled: bool = False
    pacing_rate_x100: int = 0
    pacing_prefill_ms: int = 0
    pacing_sleep_count: int = 0
    pacing_sleep_ms: int = 0
    buffer_leads_ms: list[int] = field(default_factory=list)

    def record_chunk(self, *, byte_count: int = 0, sent_at: float | None = None) -> None:
        now = time.monotonic() if sent_at is None else float(sent_at)
        if not self.send_started_at:
            self.send_started_at = now
            self.pacing_enabled = bool(TTS_AUDIO_SEND_PACING_ENABLED)
            self.pacing_rate_x100 = int(float(TTS_AUDIO_SEND_PACING_RATE or 1.0) * 100)
            self.pacing_prefill_ms = max(0, int(TTS_AUDIO_SEND_PACING_PREFILL_MS or 0))
        if self.chunk_count <= 0:
            self.first_audio_ms = max(0, int((now - self.started_at) * 1000))
        elif self.last_sent_at:
            self.gaps_ms.append(max(0, int((now - self.last_sent_at) * 1000)))
        self.audio_bytes += max(0, int(byte_count or 0))
        if self.send_started_at:
            elapsed_send_ms = max(0, int((now - self.send_started_at) * 1000))
            buffered_audio_ms = int(
                self.audio_bytes * 1000 / max(1, DEVICE_SAMPLE_RATE * DEVICE_SAMPLE_WIDTH * DEVICE_CHANNELS)
            )
            self.buffer_leads_ms.append(buffered_audio_ms - elapsed_send_ms)
        self.last_sent_at = now
        self.finished_at = now
        self.chunk_count += 1

    def record_pacing_sleep(self, delay_seconds: float) -> None:
        sleep_ms = max(0, int(float(delay_seconds or 0.0) * 1000))
        if sleep_ms:
            self.pacing_sleep_count += 1
            self.pacing_sleep_ms += sleep_ms
        self.finished_at = time.monotonic()

    def finish(self) -> None:
        if self.chunk_count:
            self.finished_at = time.monotonic()


@dataclass(frozen=True)
class StreamTtsItem:
    index: int
    text: str
    is_final: bool
    queued_ms: int
    task: asyncio.Task[TtsResult] | None = None
    source: str = ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aura-lily-gateway",
        description="ESP32 WebSocket gateway for Aura Lily voice turns.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--bridge-url", default=DEFAULT_BRIDGE_URL)
    parser.add_argument("--placeholder-goal", default=DEFAULT_PLACEHOLDER_GOAL)
    parser.add_argument("--bridge-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


async def run_gateway(config: GatewayConfig) -> None:
    async def handler(websocket: Any) -> None:
        await handle_connection(websocket, config)

    # 可选 mDNS 广播（AURA_MDNS_ADVERTISE_ENABLED=1 时生效），供固件自动发现网关。
    from integrations.hermes_lily_cli.mdns_advertise import maybe_start_mdns_advertise

    mdns_advertiser = maybe_start_mdns_advertise(config.port)
    try:
        # ESP32 在 WiFi/内存紧张时 TCP 会整体停摆十几秒；默认 ping_timeout=20s
        # 会把这种瞬时卡顿直接判死踢下线（设备日志表现为 Connection reset）。
        # 放宽到 60s：卡顿自行恢复就继续用原连接，真断了 60s 内也能清理。
        async with serve(
            handler,
            config.host,
            config.port,
            max_size=MAX_AUDIO_BYTES,
            ping_interval=20,
            ping_timeout=60,
        ):
            print(
                f"aura-lily-gateway listening on ws://{config.host}:{config.port}/ws "
                f"(bridge={config.bridge_url})",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.Future()
    finally:
        if mdns_advertiser is not None:
            mdns_advertiser.close()


async def handle_connection(websocket: Any, config: GatewayConfig) -> None:
    state = TurnState()
    world_sync_task: asyncio.Task[None] | None = None
    state.client_ip = websocket_client_ip(websocket)
    register_active_device_connection(websocket, state)
    try:
        runtime_config = load_runtime_config_for_gateway()
        ensure_tts_preface_task(runtime_config)
        await send_json(websocket, {
            "type": "hello",
            "payload": {"action": "hello", "status": "ready", "gateway": "aura-lily-local"},
        })
        await maybe_start_stepfun_tts_warm_session(websocket, state, runtime_config, reason="connect")
        start_status_update_task(websocket, runtime_config)
        world_sync_task = asyncio.create_task(poll_world_status_updates(websocket, runtime_config))
        async for message in websocket:
            if isinstance(message, bytes):
                if state.processing_task and not state.processing_task.done():
                    log_gateway(
                        "audio_packet_ignored_processing",
                        turn_id=state.turn_id,
                        bytes=len(message),
                    )
                    continue
                state.audio_bytes += len(message)
                if state.audio_bytes > MAX_AUDIO_BYTES:
                    await send_system(websocket, "audio_too_large", status="failed", turn_id=state.turn_id)
                    await websocket.close(code=1009, reason="audio too large")
                    return
                packet = bytes(message)
                state.audio_packet_count += 1
                packet_ms = _elapsed_ms(state.started_at) if state.started_at else 0
                if not state.audio_first_packet_ms:
                    state.audio_first_packet_ms = packet_ms
                    log_gateway(
                        "audio_first_packet",
                        turn_id=state.turn_id,
                        packet=state.audio_packet_count,
                        bytes=len(packet),
                        audio_bytes=state.audio_bytes,
                        since_start_ms=packet_ms,
                    )
                state.audio_last_packet_ms = packet_ms
                if state.audio_packet_count <= 3 or state.audio_packet_count % 50 == 0:
                    log_gateway(
                        "audio_packet_received",
                        turn_id=state.turn_id,
                        packet=state.audio_packet_count,
                        bytes=len(packet),
                        audio_bytes=state.audio_bytes,
                        since_start_ms=packet_ms,
                    )
                state.audio_chunks.append(packet)
                await forward_stepfun_step_plan_realtime_packet(state, packet)
                await maybe_trigger_stepfun_realtime_speech_stopped(websocket, state)
                await forward_streaming_asr_packet(state, packet)
                await maybe_trigger_streaming_asr_final_turn(websocket, config, state)
                await maybe_trigger_server_vad(websocket, config, state, packet)
                continue

            await handle_text_message(websocket, config, state, str(message))
        if state.processing_task and not state.processing_task.done():
            await state.processing_task
    except ConnectionClosed:
        return
    finally:
        if world_sync_task is not None:
            world_sync_task.cancel()
            try:
                await world_sync_task
            except asyncio.CancelledError:
                pass
        unregister_active_device_connection(websocket)
        await cancel_recording_watchdog(state, reason="connection_close")
        await close_stepfun_tts_warm_session(state)
        await close_stepfun_step_plan_realtime_session(state)
        await close_streaming_asr_session(state)


async def handle_text_message(websocket: Any, config: GatewayConfig, state: TurnState, text: str) -> None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        await send_json(websocket, {"type": "status", "text": "收到非 JSON 消息，已忽略。"})
        return

    message_type = str(payload.get("type") or "")
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}

    if message_type == "hello":
        device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
        state.device_id = str(body.get("device_id") or device.get("id") or "")
        state.boot_id = str(body.get("boot_id") or device.get("boot_id") or "")
        state.fw_version = str(device.get("fw_version") or body.get("fw_version") or "").strip()
        state.supports_local_audio = firmware_supports_local_audio(state.fw_version)
        state.language = _payload_language(payload, body, state.language)
        state.device_public_ip = _public_ip_from_payload(payload, body) or state.device_public_ip
        log_gateway(
            "device_hello",
            device_id=state.device_id,
            boot_id=state.boot_id,
            client_ip=state.client_ip,
            device_public_ip_configured=bool(state.device_public_ip),
            language=state.language,
            fw_version=state.fw_version,
            supports_local_audio=state.supports_local_audio,
        )
        write_gateway_status(state, source_event="hello")
        await send_json(websocket, {
            "type": "hello",
            "payload": {"action": "hello", "status": "ready"},
        })
        runtime_config = load_runtime_config_for_gateway()
        await maybe_start_stepfun_tts_warm_session(websocket, state, runtime_config, reason="hello")
        start_status_update_task(websocket, runtime_config)
        return

    if message_type == "heartbeat":
        await send_json(websocket, {
            "type": "system",
            "payload": {"action": "heartbeat", "status": "ok"},
        })
        return

    if message_type == "button_press":
        button = str(body.get("button") or payload.get("button") or "unknown")
        log_gateway(
            "button_press",
            button=button,
            device_id=state.device_id,
            boot_id=state.boot_id,
            client_ip=state.client_ip,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {"action": "button_press", "status": "ok", "button": button},
        })
        return

    if message_type == "gpio_diag":
        log_gateway(
            "gpio_diag",
            pin=body.get("pin", payload.get("pin")),
            old=body.get("old", payload.get("old")),
            new=body.get("new", payload.get("new")),
            device_id=state.device_id,
            boot_id=state.boot_id,
            client_ip=state.client_ip,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {"action": "gpio_diag", "status": "ok"},
        })
        return

    if message_type == "gpio_snapshot":
        log_gateway(
            "gpio_snapshot",
            label=body.get("label", payload.get("label")),
            key=body.get("key", payload.get("key")),
            boot=body.get("boot", payload.get("boot")),
            device_id=state.device_id,
            boot_id=state.boot_id,
            client_ip=state.client_ip,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {"action": "gpio_snapshot", "status": "ok"},
        })
        return

    if message_type == "dessert_purchase":
        result = await asyncio.to_thread(handle_dessert_purchase, body)
        await send_json(websocket, {"type": "companion_action", "payload": result})
        if result.get("ok"):
            runtime_config = load_runtime_config_for_gateway()
            await send_status_update(websocket, runtime_config)
        return

    if message_type == "start":
        start_body = _start_payload(payload, body)
        await cancel_active_turn(state, reason="new_start")
        close_state_vad_decoder(state)
        await close_stepfun_step_plan_realtime_session(state)
        await close_streaming_asr_session(state)
        state.turn_id = _coerce_int(start_body.get("turn_id"), state.turn_id + 1)
        state.device_id = str(start_body.get("device_id") or state.device_id)
        state.boot_id = str(start_body.get("boot_id") or state.boot_id)
        state.language = _payload_language(payload, start_body, state.language)
        state.device_public_ip = _public_ip_from_payload(payload, start_body) or state.device_public_ip
        state.started_at = time.monotonic()
        state.audio_bytes = 0
        state.audio_packet_count = 0
        state.audio_first_packet_ms = 0
        state.audio_last_packet_ms = 0
        state.audio_stop_received_ms = 0
        state.audio_chunks.clear()
        state.sample_rate = _coerce_int(start_body.get("sample_rate"), DEVICE_SAMPLE_RATE)
        state.audio_format = str(start_body.get("format") or "opus").strip().lower()
        state.frame_duration_ms = _coerce_int(start_body.get("frame_duration"), 60)
        state.metadata = dict(start_body)
        state.processing_started_at = 0.0
        state.turn_trigger_reason = ""
        state.turn_trigger_detail = ""
        state.turn_triggered_at = 0.0
        state.turn_trigger_ms = 0
        state.turn_trigger_audio_ms = 0
        state.turn_trigger_silence_ms = 0
        state.asr_latency_ms = 0
        state.asr_decode_ms = 0
        state.asr_backend_ms = 0
        state.asr_wav_bytes = 0
        state.asr_pcm_ms = 0
        state.asr_pcm_rms = 0
        state.asr_pcm_peak = 0
        state.asr_pcm_clipping_ratio_x10000 = 0
        state.bridge_latency_ms = 0
        state.server_vad_enabled = SERVER_VAD_ENABLED and _coerce_bool(start_body.get("server_vad"), False)
        state.server_vad_triggered = False
        state.processing_task = None
        state.recording_watchdog_task = None
        state.vad_seen_speech = False
        state.vad_speech_ms = 0
        state.vad_silence_ms = 0
        state.vad_audio_ms = 0
        state.vad_opus_decoder = None
        state.stepfun_realtime_session = None
        state.stepfun_realtime_enabled = False
        state.stepfun_realtime_triggered = False
        state.stepfun_realtime_started_at = 0.0
        state.stepfun_realtime_first_audio_ms = 0
        state.stepfun_realtime_first_audio_after_response_ms = 0
        state.stepfun_realtime_total_ms = 0
        state.stepfun_realtime_audio_bytes = 0
        state.stepfun_realtime_forwarded_frames = 0
        state.stepfun_realtime_text = ""
        state.stepfun_realtime_monitor_task = None
        state.streaming_asr_started_at = 0.0
        state.streaming_asr_first_delta_ms = 0
        state.streaming_asr_final_ms = 0
        state.streaming_asr_audio_bytes = 0
        state.streaming_asr_forwarded_frames = 0
        state.streaming_asr_queue_drops = 0
        state.streaming_asr_final_ready = False
        state.streaming_asr_final_text = ""
        state.streaming_asr_final_reason = ""
        state.streaming_asr_early_turn_triggered = False
        state.streaming_asr_early_turn_blocked = False
        state.streaming_asr_monitor_task = None
        await cancel_streaming_asr_prefetch(state)
        await cancel_bridge_speculative(state, reason="new_turn")
        runtime_config = runtime_config_for_language(load_runtime_config_for_gateway(), state.language)
        log_gateway(
            "turn_start_received",
            turn_id=state.turn_id,
            device_id=state.device_id,
            client_ip=state.client_ip,
            device_public_ip_configured=bool(state.device_public_ip),
            requested_server_vad=_coerce_bool(start_body.get("server_vad"), False),
            language=state.language,
            **runtime_voice_route_payload(runtime_config),
        )
        await send_json(websocket, {
            "type": "status",
            "text": "开始接收语音",
            "payload": {
                "turn_id": state.turn_id,
                "asr_streaming_available": streaming_asr_available(runtime_config),
                "asr_streaming_reason": streaming_asr_unavailable_reason(runtime_config),
            },
        })
        await maybe_start_stepfun_step_plan_realtime_session(websocket, config, state, runtime_config)
        if state.stepfun_realtime_session is not None:
            await close_stepfun_tts_warm_session(state)
            state.server_vad_enabled = bool(SERVER_VAD_ENABLED)
            state.stepfun_realtime_monitor_task = asyncio.create_task(
                monitor_stepfun_realtime_speech_stopped(websocket, state)
            )
        if state.stepfun_realtime_session is None:
            await maybe_start_stepfun_tts_warm_session(websocket, state, runtime_config, reason="start")
            await maybe_start_streaming_asr_session(state, runtime_config, config=config)
            maybe_warm_asr_http_connection(state, runtime_config)
        if state.streaming_asr_session is not None and STEPFUN_WS_ASR_EARLY_TURN_ENABLED:
            state.streaming_asr_monitor_task = asyncio.create_task(
                monitor_streaming_asr_final(websocket, config, state)
            )
        state.recording_watchdog_task = asyncio.create_task(
            monitor_recording_watchdog(websocket, config, state, owner_turn_id=state.turn_id)
        )
        log_gateway(
            "turn_start",
            turn_id=state.turn_id,
            device_id=state.device_id,
            client_ip=state.client_ip,
            device_public_ip_configured=bool(state.device_public_ip),
            server_vad=state.server_vad_enabled,
        )
        log_gateway(
            "turn_start_runtime",
            turn_id=state.turn_id,
            requested_server_vad=_coerce_bool(start_body.get("server_vad"), False),
            effective_server_vad=state.server_vad_enabled,
            stepfun_realtime_session=state.stepfun_realtime_session is not None,
            streaming_asr_session=state.streaming_asr_session is not None,
            **runtime_voice_route_payload(runtime_config),
        )
        write_gateway_status(state, source_event="start")
        return

    if message_type == "stop":
        if body.get("turn_id"):
            state.turn_id = _coerce_int(body.get("turn_id"), state.turn_id)
        state.audio_stop_received_ms = _elapsed_ms(state.started_at) if state.started_at else 0
        log_gateway(
            "turn_stop",
            turn_id=state.turn_id,
            packets=state.audio_packet_count,
            audio_bytes=state.audio_bytes,
            stop_ms=state.audio_stop_received_ms,
            first_packet_ms=state.audio_first_packet_ms,
            last_packet_ms=state.audio_last_packet_ms,
            gap_since_last_packet_ms=max(0, state.audio_stop_received_ms - state.audio_last_packet_ms)
            if state.audio_last_packet_ms
            else 0,
        )
        await send_system(websocket, "audio_received", turn_id=state.turn_id)
        if state.stepfun_realtime_session is not None:
            if state.processing_task and not state.processing_task.done():
                log_gateway("turn_stop_ignored_realtime_processing", turn_id=state.turn_id)
                return
            await cancel_recording_watchdog(state, reason="client_stop")
            mark_turn_trigger(state, "client_stop")
            state.processing_task = asyncio.create_task(run_stepfun_step_plan_realtime_turn(websocket, state))
            await state.processing_task
            return
        if state.processing_task and not state.processing_task.done():
            log_gateway("turn_stop_ignored_processing", turn_id=state.turn_id)
            await send_system(
                websocket,
                "turn_stop_deferred",
                status="processing",
                turn_id=state.turn_id,
                reason="processing_task_active",
            )
            return
        if state.streaming_asr_early_turn_triggered:
            log_gateway("turn_stop_ignored_streaming_asr_final", turn_id=state.turn_id)
            await send_system(
                websocket,
                "turn_stop_deferred",
                status="processing",
                turn_id=state.turn_id,
                reason="streaming_asr_final_already_triggered",
            )
            return
        if state.server_vad_triggered:
            log_gateway("turn_stop_ignored_server_vad", turn_id=state.turn_id)
            await send_system(
                websocket,
                "turn_stop_deferred",
                status="processing",
                turn_id=state.turn_id,
                reason="server_vad_already_triggered",
            )
            return
        await cancel_recording_watchdog(state, reason="client_stop")
        mark_turn_trigger(state, "client_stop")
        state.processing_task = asyncio.create_task(run_voice_turn(websocket, config, state))
        return

    if message_type == "cancel":
        if body.get("turn_id"):
            state.turn_id = _coerce_int(body.get("turn_id"), state.turn_id)
        reason = str(body.get("reason") or "client_cancel").strip() or "client_cancel"
        log_gateway(
            "turn_cancel",
            turn_id=state.turn_id,
            reason=reason,
            packets=state.audio_packet_count,
            audio_bytes=state.audio_bytes,
            **audio_receive_payload(state),
        )
        await cancel_active_turn(state, reason=f"client_cancel:{reason}")
        await send_system(websocket, "turn_cancelled", turn_id=state.turn_id)
        write_gateway_status(state, source_event="cancel")
        return

    await send_json(websocket, {
        "type": "status",
        "text": f"暂不支持的消息类型：{message_type or 'unknown'}",
        "payload": {"turn_id": state.turn_id},
    })


def handle_dessert_purchase(body: dict[str, Any]) -> dict[str, Any]:
    """Apply a device dessert purchase to the private companion state."""
    if not apply_dessert_purchase or not load_persona_config or not LilyPersonaStore:
        return {"ok": False, "status": "world_unavailable", "error": "world model unavailable"}
    try:
        persona_config = load_persona_config()
        store = LilyPersonaStore(persona_config.companion_db_path)
        state = store.get_or_create_state(persona_config.scope)
        updated, result = apply_dessert_purchase(
            state,
            body.get("item_id"),
            device_beans=body.get("beans"),
        )
        if not result.get("ok") or updated is None:
            return result
        store.save_state(persona_config.scope, updated)
        store.record_life_event(
            persona_config.scope,
            event_type="companion.dessert",
            event_key=f"dessert:{int(time.time() * 1000)}:{result.get('item_id')}",
            title=f"吃了{result.get('name') or '甜品'}",
            description="甜品带来短暂的满足感和心情改善。",
            location="outside.cafe",
            activity="吃甜品",
            visibility="private",
            delta={**dict(result.get("effects") or {}), "beans": -int(result.get("price") or 0)},
            payload={"source": "device_dessert_menu", "item_id": result.get("item_id")},
        )
        return result
    except Exception as exc:  # pragma: no cover - defensive websocket boundary
        log_gateway("dessert_purchase_failed", detail=exc.__class__.__name__)
        return {"ok": False, "status": "failed", "error": "dessert purchase failed"}


def _start_payload(payload: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    merged = {key: value for key, value in payload.items() if key != "payload"}
    merged.update(body)
    return merged


def continue_listening_for_device(state: TurnState) -> bool:
    """Return the session opt-in without changing legacy clients.

    Continuous capture is a device/session capability, not a global gateway
    default. The public firmware sends this flag on each start frame; clients
    that omit it keep the original press-to-talk behavior.
    """
    metadata = state.metadata if isinstance(state.metadata, dict) else {}
    return _coerce_bool(
        metadata.get("continuous_dialogue", metadata.get("hands_free", False)),
        False,
    )


async def cancel_active_turn(state: TurnState, *, reason: str) -> None:
    await cancel_recording_watchdog(state, reason=reason)
    await cancel_stream_tts_resources(state, reason=reason)
    task = state.processing_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        log_gateway("turn_processing_cancelled", turn_id=state.turn_id, reason=reason)
    await cancel_stream_tts_resources(state, reason=reason)
    state.processing_task = None
    state.server_vad_triggered = False
    state.stepfun_realtime_triggered = False
    state.streaming_asr_early_turn_triggered = False
    state.streaming_asr_early_turn_blocked = False
    await cancel_bridge_speculative(state, reason=reason)


async def cancel_recording_watchdog(state: TurnState, *, reason: str) -> None:
    task = state.recording_watchdog_task
    state.recording_watchdog_task = None
    if task is None:
        return
    current = asyncio.current_task()
    if task is current:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    else:
        return
    log_gateway("recording_watchdog_cancelled", turn_id=state.turn_id, reason=reason)


def turn_is_waiting_for_audio_or_stop(state: TurnState, *, owner_turn_id: int) -> bool:
    if state.turn_id != owner_turn_id:
        return False
    if state.processing_task and not state.processing_task.done():
        return False
    if state.server_vad_triggered or state.streaming_asr_early_turn_triggered or state.stepfun_realtime_triggered:
        return False
    return True


async def monitor_recording_watchdog(
    websocket: Any,
    config: GatewayConfig,
    state: TurnState,
    *,
    owner_turn_id: int,
) -> None:
    try:
        if RECORDING_NO_AUDIO_TIMEOUT_SECONDS > 0:
            await asyncio.sleep(RECORDING_NO_AUDIO_TIMEOUT_SECONDS)
            if not turn_is_waiting_for_audio_or_stop(state, owner_turn_id=owner_turn_id):
                return
            if state.audio_bytes <= 0:
                state.server_vad_triggered = True
                mark_turn_trigger(state, "no_audio_timeout")
                log_gateway(
                    "recording_watchdog_no_audio",
                    turn_id=state.turn_id,
                    timeout_ms=int(RECORDING_NO_AUDIO_TIMEOUT_SECONDS * 1000),
                )
                runtime_config = runtime_config_for_language(load_runtime_config_for_gateway(), state.language)
                state.asr_latency_ms = _elapsed_ms(state.started_at) if state.started_at else 0
                state.processing_task = asyncio.create_task(send_low_confidence_or_no_audio_reply(
                    websocket,
                    runtime_config,
                    state,
                    response=localized_gateway_text(
                        state.language,
                        zh="我没收到声音，再按一下试试。",
                        en="I didn't receive any audio. Please press the button and try again.",
                        ja="音声が届きませんでした。ボタンを押して、もう一度試してください。",
                    ),
                    transcript="",
                    reason="no_audio_timeout",
                    ok=False,
                ))
                return

        remaining = max(0.0, RECORDING_MAX_SECONDS - RECORDING_NO_AUDIO_TIMEOUT_SECONDS)
        stall_reason = "recording_timeout"
        if RECORDING_STALL_TIMEOUT_SECONDS > 0:
            # 轮询检测音频断流：设备上行掉线且没发 stop 时，不必等满 RECORDING_MAX_SECONDS。
            poll_seconds = min(0.25, max(0.05, RECORDING_STALL_TIMEOUT_SECONDS / 4))
            deadline = time.monotonic() + remaining
            while time.monotonic() < deadline:
                await asyncio.sleep(poll_seconds)
                if not turn_is_waiting_for_audio_or_stop(state, owner_turn_id=owner_turn_id):
                    return
                if state.audio_bytes > 0 and state.audio_last_packet_ms > 0 and state.started_at:
                    gap_ms = _elapsed_ms(state.started_at) - int(state.audio_last_packet_ms)
                    if gap_ms >= int(RECORDING_STALL_TIMEOUT_SECONDS * 1000):
                        stall_reason = "audio_stall"
                        break
        elif remaining > 0:
            await asyncio.sleep(remaining)
        if not turn_is_waiting_for_audio_or_stop(state, owner_turn_id=owner_turn_id):
            return
        if state.audio_bytes <= 0:
            return
        state.server_vad_triggered = True
        mark_turn_trigger(
            state,
            stall_reason,
            audio_ms=streaming_asr_audio_ms(state) or state.vad_audio_ms,
            silence_ms=state.vad_silence_ms,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "server_vad_stop",
                "status": "ok",
                "turn_id": state.turn_id,
                "reason": stall_reason,
                "audio_bytes": state.audio_bytes,
                "timeout_ms": int(RECORDING_MAX_SECONDS * 1000),
            },
        })
        log_gateway(
            "recording_watchdog_timeout",
            turn_id=state.turn_id,
            reason=stall_reason,
            audio_bytes=state.audio_bytes,
            audio_last_packet_ms=state.audio_last_packet_ms,
            streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
            forwarded_frames=state.streaming_asr_forwarded_frames,
            timeout_ms=int(RECORDING_MAX_SECONDS * 1000),
        )
        if state.stepfun_realtime_session is not None:
            state.stepfun_realtime_triggered = True
            await cancel_recording_watchdog(state, reason="local_server_vad_stop")
            state.processing_task = asyncio.create_task(run_stepfun_step_plan_realtime_turn(
                websocket,
                state,
                reason=stall_reason,
            ))
            return
        state.processing_task = asyncio.create_task(run_voice_turn(websocket, config, state))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive async boundary
        log_gateway("recording_watchdog_failed", turn_id=state.turn_id, detail=exc.__class__.__name__)


async def cancel_stream_tts_resources(state: TurnState, *, reason: str, owner_turn_id: int | None = None) -> None:
    if owner_turn_id is not None and state.stream_tts_turn_id != owner_turn_id:
        return
    tasks: list[asyncio.Task[Any]] = []
    for task in (state.stream_tts_sender_task, state.stream_tts_stepfun_task):
        if task is not None:
            tasks.append(task)
    tasks.extend(state.stream_tts_tasks)
    unique_tasks: list[asyncio.Task[Any]] = []
    seen_task_ids: set[int] = set()
    for task in tasks:
        if id(task) in seen_task_ids:
            continue
        seen_task_ids.add(id(task))
        unique_tasks.append(task)
    for task in unique_tasks:
        if not task.done():
            task.cancel()
    session = state.stream_tts_stepfun_session
    for task in unique_tasks:
        try:
            result = await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        else:
            if task is state.stream_tts_stepfun_task and result is not None and result is not session:
                maybe_close = getattr(result, "close", None)
                if callable(maybe_close):
                    try:
                        await maybe_close()
                    except Exception:
                        pass
    if session is not None:
        try:
            await session.close()
        except Exception:
            pass
    if unique_tasks or session is not None:
        log_gateway(
            "stream_tts_cancelled",
            turn_id=state.turn_id,
            reason=reason,
            task_count=len(unique_tasks),
            stepfun_session=bool(session),
        )
    clear_stream_tts_resource_refs(state, owner_turn_id=owner_turn_id)


def clear_stream_tts_resource_refs(state: TurnState, *, owner_turn_id: int | None = None) -> None:
    if owner_turn_id is not None and state.stream_tts_turn_id != owner_turn_id:
        return
    state.stream_tts_turn_id = 0
    state.stream_tts_sender_task = None
    state.stream_tts_tasks = []
    state.stream_tts_stepfun_task = None
    state.stream_tts_stepfun_session = None


def _public_ip_from_payload(payload: dict[str, Any], body: dict[str, Any]) -> str:
    for source in (body, payload):
        for key in ("device_public_ip", "public_ip", "wan_ip"):
            value = str(source.get(key) or "").strip()
            if value:
                return value.split(",", 1)[0].strip()
    device = body.get("device") if isinstance(body.get("device"), dict) else payload.get("device")
    if isinstance(device, dict):
        value = str(device.get("public_ip") or device.get("device_public_ip") or "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    return ""


def normalize_conversation_language(value: Any) -> str:
    language = str(value or "").strip().lower().replace("_", "-")
    if language == "jp" or language == "ja" or language.startswith("ja-"):
        return "ja"
    if language == "en" or language.startswith("en-"):
        return "en"
    if language in {"zh", "cn"} or language.startswith("zh-"):
        return "zh"
    return "zh"


def firmware_supports_local_audio(value: Any) -> bool:
    """Local prompt audio starts with the 0.16.9 device resource layout."""
    parts = re.findall(r"\d+", str(value or ""))
    if len(parts) < 3:
        return False
    try:
        return tuple(int(part) for part in parts[:3]) >= (0, 16, 9)
    except ValueError:
        return False


def _payload_language(payload: dict[str, Any], body: dict[str, Any], fallback: str = "zh") -> str:
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    raw = body.get("language") or payload.get("language") or session.get("language")
    return normalize_conversation_language(raw if raw is not None else fallback)


def runtime_config_for_language(runtime_config: Any, language: str) -> Any:
    if runtime_config is None:
        return None
    return replace(runtime_config, asr_language=normalize_conversation_language(language))


def localized_gateway_text(language: str, *, zh: str, en: str, ja: str) -> str:
    return {"zh": zh, "en": en, "ja": ja}[normalize_conversation_language(language)]


def write_gateway_status(state: TurnState, *, source_event: str) -> None:
    path = Path(GATEWAY_STATUS_PATH).expanduser()
    payload = {
        "updated_at": time.time(),
        "source_event": source_event,
        "device_id": state.device_id,
        "boot_id": state.boot_id,
        "fw_version": state.fw_version,
        "supports_local_audio": state.supports_local_audio,
        "language": state.language,
        "client_ip": state.client_ip,
        "device_public_ip": state.device_public_ip,
        "device_public_ip_configured": bool(state.device_public_ip),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        log_gateway("gateway_status_write_failed", detail=exc.__class__.__name__)


async def send_low_confidence_or_no_audio_reply(
    websocket: Any,
    runtime_config: Any,
    state: TurnState,
    *,
    response: str,
    transcript: str,
    reason: str,
    ok: bool,
) -> None:
    log_gateway(
        "local_audio_guard_reply",
        turn_id=state.turn_id,
        reason=reason,
        transcript_preview=_transcript_preview(transcript),
        audio_bytes=state.audio_bytes,
        streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
        forwarded_frames=state.streaming_asr_forwarded_frames,
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": reason,
            "status": "failed" if not ok else "ok",
            "turn_id": state.turn_id,
            "audio_bytes": state.audio_bytes,
        },
    })
    await send_dialogue_and_tts(
        websocket,
        runtime_config,
        state,
        response,
        ok=ok,
        transcript=transcript,
        bridge_evidence={"local_audio_guard": True, "reason": reason},
    )


async def run_voice_turn(websocket: Any, config: GatewayConfig, state: TurnState) -> None:
    try:
        await _run_voice_turn(websocket, config, state)
    except ConnectionClosed as exc:
        # 设备端 WS 掉线（例如回复发送途中断开）不应变成未捕获的 task 异常。
        log_gateway(
            "turn_reply_send_failed",
            turn_id=state.turn_id,
            reason="connection_closed",
            detail=exc.__class__.__name__,
        )


async def _run_voice_turn(websocket: Any, config: GatewayConfig, state: TurnState) -> None:
    mark_turn_trigger(state, "client_stop")
    runtime_config = runtime_config_for_language(load_runtime_config_for_gateway(), state.language)
    preface_task = ensure_tts_preface_task(runtime_config) if TTS_PREFACE_ENABLED and state.language == "zh" else None
    state.processing_started_at = time.monotonic()
    preface = None
    await send_json(websocket, {
        "type": "status",
        "text": "正在识别语音",
        "payload": {"turn_id": state.turn_id},
    })
    asr_started = time.monotonic()
    log_gateway(
        "asr_route",
        turn_id=state.turn_id,
        streaming_asr_session=state.streaming_asr_session is not None,
        streaming_asr_reason=streaming_asr_unavailable_reason(runtime_config),
        audio_format=state.audio_format,
        packets=state.audio_packet_count,
        audio_bytes=state.audio_bytes,
        **audio_receive_payload(state),
    )
    streaming_finalize_started = time.monotonic()
    streaming_asr = await finalize_streaming_asr_session(state, runtime_config)
    streaming_finalize_ms = int((time.monotonic() - streaming_finalize_started) * 1000)
    fallback_asr_used = False
    fallback_asr_ms = 0
    if streaming_asr.ok and streaming_asr.text.strip():
        asr = streaming_asr
    else:
        if streaming_asr.status not in {"streaming_asr_unavailable", "streaming_asr_disabled"}:
            log_gateway(
                "streaming_asr_fallback",
                turn_id=state.turn_id,
                status=streaming_asr.status,
                detail=streaming_asr.detail,
            )
        fallback_asr_used = True
        fallback_asr_started = time.monotonic()
        asr_task = asyncio.create_task(asyncio.to_thread(transcribe_turn_audio, runtime_config, state))
        try:
            asr = await asyncio.wait_for(
                asyncio.shield(asr_task),
                timeout=max(0, TTS_PREFACE_DELAY_MS) / 1000,
            )
        except asyncio.TimeoutError:
            if state.language == "zh":
                preface = await maybe_send_tts_preface(websocket, runtime_config, state, preface_task=preface_task)
            asr = await asr_task
        fallback_asr_ms = int((time.monotonic() - fallback_asr_started) * 1000)
    state.asr_latency_ms = _elapsed_ms(asr_started)
    if asr.status == "streaming_asr":
        await await_streaming_asr_prefetch(state)
        if state.streaming_asr_prefetch_status == "done":
            runtime_config = runtime_config_for_language(load_runtime_config_for_gateway(), state.language)
    await send_json(websocket, {
        "type": "asr_result",
        "payload": {
            "turn_id": state.turn_id,
            "text": asr.text,
            "status": asr.status,
            "detail": asr.detail if not asr.ok else "",
            "audio_bytes": state.audio_bytes,
            "latency_ms": state.asr_latency_ms,
            "streaming_asr": asr.status == "streaming_asr",
            "streaming_asr_first_delta_ms": state.streaming_asr_first_delta_ms,
            "streaming_asr_final_ms": state.streaming_asr_final_ms,
            "streaming_asr_final_reason": state.streaming_asr_final_reason,
            "streaming_asr_audio_bytes": state.streaming_asr_audio_bytes,
            "streaming_asr_forwarded_frames": state.streaming_asr_forwarded_frames,
            "streaming_asr_queue_drops": state.streaming_asr_queue_drops,
            "streaming_asr_unavailable_reason": streaming_asr_unavailable_reason(runtime_config),
            "streaming_asr_status": streaming_asr.status,
            "streaming_finalize_ms": streaming_finalize_ms,
            "fallback_asr_used": fallback_asr_used,
            "fallback_asr_ms": fallback_asr_ms,
            **asr_diagnostic_payload(state),
            **audio_receive_payload(state),
            **streaming_asr_quality_gate_payload(state),
            **turn_trigger_payload(state),
            **streaming_asr_prefetch_payload(state),
            **bridge_speculative_payload(state),
        },
    })
    log_gateway(
        "asr_result",
        turn_id=state.turn_id,
        ok=asr.ok,
        status=asr.status,
        latency_ms=state.asr_latency_ms,
        text_chars=len(asr.text or ""),
        transcript_preview=_transcript_preview(asr.text),
        streaming_asr=asr.status == "streaming_asr",
        streaming_asr_first_delta_ms=state.streaming_asr_first_delta_ms,
        streaming_asr_final_ms=state.streaming_asr_final_ms,
        streaming_asr_final_reason=state.streaming_asr_final_reason,
        streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
        streaming_asr_forwarded_frames=state.streaming_asr_forwarded_frames,
        streaming_asr_queue_drops=state.streaming_asr_queue_drops,
        streaming_asr_unavailable_reason=streaming_asr_unavailable_reason(runtime_config),
        streaming_asr_status=streaming_asr.status,
        streaming_finalize_ms=streaming_finalize_ms,
        fallback_asr_used=fallback_asr_used,
        fallback_asr_ms=fallback_asr_ms,
        **asr_diagnostic_payload(state),
        **audio_receive_payload(state),
        **streaming_asr_quality_gate_payload(state),
        **turn_trigger_payload(state),
        **streaming_asr_prefetch_log_fields(state),
        **bridge_speculative_log_fields(state),
    )
    log_gateway(
        "turn_asr_timing",
        turn_id=state.turn_id,
        status=asr.status,
        ok=asr.ok,
        asr_ms=state.asr_latency_ms,
        stop_to_processing_start_ms=max(
            0,
            int((state.processing_started_at - state.started_at) * 1000) - int(state.audio_stop_received_ms)
            if state.started_at and state.processing_started_at and state.audio_stop_received_ms
            else 0,
        ),
        streaming_finalize_ms=streaming_finalize_ms,
        streaming_asr_status=streaming_asr.status,
        streaming_asr_ok=streaming_asr.ok,
        fallback_asr_used=fallback_asr_used,
        fallback_asr_ms=fallback_asr_ms,
        streaming_asr_unavailable_reason=streaming_asr_unavailable_reason(runtime_config),
        **asr_diagnostic_payload(state),
        **audio_receive_payload(state),
        **streaming_asr_quality_gate_payload(state),
        **turn_trigger_payload(state),
    )
    if not asr.ok or not asr.text.strip():
        await send_json(websocket, {
            "type": "system",
            "payload": {"action": "asr_failed", "status": "failed", "turn_id": state.turn_id},
        })
        asr_pcm_ms = int(state.asr_pcm_ms or 0)
        if (
            ASR_TAP_GUARD_MS > 0
            and asr.status in {"empty_transcript", "empty_audio"}
            and asr_pcm_ms < ASR_TAP_GUARD_MS
        ):
            # 误触护栏：0.x 秒的点按录音没有可识别内容，设备端已经播错误音并回 IDLE，
            # 不再花 5 秒合成“没听清楚”的语音回复，把设备尽快还给用户。
            log_gateway(
                "asr_tap_guard_skip_reply",
                turn_id=state.turn_id,
                status=asr.status,
                asr_pcm_ms=asr_pcm_ms,
                threshold_ms=ASR_TAP_GUARD_MS,
            )
            return
        await send_dialogue_and_tts(
            websocket,
            runtime_config,
            state,
            asr_failure_reply(asr, language=state.language),
            ok=False,
            preface=preface,
            transcript=asr.text,
        )
        return
    if transcript_is_low_confidence_fragment(asr.text):
        await send_low_confidence_or_no_audio_reply(
            websocket,
            runtime_config,
            state,
            response=localized_gateway_text(
                state.language,
                zh="我在，刚才只听到一点点。",
                en="I'm here. I only caught a little of that.",
                ja="いるよ。今のは少ししか聞き取れなかった。",
            ),
            transcript=asr.text,
            reason="asr_low_confidence",
            ok=False,
        )
        return

    await send_json(websocket, {
        "type": "status",
        "text": "正在生成回复",
        "payload": {"turn_id": state.turn_id},
    })
    if should_stream_bridge(runtime_config):
        speculative_reuse = await resolve_bridge_speculative_reuse(state, asr.text)
        streamed = await stream_dialogue_and_tts_from_bridge(
            websocket,
            config,
            runtime_config,
            state,
            asr.text,
            preface=preface,
            prefetched_events=speculative_reuse.events,
            prefetched_event_source=speculative_reuse.event_source,
        )
        if streamed:
            return

    bridge_started = time.monotonic()
    bridge_task = asyncio.create_task(asyncio.to_thread(call_bridge, config, state, asr.text))
    try:
        bridge_result = await asyncio.wait_for(
            asyncio.shield(bridge_task),
            timeout=max(0, TTS_PREFACE_DELAY_MS) / 1000,
        )
    except asyncio.TimeoutError:
        if not (preface and preface.ok):
            preface = await maybe_send_tts_preface(websocket, runtime_config, state, preface_task=preface_task)
        bridge_result = await bridge_task
    state.bridge_latency_ms = _elapsed_ms(bridge_started)
    log_gateway(
        "bridge_result",
        turn_id=state.turn_id,
        ok=bool(bridge_result.get("ok")),
        latency_ms=state.bridge_latency_ms,
        response_chars=len(str(bridge_result.get("response") or "")),
    )
    response = str(bridge_result.get("response") or "").strip()
    if not response:
        response = "回复生成失败了，你再试一次。"
    evidence = bridge_result.get("evidence") if isinstance(bridge_result.get("evidence"), dict) else {}
    await send_dialogue_and_tts(
        websocket,
        runtime_config,
        state,
        response,
        ok=bool(bridge_result.get("ok")),
        deferred=bool((bridge_result.get("evidence") or {}).get("deferred")),
        request_id=str(bridge_result.get("request_id") or ""),
        preface=preface,
        transcript=asr.text,
        bridge_evidence=evidence,
        voice_turn=bridge_result.get("voice_turn") if isinstance(bridge_result.get("voice_turn"), dict) else None,
    )
    maybe_schedule_voice_reminder(
        evidence,
        bridge_result.get("voice_turn") if isinstance(bridge_result.get("voice_turn"), dict) else None,
    )
    task_id = str(evidence.get("task_id") or "").strip()
    if bool(evidence.get("deferred")) and task_id:
        await poll_and_send_background_result(websocket, config, runtime_config, state, task_id)


async def maybe_trigger_server_vad(websocket: Any, config: GatewayConfig, state: TurnState, packet: bytes) -> None:
    if not state.server_vad_enabled or state.server_vad_triggered:
        return
    if state.processing_task and not state.processing_task.done():
        return
    pcm = server_vad_pcm_from_packet(state, packet)
    if not pcm:
        return
    duration_ms = max(1, int((len(pcm) // DEVICE_SAMPLE_WIDTH) * 1000 / DEVICE_SAMPLE_RATE))
    state.vad_audio_ms += duration_ms
    rms = pcm16_rms(pcm)
    if rms >= SERVER_VAD_SPEECH_RMS:
        state.vad_speech_ms += duration_ms
        state.vad_silence_ms = 0
        if state.vad_speech_ms >= SERVER_VAD_MIN_SPEECH_MS:
            state.vad_seen_speech = True
        return
    if state.vad_seen_speech and rms <= SERVER_VAD_SILENCE_RMS:
        state.vad_silence_ms += duration_ms
    else:
        state.vad_silence_ms = 0
    silence_target_ms = (
        min(SERVER_VAD_SILENCE_MS, REALTIME_LOCAL_VAD_SILENCE_MS)
        if state.stepfun_realtime_session is not None
        else min(SERVER_VAD_SILENCE_MS, STREAMING_ASR_LOCAL_VAD_SILENCE_MS)
        if state.streaming_asr_session is not None
        else SERVER_VAD_SILENCE_MS
    )
    if (
        state.vad_seen_speech
        and state.vad_audio_ms >= SERVER_VAD_MIN_AUDIO_MS
        and state.vad_silence_ms >= silence_target_ms
    ):
        state.server_vad_triggered = True
        mark_turn_trigger(
            state,
            "local_server_vad_stop",
            audio_ms=state.vad_audio_ms,
            silence_ms=state.vad_silence_ms,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "server_vad_stop",
                "status": "ok",
                "turn_id": state.turn_id,
                "reason": "local_server_vad_stop",
                "rms": rms,
                "speech_ms": state.vad_speech_ms,
                "silence_ms": state.vad_silence_ms,
                "silence_target_ms": silence_target_ms,
                "audio_ms": state.vad_audio_ms,
            },
        })
        log_gateway(
            "server_vad_stop",
            turn_id=state.turn_id,
            rms=rms,
            reason="local_server_vad_stop",
            speech_ms=state.vad_speech_ms,
            silence_ms=state.vad_silence_ms,
            silence_target_ms=silence_target_ms,
            audio_ms=state.vad_audio_ms,
            audio_bytes=state.audio_bytes,
        )
        if state.stepfun_realtime_session is not None:
            state.stepfun_realtime_triggered = True
            state.processing_task = asyncio.create_task(run_stepfun_step_plan_realtime_turn(
                websocket,
                state,
                reason="local_server_vad_stop",
            ))
        else:
            await cancel_recording_watchdog(state, reason="local_server_vad_stop")
            state.processing_task = asyncio.create_task(run_voice_turn(websocket, config, state))


async def maybe_trigger_stepfun_realtime_speech_stopped(websocket: Any, state: TurnState) -> None:
    session = state.stepfun_realtime_session
    if session is None:
        return
    if state.stepfun_realtime_triggered:
        return
    if not getattr(session, "speech_stopped_event", None) or not session.speech_stopped_event.is_set():
        return
    if state.processing_task and not state.processing_task.done():
        return
    state.stepfun_realtime_triggered = True
    state.server_vad_triggered = True
    mark_turn_trigger(state, "stepfun_realtime_speech_stopped")
    await cancel_recording_watchdog(state, reason="stepfun_realtime_speech_stopped")
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "server_vad_stop",
            "status": "ok",
            "turn_id": state.turn_id,
            "reason": "stepfun_realtime_speech_stopped",
        },
    })
    state.processing_task = asyncio.create_task(run_stepfun_step_plan_realtime_turn(
        websocket,
        state,
        reason="stepfun_realtime_speech_stopped",
    ))


async def monitor_stepfun_realtime_speech_stopped(websocket: Any, state: TurnState) -> None:
    session = state.stepfun_realtime_session
    event = getattr(session, "speech_stopped_event", None)
    if event is None:
        return
    try:
        await event.wait()
        await maybe_trigger_stepfun_realtime_speech_stopped(websocket, state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive async boundary
        log_gateway("stepfun_realtime_monitor_failed", turn_id=state.turn_id, detail=exc.__class__.__name__)


async def maybe_trigger_streaming_asr_final_turn(websocket: Any, config: GatewayConfig, state: TurnState) -> None:
    if not STEPFUN_WS_ASR_EARLY_TURN_ENABLED:
        return
    if not state.streaming_asr_final_ready or state.streaming_asr_early_turn_triggered:
        return
    if state.processing_task and not state.processing_task.done():
        return
    if transcript_is_low_confidence_fragment(state.streaming_asr_final_text):
        if not state.streaming_asr_early_turn_blocked:
            state.streaming_asr_early_turn_blocked = True
            log_gateway(
                "streaming_asr_early_turn_blocked",
                turn_id=state.turn_id,
                reason="low_confidence_fragment",
                text_chars=len((state.streaming_asr_final_text or "").strip()),
                audio_ms=streaming_asr_audio_ms(state),
                transcript_preview=_transcript_preview(state.streaming_asr_final_text),
            )
        return
    text_chars = len((state.streaming_asr_final_text or "").strip())
    audio_ms = streaming_asr_audio_ms(state)
    if text_chars < STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS or audio_ms < STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS:
        if not state.streaming_asr_early_turn_blocked:
            state.streaming_asr_early_turn_blocked = True
            log_gateway(
                "streaming_asr_early_turn_blocked",
                turn_id=state.turn_id,
                reason=state.streaming_asr_final_reason,
                text_chars=text_chars,
                audio_ms=audio_ms,
                min_chars=STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS,
                min_audio_ms=STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS,
            )
        return
    trusted_partial = (
        state.streaming_asr_final_reason == "deterministic_partial"
        or (
            state.streaming_asr_final_reason == "stable_partial"
            and STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED
        )
    )
    if (
        state.streaming_asr_final_reason != "final"
        and not trusted_partial
        and not STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL
    ):
        if not state.streaming_asr_early_turn_blocked:
            state.streaming_asr_early_turn_blocked = True
            log_gateway(
                "streaming_asr_early_turn_blocked",
                turn_id=state.turn_id,
                reason=state.streaming_asr_final_reason,
                text_chars=text_chars,
                audio_ms=audio_ms,
            )
        return
    state.streaming_asr_early_turn_triggered = True
    state.server_vad_triggered = True
    mark_turn_trigger(
        state,
        "streaming_asr_final",
        detail=state.streaming_asr_final_reason,
        audio_ms=streaming_asr_audio_ms(state),
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "server_vad_stop",
            "status": "ok",
            "turn_id": state.turn_id,
            "reason": "streaming_asr_final",
            "streaming_asr_reason": state.streaming_asr_final_reason,
            "streaming_asr_final_ms": state.streaming_asr_final_ms,
        },
    })
    log_gateway(
        "streaming_asr_final_turn",
        turn_id=state.turn_id,
        final_ms=state.streaming_asr_final_ms,
        reason=state.streaming_asr_final_reason,
        text_chars=text_chars,
        audio_ms=audio_ms,
    )
    await cancel_recording_watchdog(state, reason="streaming_asr_final")
    state.processing_task = asyncio.create_task(run_voice_turn(websocket, config, state))


async def monitor_streaming_asr_final(websocket: Any, config: GatewayConfig, state: TurnState) -> None:
    session = state.streaming_asr_session
    event = getattr(session, "final_event", None)
    if event is None:
        return
    try:
        await event.wait()
        await maybe_trigger_streaming_asr_final_turn(websocket, config, state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive async boundary
        log_gateway("streaming_asr_final_monitor_failed", turn_id=state.turn_id, detail=exc.__class__.__name__)


def server_vad_pcm_from_packet(state: TurnState, packet: bytes) -> bytes:
    audio_format = str(state.audio_format or "").strip().lower()
    if audio_format in {"pcm", "s16le", "raw"}:
        pcm = bytes(packet or b"")
        if state.sample_rate != DEVICE_SAMPLE_RATE:
            return resample_pcm16_mono(pcm, source_rate=state.sample_rate, target_rate=DEVICE_SAMPLE_RATE)
        return pcm
    if audio_format == "opus":
        try:
            if state.vad_opus_decoder is None:
                state.vad_opus_decoder = OpusPacketDecoder(sample_rate=state.sample_rate or DEVICE_SAMPLE_RATE)
            return state.vad_opus_decoder.decode_packet(packet, frame_duration_ms=state.frame_duration_ms or 60)
        except Exception:
            return b""
    return b""


def close_state_vad_decoder(state: TurnState) -> None:
    decoder = getattr(state, "vad_opus_decoder", None)
    if decoder is not None:
        try:
            decoder.close()
        except Exception:
            pass
    state.vad_opus_decoder = None
    decoder = getattr(state, "streaming_asr_opus_decoder", None)
    if decoder is not None:
        try:
            decoder.close()
        except Exception:
            pass
    state.streaming_asr_opus_decoder = None


def pcm16_rms(pcm: bytes) -> int:
    sample_count = len(pcm) // 2
    if sample_count <= 0:
        return 0
    total = 0
    for index in range(sample_count):
        sample = int.from_bytes(pcm[index * 2:index * 2 + 2], "little", signed=True)
        total += sample * sample
    return int((total / sample_count) ** 0.5)


def streaming_asr_audio_ms(state: TurnState) -> int:
    bytes_per_second = DEVICE_SAMPLE_RATE * DEVICE_SAMPLE_WIDTH * DEVICE_CHANNELS
    if bytes_per_second <= 0:
        return 0
    return int(max(0, state.streaming_asr_audio_bytes) * 1000 / bytes_per_second)


def streaming_asr_prefetch_payload(state: TurnState) -> dict[str, Any]:
    return {
        "streaming_asr_prefetch_status": state.streaming_asr_prefetch_status,
        "streaming_asr_prefetch_intent": state.streaming_asr_prefetch_intent,
        "streaming_asr_prefetch_subject": state.streaming_asr_prefetch_subject,
        "streaming_asr_prefetch_location": state.streaming_asr_prefetch_location,
        "streaming_asr_prefetch_started_ms": state.streaming_asr_prefetch_started_ms,
        "streaming_asr_prefetch_done_ms": state.streaming_asr_prefetch_done_ms,
        "streaming_asr_prefetch_wait_ms": state.streaming_asr_prefetch_wait_ms,
    }


def streaming_asr_prefetch_log_fields(state: TurnState) -> dict[str, Any]:
    payload = streaming_asr_prefetch_payload(state)
    if state.streaming_asr_prefetch_error:
        payload["streaming_asr_prefetch_error"] = state.streaming_asr_prefetch_error
    return payload


async def cancel_streaming_asr_prefetch(state: TurnState) -> None:
    task = state.streaming_asr_prefetch_task
    state.streaming_asr_prefetch_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    reset_streaming_asr_prefetch_state(state)


def reset_streaming_asr_prefetch_state(state: TurnState) -> None:
    state.streaming_asr_prefetch_text = ""
    state.streaming_asr_prefetch_intent = ""
    state.streaming_asr_prefetch_subject = ""
    state.streaming_asr_prefetch_location = ""
    state.streaming_asr_prefetch_status = ""
    state.streaming_asr_prefetch_started_ms = 0
    state.streaming_asr_prefetch_done_ms = 0
    state.streaming_asr_prefetch_wait_ms = 0
    state.streaming_asr_prefetch_error = ""


async def await_streaming_asr_prefetch(state: TurnState) -> None:
    task = state.streaming_asr_prefetch_task
    if task is None or task.done() or STREAMING_ASR_PREFETCH_WAIT_MS <= 0:
        return
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=STREAMING_ASR_PREFETCH_WAIT_MS / 1000)
    except asyncio.TimeoutError:
        state.streaming_asr_prefetch_wait_ms += _elapsed_ms(started)
        log_gateway(
            "streaming_asr_prefetch_wait_timeout",
            turn_id=state.turn_id,
            wait_ms=state.streaming_asr_prefetch_wait_ms,
            intent=state.streaming_asr_prefetch_intent,
            subject=state.streaming_asr_prefetch_subject,
            location=state.streaming_asr_prefetch_location,
        )
        return
    except Exception as exc:  # pragma: no cover - defensive async boundary
        state.streaming_asr_prefetch_wait_ms += _elapsed_ms(started)
        state.streaming_asr_prefetch_status = "failed"
        state.streaming_asr_prefetch_error = exc.__class__.__name__
        return
    state.streaming_asr_prefetch_wait_ms += _elapsed_ms(started)


def maybe_start_streaming_asr_prefetch(state: TurnState, text: str, runtime_config: Any) -> None:
    if not STREAMING_ASR_PREFETCH_ENABLED:
        return
    if not text or len(text.strip()) < STREAMING_ASR_PREFETCH_MIN_CHARS:
        return
    if state.streaming_asr_prefetch_task is not None:
        return
    user_geo = state.metadata.get("user_geo") if isinstance(state.metadata.get("user_geo"), dict) else None
    plan = streaming_asr_prefetch_plan(text, runtime_config, user_geo=user_geo)
    if not plan:
        return
    state.streaming_asr_prefetch_text = text.strip()
    state.streaming_asr_prefetch_intent = str(plan.get("intent") or "")
    state.streaming_asr_prefetch_subject = str(plan.get("subject") or "")
    state.streaming_asr_prefetch_location = str(plan.get("location") or "")
    state.streaming_asr_prefetch_status = "started"
    state.streaming_asr_prefetch_started_ms = (
        _elapsed_ms(state.streaming_asr_started_at)
        if state.streaming_asr_started_at
        else 0
    )
    state.streaming_asr_prefetch_error = ""
    state.streaming_asr_prefetch_task = asyncio.create_task(run_streaming_asr_prefetch(state, runtime_config, plan))
    log_gateway(
        "streaming_asr_prefetch_started",
        turn_id=state.turn_id,
        intent=state.streaming_asr_prefetch_intent,
        subject=state.streaming_asr_prefetch_subject,
        location=state.streaming_asr_prefetch_location,
        started_ms=state.streaming_asr_prefetch_started_ms,
        text_chars=len(text.strip()),
    )


def streaming_asr_prefetch_plan(text: str, runtime_config: Any, *, user_geo: dict[str, Any] | None = None) -> dict[str, Any]:
    if not runtime_config or not load_persona_config or not resolve_query_context:
        return {}
    if _streaming_asr_has_correction_marker(text):
        return {}
    try:
        persona_config = load_persona_config()
        provided_user_geo = user_geo if isinstance(user_geo, dict) else {}
        user_geo = provided_user_geo or persona_config.configured_user_geo()
        local_intent = streaming_asr_local_quality_intent(text)
        if local_intent:
            plan = {
                "intent": local_intent,
                "subject": "aura" if local_intent == "state_mood" else "user",
                "location": "",
                "local_quality": True,
            }
            if local_intent == "outing_weather_advice":
                latitude = str(user_geo.get("latitude") or user_geo.get("lat") or "").strip()
                longitude = str(user_geo.get("longitude") or user_geo.get("lon") or user_geo.get("lng") or "").strip()
                plan.update({
                    "location": str(user_geo.get("city") or ""),
                    "city": str(user_geo.get("city") or ""),
                    "latitude": latitude,
                    "longitude": longitude,
                    "user_weather": bool(str(user_geo.get("city") or "").strip() or (latitude and longitude)),
                })
            return plan
        query = resolve_query_context(
            text,
            aura_home_city=persona_config.aura_home_city,
            user_home_city=persona_config.user_home_city,
            user_geo=user_geo,
        )
    except Exception:
        return {}
    if query.intent == "time":
        return {
            "intent": "time",
            "subject": query.subject_entity,
            "location": query.target_location,
        }
    if query.intent not in {"weather", "weather_advice"}:
        return {}
    if query.intent == "weather_advice" and not _streaming_asr_can_prefetch_weather_advice(text):
        return {}
    target = str(query.target_location or "").strip()
    if query.subject_entity == "aura" or query.location_source == "aura_home":
        return {
            "intent": query.intent,
            "subject": "aura",
            "location": target or persona_config.aura_home_city,
            "city": target or persona_config.aura_home_city,
            "latitude": "",
            "longitude": "",
            "user_weather": False,
        }
    if query.subject_entity == "location" and target:
        return {
            "intent": query.intent,
            "subject": "location",
            "location": target,
            "city": target,
            "latitude": "",
            "longitude": "",
            "user_weather": True,
        }
    if query.subject_entity == "user":
        latitude = str(user_geo.get("latitude") or user_geo.get("lat") or "").strip()
        longitude = str(user_geo.get("longitude") or user_geo.get("lon") or user_geo.get("lng") or "").strip()
        if not (target or (latitude and longitude)):
            return {}
        return {
            "intent": query.intent,
            "subject": "user",
            "location": target or str(user_geo.get("city") or ""),
            "city": target,
            "latitude": latitude,
            "longitude": longitude,
            "user_weather": True,
        }
    return {}


def streaming_asr_local_quality_intent(text: str) -> str:
    clean = _normalized_asr_text(text).lower()
    if not clean:
        return ""
    if any(marker in clean for marker in STREAMING_ASR_STABLE_PARTIAL_BLOCK_MARKERS):
        return ""
    if any(token in clean for token in ("为什么", "为啥", "原因", "建议")):
        return ""
    if _streaming_asr_is_quick_ack(clean):
        return "quick_ack"
    if _streaming_asr_is_supportive_chat(clean):
        return "supportive_chat"
    if any(token in clean for token in ("心情怎么样", "心情好吗", "开心吗", "高兴吗", "今天心情")):
        return "state_mood"
    if _streaming_asr_is_latency_diagnostic(clean):
        return "voice_latency_diagnostic"
    has_outing = any(token in clean for token in ("出门", "出去", "外出", "去外面", "下午出去", "下午出门"))
    has_near_term = any(token in clean for token in ("今天", "下午", "一会", "等会", "现在", "待会", "打算", "准备"))
    if has_outing and has_near_term:
        return "outing_weather_advice"
    return ""


def _streaming_asr_is_quick_ack(clean: str) -> bool:
    value = str(clean or "")
    if not value:
        return False
    if any(token in value for token in ("天气", "几点", "多少", "在哪", "干嘛", "查", "写", "生成", "总结")):
        return False
    if value in {"测试", "测试一下", "测试下", "试一下", "试下", "听得到吗", "能听到吗", "还在吗", "你还在吗"}:
        return True
    return "测试" in value and any(token in value for token in ("简单回应", "简单回复", "一句"))


def _streaming_asr_is_supportive_chat(clean: str) -> bool:
    value = str(clean or "")
    if not value:
        return False
    return any(token in value for token in ("累", "困", "难受", "不舒服", "烦", "焦虑", "压力", "低落", "难过", "委屈", "睡不着", "陪我", "聊两句", "说说话", "安慰"))


def _streaming_asr_is_latency_diagnostic(clean: str) -> bool:
    value = str(clean or "")
    if not value:
        return False
    has_topic = any(token in value for token in ("回复速度", "响应速度", "反应速度", "语音链路", "首字", "首包", "首音频"))
    has_bottleneck = any(token in value for token in ("哪里慢", "慢在哪里", "卡在哪里", "耗时", "慢", "卡", "速度"))
    return has_topic and has_bottleneck


def _streaming_asr_can_prefetch_weather_advice(text: str) -> bool:
    normalized = str(text or "").translate(str.maketrans({
        "？": "?",
        "！": "!",
        "，": ",",
        "。": ".",
        "、": ",",
    }))
    clean = re.sub(r"[\s,.;:!?~～]+", "", normalized).lower()
    if not clean:
        return False
    if any(token in clean for token in ("为什么", "为啥", "原因", "建议")):
        return False
    return any(token in clean for token in ("我需要带伞", "需不需要带伞", "需要带伞吗", "带不带伞"))


def streaming_asr_deterministic_partial_plan(text: str, runtime_config: Any) -> dict[str, Any]:
    if not STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED:
        return {}
    value = str(text or "").strip()
    if _streaming_asr_has_correction_marker(value):
        return {}
    if len(value) < min(STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_CHARS, STREAMING_ASR_GROUNDED_CURRENT_MIN_CHARS):
        return {}
    grounded_intent = classify_grounded_current_intent(value) if classify_grounded_current_intent else None
    if grounded_intent:
        return {
            "intent": "activity_or_location",
            "subject": "aura",
            "location": "",
            "grounded_current_intent": str(grounded_intent),
        }
    local_intent = streaming_asr_local_quality_intent(value)
    if local_intent in {"quick_ack", "supportive_chat", "state_mood", "voice_latency_diagnostic"}:
        return {
            "intent": "local_quality",
            "subject": "aura",
            "location": "",
            "local_quality": True,
            "local_quality_intent": local_intent,
        }
    if len(value) < STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_CHARS:
        return {}
    plan = streaming_asr_prefetch_plan(value, runtime_config)
    if not plan:
        return {}
    intent = str(plan.get("intent") or "")
    if intent in {"state_mood"}:
        return {
            "intent": "local_quality",
            "subject": "aura",
            "location": "",
            "local_quality": True,
            "local_quality_intent": intent,
        }
    if intent not in {"weather", "time", "activity_or_location"}:
        return {}
    subject = str(plan.get("subject") or "")
    location = str(plan.get("location") or "")
    if intent == "weather" and not location:
        return {}
    if (
        intent in {"weather", "weather_advice"}
        and subject == "user"
        and not location
        and not (str(plan.get("latitude") or "") and str(plan.get("longitude") or ""))
    ):
        return {}
    return plan


def streaming_asr_can_trigger_deterministic_partial(state: TurnState, text: str, runtime_config: Any) -> tuple[bool, dict[str, Any], str]:
    plan = streaming_asr_deterministic_partial_plan(text, runtime_config)
    if not plan:
        return False, {}, "not_deterministic"
    intent = str(plan.get("intent") or "")
    text_chars = len(str(text or "").strip())
    audio_ms = streaming_asr_audio_ms(state)
    is_grounded_current = intent == "activity_or_location"
    is_local_quality = intent == "local_quality"
    min_chars = STREAMING_ASR_GROUNDED_CURRENT_MIN_CHARS if is_grounded_current else STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_CHARS
    min_audio_ms = (
        STREAMING_ASR_GROUNDED_CURRENT_MIN_AUDIO_MS
        if is_grounded_current
        else STREAMING_ASR_LOCAL_QUALITY_MIN_AUDIO_MS
        if is_local_quality
        else STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS
    )
    if text_chars < min_chars:
        return False, plan, "too_short"
    if audio_ms < min_audio_ms:
        return False, plan, "audio_too_short"
    if intent not in STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS:
        return False, plan, "intent_not_early_safe"
    return True, plan, "ok"


STREAMING_ASR_STABLE_PARTIAL_BLOCK_MARKERS = (
    "等一下",
    "等等",
    "先别回答",
    "别回答",
    "不是",
    "我不是",
    "不是问",
    "我想问的是",
    "想问的是",
    "改一下",
    "更正",
    "纠正",
    "不是这个",
    "别急",
)


def _streaming_asr_has_correction_marker(text: str) -> bool:
    value = _normalized_asr_text(text)
    if not value:
        return False
    return any(marker and marker in value for marker in STREAMING_ASR_STABLE_PARTIAL_BLOCK_MARKERS)


def _normalized_asr_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def streaming_asr_can_trigger_stable_partial(state: TurnState, text: str) -> tuple[bool, str]:
    if not STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED:
        return False, "disabled"
    value = _normalized_asr_text(text)
    text_chars = len(value)
    if text_chars < STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS:
        return False, "too_short"
    audio_ms = streaming_asr_audio_ms(state)
    if audio_ms < STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS:
        return False, "audio_too_short"
    if _streaming_asr_has_correction_marker(value):
        return False, "correction_marker"
    if value.endswith(("，", "、", ",", "；", ";", "：", ":")):
        return False, "trailing_clause"
    return True, "ok"


async def run_streaming_asr_prefetch(state: TurnState, runtime_config: Any, plan: dict[str, Any]) -> None:
    try:
        if plan.get("intent") in {"time", "state_mood"}:
            state.streaming_asr_prefetch_status = "done"
            state.streaming_asr_prefetch_done_ms = (
                _elapsed_ms(state.streaming_asr_started_at)
                if state.streaming_asr_started_at
                else 0
            )
            log_gateway(
                "streaming_asr_prefetch_done",
                turn_id=state.turn_id,
                intent=state.streaming_asr_prefetch_intent,
                subject=state.streaming_asr_prefetch_subject,
                location=state.streaming_asr_prefetch_location,
                done_ms=state.streaming_asr_prefetch_done_ms,
            )
            return
        if plan.get("intent") == "outing_weather_advice" and not plan.get("user_weather"):
            state.streaming_asr_prefetch_status = "skipped"
            return
        if plan.get("intent") not in {"weather", "weather_advice", "outing_weather_advice"}:
            state.streaming_asr_prefetch_status = "skipped"
            return
        if plan.get("user_weather"):
            if not refresh_user_weather_if_needed:
                state.streaming_asr_prefetch_status = "unavailable"
                return
            _updated, snapshot = await asyncio.to_thread(
                refresh_user_weather_if_needed,
                runtime_config,
                city=str(plan.get("city") or ""),
                latitude=str(plan.get("latitude") or ""),
                longitude=str(plan.get("longitude") or ""),
            )
        else:
            if not refresh_cached_weather_if_needed:
                state.streaming_asr_prefetch_status = "unavailable"
                return
            _updated, refresh = await asyncio.to_thread(
                refresh_cached_weather_if_needed,
                runtime_config,
                city=str(plan.get("city") or ""),
            )
            snapshot = dict(refresh.get("weather") or {}) if isinstance(refresh, dict) else {}
        status = str((snapshot or {}).get("status") or (snapshot or {}).get("condition") or "")
        state.streaming_asr_prefetch_status = "done"
        state.streaming_asr_prefetch_done_ms = (
            _elapsed_ms(state.streaming_asr_started_at)
            if state.streaming_asr_started_at
            else 0
        )
        log_gateway(
            "streaming_asr_prefetch_done",
            turn_id=state.turn_id,
            intent=state.streaming_asr_prefetch_intent,
            subject=state.streaming_asr_prefetch_subject,
            location=state.streaming_asr_prefetch_location,
            done_ms=state.streaming_asr_prefetch_done_ms,
            weather_status=status,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive async boundary
        state.streaming_asr_prefetch_status = "failed"
        state.streaming_asr_prefetch_error = exc.__class__.__name__
        state.streaming_asr_prefetch_done_ms = (
            _elapsed_ms(state.streaming_asr_started_at)
            if state.streaming_asr_started_at
            else 0
        )
        log_gateway(
            "streaming_asr_prefetch_failed",
            turn_id=state.turn_id,
            detail=exc.__class__.__name__,
            intent=state.streaming_asr_prefetch_intent,
            subject=state.streaming_asr_prefetch_subject,
            location=state.streaming_asr_prefetch_location,
        )


def emotion_to_pose(emotion: str | None) -> str | None:
    """回复情绪 → 设备姿势名；None 表示不带 pose，设备保持随机说话姿势。"""
    value = str(emotion or "").strip().lower()
    if value in ("warm", "happy", "excited"):
        return "happy"
    if value in ("firm", "proud"):
        return "proud"
    return None


WORLD_LOCATION_SCENES = {
    "home.living_room": "home.living_room",
    "home.study": "home.study",
    "home.bedroom": "home.bedroom",
    "home.kitchen": "home.kitchen",
    "home.balcony": "home.balcony",
    "outside.neighborhood": "outside.neighborhood",
    "outside.cafe": "outside.cafe",
    "outside.shop": "outside.shop",
    "outside.park": "outside.park",
    "outside.mall": "outside.mall",
    "outside.riverside": "outside.riverside",
    "home": "home.living_room",
    "desk": "home.study",
    "neighborhood_food": "outside.neighborhood",
    "nearby_walk": "outside.park",
    "quiet_stop": "outside.cafe",
    "daily_shop": "outside.shop",
    "park": "outside.park",
    "mall": "outside.mall",
    "riverside": "outside.riverside",
}


def scene_for_world_location(location_key: str | None) -> str:
    return WORLD_LOCATION_SCENES.get(str(location_key or "").strip(), "home.living_room")


def dialogue_scene(payload: dict[str, Any] | None) -> str:
    body = payload if isinstance(payload, dict) else {}
    evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else {}
    location_key = str(evidence.get("world_location_key") or "").strip()
    if not location_key:
        debug = body.get("debug") if isinstance(body.get("debug"), dict) else {}
        context = debug.get("context") if isinstance(debug.get("context"), dict) else {}
        world = context.get("world_snapshot") if isinstance(context.get("world_snapshot"), dict) else {}
        current = world.get("current") if isinstance(world.get("current"), dict) else {}
        location_key = str(current.get("location_key") or "").strip()
    return scene_for_world_location(location_key)


def voice_turn_pose(voice_turn: dict[str, Any] | None) -> str | None:
    if not isinstance(voice_turn, dict):
        return None
    return emotion_to_pose(voice_turn.get("emotion"))


async def send_dialogue_and_tts(
    websocket: Any,
    runtime_config: Any,
    state: TurnState,
    response: str,
    *,
    ok: bool,
    deferred: bool = False,
    request_id: str = "",
    preface: TtsResult | None = None,
    transcript: str = "",
    bridge_evidence: dict[str, Any] | None = None,
    voice_turn: dict[str, Any] | None = None,
    coins_earned: int = 0,
) -> None:
    await close_stepfun_tts_warm_session(state)
    spoken_response = device_spoken_text(response)
    local_audio_id = ""
    if state.supports_local_audio:
        localized = match_local_audio(spoken_response, state.language)
        if localized:
            spoken_response, local_audio_id = localized
    quality_trace = voice_quality_trace_payload(
        transcript=transcript,
        response=spoken_response,
        evidence=bridge_evidence,
        voice_turn=voice_turn,
    )
    await send_json(websocket, {
        "type": "dialogue",
        "payload": {
            "turn_id": state.turn_id,
            "request_id": request_id,
            "text": spoken_response,
            "segments": dialogue_segments(spoken_response),
            "pose": voice_turn_pose(voice_turn),
            "scene": dialogue_scene({"evidence": bridge_evidence or {}}),
            "coins_earned": max(0, int(coins_earned or 0)),
            "deferred": bool(deferred),
            "continue_listening": continue_listening_for_device(state),
            **({"local_audio_id": local_audio_id} if local_audio_id else {}),
            "timing": {
                "asr_ms": state.asr_latency_ms,
                "bridge_ms": state.bridge_latency_ms,
                "streaming_asr_first_delta_ms": state.streaming_asr_first_delta_ms,
                "streaming_asr_final_ms": state.streaming_asr_final_ms,
                "streaming_asr_final_reason": state.streaming_asr_final_reason,
                **asr_diagnostic_payload(state),
                **streaming_asr_quality_gate_payload(state),
                **turn_trigger_payload(state),
                **quality_trace,
            },
        },
    })
    if local_audio_id:
        tts = TtsResult(ok=True, detail="local_audio", device_sample_rate=DEVICE_SAMPLE_RATE)
    else:
        tts = await synthesize_and_stream_tts(
            websocket,
            runtime_config,
            state.turn_id,
            spoken_response,
            stream_id=1,
            is_final=True,
            preface=False,
        )
    first_audio_ms = preface.first_audio_ms if preface and preface.ok and preface.first_audio_ms else tts.first_audio_ms
    tts_total_ms = tts.latency_ms + (preface.latency_ms if preface and preface.ok else 0)
    tts_chunk_count = tts.chunk_count + (preface.chunk_count if preface and preface.ok else 0)
    tts_first_audio_since_bridge_ms = state.bridge_latency_ms + first_audio_ms if first_audio_ms else 0
    tts_gap_payload = tts_chunk_timing_payload(tts)
    timing_breakdown = voice_latency_breakdown(
        state,
        bridge_first_delta_ms=state.bridge_latency_ms,
        tts_first_audio_since_bridge_ms=tts_first_audio_since_bridge_ms,
    )
    diagnosis = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=state.bridge_latency_ms,
        tts_first_text_ms=state.bridge_latency_ms,
        tts_first_audio_since_bridge_ms=tts_first_audio_since_bridge_ms,
        tts_first_text_to_audio_ms=first_audio_ms,
        audio_send_realtime_x100=int(tts_gap_payload.get("tts_audio_send_realtime_x100") or 0),
        audio_chunk_stall_count=int(tts_gap_payload.get("tts_audio_chunk_stall_count") or 0),
        streamed_bridge=False,
    )
    log_gateway(
        "turn_audio_timing",
        turn_id=state.turn_id,
        status="ok" if tts.ok else "failed",
        asr_ms=state.asr_latency_ms,
        streaming_asr_first_delta_ms=state.streaming_asr_first_delta_ms,
        streaming_asr_final_ms=state.streaming_asr_final_ms,
        streaming_asr_final_reason=state.streaming_asr_final_reason,
        streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
        streaming_asr_forwarded_frames=state.streaming_asr_forwarded_frames,
        streaming_asr_queue_drops=state.streaming_asr_queue_drops,
        **asr_diagnostic_payload(state),
        **audio_receive_payload(state),
        **streaming_asr_quality_gate_payload(state),
        **turn_trigger_payload(state),
        **streaming_asr_prefetch_log_fields(state),
        **quality_trace,
        bridge_ms=state.bridge_latency_ms,
        **timing_breakdown,
        **diagnosis,
        tts_first_chunk_ms=tts.first_chunk_ms,
        tts_first_audio_ms=first_audio_ms,
        tts_first_audio_since_bridge_ms=tts_first_audio_since_bridge_ms,
        **tts_gap_payload,
        tts_total_ms=tts_total_ms,
        tts_chunk_count=tts_chunk_count,
        response_chars=len(spoken_response),
        preface_ms=preface.first_audio_ms if preface and preface.ok else 0,
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "turn_audio_timing",
            "status": "ok" if tts.ok else "failed",
            "turn_id": state.turn_id,
            "asr_ms": state.asr_latency_ms,
            "streaming_asr_first_delta_ms": state.streaming_asr_first_delta_ms,
            "streaming_asr_final_ms": state.streaming_asr_final_ms,
            "streaming_asr_final_reason": state.streaming_asr_final_reason,
            "streaming_asr_audio_bytes": state.streaming_asr_audio_bytes,
            "streaming_asr_forwarded_frames": state.streaming_asr_forwarded_frames,
            "streaming_asr_queue_drops": state.streaming_asr_queue_drops,
            **asr_diagnostic_payload(state),
            **audio_receive_payload(state),
            **streaming_asr_quality_gate_payload(state),
            **turn_trigger_payload(state),
            **streaming_asr_prefetch_payload(state),
            **quality_trace,
            "bridge_ms": state.bridge_latency_ms,
            **timing_breakdown,
            **diagnosis,
            "tts_first_chunk_ms": tts.first_chunk_ms,
            "tts_first_audio_ms": first_audio_ms,
            "tts_first_audio_since_bridge_ms": tts_first_audio_since_bridge_ms,
            **tts_gap_payload,
            "tts_total_ms": tts_total_ms,
            "tts_chunk_count": tts_chunk_count,
            "tts_preface_ms": preface.first_audio_ms if preface and preface.ok else 0,
        },
    })
    # 每轮对话都会消耗体力/涨好感，回合结束顺带把最新数值推给设备。
    start_status_update_task(websocket, runtime_config)


async def maybe_send_tts_preface(
    websocket: Any,
    runtime_config: Any,
    state: TurnState,
    *,
    preface_task: asyncio.Task[TtsResult] | None = None,
) -> TtsResult | None:
    if not TTS_PREFACE_ENABLED:
        return None
    result = await tts_preface_audio(runtime_config, preface_task=preface_task)
    first_audio_since_stop_ms = 0
    if result.ok and result.audio:
        started = time.monotonic()
        await send_tts_pcm_stream(websocket, state.turn_id, result.audio, stream_id=1, is_final=False)
        first_audio_ms = _elapsed_ms(started)
        first_audio_since_stop_ms = _elapsed_ms(state.processing_started_at) if state.processing_started_at else first_audio_ms
        result = TtsResult(
            ok=True,
            audio=b"",
            detail=result.detail,
            chunk_count=result.chunk_count,
            audio_chunk_count=(len(result.audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES,
            audio_bytes=len(result.audio),
            latency_ms=_elapsed_ms(started),
            first_chunk_ms=0,
            first_audio_ms=first_audio_since_stop_ms,
            source_sample_rate=result.source_sample_rate,
            device_sample_rate=DEVICE_SAMPLE_RATE,
            streamed=True,
        )
    log_gateway(
        "tts_preface",
        turn_id=state.turn_id,
        ok=result.ok,
        text=TTS_PREFACE_TEXT,
        first_audio_ms=result.first_audio_ms,
        first_audio_since_stop_ms=first_audio_since_stop_ms if result.ok else 0,
        latency_ms=result.latency_ms,
    )
    return result


def tts_preface_cache_key(runtime_config: Any) -> tuple[str, str, str, str, int, str]:
    return (
        str(getattr(runtime_config, "tts_provider", "") or ""),
        str(getattr(runtime_config, "tts_model", "") or ""),
        str(getattr(runtime_config, "tts_voice", "") or ""),
        str(getattr(runtime_config, "tts_base_url", "") or ""),
        _tts_source_sample_rate(runtime_config),
        TTS_PREFACE_TEXT,
    )


def ensure_tts_preface_task(runtime_config: Any) -> asyncio.Task[TtsResult] | None:
    if not TTS_PREFACE_ENABLED or not runtime_config or not getattr(runtime_config, "tts_enabled", False):
        return None
    key = tts_preface_cache_key(runtime_config)
    cached = _TTS_PREFACE_CACHE.get(key)
    if cached and cached.ok:
        return None
    existing = _TTS_PREFACE_TASKS.get(key)
    if existing and not existing.done():
        return existing
    task = asyncio.create_task(asyncio.to_thread(synthesize_tts_preface, runtime_config, key))
    task.add_done_callback(lambda finished, cache_key=key: _finish_tts_preface_task(cache_key, finished))
    _TTS_PREFACE_TASKS[key] = task
    return task


def _finish_tts_preface_task(key: tuple[str, str, str, str, int, str], task: asyncio.Task[TtsResult]) -> None:
    try:
        result = task.result()
    except Exception as exc:  # pragma: no cover - defensive background-task guard
        log_gateway("tts_preface_warm_failed", detail=exc.__class__.__name__)
        return
    if result.ok:
        _TTS_PREFACE_CACHE[key] = result
        log_gateway(
            "tts_preface_warmed",
            ok=True,
            chars=len(key[-1]),
            audio_bytes=result.audio_bytes,
            latency_ms=result.latency_ms,
            source_sample_rate=result.source_sample_rate,
        )


async def tts_preface_audio(runtime_config: Any, *, preface_task: asyncio.Task[TtsResult] | None = None) -> TtsResult:
    if not runtime_config or not getattr(runtime_config, "tts_enabled", False):
        return TtsResult(ok=False, detail="TTS 未启用")
    key = tts_preface_cache_key(runtime_config)
    cached = _TTS_PREFACE_CACHE.get(key)
    if cached and cached.ok:
        return cached
    task = preface_task or ensure_tts_preface_task(runtime_config)
    if task:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0, TTS_PREFACE_MAX_WAIT_MS) / 1000)
        except asyncio.TimeoutError:
            return TtsResult(ok=False, detail="preface still warming")
        except Exception as exc:
            return TtsResult(ok=False, detail=f"preface warm failed: {exc.__class__.__name__}")
    return await asyncio.to_thread(synthesize_tts_preface, runtime_config, key)


def synthesize_tts_preface(runtime_config: Any, key: tuple[str, str, str, str, int, str] | None = None) -> TtsResult:
    started = time.monotonic()
    result = synthesize_tts(runtime_config, TTS_PREFACE_TEXT)
    if result.ok and result.audio:
        audio = result.audio
        if TTS_PREFACE_MIN_DEVICE_BYTES > 0 and len(audio) < TTS_PREFACE_MIN_DEVICE_BYTES:
            audio += b"\x00" * (TTS_PREFACE_MIN_DEVICE_BYTES - len(audio))
        result = TtsResult(
            ok=True,
            audio=audio,
            detail=result.detail,
            chunk_count=result.chunk_count,
            audio_chunk_count=(len(audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES,
            audio_bytes=len(audio),
            latency_ms=_elapsed_ms(started),
            first_chunk_ms=result.first_chunk_ms,
            first_audio_ms=result.first_audio_ms,
            source_sample_rate=result.source_sample_rate,
            device_sample_rate=DEVICE_SAMPLE_RATE,
            streamed=False,
        )
        _TTS_PREFACE_CACHE[key or tts_preface_cache_key(runtime_config)] = result
    return result


def background_work_beans(duration_seconds: float) -> int:
    """按后台任务时长折算打工豆子：每 5 秒 1 豆，至少 1 豆，封顶 20 豆。"""
    try:
        seconds = max(0.0, float(duration_seconds or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    return max(1, min(20, int(seconds // 5) + 1))


def background_work_progress(elapsed_seconds: float, timeout_seconds: float) -> int:
    """打工进度条：从 10% 匀速爬到 95%，结果到手前不到 100%。"""
    try:
        total = max(1.0, float(timeout_seconds or 1.0))
        elapsed = max(0.0, float(elapsed_seconds or 0.0))
    except (TypeError, ValueError):
        return 10
    return min(95, 10 + int((elapsed / total) * 85))


async def poll_and_send_background_result(
    websocket: Any,
    config: GatewayConfig,
    runtime_config: Any,
    state: TurnState,
    task_id: str,
) -> None:
    started = time.monotonic()
    await send_json(websocket, {
        "type": "status",
        "text": "后台任务处理中",
        "payload": {"turn_id": state.turn_id, "task_id": task_id},
    })
    deadline = started + BACKGROUND_POLL_TIMEOUT_SECONDS
    last_progress_sent = 0.0
    while time.monotonic() < deadline:
        await asyncio.sleep(BACKGROUND_POLL_INTERVAL_SECONDS)
        now = time.monotonic()
        # 打工进度心跳：设备端 IDLE 时靠它把 WORK 面板重新亮起来，
        # 不发的话进度条在首句回复后就直接走完了，打工的感觉就没了。
        if now - last_progress_sent >= BACKGROUND_PROGRESS_INTERVAL_SECONDS:
            last_progress_sent = now
            await send_json(websocket, {
                "type": "system",
                "payload": {
                    "action": "background_task_progress",
                    "status": "working",
                    "turn_id": state.turn_id,
                    "task_id": task_id,
                    "progress": background_work_progress(now - started, BACKGROUND_POLL_TIMEOUT_SECONDS),
                    "elapsed_seconds": int(now - started),
                },
            })
        result = await asyncio.to_thread(fetch_background_task_result, config, task_id)
        if str(result.get("status") or "") == "pending":
            continue
        if result.get("ok") and str(result.get("body") or "").strip():
            duration_seconds = time.monotonic() - started
            beans = background_work_beans(duration_seconds)
            await send_dialogue_and_tts(
                websocket,
                runtime_config,
                state,
                f"后台任务完成：{str(result.get('body') or '').strip()}",
                ok=True,
                deferred=True,
                request_id=task_id,
                coins_earned=beans,
            )
            # 打工结算卡片：跑得越久挣得越多，也越累（体力写回服务端状态）。
            energy_cost = await asyncio.to_thread(companion_apply_work_energy_cost, duration_seconds)
            await send_json(websocket, {
                "type": "companion_settlement",
                "payload": {
                    "beans_delta": beans,
                    "energy_delta": -energy_cost,
                    "mood_delta": 0,
                    "duration_seconds": int(duration_seconds),
                    "turn_id": state.turn_id,
                    "task_id": task_id,
                },
            })
            # 结算后刷新设备上的体力数值。
            start_status_update_task(websocket, runtime_config)
            return
        # 失败也要说出来，否则用户只听到“先处理”就没下文了。
        await send_dialogue_and_tts(
            websocket,
            runtime_config,
            state,
            "刚才那个任务我没跑成，稍后你可以再叫我试一次。",
            ok=False,
            deferred=True,
            request_id=task_id,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "background_task_failed",
                "status": "failed",
                "turn_id": state.turn_id,
                "task_id": task_id,
            },
        })
        return
    await send_dialogue_and_tts(
        websocket,
        runtime_config,
        state,
        "那个任务还没跑完，我拿到结果再告诉你。",
        ok=False,
        deferred=True,
        request_id=task_id,
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "background_task_timeout",
            "status": "pending",
            "turn_id": state.turn_id,
            "task_id": task_id,
        },
    })


def load_runtime_config_for_gateway() -> Any:
    if not load_aura_runtime_config:
        return None
    persona_home = ""
    if load_persona_config:
        try:
            persona_home = load_persona_config().persona_home
        except Exception:
            persona_home = ""
    return load_aura_runtime_config(persona_home=persona_home)


# ---------------------------------------------------------------------------
# 语音闹钟/定时提醒调度（v1：网关进程内存，重启即清空）。
# persona 侧只负责把"11点10分提醒我"解析成 evidence["reminder"]，
# 真正到点播报靠这里：网关握着设备 WS 连接，能主动推 dialogue+TTS。
# ---------------------------------------------------------------------------

# 设备端只有一台，v1 用单槽记录"当前活跃连接"；到点时用最新的连接播报。
_ACTIVE_DEVICE_CONNECTION: dict[str, Any] = {}
_SCHEDULED_REMINDERS: dict[str, asyncio.Task] = {}
_SCHEDULED_REMINDER_INFO: dict[str, dict[str, Any]] = {}
REMINDER_LATE_GRACE_SECONDS = 600.0  # 到点时设备掉线，最多等 10 分钟补播。
REMINDER_RETRY_INTERVAL_SECONDS = 5.0


def register_active_device_connection(websocket: Any, state: TurnState) -> None:
    _ACTIVE_DEVICE_CONNECTION["websocket"] = websocket
    _ACTIVE_DEVICE_CONNECTION["state"] = state


def unregister_active_device_connection(websocket: Any) -> None:
    if _ACTIVE_DEVICE_CONNECTION.get("websocket") is websocket:
        _ACTIVE_DEVICE_CONNECTION.clear()


def extract_reminder_payload(
    evidence: dict[str, Any] | None,
    voice_turn_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    for source in (
        (evidence or {}).get("reminder"),
        ((voice_turn_payload or {}).get("debug") or {}).get("reminder")
        if isinstance((voice_turn_payload or {}).get("debug"), dict)
        else None,
    ):
        if isinstance(source, dict) and source:
            return source
    return None


def maybe_schedule_voice_reminder(
    evidence: dict[str, Any] | None,
    voice_turn_payload: dict[str, Any] | None,
) -> None:
    reminder = extract_reminder_payload(evidence, voice_turn_payload)
    if not reminder:
        return
    if reminder.get("cancel_all"):
        cancelled = list(_SCHEDULED_REMINDERS.keys())
        for task in _SCHEDULED_REMINDERS.values():
            task.cancel()
        _SCHEDULED_REMINDERS.clear()
        _SCHEDULED_REMINDER_INFO.clear()
        log_gateway("reminder_cancel_all", cancelled=len(cancelled), reminder_ids=",".join(cancelled))
        return
    reminder_id = str(reminder.get("reminder_id") or "").strip()
    fire_at = float(reminder.get("fire_at_epoch") or 0)
    announce = str(reminder.get("announce_text") or "").strip()
    if not reminder_id or not announce or fire_at <= time.time():
        return
    if reminder_id in _SCHEDULED_REMINDERS:
        return
    task = asyncio.create_task(_fire_voice_reminder(reminder_id, fire_at, announce))
    _SCHEDULED_REMINDERS[reminder_id] = task
    _SCHEDULED_REMINDER_INFO[reminder_id] = {
        "fire_at": fire_at,
        "label": str(reminder.get("label") or announce).strip(),
    }
    log_gateway(
        "reminder_scheduled",
        reminder_id=reminder_id,
        kind=str(reminder.get("kind") or ""),
        fire_at_iso=str(reminder.get("fire_at_iso") or ""),
        delay_s=int(fire_at - time.time()),
        label=str(reminder.get("label") or ""),
        pending=len(_SCHEDULED_REMINDERS),
    )


async def _fire_voice_reminder(reminder_id: str, fire_at: float, announce: str) -> None:
    try:
        delay = fire_at - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        deadline = time.time() + REMINDER_LATE_GRACE_SECONDS
        while time.time() < deadline:
            websocket = _ACTIVE_DEVICE_CONNECTION.get("websocket")
            state = _ACTIVE_DEVICE_CONNECTION.get("state")
            if websocket is None or state is None:
                await asyncio.sleep(REMINDER_RETRY_INTERVAL_SECONDS)
                continue
            # 正在处理别的语音轮次时稍等，避免播报和回答混在一起。
            wait_until = time.monotonic() + 30
            while (
                state.processing_task is not None
                and not state.processing_task.done()
                and time.monotonic() < wait_until
            ):
                await asyncio.sleep(0.5)
            try:
                runtime_config = load_runtime_config_for_gateway()
                await send_dialogue_and_tts(
                    websocket,
                    runtime_config,
                    state,
                    announce,
                    ok=True,
                    request_id=reminder_id,
                )
                log_gateway("reminder_fired", reminder_id=reminder_id, late_s=int(time.time() - fire_at))
                return
            except Exception as exc:  # 连接可能刚好断了，等重连后重试。
                log_gateway("reminder_send_retry", reminder_id=reminder_id, error=str(exc)[:200])
                await asyncio.sleep(REMINDER_RETRY_INTERVAL_SECONDS)
        log_gateway("reminder_dropped", reminder_id=reminder_id, reason="device_offline_too_long")
    except asyncio.CancelledError:
        raise
    finally:
        _SCHEDULED_REMINDERS.pop(reminder_id, None)
        _SCHEDULED_REMINDER_INFO.pop(reminder_id, None)


def pending_info_board_notes(*, now: float | None = None) -> list[str]:
    """Return the next three live reminders for the device idle board."""

    current = time.time() if now is None else float(now)
    rows: list[tuple[float, str]] = []
    for reminder in _SCHEDULED_REMINDER_INFO.values():
        try:
            fire_at = float(reminder.get("fire_at") or 0)
        except (TypeError, ValueError):
            continue
        label = str(reminder.get("label") or "").strip()
        if fire_at > current and label:
            rows.append((fire_at, label))
    rows.sort(key=lambda row: row[0])
    result: list[str] = []
    for fire_at, label in rows[:3]:
        moment = time.localtime(fire_at)
        result.append(f"{moment.tm_mon:02d}/{moment.tm_mday:02d} {moment.tm_hour:02d}:{moment.tm_min:02d} {label}")
    return result


async def send_status_update(websocket: Any, runtime_config: Any) -> None:
    runtime_config = await asyncio.to_thread(refresh_runtime_weather_for_gateway, runtime_config)
    await asyncio.to_thread(refresh_model_quota_cache, runtime_config)
    await asyncio.to_thread(refresh_hermes_quota_cache)
    payload = status_update_payload(runtime_config)
    if payload:
        await send_json(websocket, {"type": "status_update", "payload": payload})


async def poll_world_status_updates(websocket: Any, runtime_config: Any) -> None:
    while True:
        await asyncio.sleep(WORLD_SYNC_POLL_INTERVAL_SECONDS)
        try:
            await send_status_update(websocket, runtime_config)
        except ConnectionClosed:
            return
        except Exception as exc:  # pragma: no cover - defensive async boundary
            log_gateway("world_status_update_failed", detail=exc.__class__.__name__)


def start_status_update_task(websocket: Any, runtime_config: Any) -> asyncio.Task[None]:
    async def runner() -> None:
        try:
            await send_status_update(websocket, runtime_config)
        except ConnectionClosed:
            return
        except Exception as exc:  # pragma: no cover - defensive async boundary
            log_gateway("status_update_failed", detail=exc.__class__.__name__)

    return asyncio.create_task(runner())


def refresh_runtime_weather_for_gateway(runtime_config: Any) -> Any:
    if not runtime_config or not refresh_cached_weather_if_needed:
        return runtime_config
    city = str(getattr(runtime_config, "cached_weather_city", "") or "").strip()
    if not city and load_persona_config:
        try:
            city = str(load_persona_config().aura_home_city or "").strip()
        except Exception:
            city = ""
    try:
        updated, _result = refresh_cached_weather_if_needed(runtime_config, city=city)
        return updated
    except Exception:
        return runtime_config


def companion_status_fields() -> dict[str, Any]:
    """从 companion.db 读取 Aura 数值状态（附带闲置恢复结算），供 status_update 下发。

    刻意不下发 beans/coins：豆子余额由设备本地记账（商店购买不回传服务端），
    下发会把用户刚花掉的豆子又“变”回来。
    """
    if not load_persona_config or not LilyPersonaStore or not apply_time_recovery:
        return {}
    try:
        persona_config = load_persona_config()
        store = LilyPersonaStore(persona_config.companion_db_path)
        scope = persona_config.scope
        state = store.get_or_create_state(scope)
        recovered = apply_time_recovery(state)
        if recovered is not state:
            store.save_state(scope, recovered)
            state = recovered
        affinity_xp = int(state.get("affinity_xp") or 0)
        return {
            "mood": int(state.get("mood") or 0),
            "energy": int(state.get("energy") or 0),
            "satiety": int(state.get("satiety") or 0),
            "affinity_xp": affinity_xp,
            "affinity_level": compute_affinity_level(affinity_xp),
        }
    except Exception as exc:
        log_gateway("companion_status_read_failed", detail=str(exc)[:200])
        return {}


def _model_quota_cache_key(runtime_config: Any) -> str:
    if runtime_config is None:
        return ""
    public_values = tuple(
        str(getattr(runtime_config, field, "") or "").strip()
        for field in ("aura_model_provider", "aura_model_model", "aura_model_base_url")
    )
    if not any(public_values):
        return ""
    public_key = "|".join(public_values)
    secret = str(getattr(runtime_config, "aura_model_api_key", "") or "").strip().encode("utf-8")
    return f"{public_key}|key:{hashlib.sha256(secret).hexdigest()[:16]}"


def refresh_model_quota_cache(runtime_config: Any) -> dict[str, Any]:
    """Refresh the configured Aura model quota without blocking the gateway loop."""
    key = _model_quota_cache_key(runtime_config)
    if not key or runtime_config is None:
        return {}
    now = time.monotonic()
    with _QUOTA_CACHE_LOCK:
        cached = _MODEL_QUOTA_CACHE.get(key)
        if cached and now - cached[0] < QUOTA_CACHE_TTL_SECONDS:
            return cached[1]
        result = query_quota(
            provider=str(getattr(runtime_config, "aura_model_provider", "") or ""),
            model=str(getattr(runtime_config, "aura_model_model", "") or ""),
            base_url=str(getattr(runtime_config, "aura_model_base_url", "") or ""),
            api_key=str(getattr(runtime_config, "aura_model_api_key", "") or ""),
        )
        _MODEL_QUOTA_CACHE[key] = (now, result)
        return result


def _hermes_quota_config() -> HermesLilyConfig:
    persona = load_persona_config() if load_persona_config else None
    hermes_home = str(getattr(persona, "hermes_home", "") or os.environ.get("HERMES_HOME", "")).strip()
    return HermesLilyConfig(hermes_home=hermes_home)


def refresh_hermes_quota_cache() -> dict[str, Any]:
    """Refresh Hermes' upstream quota without exposing its API key."""
    config = _hermes_quota_config()
    public = read_hermes_provider_config(config)
    provider = str(public.get("provider") or "").strip()
    model = str(public.get("model") or "").strip()
    base_url = str(public.get("base_url") or "").strip()
    if not any((provider, model, base_url)):
        return {}
    secret = read_hermes_provider_secret(config, "api_key")
    api_key = str(secret.get("value") or "") if secret.get("ok") else ""
    key = "|".join((provider, model, base_url, hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]))
    now = time.monotonic()
    with _QUOTA_CACHE_LOCK:
        cached = _HERMES_QUOTA_CACHE.get(key)
        if cached and now - cached[0] < QUOTA_CACHE_TTL_SECONDS:
            return cached[1]
        result = query_quota(provider=provider, model=model, base_url=base_url, api_key=api_key)
        _HERMES_QUOTA_CACHE[key] = (now, result)
        return result


def _dialogue_quota_snapshot(runtime_config: Any) -> dict[str, Any]:
    persona = load_persona_config() if load_persona_config else None
    home = str(getattr(persona, "persona_home", "") or os.environ.get("AURA_PERSONA_HOME", "")).strip()
    path = str(os.environ.get("AURA_DIALOGUE_QUOTA_DB") or "").strip()
    if not path and home:
        path = str(Path(home).expanduser() / "config" / "dialogue_quota.sqlite3")
    if not path:
        return {}
    limit = int(getattr(runtime_config, "dialogue_quota_limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)
    window = int(getattr(runtime_config, "dialogue_quota_window_seconds", DEFAULT_WINDOW_SECONDS) or DEFAULT_WINDOW_SECONDS)
    try:
        return DialogueQuota(path).snapshot(limit=limit, window_seconds=window)
    except Exception as exc:
        log_gateway("dialogue_quota_status_failed", detail=exc.__class__.__name__)
        return {}


def _quota_row(window: dict[str, Any] | None, *, label: str, prefix: str = "") -> dict[str, Any]:
    row = dict(window or {})
    row["name"] = label
    row["label"] = label
    if prefix:
        remaining = row.get("remaining")
        total = row.get("total")
        if remaining is not None and total is not None:
            row["display"] = f"{prefix} {float(remaining):g}/{float(total):g}"
        else:
            row["display"] = f"{prefix} --"
    return row


def _combined_device_quota_payload(
    runtime_config: Any,
    aura_result: dict[str, Any] | None,
    hermes_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    aura_payload = _device_quota_payload(aura_result)
    local = _dialogue_quota_snapshot(runtime_config)
    if not local or not local.get("ok"):
        return aura_payload

    local_remaining = float(local.get("remaining") or 0)
    local_limit = float(local.get("limit") or 0)
    local_row = _quota_row(
        {"remaining": local_remaining, "total": local_limit, "unit": "turns"},
        label="AURA",
        prefix="AURA",
    )
    local_percent = int(round(local_remaining / local_limit * 100.0)) if local_limit > 0 else 0
    hermes_provider = str((hermes_result or {}).get("provider") or "").lower()
    if "kimi" not in hermes_provider or not isinstance(hermes_result, dict) or not hermes_result.get("ok"):
        return {
            "provider": "aura",
            "provider_display": "Aura",
            "headline": "AURA",
            "headline_short": "AURA",
            "summary_percent": local_percent,
            "summary_display": f"AURA {int(local_remaining)}/{int(local_limit)}",
            "primary": local_row,
            "secondary": {},
        }
    kimi_windows = hermes_result.get("windows") if isinstance(hermes_result.get("windows"), list) else []
    kimi_primary = hermes_result.get("primary") if isinstance(hermes_result.get("primary"), dict) else (kimi_windows[0] if kimi_windows else None)
    if not kimi_primary:
        return {
            "provider": "aura",
            "provider_display": "Aura",
            "headline": "AURA",
            "headline_short": "AURA",
            "summary_percent": local_percent,
            "summary_display": f"AURA {int(local_remaining)}/{int(local_limit)}",
            "primary": local_row,
            "secondary": {},
        }
    kimi_percent = _quota_window_percent(kimi_primary)
    kimi_row = _quota_row(kimi_primary, label="KIMI")
    kimi_row["display"] = f"{_quota_window_label(kimi_primary)} {kimi_percent}%"
    return {
        "provider": "aura-hermes",
        "provider_display": "AURA",
        "headline": "AURA",
        "headline_short": "AURA",
        "summary_percent": min(local_percent, kimi_percent),
        "summary_display": f"AURA {int(local_remaining)}/{int(local_limit)}",
        "primary": local_row,
        "secondary": kimi_row,
    }


def _quota_window_percent(window: dict[str, Any] | None) -> int:
    if not isinstance(window, dict):
        return 0
    try:
        total = float(window.get("total"))
        remaining = float(window.get("remaining"))
        if total <= 0:
            return 0
        return max(0, min(100, int(round(remaining / total * 100.0))))
    except (TypeError, ValueError):
        return 0


def _quota_window_label(window: dict[str, Any] | None) -> str:
    name = str((window or {}).get("name") or "quota").lower()
    return {
        "five_hour": "5h",
        "5h": "5h",
        "weekly_limit": "week",
        "weekly": "week",
        "monthly": "month",
        "plan_credit": "Credit",
        "topup": "topup",
        "balance": "balance",
    }.get(name, name[:7] or "quota")


def _quota_window_display(window: dict[str, Any] | None) -> str:
    if not isinstance(window, dict):
        return "--"
    label = _quota_window_label(window)
    remaining = window.get("remaining")
    total = window.get("total")
    unit = str(window.get("unit") or "").strip()
    try:
        remaining_text = f"{float(remaining):.0f}" if float(remaining).is_integer() else f"{float(remaining):.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        remaining_text = "--"
    if total is not None:
        try:
            total_text = f"{float(total):.0f}" if float(total).is_integer() else f"{float(total):.2f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            total_text = "--"
        return f"{label} {remaining_text}/{total_text}{unit}"
    return f"{label} {remaining_text}{unit}"


def _device_quota_payload(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    windows = result.get("windows") if isinstance(result.get("windows"), list) else []
    primary = result.get("primary") if isinstance(result.get("primary"), dict) else (windows[0] if windows else None)
    secondary = result.get("secondary") if isinstance(result.get("secondary"), dict) else (windows[1] if len(windows) > 1 else None)
    if not primary:
        return None
    primary_percent = _quota_window_percent(primary)
    secondary_percent = _quota_window_percent(secondary)
    percentages = [primary_percent]
    if secondary:
        percentages.append(secondary_percent)
    summary_percent = min(percentages) if percentages else primary_percent
    return {
        "provider": str(result.get("provider") or ""),
        "provider_display": "StepFun" if str(result.get("provider") or "").lower() == "stepfun" else str(result.get("provider") or ""),
        "plan_display": "Step Plan" if result.get("plan") == "step_plan" else "",
        "headline": "Step Plan" if result.get("plan") == "step_plan" else str(result.get("provider") or "TOKEN"),
        "headline_short": "Step Plan" if result.get("plan") == "step_plan" else str(result.get("provider") or "TOKEN"),
        "summary_percent": summary_percent,
        "summary_display": f"{summary_percent}%" if summary_percent else "--",
        "primary": {
            "label": _quota_window_label(primary),
            "display": _quota_window_display(primary),
            "limit": primary.get("total"),
            "remaining": primary.get("remaining"),
        },
        "secondary": {
            "label": _quota_window_label(secondary),
            "display": _quota_window_display(secondary),
            "limit": secondary.get("total"),
            "remaining": secondary.get("remaining"),
        } if secondary else {},
    }


def _cached_device_quota_payload(runtime_config: Any) -> dict[str, Any] | None:
    key = _model_quota_cache_key(runtime_config)
    with _QUOTA_CACHE_LOCK:
        cached = _MODEL_QUOTA_CACHE.get(key)
    return _device_quota_payload(cached[1]) if cached else None


def _cached_hermes_quota_result() -> dict[str, Any] | None:
    with _QUOTA_CACHE_LOCK:
        if not _HERMES_QUOTA_CACHE:
            return None
        _timestamp, result = max(_HERMES_QUOTA_CACHE.values(), key=lambda item: item[0])
    return result


def companion_world_status_fields(runtime_config: Any, *, now: float | None = None) -> dict[str, Any]:
    if not load_persona_config or not LilyPersonaStore or not build_world_snapshot:
        return {}
    try:
        persona_config = load_persona_config()
        if not persona_config.world_model_enabled:
            return {}
        store = LilyPersonaStore(persona_config.companion_db_path)
        state = store.get_or_create_state(persona_config.scope)
        weather = (
            cached_weather_snapshot(runtime_config)
            if runtime_config is not None and cached_weather_snapshot
            else {}
        )
        snapshot = build_world_snapshot(
            config=persona_config,
            store=store,
            state=state,
            query_context={
                "intent": "world_tick",
                "subject_entity": "aura",
                "boundary": "current",
            },
            recent_messages=[],
            weather_snapshot=weather,
            now=now,
        )
        current = snapshot.get("current") if isinstance(snapshot.get("current"), dict) else {}
        location_key = str(current.get("location_key") or "").strip()
        slot_key = str(current.get("slot_key") or "").strip()
        if not location_key:
            return {}
        sleepwear = location_key == "home.bedroom" and slot_key in {"sleep", "night_settle"}
        return {
            "scene": scene_for_world_location(location_key),
            "world_activity": str(current.get("activity_label") or ""),
            "world_slot": slot_key,
            "outfit_mode": "sleepwear" if sleepwear else "daywear",
        }
    except Exception as exc:
        log_gateway("companion_world_read_failed", detail=str(exc)[:200])
        return {}


def companion_apply_work_energy_cost(duration_seconds: float) -> int:
    """打工消耗体力：约每分钟 1 点，2..15 封顶；写回 companion.db，返回实际扣减值。"""
    if not load_persona_config or not LilyPersonaStore:
        return 0
    cost = max(2, min(15, int(duration_seconds // 60)))
    try:
        persona_config = load_persona_config()
        store = LilyPersonaStore(persona_config.companion_db_path)
        scope = persona_config.scope
        state = store.get_or_create_state(scope)
        energy = int(state.get("energy") or 100)
        applied = min(cost, max(0, energy))
        state = dict(state)
        state["energy"] = max(0, energy - cost)
        store.save_state(scope, state)
        return applied
    except Exception as exc:
        log_gateway("companion_work_energy_failed", detail=str(exc)[:200])
        return 0


def status_update_payload(runtime_config: Any, *, now: float | None = None) -> dict[str, Any]:
    # Aura 数值状态（体力/心情/饱腹/好感）——设备主界面靠它刷新。
    payload: dict[str, Any] = dict(companion_status_fields())
    payload.update(companion_world_status_fields(runtime_config, now=now))
    payload["notes"] = pending_info_board_notes(now=now)
    with _QUOTA_CACHE_LOCK:
        aura_cached = _MODEL_QUOTA_CACHE.get(_model_quota_cache_key(runtime_config))
    aura_result = aura_cached[1] if aura_cached else None
    quota = _combined_device_quota_payload(runtime_config, aura_result, _cached_hermes_quota_result())
    if quota is None:
        quota = _cached_device_quota_payload(runtime_config)
    if quota:
        payload["quota"] = quota
    if not runtime_config or not cached_weather_snapshot:
        return payload
    weather = cached_weather_snapshot(runtime_config)
    if weather.get("status") != "fresh":
        return payload
    temperature = _temperature_number(weather.get("temperature"))
    if temperature is not None:
        payload["weather_temperature"] = temperature
    payload["weather_icon"] = int(weather.get("weather_icon") or 0)
    if weather.get("city"):
        payload["weather_city"] = str(weather.get("city"))
    if weather.get("condition"):
        payload["weather_condition"] = str(weather.get("condition"))
    if weather.get("humidity"):
        payload["weather_humidity"] = str(weather.get("humidity"))
    if weather.get("source"):
        payload["weather_source"] = str(weather.get("source"))
    if weather.get("observed_at"):
        payload["weather_observed_at"] = str(weather.get("observed_at"))
    return payload


def _temperature_number(value: Any) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def asr_failure_reply(asr: AsrResult, *, language: str = "zh") -> str:
    status = str(asr.status or "").strip()
    if status in {"asr_disabled", "asr_base_url_missing"}:
        return localized_gateway_text(
            language,
            zh="我还没听清楚。语音识别还没配置好，去后台设置 ASR 后再试一次。",
            en="I couldn't understand that. Speech recognition isn't configured yet, so please set up ASR and try again.",
            ja="聞き取れませんでした。音声認識がまだ設定されていないので、ASRを設定してからもう一度試してください。",
        )
    if status in {"asr_http_error", "asr_api_failed"}:
        return localized_gateway_text(
            language,
            zh="我刚才没连上语音识别服务，稍等一下再说一遍好吗？",
            en="I couldn't reach the speech recognition service. Please try saying that again in a moment.",
            ja="音声認識サービスに接続できませんでした。少し待ってから、もう一度話してください。",
        )
    if status == "empty_audio":
        return localized_gateway_text(
            language,
            zh="我这边没有收到声音，靠近一点再说一次试试。",
            en="I didn't receive any audio. Please move a little closer and try again.",
            ja="音声が届きませんでした。もう少し近づいて、もう一度話してみてください。",
        )
    return localized_gateway_text(
        language,
        zh="我还没听清楚，再说一遍好吗？",
        en="I didn't quite catch that. Could you say it again?",
        ja="よく聞き取れませんでした。もう一度言ってもらえますか？",
    )


def transcribe_turn_audio(runtime_config: Any, state: TurnState) -> AsrResult:
    if not runtime_config or not getattr(runtime_config, "asr_enabled", False):
        return AsrResult(ok=False, status="asr_disabled", detail="ASR 未启用")
    if not state.audio_chunks:
        return AsrResult(ok=False, status="empty_audio", detail="没有收到音频")
    decode_started = time.monotonic()
    try:
        close_state_vad_decoder(state)
        pcm = decode_turn_audio(state)
        state.asr_decode_ms = int((time.monotonic() - decode_started) * 1000)
        stats = pcm16_mono_stats(pcm)
        state.asr_pcm_ms = int(stats.get("decoded_pcm_ms") or 0)
        state.asr_pcm_rms = int(stats.get("rms") or 0)
        state.asr_pcm_peak = int(stats.get("peak") or 0)
        state.asr_pcm_clipping_ratio_x10000 = int(stats.get("clipping_ratio_x10000") or 0)
        log_gateway(
            "asr_input_stats",
            turn_id=state.turn_id,
            audio_format=state.audio_format,
            frame_duration_ms=state.frame_duration_ms,
            packet_count=state.audio_packet_count,
            audio_bytes=state.audio_bytes,
            **audio_receive_payload(state),
            asr_decode_ms=state.asr_decode_ms,
            **stats,
        )
        wav_bytes = pcm_to_wav_bytes(pcm, sample_rate=DEVICE_SAMPLE_RATE)
        state.asr_wav_bytes = len(wav_bytes)
    except Exception as exc:
        state.asr_decode_ms = int((time.monotonic() - decode_started) * 1000)
        log_gateway(
            "asr_audio_decode_failed",
            turn_id=state.turn_id,
            detail=exc.__class__.__name__,
            asr_decode_ms=state.asr_decode_ms,
            audio_format=state.audio_format,
            frame_duration_ms=state.frame_duration_ms,
            packet_count=state.audio_packet_count,
            audio_bytes=state.audio_bytes,
        )
        return AsrResult(ok=False, status="audio_decode_failed", detail=exc.__class__.__name__)
    backend_started = time.monotonic()
    if str(getattr(runtime_config, "asr_mode", "local")) == "api":
        result = transcribe_with_api(runtime_config, wav_bytes)
    else:
        result = transcribe_with_local_command(runtime_config, wav_bytes)
    state.asr_backend_ms = int((time.monotonic() - backend_started) * 1000)
    log_gateway(
        "asr_backend_result",
        turn_id=state.turn_id,
        ok=result.ok,
        status=result.status,
        provider=str(getattr(runtime_config, "asr_provider", "") or ""),
        model=str(getattr(runtime_config, "asr_model", "") or ""),
        mode=str(getattr(runtime_config, "asr_mode", "") or ""),
        asr_backend_ms=state.asr_backend_ms,
        asr_wav_bytes=state.asr_wav_bytes,
        text_chars=len(result.text or ""),
        transcript_preview=_transcript_preview(result.text),
    )
    return result


def decode_turn_audio(state: TurnState) -> bytes:
    audio_format = str(state.audio_format or "").strip().lower()
    if audio_format == "opus":
        decoder = OpusPacketDecoder(sample_rate=state.sample_rate or DEVICE_SAMPLE_RATE)
        return decoder.decode_packets(state.audio_chunks, frame_duration_ms=state.frame_duration_ms or 60)
    if audio_format in {"pcm", "s16le", "raw"}:
        pcm = b"".join(state.audio_chunks)
        if state.sample_rate != DEVICE_SAMPLE_RATE:
            return resample_pcm16_mono(pcm, source_rate=state.sample_rate, target_rate=DEVICE_SAMPLE_RATE)
        return pcm
    raise ValueError(f"unsupported audio format: {audio_format}")


class OpusPacketDecoder:
    def __init__(self, *, sample_rate: int, channels: int = 1) -> None:
        lib_name = ctypes.util.find_library("opus") or os.environ.get("AURA_LIBOPUS_PATH") or "libopus.so.0"
        self.lib = ctypes.CDLL(lib_name)
        self.sample_rate = int(sample_rate or DEVICE_SAMPLE_RATE)
        self.channels = int(channels or 1)
        err = ctypes.c_int()
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.decoder = self.lib.opus_decoder_create(self.sample_rate, self.channels, ctypes.byref(err))
        if not self.decoder or err.value != 0:
            raise RuntimeError(f"opus decoder init failed: {err.value}")

    def close(self) -> None:
        if self.decoder:
            self.lib.opus_decoder_destroy(self.decoder)
            self.decoder = None

    def decode_packets(self, packets: list[bytes], *, frame_duration_ms: int) -> bytes:
        chunks: list[bytes] = []
        try:
            for packet in packets:
                decoded = self.decode_packet(packet, frame_duration_ms=frame_duration_ms)
                if decoded:
                    chunks.append(decoded)
        finally:
            self.close()
        pcm = b"".join(chunks)
        if self.sample_rate != DEVICE_SAMPLE_RATE:
            pcm = resample_pcm16_mono(pcm, source_rate=self.sample_rate, target_rate=DEVICE_SAMPLE_RATE)
        return pcm

    def decode_packet(self, packet: bytes, *, frame_duration_ms: int) -> bytes:
        if not packet:
            return b""
        if not self.decoder:
            raise RuntimeError("opus decoder is closed")
        frame_size = max(120, int(self.sample_rate * max(1, frame_duration_ms) / 1000))
        out_type = ctypes.c_int16 * (frame_size * self.channels)
        in_buf = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        out_buf = out_type()
        samples = self.lib.opus_decode(
            self.decoder,
            in_buf,
            len(packet),
            out_buf,
            frame_size,
            0,
        )
        if samples < 0:
            raise RuntimeError(f"opus decode failed: {samples}")
        pcm = ctypes.string_at(out_buf, samples * self.channels * DEVICE_SAMPLE_WIDTH)
        if self.sample_rate != DEVICE_SAMPLE_RATE:
            pcm = resample_pcm16_mono(pcm, source_rate=self.sample_rate, target_rate=DEVICE_SAMPLE_RATE)
        return pcm


def streaming_asr_available(runtime_config: Any) -> bool:
    return streaming_asr_unavailable_reason(runtime_config) == ""


def streaming_asr_unavailable_reason(runtime_config: Any) -> str:
    if not STEPFUN_WS_ASR_ENABLED:
        return "disabled_by_env"
    if not runtime_config or not getattr(runtime_config, "asr_enabled", False):
        return "asr_disabled"
    if str(getattr(runtime_config, "asr_mode", "") or "").strip().lower() != "api":
        return "asr_mode_not_api"
    if str(getattr(runtime_config, "asr_provider", "") or "").strip().lower() != "stepfun":
        return "provider_not_stepfun"
    if not str(getattr(runtime_config, "asr_api_key", "") or "").strip():
        return "api_key_missing"
    model = str(getattr(runtime_config, "asr_model", "") or "").strip()
    if model and "stream" not in model.lower():
        return "model_not_streaming"
    if not stepfun_ws_asr_url(getattr(runtime_config, "asr_base_url", "")):
        return "streaming_url_unavailable"
    return ""


def stepfun_step_plan_realtime_available(runtime_config: Any) -> bool:
    if not STEPFUN_REALTIME_DIRECT_REPLY_ENABLED:
        return False
    if not STEPFUN_WS_ASR_ENABLED:
        return False
    if not runtime_config or not getattr(runtime_config, "asr_enabled", False):
        return False
    if str(getattr(runtime_config, "asr_mode", "") or "").strip().lower() != "api":
        return False
    provider = str(getattr(runtime_config, "asr_provider", "") or "").strip().lower()
    if provider not in {"stepfun-realtime", "stepfun_realtime"}:
        return False
    if not str(getattr(runtime_config, "asr_api_key", "") or "").strip():
        return False
    model = str(getattr(runtime_config, "asr_model", "") or "").strip().lower()
    if "realtime" not in model:
        return False
    return bool(stepfun_step_plan_realtime_url(getattr(runtime_config, "asr_base_url", ""), model=model))


def runtime_voice_route_payload(runtime_config: Any) -> dict[str, Any]:
    if runtime_config is None:
        return {
            "runtime_configured": False,
            "server_vad_global": bool(SERVER_VAD_ENABLED),
        }
    return {
        "runtime_configured": True,
        "aura_model_mode": str(getattr(runtime_config, "aura_model_mode", "") or ""),
        "aura_model_provider": str(getattr(runtime_config, "aura_model_provider", "") or ""),
        "aura_model_model": str(getattr(runtime_config, "aura_model_model", "") or ""),
        "tts_enabled": bool(getattr(runtime_config, "tts_enabled", False)),
        "tts_provider": str(getattr(runtime_config, "tts_provider", "") or ""),
        "tts_model": str(getattr(runtime_config, "tts_model", "") or ""),
        "asr_enabled": bool(getattr(runtime_config, "asr_enabled", False)),
        "asr_mode": str(getattr(runtime_config, "asr_mode", "") or ""),
        "asr_provider": str(getattr(runtime_config, "asr_provider", "") or ""),
        "asr_model": str(getattr(runtime_config, "asr_model", "") or ""),
        "asr_streaming_available": streaming_asr_available(runtime_config),
        "asr_streaming_reason": streaming_asr_unavailable_reason(runtime_config),
        "stepfun_realtime_available": stepfun_step_plan_realtime_available(runtime_config),
        "stepfun_ws_asr_enabled": bool(STEPFUN_WS_ASR_ENABLED),
        "stepfun_ws_asr_partial_as_final": bool(STEPFUN_WS_ASR_USE_PARTIAL_AS_FINAL),
        "stepfun_ws_asr_early_turn": bool(STEPFUN_WS_ASR_EARLY_TURN_ENABLED),
        "server_vad_global": bool(SERVER_VAD_ENABLED),
    }


def stepfun_step_plan_realtime_url(base_url: str, *, model: str) -> str:
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        return ""
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/realtime"):
        endpoint_path = path
    elif not path:
        endpoint_path = "/step_plan/v1/realtime"
    elif path.endswith("/step_plan/v1"):
        endpoint_path = f"{path}/realtime"
    else:
        endpoint_path = f"{path}/realtime"
    query = dict(_query_items(parsed.query))
    query["model"] = str(model or "").strip() or "stepaudio-2.5-realtime"
    return urlunsplit((scheme, parsed.netloc, endpoint_path, urlencode(query), ""))


def stepfun_ws_asr_url(base_url: str) -> str:
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        return ""
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/audio/asr/sse"):
        path = path[: -len("/audio/asr/sse")]
    if path.endswith("/audio/asr"):
        path = path[: -len("/audio/asr")]
    if path.endswith("/realtime/asr/stream"):
        endpoint_path = path
    elif not path:
        endpoint_path = "/v1/realtime/asr/stream"
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/realtime/asr/stream"
    else:
        endpoint_path = f"{path}/realtime/asr/stream"
    return urlunsplit((scheme, parsed.netloc, endpoint_path, parsed.query, ""))


async def maybe_start_streaming_asr_session(state: TurnState, runtime_config: Any, *, config: GatewayConfig | None = None) -> None:
    if not streaming_asr_available(runtime_config):
        state.streaming_asr_session = None
        return
    session = StepfunWsAsrSession(runtime_config, state, config=config)
    try:
        await session.start()
    except Exception as exc:
        log_gateway(
            "streaming_asr_start_failed",
            turn_id=state.turn_id,
            detail=exc.__class__.__name__,
        )
        state.streaming_asr_session = None
        await session.close()
        return
    state.streaming_asr_session = session
    state.streaming_asr_started_at = session.started
    log_gateway("streaming_asr_started", turn_id=state.turn_id, url=session.ws_url)


async def maybe_start_stepfun_step_plan_realtime_session(
    websocket: Any,
    config: GatewayConfig,
    state: TurnState,
    runtime_config: Any,
) -> None:
    if not stepfun_step_plan_realtime_available(runtime_config):
        state.stepfun_realtime_session = None
        state.stepfun_realtime_enabled = False
        return
    session = StepfunStepPlanRealtimeSession(websocket, config, runtime_config, state)
    try:
        await session.start()
    except Exception as exc:
        log_gateway(
            "stepfun_realtime_start_failed",
            turn_id=state.turn_id,
            detail=exc.__class__.__name__,
        )
        state.stepfun_realtime_session = None
        state.stepfun_realtime_enabled = False
        await session.close()
        return
    state.stepfun_realtime_session = session
    state.stepfun_realtime_enabled = True
    state.stepfun_realtime_started_at = session.started
    log_gateway("stepfun_realtime_started", turn_id=state.turn_id, url=session.ws_url)


async def forward_stepfun_step_plan_realtime_packet(state: TurnState, packet: bytes) -> None:
    session = state.stepfun_realtime_session
    if session is None:
        return
    try:
        pcm = streaming_asr_pcm_from_packet(state, packet)
        if pcm:
            await session.send_pcm(pcm)
            state.stepfun_realtime_audio_bytes += len(pcm)
            state.stepfun_realtime_forwarded_frames += 1
    except Exception as exc:
        log_gateway("stepfun_realtime_forward_failed", turn_id=state.turn_id, detail=exc.__class__.__name__)
        await close_stepfun_step_plan_realtime_session(state)


async def run_stepfun_step_plan_realtime_turn(websocket: Any, state: TurnState, *, reason: str = "client_stop") -> None:
    session = state.stepfun_realtime_session
    if session is None:
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "stepfun_realtime_failed",
                "status": "failed",
                "turn_id": state.turn_id,
                "detail": "StepFun Realtime session is unavailable",
            },
        })
        return
    state.stepfun_realtime_triggered = True
    mark_turn_trigger(state, reason)
    state.stepfun_realtime_session = None
    state.processing_started_at = time.monotonic()
    await send_json(websocket, {
        "type": "status",
        "text": "正在实时生成语音回复",
        "payload": {"turn_id": state.turn_id, "provider_stream": "stepfun_realtime", "reason": reason},
    })
    result = await session.finish(reason=reason)
    state.stepfun_realtime_first_audio_ms = result.first_audio_ms
    state.stepfun_realtime_first_audio_after_response_ms = result.first_chunk_ms
    state.stepfun_realtime_total_ms = result.latency_ms
    state.stepfun_realtime_text = result.detail
    ok = bool(result.ok)
    if not ok:
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "stepfun_realtime_failed",
                "status": "failed",
                "turn_id": state.turn_id,
                "detail": result.detail,
            },
        })
        await send_tts_binary(websocket, state.turn_id, b"", stream_id=1, is_final=True)
        return
    spoken_detail = device_spoken_text(result.detail)
    tts_gap_payload = tts_chunk_timing_payload(result)
    await send_json(websocket, {
        "type": "dialogue",
        "payload": {
            "turn_id": state.turn_id,
            "request_id": "",
            "text": spoken_detail,
            "segments": dialogue_segments(spoken_detail),
            "pose": None,
            "scene": "home.living_room",
            "coins_earned": 0,
            "deferred": False,
            "continue_listening": continue_listening_for_device(state),
            "timing": {
                "provider_stream": "stepfun_realtime",
                **turn_trigger_payload(state),
                "first_audio_ms": result.first_audio_ms,
                "first_audio_after_response_ms": result.first_chunk_ms,
                "total_ms": result.latency_ms,
                "audio_bytes": result.audio_bytes,
                "audio_chunk_count": result.audio_chunk_count,
            },
        },
    })
    # 实时链路没有 runtime_config，也照样把数值状态推下去（无天气字段）。
    start_status_update_task(websocket, None)
    log_gateway(
        "turn_audio_timing",
        turn_id=state.turn_id,
        status="ok",
        provider_stream="stepfun_realtime",
        **turn_trigger_payload(state),
        realtime_first_audio_ms=result.first_audio_ms,
        realtime_first_audio_after_response_ms=result.first_chunk_ms,
        realtime_total_ms=result.latency_ms,
        realtime_audio_bytes=result.audio_bytes,
        realtime_audio_chunks=result.audio_chunk_count,
        realtime_audio_chunk_gap_count=result.audio_chunk_gap_count,
        realtime_audio_chunk_gap_p50_ms=result.audio_chunk_gap_p50_ms,
        realtime_audio_chunk_gap_p95_ms=result.audio_chunk_gap_p95_ms,
        realtime_audio_chunk_gap_max_ms=result.audio_chunk_gap_max_ms,
        realtime_audio_chunk_stall_count=result.audio_chunk_stall_count,
        **tts_gap_payload,
        realtime_text_chars=len(result.detail or ""),
        streaming_asr_audio_bytes=state.stepfun_realtime_audio_bytes,
        streaming_asr_forwarded_frames=state.stepfun_realtime_forwarded_frames,
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "turn_audio_timing",
            "status": "ok",
            "turn_id": state.turn_id,
            "provider_stream": "stepfun_realtime",
            **turn_trigger_payload(state),
            "realtime_first_audio_ms": result.first_audio_ms,
            "realtime_first_audio_after_response_ms": result.first_chunk_ms,
            "realtime_total_ms": result.latency_ms,
            "realtime_audio_bytes": result.audio_bytes,
            "realtime_audio_chunks": result.audio_chunk_count,
            "realtime_audio_chunk_gap_count": result.audio_chunk_gap_count,
            "realtime_audio_chunk_gap_p50_ms": result.audio_chunk_gap_p50_ms,
            "realtime_audio_chunk_gap_p95_ms": result.audio_chunk_gap_p95_ms,
            "realtime_audio_chunk_gap_max_ms": result.audio_chunk_gap_max_ms,
            "realtime_audio_chunk_stall_count": result.audio_chunk_stall_count,
            **tts_gap_payload,
            "streaming_asr_audio_bytes": state.stepfun_realtime_audio_bytes,
            "streaming_asr_forwarded_frames": state.stepfun_realtime_forwarded_frames,
        },
    })


async def forward_streaming_asr_packet(state: TurnState, packet: bytes) -> None:
    session = state.streaming_asr_session
    if session is None:
        return
    try:
        pcm = streaming_asr_pcm_from_packet(state, packet)
        if pcm:
            await session.send_pcm(pcm)
            state.streaming_asr_audio_bytes += len(pcm)
            state.streaming_asr_forwarded_frames += 1
    except Exception as exc:
        log_gateway("streaming_asr_forward_failed", turn_id=state.turn_id, detail=exc.__class__.__name__)
        await close_streaming_asr_session(state)


async def finalize_streaming_asr_session(state: TurnState, runtime_config: Any) -> AsrResult:
    session = state.streaming_asr_session
    if session is None:
        return AsrResult(ok=False, status="streaming_asr_unavailable")
    try:
        result = await session.finish()
    finally:
        state.streaming_asr_session = None
    return result


async def close_streaming_asr_session(state: TurnState) -> None:
    monitor = state.streaming_asr_monitor_task
    state.streaming_asr_monitor_task = None
    current = asyncio.current_task()
    if monitor is not None and monitor is not current and not monitor.done():
        monitor.cancel()
    await cancel_streaming_asr_prefetch(state)
    session = state.streaming_asr_session
    state.streaming_asr_session = None
    if session is not None:
        await session.close()
    decoder = getattr(state, "streaming_asr_opus_decoder", None)
    if decoder is not None:
        try:
            decoder.close()
        except Exception:
            pass
    state.streaming_asr_opus_decoder = None


async def close_stepfun_step_plan_realtime_session(state: TurnState) -> None:
    monitor = state.stepfun_realtime_monitor_task
    state.stepfun_realtime_monitor_task = None
    current = asyncio.current_task()
    if monitor is not None and monitor is not current and not monitor.done():
        monitor.cancel()
    session = state.stepfun_realtime_session
    state.stepfun_realtime_session = None
    if session is not None:
        await session.close()


async def maybe_start_stepfun_tts_warm_session(
    websocket: Any,
    state: TurnState,
    runtime_config: Any,
    *,
    reason: str = "start",
) -> None:
    if not STEPFUN_WS_TTS_WARM_ENABLED:
        await close_stepfun_tts_warm_session(state)
        return
    if not stepfun_ws_tts_available(runtime_config):
        await close_stepfun_tts_warm_session(state)
        return
    cooldown_ms = _stepfun_ws_tts_cooldown_remaining_ms()
    if cooldown_ms:
        await close_stepfun_tts_warm_session(state)
        log_gateway(
            "stepfun_ws_tts_warm_skip",
            turn_id=state.turn_id,
            reason="cooldown",
            cooldown_ms=cooldown_ms,
        )
        return
    if state.stepfun_tts_warm_task is not None:
        if not state.stepfun_tts_warm_task.done():
            log_gateway("stepfun_ws_tts_warm_reuse", turn_id=state.turn_id, reason=reason, status="pending")
            return
        try:
            session = state.stepfun_tts_warm_task.result()
        except Exception as exc:
            log_gateway(
                "stepfun_ws_tts_warm_discard",
                turn_id=state.turn_id,
                reason=reason,
                status="failed",
                detail=exc.__class__.__name__,
            )
            state.stepfun_tts_warm_task = None
            state.stepfun_tts_warm_started_at = 0.0
        else:
            if _stepfun_ws_tts_session_is_healthy(session):
                log_gateway("stepfun_ws_tts_warm_reuse", turn_id=state.turn_id, reason=reason, status="ready")
                return
            log_gateway(
                "stepfun_ws_tts_warm_discard",
                turn_id=state.turn_id,
                reason=reason,
                status="unhealthy",
                detail=session.error_detail or "receiver_stopped",
            )
            state.stepfun_tts_warm_task = None
            state.stepfun_tts_warm_started_at = 0.0
            await session.close()
    state.stepfun_tts_warm_started_at = time.monotonic()
    state.stepfun_tts_warm_task = asyncio.create_task(start_stepfun_ws_tts_session(
        websocket,
        runtime_config,
        state.turn_id,
        stream_id=1,
        started=state.stepfun_tts_warm_started_at,
        warm=True,
    ))
    log_gateway("stepfun_ws_tts_warm_start", turn_id=state.turn_id, reason=reason)


async def close_stepfun_tts_warm_session(state: TurnState) -> None:
    task = state.stepfun_tts_warm_task
    state.stepfun_tts_warm_task = None
    state.stepfun_tts_warm_started_at = 0.0
    if task is None:
        return
    if task.done():
        try:
            session = task.result()
        except Exception:
            return
        await session.close()
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


def streaming_asr_pcm_from_packet(state: TurnState, packet: bytes) -> bytes:
    audio_format = str(state.audio_format or "").strip().lower()
    if audio_format in {"pcm", "s16le", "raw"}:
        pcm = bytes(packet or b"")
        if state.sample_rate != DEVICE_SAMPLE_RATE:
            return resample_pcm16_mono(pcm, source_rate=state.sample_rate, target_rate=DEVICE_SAMPLE_RATE)
        return pcm
    if audio_format == "opus":
        if state.streaming_asr_opus_decoder is None:
            state.streaming_asr_opus_decoder = OpusPacketDecoder(sample_rate=state.sample_rate or DEVICE_SAMPLE_RATE)
        return state.streaming_asr_opus_decoder.decode_packet(packet, frame_duration_ms=state.frame_duration_ms or 60)
    return b""


class StepfunStepPlanRealtimeSession:
    def __init__(self, websocket: Any, config: GatewayConfig, runtime_config: Any, state: TurnState) -> None:
        self.websocket = websocket
        self.config = config
        self.runtime_config = runtime_config
        self.state = state
        self.model = str(getattr(runtime_config, "asr_model", "") or "").strip() or "stepaudio-2.5-realtime"
        self.api_key = str(getattr(runtime_config, "asr_api_key", "") or "").strip()
        self.ws_url = stepfun_step_plan_realtime_url(getattr(runtime_config, "asr_base_url", ""), model=self.model)
        self.timeout = max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30))
        self._connect_cm: Any = None
        self._step_ws: Any = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._closed = False
        self._audio_committed = False
        self._response_created = False
        self.server_vad_enabled = bool(STEPFUN_WS_ASR_SERVER_VAD)
        self._stop_silence_sent = False
        self.started = time.monotonic()
        self.first_audio_ms = 0
        self.response_created_ms = 0
        self.first_audio_after_response_ms = 0
        self.audio_chunk_count = 0
        self.audio_bytes = 0
        self.audio_timing = AudioChunkTiming(self.started)
        self.text = ""
        self.error_detail = ""
        self.done_event = asyncio.Event()
        self.speech_stopped_event = asyncio.Event()

    async def start(self) -> "StepfunStepPlanRealtimeSession":
        if not self.ws_url:
            raise ValueError("StepFun Step Plan Realtime URL unavailable")
        if not self.api_key:
            raise ValueError("StepFun Step Plan Realtime API key missing")
        self._connect_cm = ws_connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            proxy=STEPFUN_WS_TTS_PROXY or None,
            open_timeout=min(10.0, self.timeout),
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )
        try:
            self._step_ws = await self._connect_cm.__aenter__()
            await self._send_session_update()
        except Exception:
            await self.close()
            raise
        self._receiver_task = asyncio.create_task(self._receive_loop())
        return self

    async def _send_session_update(self) -> None:
        voice = str(getattr(self.runtime_config, "tts_voice", "") or "").strip()
        payload = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": stepfun_realtime_instructions(self.runtime_config),
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": "server_vad",
                    "prefix_padding_ms": 500,
                    "silence_duration_ms": STEPFUN_WS_ASR_SILENCE_MS,
                    "energy_awakeness_threshold": int(STEPFUN_WS_ASR_THRESHOLD * 5000),
                } if self.server_vad_enabled else None,
            },
        }
        if voice:
            payload["session"]["voice"] = voice
        if payload["session"]["turn_detection"] is None:
            del payload["session"]["turn_detection"]
        await self._step_ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def send_pcm(self, pcm: bytes) -> None:
        if self._closed or not self._step_ws or not pcm:
            return
        event = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }
        await self._step_ws.send(json.dumps(event, separators=(",", ":")))

    async def finish(self, *, reason: str = "client_stop") -> TtsResult:
        try:
            await self._commit_and_create_response()
            if self._receiver_task:
                try:
                    await asyncio.wait_for(self.done_event.wait(), timeout=max(1.0, self.timeout))
                except asyncio.TimeoutError:
                    self.error_detail = self.error_detail or "StepFun Realtime timeout"
        finally:
            await self.close()
        return TtsResult(
            ok=bool(self.audio_bytes and not self.error_detail),
            audio=b"",
            detail=self.text.strip() or self.error_detail or ("StepFun Realtime audio reply" if self.audio_bytes else "empty audio"),
            chunk_count=max(1, self.audio_chunk_count),
            audio_chunk_count=self.audio_chunk_count,
            audio_bytes=self.audio_bytes,
            latency_ms=_elapsed_ms(self.started),
            first_audio_ms=self.first_audio_ms,
            first_chunk_ms=self.first_audio_after_response_ms,
            source_sample_rate=DEVICE_SAMPLE_RATE,
            device_sample_rate=DEVICE_SAMPLE_RATE,
            streamed=True,
            **audio_chunk_timing_summary(self.audio_timing),
        )

    async def _commit_and_create_response(self) -> None:
        if not self._step_ws or self._closed:
            return
        if self.server_vad_enabled and not self.speech_stopped_event.is_set():
            await self._append_stop_silence()
        if not self.server_vad_enabled and not self._audio_committed:
            await self._step_ws.send(json.dumps({"type": "input_audio_buffer.commit"}, separators=(",", ":")))
            self._audio_committed = True
        if not self._response_created:
            await self._step_ws.send(json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "instructions": stepfun_realtime_instructions(self.runtime_config),
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            self._response_created = True
            self.response_created_ms = _elapsed_ms(self.started)

    async def _append_stop_silence(self) -> None:
        if self._stop_silence_sent or not self._step_ws or STEPFUN_REALTIME_STOP_SILENCE_MS <= 0:
            return
        silence = b"\x00\x00" * int(DEVICE_SAMPLE_RATE * STEPFUN_REALTIME_STOP_SILENCE_MS / 1000)
        if silence:
            await self.send_pcm(silence)
            self._stop_silence_sent = True

    async def close(self) -> None:
        self._closed = True
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
        if self._connect_cm is not None:
            try:
                await self._connect_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._connect_cm = None
        self._step_ws = None

    async def _receive_loop(self) -> None:
        while True:
            raw_message = await asyncio.wait_for(self._step_ws.recv(), timeout=self.timeout)
            event = _json_object(raw_message)
            event_type = str(event.get("type") or event.get("event") or "").lower()
            if any(marker in event_type for marker in ("error", "failed")):
                data = event.get("data") if isinstance(event.get("data"), dict) else event
                self.error_detail = _stepfun_error_detail(data)
                self.done_event.set()
                return
            if event_type in {"input_audio_buffer.speech_stopped", "input_audio_buffer.speech.stop"}:
                self.speech_stopped_event.set()
                continue
            text = _stepfun_realtime_text_delta(event)
            if text and text not in self.text:
                self.text += text
            if "response.audio.delta" in event_type or event_type.endswith(".audio.delta"):
                await self._send_audio_delta(event)
                continue
            if "response.audio.done" in event_type or event_type in {
                "response.done",
                "response.completed",
                "response.output_item.done",
                "response.content_part.done",
            }:
                if not self.done_event.is_set():
                    await send_tts_binary(self.websocket, self.state.turn_id, b"", stream_id=1, is_final=True)
                self.done_event.set()
                return

    async def _send_audio_delta(self, event: dict[str, Any]) -> None:
        pcm = _stepfun_realtime_audio_delta(event)
        if not pcm:
            return
        await send_tts_pcm_stream(
            self.websocket,
            self.state.turn_id,
            pcm,
            stream_id=1,
            is_final=False,
            timing=self.audio_timing,
        )
        self.audio_bytes += len(pcm)
        self.audio_chunk_count += (len(pcm) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES
        if not self.first_audio_ms:
            self.first_audio_ms = _elapsed_ms(self.started)
            if self.response_created_ms:
                self.first_audio_after_response_ms = max(0, self.first_audio_ms - self.response_created_ms)


class StepfunWsAsrSession:
    def __init__(self, runtime_config: Any, state: TurnState, *, config: GatewayConfig | None = None) -> None:
        self.runtime_config = runtime_config
        self.state = state
        self.config = config
        self.model = str(getattr(runtime_config, "asr_model", "") or "").strip() or "stepaudio-2.5-asr-stream"
        self.api_key = str(getattr(runtime_config, "asr_api_key", "") or "").strip()
        self.ws_url = stepfun_ws_asr_url(getattr(runtime_config, "asr_base_url", ""))
        self.timeout = max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30))
        self._connect_cm: Any = None
        self._step_ws: Any = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=STEPFUN_WS_ASR_QUEUE_FRAMES)
        self._closed = False
        self.server_vad_enabled = bool(STEPFUN_WS_ASR_SERVER_VAD)
        self._stop_silence_sent = False
        self.final_text = ""
        self.partial_text = ""
        self.error_detail = ""
        self.first_delta_ms = 0
        self.started = time.monotonic()
        self.final_event = asyncio.Event()
        self.confirmed_final = False
        self.event_counts: dict[str, int] = {}

    def _mark_final(self, text: str, *, reason: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        self.final_text = text
        self.confirmed_final = reason == "final"
        self.state.streaming_asr_final_ms = _elapsed_ms(self.started)
        self.state.streaming_asr_final_ready = True
        self.state.streaming_asr_final_text = text
        self.state.streaming_asr_final_reason = reason
        self.final_event.set()

    async def start(self) -> "StepfunWsAsrSession":
        if not self.ws_url:
            raise ValueError("StepFun WS ASR URL unavailable")
        if not self.api_key:
            raise ValueError("StepFun WS ASR API key missing")
        self._connect_cm = ws_connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            proxy=STEPFUN_WS_TTS_PROXY or None,
            open_timeout=min(10.0, self.timeout),
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )
        try:
            self._step_ws = await self._connect_cm.__aenter__()
            await self._send_asr_start()
        except Exception:
            await self.close()
            raise
        self._receiver_task = asyncio.create_task(self._receive_loop())
        self._sender_task = asyncio.create_task(self._send_loop())
        return self

    async def _send_asr_start(self) -> None:
        payload = {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "format": {
                            "type": "pcm",
                            "codec": "pcm_s16le",
                            "rate": DEVICE_SAMPLE_RATE,
                            "bits": DEVICE_SAMPLE_WIDTH * 8,
                            "channel": DEVICE_CHANNELS,
                        },
                        "transcription": {
                            "model": self.model,
                            "language": str(getattr(self.runtime_config, "asr_language", "") or "zh"),
                            "prompt": STEPFUN_WS_ASR_PROMPT,
                            "full_rerun_on_commit": True,
                            "enable_itn": True,
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": STEPFUN_WS_ASR_SILENCE_MS,
                            "threshold": STEPFUN_WS_ASR_THRESHOLD,
                        } if self.server_vad_enabled else None,
                    },
                },
            },
        }
        if payload["session"]["audio"]["input"]["turn_detection"] is None:
            del payload["session"]["audio"]["input"]["turn_detection"]
        await self._step_ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def send_pcm(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        try:
            self._queue.put_nowait(bytes(pcm))
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.state.streaming_asr_queue_drops += 1
            if self.state.streaming_asr_queue_drops <= 3 or self.state.streaming_asr_queue_drops % 10 == 0:
                log_gateway(
                    "streaming_asr_queue_drop",
                    turn_id=self.state.turn_id,
                    drops=self.state.streaming_asr_queue_drops,
                    queue_max=STEPFUN_WS_ASR_QUEUE_FRAMES,
                )
            try:
                self._queue.put_nowait(bytes(pcm))
            except asyncio.QueueFull:
                self.state.streaming_asr_queue_drops += 1

    async def finish(self) -> AsrResult:
        if self._sender_task:
            self.state.streaming_asr_finish_qsize_at_stop = self._queue.qsize()
            sender_cancelled_by_finish = False
            if self.server_vad_enabled and not self.final_event.is_set():
                await self._append_stop_silence()
            queue_started = time.monotonic()
            try:
                await asyncio.wait_for(
                    self._queue.put(None),
                    timeout=STEPFUN_WS_ASR_FINISH_QUEUE_TIMEOUT_MS / 1000,
                )
            except asyncio.TimeoutError:
                self.state.streaming_asr_finish_queue_timeout = True
                log_gateway(
                    "streaming_asr_finish_queue_stuck",
                    turn_id=self.state.turn_id,
                    qsize=self._queue.qsize(),
                    queue_max=STEPFUN_WS_ASR_QUEUE_FRAMES,
                    timeout_ms=STEPFUN_WS_ASR_FINISH_QUEUE_TIMEOUT_MS,
                )
                self._sender_task.cancel()
                sender_cancelled_by_finish = True
            finally:
                self.state.streaming_asr_finish_queue_ms = _elapsed_ms(queue_started)
            sender_started = time.monotonic()
            try:
                await asyncio.wait_for(asyncio.shield(self._sender_task), timeout=min(3.0, self.timeout))
            except asyncio.TimeoutError:
                self._sender_task.cancel()
                sender_cancelled_by_finish = True
            except asyncio.CancelledError:
                if not sender_cancelled_by_finish:
                    raise
            finally:
                self.state.streaming_asr_sender_drain_ms += _elapsed_ms(sender_started)
        commit_sent = False
        commit_started = 0.0
        if self._step_ws and (not self.server_vad_enabled or not (self.final_text or self.partial_text)):
            try:
                commit_started = time.monotonic()
                await self._step_ws.send(json.dumps({"type": "input_audio_buffer.commit"}, separators=(",", ":")))
                commit_sent = True
                self.state.streaming_asr_commit_sent = True
                log_gateway(
                    "streaming_asr_commit_sent",
                    turn_id=self.state.turn_id,
                    server_vad=self.server_vad_enabled,
                    had_text=bool(self.final_text or self.partial_text),
                )
            except Exception:
                pass
        if self._receiver_task:
            if self.confirmed_final or self._receiver_task.done():
                try:
                    await self._receiver_task
                except asyncio.CancelledError:
                    pass
            else:
                receiver_started = time.monotonic()
                receiver_wait_ms = STEPFUN_WS_ASR_COMMIT_WAIT_MS if commit_sent else STEPFUN_WS_ASR_FINAL_WAIT_MS
                try:
                    await asyncio.wait_for(
                        self._receiver_task,
                        timeout=max(0.1, receiver_wait_ms / 1000),
                    )
                except asyncio.TimeoutError:
                    self._receiver_task.cancel()
                finally:
                    waited_ms = _elapsed_ms(receiver_started)
                    self.state.streaming_asr_receiver_wait_ms += waited_ms
                    if commit_sent and self.confirmed_final and commit_started:
                        self.state.streaming_asr_commit_to_final_ms = _elapsed_ms(commit_started)
        await self.close()
        text = self.final_text.strip() if self.confirmed_final else ""
        if text and not self.state.streaming_asr_final_ms:
            self.state.streaming_asr_final_ms = _elapsed_ms(self.started)
        if text:
            return AsrResult(ok=True, text=text, status="streaming_asr")
        partial_text = (self.final_text or self.partial_text).strip()
        partial_reason = self.state.streaming_asr_final_reason or "partial"
        early_turn_partial_allowed = self.state.streaming_asr_early_turn_triggered and (
            partial_reason == "deterministic_partial"
            or (
                partial_reason == "stable_partial"
                and STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED
            )
            or STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL
        )
        if partial_text and (STEPFUN_WS_ASR_USE_PARTIAL_AS_FINAL or early_turn_partial_allowed):
            if not self.state.streaming_asr_final_ms:
                self.state.streaming_asr_final_ms = _elapsed_ms(self.started)
            log_gateway(
                "streaming_asr_partial_used_as_final",
                turn_id=self.state.turn_id,
                reason=partial_reason,
                text_chars=len(partial_text),
                audio_ms=streaming_asr_audio_ms(self.state),
                early_turn=bool(early_turn_partial_allowed),
            )
            return AsrResult(ok=True, text=partial_text, status="streaming_asr")
        if partial_text:
            log_gateway(
                "streaming_asr_partial_rejected",
                turn_id=self.state.turn_id,
                reason=partial_reason,
                text_chars=len(partial_text),
                audio_ms=streaming_asr_audio_ms(self.state),
            )
            return AsrResult(
                ok=False,
                status="streaming_asr_partial_only",
                detail="streaming ASR only produced partial transcript; falling back to full ASR",
            )
        event_summary = ",".join(f"{key}:{value}" for key, value in sorted(self.event_counts.items())[:12])
        detail = self.error_detail or "empty transcript"
        if event_summary:
            detail = f"{detail}; events={event_summary}"
        log_gateway(
            "streaming_asr_empty",
            turn_id=self.state.turn_id,
            audio_ms=streaming_asr_audio_ms(self.state),
            forwarded_frames=self.state.streaming_asr_forwarded_frames,
            events=event_summary,
        )
        return AsrResult(ok=False, status="streaming_asr_empty", detail=detail)

    async def _append_stop_silence(self) -> None:
        if self._stop_silence_sent or STEPFUN_WS_ASR_STOP_SILENCE_MS <= 0:
            return
        silence = b"\x00\x00" * int(DEVICE_SAMPLE_RATE * STEPFUN_WS_ASR_STOP_SILENCE_MS / 1000)
        if silence:
            await self.send_pcm(silence)
            self._stop_silence_sent = True

    async def close(self) -> None:
        self._closed = True
        for task in (self._sender_task, self._receiver_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, ConnectionClosed):
                    pass
                except Exception:
                    pass
        if self._connect_cm is not None:
            try:
                await self._connect_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._connect_cm = None
        self._step_ws = None

    async def _send_loop(self) -> None:
        while True:
            pcm = await self._queue.get()
            if pcm is None:
                return
            if not self._step_ws:
                return
            audio = base64.b64encode(pcm).decode("ascii")
            event = {
                "type": "input_audio_buffer.append",
                "audio": audio,
                "data": {"audio": audio},
            }
            await self._step_ws.send(json.dumps(event, separators=(",", ":")))

    async def _receive_loop(self) -> None:
        while True:
            raw_message = await asyncio.wait_for(self._step_ws.recv(), timeout=self.timeout)
            event = _json_object(raw_message)
            event_type = str(event.get("type") or event.get("event") or "").lower()
            if event_type:
                self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
            if any(marker in event_type for marker in ("error", "failed")):
                data = event.get("data") if isinstance(event.get("data"), dict) else event
                self.error_detail = _stepfun_error_detail(data)
                return
            if event_type in {"session.created", "session.updated", "input_audio_buffer.speech_started", "input_audio_buffer.committed", "conversation.item.created"}:
                continue
            text = _stepfun_asr_text_from_payload(event)
            if text:
                if not self.first_delta_ms:
                    self.first_delta_ms = _elapsed_ms(self.started)
                    self.state.streaming_asr_first_delta_ms = self.first_delta_ms
                maybe_start_streaming_asr_prefetch(self.state, text, self.runtime_config)
                if self.config is not None:
                    maybe_start_bridge_speculative(self.state, self.config, text, self.runtime_config)
                if any(marker in event_type for marker in ("done", "completed", "complete", "final")):
                    text_chars = len(text.strip())
                    audio_ms = streaming_asr_audio_ms(self.state)
                    if text_chars < STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS or audio_ms < STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS:
                        self._mark_final(text, reason="final")
                        log_gateway(
                            "streaming_asr_final_deferred",
                            turn_id=self.state.turn_id,
                            first_delta_ms=self.first_delta_ms,
                            text_chars=text_chars,
                            audio_ms=audio_ms,
                            min_chars=STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS,
                            min_audio_ms=STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS,
                        )
                        return
                    self._mark_final(text, reason="final")
                    log_gateway(
                        "streaming_asr_final",
                        turn_id=self.state.turn_id,
                        first_delta_ms=self.first_delta_ms,
                        reason="final",
                        text_chars=len(text),
                    )
                    return
                self.partial_text = text if len(text) >= len(self.partial_text) else self.partial_text + text
                allowed, plan, blocked_reason = streaming_asr_can_trigger_deterministic_partial(
                    self.state,
                    self.partial_text,
                    self.runtime_config,
                )
                if allowed:
                    self._mark_final(self.partial_text, reason="deterministic_partial")
                    log_gateway(
                        "streaming_asr_deterministic_partial_ready",
                        turn_id=self.state.turn_id,
                        first_delta_ms=self.first_delta_ms,
                        text_chars=len(self.partial_text),
                        audio_ms=streaming_asr_audio_ms(self.state),
                        intent=plan.get("intent"),
                        subject=plan.get("subject"),
                        location=plan.get("location"),
                    )
                    return
                if blocked_reason != "not_deterministic":
                    log_gateway(
                        "streaming_asr_deterministic_partial_blocked",
                        turn_id=self.state.turn_id,
                        reason=blocked_reason,
                        text_chars=len(self.partial_text),
                        audio_ms=streaming_asr_audio_ms(self.state),
                        intent=plan.get("intent"),
                        subject=plan.get("subject"),
                        location=plan.get("location"),
                    )
            if any(marker in event_type for marker in ("speech_stopped", "speech.stop")) and self.partial_text:
                stable, stable_reason = streaming_asr_can_trigger_stable_partial(self.state, self.partial_text)
                final_reason = "stable_partial" if stable else "speech_stopped_partial"
                self._mark_final(self.partial_text, reason=final_reason)
                log_gateway(
                    "streaming_asr_partial_ready",
                    turn_id=self.state.turn_id,
                    first_delta_ms=self.first_delta_ms,
                    reason=final_reason,
                    stable_blocked_reason="" if stable else stable_reason,
                    text_chars=len(self.partial_text),
                    audio_ms=streaming_asr_audio_ms(self.state),
                )
                if stable:
                    return


def transcribe_with_local_command(runtime_config: Any, wav_bytes: bytes) -> AsrResult:
    command = os.environ.get("AURA_LOCAL_ASR_COMMAND", "").strip()
    if not command:
        command_path = shutil.which("whisper") or shutil.which("faster-whisper")
        if command_path:
            command = f"{command_path} --language {getattr(runtime_config, 'asr_language', 'zh')} --model {getattr(runtime_config, 'asr_model', 'whisper-base')}"
    if not command:
        return AsrResult(
            ok=False,
            status="local_asr_unavailable",
            detail="本地 ASR 模式已启用，但容器内没有可用 whisper/faster-whisper 命令",
        )
    with tempfile.TemporaryDirectory(prefix="aura-asr-") as tmp:
        wav_path = Path(tmp) / "turn.wav"
        wav_path.write_bytes(wav_bytes)
        completed = subprocess.run(
            [*command.split(), str(wav_path)],
            text=True,
            capture_output=True,
            timeout=max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30)),
            check=False,
        )
    text = (completed.stdout or "").strip().splitlines()[-1:] or [""]
    if completed.returncode != 0 or not text[0].strip():
        return AsrResult(ok=False, status="local_asr_failed", detail=(completed.stderr or "empty transcript")[:240])
    return AsrResult(ok=True, text=text[0].strip())


def transcribe_with_api(runtime_config: Any, wav_bytes: bytes) -> AsrResult:
    provider = str(getattr(runtime_config, "asr_provider", "") or "").strip().lower()
    timeout = max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30))
    if provider == "stepfun":
        endpoint = asr_transcription_url(getattr(runtime_config, "asr_base_url", ""), provider=provider)
        if not endpoint:
            return AsrResult(ok=False, status="asr_base_url_missing", detail="ASR Base URL 为空或不是 http/https")
        return transcribe_with_stepfun_asr(runtime_config, wav_bytes, endpoint)
    provider_handlers = {
        "aliyun-nls": transcribe_aliyun_nls,
        "volcengine": transcribe_volcengine,
        "qwen3-asr": transcribe_qwen3_asr,
        "baidu": transcribe_baidu,
        "tencent": transcribe_tencent,
    }
    if provider in provider_handlers:
        try:
            text = provider_handlers[provider](runtime_config, wav_bytes, timeout=timeout)
        except VoiceProviderError as exc:
            return AsrResult(ok=False, status="asr_api_failed", detail=str(exc)[:240])
        return AsrResult(ok=bool(text), text=text, status="ok" if text else "empty_transcript")
    endpoint = asr_transcription_url(getattr(runtime_config, "asr_base_url", ""), provider=provider)
    if not endpoint:
        return AsrResult(ok=False, status="asr_base_url_missing", detail="ASR Base URL 为空或不是 http/https")
    fields = {
        "model": getattr(runtime_config, "asr_model", "") or "whisper-1",
        "language": getattr(runtime_config, "asr_language", "") or "zh",
    }
    body, content_type = multipart_form_data(fields, "file", "turn.wav", "audio/wav", wav_bytes)
    headers = {"content-type": content_type}
    api_key = str(getattr(runtime_config, "asr_api_key", "") or "").strip()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with request.urlopen(req, timeout=max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30))) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except HTTPError as exc:
        return AsrResult(ok=False, status="asr_http_error", detail=f"HTTP {exc.code}")
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return AsrResult(ok=False, status="asr_api_failed", detail=exc.__class__.__name__)
    text = str(payload.get("text") or payload.get("transcript") or "").strip()
    return AsrResult(ok=bool(text), text=text, status="ok" if text else "empty_transcript")


def maybe_warm_asr_http_connection(state: TurnState, runtime_config: Any) -> None:
    if not ASR_HTTP_KEEPALIVE_ENABLED or not ASR_HTTP_WARM_ENABLED or warm_pooled_http_url is None:
        return
    if not runtime_config or not getattr(runtime_config, "asr_enabled", False):
        return
    if str(getattr(runtime_config, "asr_mode", "") or "").strip().lower() != "api":
        return
    endpoint = asr_transcription_url(
        getattr(runtime_config, "asr_base_url", ""),
        provider=getattr(runtime_config, "asr_provider", ""),
    )
    if not endpoint:
        return

    def _warm() -> None:
        try:
            result = warm_pooled_http_url(endpoint, timeout_seconds=1.5)
            log_gateway(
                "asr_http_warm",
                turn_id=state.turn_id,
                ok=bool(result.get("ok")),
                status=str(result.get("status") or ""),
                latency_ms=int(result.get("latency_ms") or 0),
                endpoint_host=str(result.get("endpoint_host") or ""),
            )
        except Exception as exc:  # pragma: no cover - defensive warm path
            log_gateway("asr_http_warm", turn_id=state.turn_id, ok=False, status=exc.__class__.__name__)

    Thread(target=_warm, daemon=True).start()


def transcribe_with_stepfun_asr(runtime_config: Any, wav_bytes: bytes, endpoint: str) -> AsrResult:
    audio_b64 = base64.b64encode(wav_bytes).decode("ascii")
    payload: dict[str, Any] = {
        "audio": {
            "data": audio_b64,
            "input": {
                "transcription": {
                    "model": getattr(runtime_config, "asr_model", "") or "stepaudio-2.5-asr",
                    "enable_itn": True,
                },
                "format": {"type": "wav"},
            },
        },
    }
    language = str(getattr(runtime_config, "asr_language", "") or "").strip()
    if language:
        payload["audio"]["input"]["transcription"]["language"] = language
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    api_key = str(getattr(runtime_config, "asr_api_key", "") or "").strip()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    req = request.Request(endpoint, data=body, method="POST", headers=headers)
    timeout_seconds = max(1.0, float(getattr(runtime_config, "asr_timeout_seconds", 30) or 30))
    try:
        if ASR_HTTP_KEEPALIVE_ENABLED and open_pooled_http_request is not None:
            with open_pooled_http_request(req, timeout=timeout_seconds) as res:
                raw = res.read()
        else:
            with request.urlopen(req, timeout=timeout_seconds) as res:
                raw = res.read()
    except HTTPError as exc:
        return AsrResult(ok=False, status="asr_http_error", detail=f"HTTP {exc.code}")
    except (OSError, URLError) as exc:
        return AsrResult(ok=False, status="asr_api_failed", detail=exc.__class__.__name__)
    try:
        text = stepfun_asr_text_from_response(raw)
    except json.JSONDecodeError as exc:
        return AsrResult(ok=False, status="asr_api_failed", detail=exc.__class__.__name__)
    return AsrResult(ok=bool(text), text=text, status="ok" if text else "empty_transcript")


def stepfun_asr_text_from_response(raw: bytes | str) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
    value = text.strip()
    if not value:
        return ""
    if not value.startswith("data:") and not value.startswith("event:"):
        payload = json.loads(value)
        return _stepfun_asr_text_from_payload(payload)

    parts: list[str] = []
    final_text = ""
    for block in re.split(r"\r?\n\r?\n", value):
        data_lines = []
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data and data != "[DONE]":
                data_lines.append(data)
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        chunk_text = _stepfun_asr_text_from_payload(payload)
        if not chunk_text:
            continue
        event_type = str(payload.get("type") or payload.get("event") or "").lower()
        is_final = bool(payload.get("is_final") or payload.get("final")) or any(
            marker in event_type for marker in ("completed", "complete", "done", "final")
        )
        if is_final:
            final_text = chunk_text
        else:
            parts.append(chunk_text)
    return (final_text or "".join(parts)).strip()


def _stepfun_asr_text_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [
        payload.get("text"),
        payload.get("transcript"),
        payload.get("result"),
        payload.get("content"),
        data.get("text"),
        data.get("transcript"),
        data.get("result"),
        data.get("content"),
        payload.get("delta"),
        data.get("delta"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    choices = payload.get("choices") or data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for key in ("text", "transcript", "content"):
                value = choice.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def synthesize_tts(runtime_config: Any, text: str) -> TtsResult:
    started = time.monotonic()
    if not runtime_config or not getattr(runtime_config, "tts_enabled", False):
        return TtsResult(ok=False, detail="TTS 未启用")
    provider = str(getattr(runtime_config, "tts_provider", "") or "").strip().lower()
    provider_handlers = {"aliyun-nls", "volcengine", "baidu", "minimax", "tencent"}
    endpoint = tts_speech_url(getattr(runtime_config, "tts_base_url", ""), provider=provider)
    if not endpoint and provider not in provider_handlers:
        return TtsResult(ok=False, detail="TTS Base URL 为空或不是 http/https")
    chunks = tts_text_chunks(text)
    if not chunks:
        return TtsResult(ok=False, detail="empty text")
    audio_parts: list[bytes] = []
    source_rate = _tts_source_sample_rate(runtime_config)
    for index, chunk in enumerate(chunks, start=1):
        result = _synthesize_tts_chunk(runtime_config, endpoint, chunk, sample_rate=source_rate)
        if not result.ok:
            return TtsResult(
                ok=False,
                detail=f"chunk {index}/{len(chunks)}: {result.detail}",
                chunk_count=len(chunks),
                latency_ms=_elapsed_ms(started),
                source_sample_rate=source_rate,
            )
        audio_parts.append(_tts_audio_for_device(result.audio, source_rate=source_rate))
    audio = b"".join(audio_parts)
    return TtsResult(
        ok=bool(audio),
        audio=audio,
        detail="" if audio else "empty audio",
        chunk_count=len(chunks),
        latency_ms=_elapsed_ms(started),
        source_sample_rate=source_rate,
    )


async def synthesize_and_stream_tts(
    websocket: Any,
    runtime_config: Any,
    turn_id: int,
    text: str,
    *,
    stream_id: int,
    is_final: bool = True,
    preface: bool = False,
    allow_stepfun_ws: bool = True,
    dialogue_segment_index: int | None = None,
    dialogue_segment_text: str = "",
) -> TtsResult:
    started = time.monotonic()
    if not runtime_config or not getattr(runtime_config, "tts_enabled", False):
        result = TtsResult(ok=False, detail="TTS 未启用", latency_ms=_elapsed_ms(started))
        if not preface:
            await send_tts_failure(websocket, turn_id, result, stream_id=stream_id)
        return result
    provider = str(getattr(runtime_config, "tts_provider", "") or "").strip().lower()
    provider_handlers = {"aliyun-nls", "volcengine", "baidu", "minimax", "tencent"}
    endpoint = tts_speech_url(getattr(runtime_config, "tts_base_url", ""), provider=provider)
    if not endpoint and provider not in provider_handlers:
        result = TtsResult(ok=False, detail="TTS Base URL 为空或不是 http/https", latency_ms=_elapsed_ms(started))
        if not preface:
            await send_tts_failure(websocket, turn_id, result, stream_id=stream_id)
        return result
    chunks = tts_text_chunks(text)
    if not chunks:
        result = TtsResult(ok=False, detail="empty text", latency_ms=_elapsed_ms(started))
        if not preface:
            await send_tts_failure(websocket, turn_id, result, stream_id=stream_id)
        return result

    if allow_stepfun_ws and stepfun_ws_tts_available(runtime_config):
        ws_result = await synthesize_and_stream_stepfun_ws_tts(
            websocket,
            runtime_config,
            turn_id,
            text,
            stream_id=stream_id,
            is_final=is_final,
            preface=preface,
            started=started,
        )
        if ws_result.ok or not STEPFUN_WS_TTS_FALLBACK_HTTP:
            if not ws_result.ok and not preface:
                await send_tts_failure(websocket, turn_id, ws_result, stream_id=stream_id)
            return ws_result
        log_gateway(
            "stepfun_ws_tts_fallback_http",
            turn_id=turn_id,
            detail=ws_result.detail,
            latency_ms=ws_result.latency_ms,
        )

    source_rate = _tts_source_sample_rate(runtime_config)
    total_audio_bytes = 0
    first_chunk_ms = 0
    first_audio_ms = 0
    audio_chunk_count = 0
    audio_timing = AudioChunkTiming(started)
    concurrency = max(1, min(TTS_PREFETCH_CONCURRENCY, len(chunks)))
    semaphore = asyncio.Semaphore(concurrency)

    async def synthesize_one(index: int, chunk: str) -> tuple[int, str, TtsResult, int]:
        chunk_started = time.monotonic()
        async with semaphore:
            result = await asyncio.to_thread(
                _synthesize_tts_chunk,
                runtime_config,
                endpoint,
                chunk,
                sample_rate=source_rate,
            )
        return index, chunk, result, _elapsed_ms(chunk_started)

    async def stream_synthesized(
        index: int,
        chunk: str,
        result: TtsResult,
        synth_ms: int,
        pending_tasks: list[asyncio.Task[tuple[int, str, TtsResult, int]]],
    ) -> TtsResult | None:
        nonlocal total_audio_bytes, first_chunk_ms, first_audio_ms, audio_chunk_count
        if index != expected_index:
            result = TtsResult(ok=False, detail=f"internal order mismatch: {index}/{expected_index}")
        if not result.ok:
            for pending in pending_tasks:
                pending.cancel()
            failed = TtsResult(
                ok=False,
                detail=f"chunk {index}/{len(chunks)}: {result.detail}",
                chunk_count=len(chunks),
                audio_chunk_count=audio_chunk_count,
                audio_bytes=total_audio_bytes,
                latency_ms=_elapsed_ms(started),
                first_chunk_ms=first_chunk_ms,
                first_audio_ms=first_audio_ms,
                source_sample_rate=source_rate,
            )
            if not preface:
                await send_tts_failure(websocket, turn_id, failed, stream_id=stream_id)
            return failed
        audio = _tts_audio_for_device(result.audio, source_rate=source_rate)
        if (
            len(chunks) == 1
            and dialogue_segment_index is not None
            and dialogue_segment_index >= 0
            and audio
        ):
            await send_dialogue_segment_timing(
                websocket,
                turn_id=turn_id,
                segment_index=dialogue_segment_index,
                text=dialogue_segment_text or chunk,
                audio_bytes=len(audio),
            )
        if index == 1:
            first_chunk_ms = synth_ms
        frame_count = (len(audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES if audio else 0
        await send_tts_pcm_stream(
            websocket,
            turn_id,
            audio,
            stream_id=stream_id,
            is_final=is_final and index == len(chunks),
            timing=audio_timing,
        )
        if audio_timing.first_audio_ms:
            first_audio_ms = audio_timing.first_audio_ms
        log_gateway(
            "tts_preface_chunk" if preface else "tts_chunk",
            turn_id=turn_id,
            index=index,
            total=len(chunks),
            chars=len(chunk),
            synth_ms=synth_ms,
            audio_bytes=len(audio),
            audio_chunks=frame_count,
            first_audio_ms=first_audio_ms if index == 1 else 0,
            streamed=True,
            prefetch_concurrency=concurrency,
            final=is_final and index == len(chunks),
        )
        total_audio_bytes += len(audio)
        audio_chunk_count += frame_count
        return None

    start_index = 1
    if len(chunks) > 1 and not preface:
        expected_index = 1
        index, chunk, result, synth_ms = await synthesize_one(1, chunks[0])
        failed = await stream_synthesized(index, chunk, result, synth_ms, [])
        if failed:
            return failed
        start_index = 2

    tasks = [
        asyncio.create_task(synthesize_one(index, chunk))
        for index, chunk in enumerate(chunks[start_index - 1:], start=start_index)
    ]

    for offset, task in enumerate(tasks):
        expected_index = start_index + offset
        index, chunk, result, synth_ms = await task
        failed = await stream_synthesized(index, chunk, result, synth_ms, tasks[offset + 1:])
        if failed:
            return failed
    final = TtsResult(
        ok=total_audio_bytes > 0,
        audio=b"",
        detail="" if total_audio_bytes else "empty audio",
        chunk_count=len(chunks),
        audio_chunk_count=audio_chunk_count,
        audio_bytes=total_audio_bytes,
        latency_ms=_elapsed_ms(started),
        first_chunk_ms=first_chunk_ms,
        first_audio_ms=first_audio_ms,
        source_sample_rate=source_rate,
        device_sample_rate=DEVICE_SAMPLE_RATE,
        streamed=True,
        **audio_chunk_timing_summary(audio_timing),
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "tts_completed",
            "status": "ok" if final.ok else "failed",
            "turn_id": turn_id,
            "latency_ms": final.latency_ms,
            "first_chunk_ms": first_chunk_ms,
            "first_audio_ms": first_audio_ms,
            "chunk_count": len(chunks),
            "audio_chunk_count": audio_chunk_count,
            "audio_bytes": total_audio_bytes,
            **tts_chunk_timing_payload(final),
            "source_sample_rate": source_rate,
            "device_sample_rate": DEVICE_SAMPLE_RATE,
            "streamed": True,
            "prefetch_concurrency": concurrency,
            "final": is_final,
            "preface": preface,
        },
    })
    return final


async def send_tts_failure(websocket: Any, turn_id: int, result: TtsResult, *, stream_id: int) -> None:
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "tts_failed",
            "status": "failed",
            "turn_id": turn_id,
            "detail": result.detail,
            "latency_ms": result.latency_ms,
            "chunk_count": result.chunk_count,
        },
    })
    await send_tts_binary(websocket, turn_id, b"", stream_id=stream_id, is_final=True)


def _synthesize_tts_chunk(runtime_config: Any, endpoint: str, text: str, *, sample_rate: int) -> TtsResult:
    provider = str(getattr(runtime_config, "tts_provider", "") or "").strip().lower()
    provider_handlers = {
        "aliyun-nls": synthesize_aliyun_nls,
        "volcengine": synthesize_volcengine,
        "baidu": synthesize_baidu,
        "minimax": synthesize_minimax,
        "tencent": synthesize_tencent,
    }
    timeout = max(1.0, float(getattr(runtime_config, "tts_timeout_seconds", 15) or 15))
    if provider in provider_handlers:
        try:
            audio = provider_handlers[provider](runtime_config, text, sample_rate=sample_rate, timeout=timeout)
        except VoiceProviderError as exc:
            return TtsResult(ok=False, detail=str(exc)[:240])
        return TtsResult(ok=bool(audio), audio=audio, detail="" if audio else "empty audio")
    req = _build_tts_request(runtime_config, endpoint, text, sample_rate=sample_rate)
    try:
        with request.urlopen(req, timeout=timeout) as res:
            audio = res.read()
    except HTTPError as exc:
        return TtsResult(ok=False, detail=f"HTTP {exc.code}")
    except (OSError, URLError) as exc:
        return TtsResult(ok=False, detail=exc.__class__.__name__)
    return TtsResult(ok=bool(audio), audio=audio, detail="" if audio else "empty audio")


def _build_tts_request(runtime_config: Any, endpoint: str, text: str, *, sample_rate: int) -> request.Request:
    body = json.dumps(
        {
            "model": getattr(runtime_config, "tts_model", "") or "tts",
            "input": text,
            "voice": getattr(runtime_config, "tts_voice", "") or "aura",
            "response_format": "pcm",
            "sample_rate": sample_rate,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"content-type": "application/json"}
    api_key = str(getattr(runtime_config, "tts_api_key", "") or "").strip()
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    return request.Request(endpoint, data=body, method="POST", headers=headers)


def stepfun_ws_tts_available(runtime_config: Any) -> bool:
    if not STEPFUN_WS_TTS_ENABLED:
        return False
    if not runtime_config or not getattr(runtime_config, "tts_enabled", False):
        return False
    provider = str(getattr(runtime_config, "tts_provider", "") or "").strip().lower()
    if provider != "stepfun":
        return False
    if not str(getattr(runtime_config, "tts_api_key", "") or "").strip():
        return False
    model = str(getattr(runtime_config, "tts_model", "") or "").strip()
    if not model:
        return False
    return bool(stepfun_ws_tts_url(getattr(runtime_config, "tts_base_url", ""), model=model))


def stepfun_ws_tts_url(base_url: str, *, model: str) -> str:
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        return ""
    scheme = "wss" if parsed.scheme in {"https", "wss"} else "ws"
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/audio/speech"):
        path = path[: -len("/audio/speech")]
    if path.endswith("/realtime/audio"):
        endpoint_path = path
    elif not path:
        endpoint_path = "/v1/realtime/audio"
    elif path.endswith("/v1") or path.endswith("/step_plan/v1"):
        endpoint_path = f"{path}/realtime/audio"
    else:
        endpoint_path = f"{path}/realtime/audio"
    query = dict(_query_items(parsed.query))
    query["model"] = model
    return urlunsplit((scheme, parsed.netloc, endpoint_path, urlencode(query), ""))


def _query_items(query: str) -> list[tuple[str, str]]:
    if not query:
        return []
    rows: list[tuple[str, str]] = []
    for part in str(query).split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        rows.append((key, value))
    return rows


def _json_object(message: Any) -> dict[str, Any]:
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    payload = json.loads(str(message or "{}"))
    return payload if isinstance(payload, dict) else {}


def _ws_exception_detail(prefix: str, exc: BaseException) -> str:
    base = f"{prefix} {exc.__class__.__name__}"
    if isinstance(exc, InvalidStatus):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        reason = str(getattr(response, "reason_phrase", "") or "").strip()
        body = getattr(response, "body", b"") if response is not None else b""
        if isinstance(body, bytes | bytearray):
            body_text = bytes(body[:1000]).decode("utf-8", errors="replace")
        else:
            body_text = str(body or "")
        headers = getattr(response, "headers", None)
        content_type = ""
        try:
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").strip() if headers else ""
        except Exception:
            content_type = ""
        parts = [base]
        if status_code is not None:
            parts.append(f"HTTP {status_code}")
        if reason:
            parts.append(reason)
        if content_type:
            parts.append(f"content-type={content_type}")
        body_preview = scrub_text(body_text, 300).strip()
        if body_preview:
            parts.append(f"body={body_preview}")
        return "; ".join(parts)
    text = scrub_text(str(exc), 300).strip()
    return f"{base}: {text}" if text else base


def _stepfun_ws_tts_semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    limit = max(1, int(STEPFUN_WS_TTS_MAX_SESSIONS or 1))
    existing = _STEPFUN_WS_TTS_SEMAPHORES.get(loop_id)
    if existing is None or _STEPFUN_WS_TTS_SEMAPHORE_LIMITS.get(loop_id) != limit:
        existing = asyncio.Semaphore(limit)
        _STEPFUN_WS_TTS_SEMAPHORES[loop_id] = existing
        _STEPFUN_WS_TTS_SEMAPHORE_LIMITS[loop_id] = limit
    return existing


def _stepfun_ws_tts_cooldown_remaining_ms(now: float | None = None) -> int:
    current = time.monotonic() if now is None else now
    return max(0, int((_STEPFUN_WS_TTS_COOLDOWN_UNTIL - current) * 1000))


def stepfun_ws_tts_cooling_down() -> bool:
    return _stepfun_ws_tts_cooldown_remaining_ms() > 0


def _stepfun_ws_tts_mark_failure(detail: str) -> None:
    global _STEPFUN_WS_TTS_COOLDOWN_UNTIL, _STEPFUN_WS_TTS_COOLDOWN_DETAIL
    text = scrub_text(str(detail or ""), 300)
    lower = text.lower()
    is_rate_limited = (
        "HTTP 429" in text
        or "Too Many Requests" in text
        or "too many requests" in lower
        or "request limited" in lower
        or "rate limit" in lower
        or "concurrency" in lower
    )
    if not is_rate_limited or STEPFUN_WS_TTS_429_COOLDOWN_SECONDS <= 0:
        return
    _STEPFUN_WS_TTS_COOLDOWN_UNTIL = max(
        _STEPFUN_WS_TTS_COOLDOWN_UNTIL,
        time.monotonic() + float(STEPFUN_WS_TTS_429_COOLDOWN_SECONDS),
    )
    _STEPFUN_WS_TTS_COOLDOWN_DETAIL = text
    log_gateway(
        "stepfun_ws_tts_cooldown",
        cooldown_ms=_stepfun_ws_tts_cooldown_remaining_ms(),
        max_sessions=max(1, int(STEPFUN_WS_TTS_MAX_SESSIONS or 1)),
        detail=text,
    )


async def _stepfun_ws_tts_acquire_slot(*, warm: bool = False) -> asyncio.Semaphore | None:
    if stepfun_ws_tts_cooling_down():
        return None
    semaphore = _stepfun_ws_tts_semaphore()
    timeout = STEPFUN_WS_TTS_WARM_ACQUIRE_TIMEOUT_SECONDS if warm else STEPFUN_WS_TTS_ACQUIRE_TIMEOUT_SECONDS
    try:
        if timeout <= 0:
            await semaphore.acquire()
        else:
            await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    if stepfun_ws_tts_cooling_down():
        semaphore.release()
        return None
    return semaphore


async def synthesize_and_stream_stepfun_ws_tts(
    websocket: Any,
    runtime_config: Any,
    turn_id: int,
    text: str,
    *,
    stream_id: int,
    is_final: bool = True,
    preface: bool = False,
    started: float | None = None,
) -> TtsResult:
    started = time.monotonic() if started is None else started
    cooldown_ms = _stepfun_ws_tts_cooldown_remaining_ms()
    if cooldown_ms:
        return TtsResult(
            ok=False,
            detail=f"StepFun WS TTS cooling down after rate limit; retry in {cooldown_ms}ms",
            latency_ms=_elapsed_ms(started),
        )
    slot = await _stepfun_ws_tts_acquire_slot()
    if slot is None:
        return TtsResult(
            ok=False,
            detail=(
                "StepFun WS TTS session limit reached"
                if not stepfun_ws_tts_cooling_down()
                else f"StepFun WS TTS cooling down after rate limit; retry in {_stepfun_ws_tts_cooldown_remaining_ms()}ms"
            ),
            latency_ms=_elapsed_ms(started),
        )
    model = str(getattr(runtime_config, "tts_model", "") or "").strip()
    ws_url = stepfun_ws_tts_url(getattr(runtime_config, "tts_base_url", ""), model=model)
    try:
        if not ws_url:
            return TtsResult(ok=False, detail="StepFun WS TTS URL unavailable", latency_ms=_elapsed_ms(started))
        api_key = str(getattr(runtime_config, "tts_api_key", "") or "").strip()
        if not api_key:
            return TtsResult(ok=False, detail="StepFun WS TTS API key missing", latency_ms=_elapsed_ms(started))
        source_rate = _stepfun_ws_source_sample_rate()
        timeout = max(1.0, float(getattr(runtime_config, "tts_timeout_seconds", 15) or 15))
        total_audio_bytes = 0
        audio_chunk_count = 0
        first_audio_ms = 0
        first_chunk_ms = 0
        sentence_count = 0
        done_seen = False
        final_frame_sent = False
        audio_timing = AudioChunkTiming(started)
        async with ws_connect(
            ws_url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            proxy=STEPFUN_WS_TTS_PROXY or None,
            open_timeout=min(STEPFUN_WS_TTS_OPEN_TIMEOUT_SECONDS, timeout),
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as step_ws:
            session_id = await _stepfun_ws_wait_session_id(step_ws, timeout=timeout)
            await step_ws.send(json.dumps(
                _stepfun_ws_create_event(runtime_config, session_id, sample_rate=source_rate),
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            await _stepfun_ws_wait_created(step_ws, timeout=timeout)
            await step_ws.send(json.dumps(
                {"type": "tts.text.delta", "data": {"session_id": session_id, "text": str(text or "")}},
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            if STEPFUN_WS_TTS_FLUSH_AFTER_DELTA:
                await step_ws.send(json.dumps(
                    {"type": "tts.text.flush", "data": {"session_id": session_id}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ))
            await step_ws.send(json.dumps(
                {"type": "tts.text.done", "data": {"session_id": session_id}},
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            while True:
                raw_message = await asyncio.wait_for(step_ws.recv(), timeout=timeout)
                event = _json_object(raw_message)
                event_type = str(event.get("type") or "")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if event_type == "tts.response.error":
                    return TtsResult(
                        ok=False,
                        detail=_stepfun_error_detail(data),
                        audio_chunk_count=audio_chunk_count,
                        audio_bytes=total_audio_bytes,
                        latency_ms=_elapsed_ms(started),
                        first_chunk_ms=first_chunk_ms,
                        first_audio_ms=first_audio_ms,
                        source_sample_rate=source_rate,
                        device_sample_rate=DEVICE_SAMPLE_RATE,
                        streamed=True,
                        **audio_chunk_timing_summary(audio_timing),
                    )
                if event_type == "tts.response.sentence.start":
                    sentence_count += 1
                    if not first_chunk_ms:
                        first_chunk_ms = _elapsed_ms(started)
                    continue
                if event_type == "tts.response.audio.delta":
                    pcm = _stepfun_audio_delta(data)
                    if not pcm:
                        continue
                    device_audio = _tts_audio_for_device(pcm, source_rate=source_rate)
                    frame_count = (len(device_audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES
                    await send_tts_pcm_stream(
                        websocket,
                        turn_id,
                        device_audio,
                        stream_id=stream_id,
                        is_final=False,
                        timing=audio_timing,
                    )
                    total_audio_bytes += len(device_audio)
                    audio_chunk_count += frame_count
                    if not first_audio_ms:
                        first_audio_ms = audio_timing.first_audio_ms or _elapsed_ms(started)
                    continue
                if event_type == "tts.response.audio.done":
                    if not total_audio_bytes:
                        pcm = _stepfun_audio_delta(data)
                        if pcm:
                            device_audio = _tts_audio_for_device(pcm, source_rate=source_rate)
                            frame_count = (len(device_audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES
                            await send_tts_pcm_stream(
                                websocket,
                                turn_id,
                                device_audio,
                                stream_id=stream_id,
                                is_final=is_final,
                                timing=audio_timing,
                            )
                            final_frame_sent = bool(is_final)
                            total_audio_bytes += len(device_audio)
                            audio_chunk_count += frame_count
                            if not first_audio_ms:
                                first_audio_ms = audio_timing.first_audio_ms or _elapsed_ms(started)
                    done_seen = True
                    break
    except asyncio.TimeoutError:
        return TtsResult(ok=False, detail="StepFun WS TTS timeout", latency_ms=_elapsed_ms(started))
    except (OSError, ConnectionClosed, ValueError, json.JSONDecodeError) as exc:
        detail = _ws_exception_detail("StepFun WS TTS", exc)
        _stepfun_ws_tts_mark_failure(detail)
        return TtsResult(ok=False, detail=detail, latency_ms=_elapsed_ms(started))
    except Exception as exc:  # pragma: no cover - provider boundary
        detail = _ws_exception_detail("StepFun WS TTS", exc)
        _stepfun_ws_tts_mark_failure(detail)
        return TtsResult(ok=False, detail=detail, latency_ms=_elapsed_ms(started))
    finally:
        slot.release()

    if is_final and not final_frame_sent:
        await send_tts_binary(websocket, turn_id, b"", stream_id=stream_id, is_final=True)
    result = TtsResult(
        ok=total_audio_bytes > 0 and done_seen,
        audio=b"",
        detail="" if total_audio_bytes else "empty audio",
        chunk_count=max(1, sentence_count),
        audio_chunk_count=audio_chunk_count,
        audio_bytes=total_audio_bytes,
        latency_ms=_elapsed_ms(started),
        first_chunk_ms=first_chunk_ms,
        first_audio_ms=first_audio_ms,
        source_sample_rate=source_rate,
        device_sample_rate=DEVICE_SAMPLE_RATE,
        streamed=True,
        **audio_chunk_timing_summary(audio_timing),
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "tts_completed",
            "status": "ok" if result.ok else "failed",
            "turn_id": turn_id,
            "latency_ms": result.latency_ms,
            "first_chunk_ms": result.first_chunk_ms,
            "first_audio_ms": result.first_audio_ms,
            "chunk_count": result.chunk_count,
            "audio_chunk_count": result.audio_chunk_count,
            "audio_bytes": result.audio_bytes,
            **tts_chunk_timing_payload(result),
            "source_sample_rate": source_rate,
            "device_sample_rate": DEVICE_SAMPLE_RATE,
            "streamed": True,
            "provider_stream": "stepfun_ws",
            "final": is_final,
            "preface": preface,
        },
    })
    return result


class StepfunWsTtsSession:
    def __init__(
        self,
        websocket: Any,
        runtime_config: Any,
        turn_id: int,
        *,
        stream_id: int,
        started: float,
        warm: bool = False,
    ) -> None:
        self.websocket = websocket
        self.runtime_config = runtime_config
        self.turn_id = turn_id
        self.stream_id = stream_id
        self.started = started
        self.warm = warm
        self.model = str(getattr(runtime_config, "tts_model", "") or "").strip()
        self.ws_url = stepfun_ws_tts_url(getattr(runtime_config, "tts_base_url", ""), model=self.model)
        self.api_key = str(getattr(runtime_config, "tts_api_key", "") or "").strip()
        self.source_rate = _stepfun_ws_source_sample_rate()
        self.timeout = max(1.0, float(getattr(runtime_config, "tts_timeout_seconds", 15) or 15))
        self.session_id = ""
        self._connect_cm: Any = None
        self._step_ws: Any = None
        self._receiver_task: asyncio.Task[None] | None = None
        self.first_text_ms = 0
        self.first_chunk_abs_ms = 0
        self.first_audio_abs_ms = 0
        self.audio_chunk_count = 0
        self.audio_bytes = 0
        self.audio_timing = AudioChunkTiming(self.started)
        self.sentence_count = 0
        self.text_count = 0
        self.done_seen = False
        self.error_detail = ""
        self.first_audio_event = asyncio.Event()
        self._slot: asyncio.Semaphore | None = None

    def is_healthy(self) -> bool:
        if self.error_detail:
            return False
        if self._step_ws is None:
            return False
        if self._receiver_task is None:
            return False
        return not self._receiver_task.done()

    def bind_turn(self, turn_id: int, *, stream_id: int | None = None) -> None:
        self.turn_id = turn_id
        if stream_id is not None:
            self.stream_id = stream_id

    async def start(self) -> "StepfunWsTtsSession":
        if not self.ws_url:
            raise ValueError("StepFun WS TTS URL unavailable")
        if not self.api_key:
            raise ValueError("StepFun WS TTS API key missing")
        cooldown_ms = _stepfun_ws_tts_cooldown_remaining_ms()
        if cooldown_ms:
            raise RuntimeError(f"StepFun WS TTS cooling down after rate limit; retry in {cooldown_ms}ms")
        self._slot = await _stepfun_ws_tts_acquire_slot(warm=self.warm)
        if self._slot is None:
            raise RuntimeError(
                "StepFun WS TTS session limit reached"
                if not stepfun_ws_tts_cooling_down()
                else f"StepFun WS TTS cooling down after rate limit; retry in {_stepfun_ws_tts_cooldown_remaining_ms()}ms"
            )
        self._connect_cm = ws_connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {self.api_key}"},
            proxy=STEPFUN_WS_TTS_PROXY or None,
            open_timeout=min(STEPFUN_WS_TTS_OPEN_TIMEOUT_SECONDS, self.timeout),
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        )
        try:
            self._step_ws = await self._connect_cm.__aenter__()
            self.session_id = await _stepfun_ws_wait_session_id(self._step_ws, timeout=self.timeout)
            await self._step_ws.send(json.dumps(
                _stepfun_ws_create_event(self.runtime_config, self.session_id, sample_rate=self.source_rate),
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            await _stepfun_ws_wait_created(self._step_ws, timeout=self.timeout)
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                _stepfun_ws_tts_mark_failure(_ws_exception_detail("StepFun WS TTS", exc))
            await self.close()
            raise
        self._receiver_task = asyncio.create_task(self._receive_loop())
        return self

    async def send_text(self, text: str) -> None:
        clean = str(text or "").strip()
        if not clean or not self._step_ws:
            return
        if not self.first_text_ms:
            self.first_text_ms = _elapsed_ms(self.started)
        self.text_count += 1
        await self._step_ws.send(json.dumps(
            {"type": "tts.text.delta", "data": {"session_id": self.session_id, "text": clean}},
            ensure_ascii=False,
            separators=(",", ":"),
        ))
        should_flush = STEPFUN_WS_TTS_FLUSH_AFTER_DELTA and (STEPFUN_WS_TTS_FLUSH_EACH_DELTA or self.text_count == 1)
        if should_flush:
            await self._step_ws.send(json.dumps(
                {"type": "tts.text.flush", "data": {"session_id": self.session_id}},
                ensure_ascii=False,
                separators=(",", ":"),
            ))

    async def finish(self, *, is_final: bool = True) -> TtsResult:
        if self._step_ws and self.session_id:
            try:
                await self._step_ws.send(json.dumps(
                    {"type": "tts.text.done", "data": {"session_id": self.session_id}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ))
            except (OSError, ConnectionClosed) as exc:
                self.error_detail = _ws_exception_detail("StepFun WS TTS", exc)
        if self._receiver_task:
            # _receive_loop already applies an idle timeout to each recv. A total
            # timeout here truncates healthy long-form synthesis.
            await self._receiver_task
        await self.close()
        if is_final and (self.audio_bytes or not self.error_detail):
            await send_tts_binary(self.websocket, self.turn_id, b"", stream_id=self.stream_id, is_final=True)
        first_text_ms = self.first_text_ms or 0
        first_chunk_ms = max(0, self.first_chunk_abs_ms - first_text_ms) if self.first_chunk_abs_ms else 0
        first_audio_ms = max(0, self.first_audio_abs_ms - first_text_ms) if self.first_audio_abs_ms else 0
        latency_ms = max(0, _elapsed_ms(self.started) - first_text_ms) if first_text_ms else _elapsed_ms(self.started)
        ok = bool(self.audio_bytes and self.done_seen and not self.error_detail)
        return TtsResult(
            ok=ok,
            audio=b"",
            detail=self.error_detail or ("" if self.audio_bytes else "empty audio"),
            chunk_count=max(1, self.sentence_count, self.text_count),
            audio_chunk_count=self.audio_chunk_count,
            audio_bytes=self.audio_bytes,
            latency_ms=latency_ms,
            first_chunk_ms=first_chunk_ms,
            first_audio_ms=first_audio_ms,
            source_sample_rate=self.source_rate,
            device_sample_rate=DEVICE_SAMPLE_RATE,
            streamed=True,
            **audio_chunk_timing_summary(self.audio_timing),
        )

    async def close(self) -> None:
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except (asyncio.CancelledError, ConnectionClosed):
                pass
            except Exception:
                pass
        if self._connect_cm is not None:
            try:
                await self._connect_cm.__aexit__(None, None, None)
            except Exception:
                pass
        self._connect_cm = None
        self._step_ws = None
        if self._slot is not None:
            self._slot.release()
            self._slot = None

    async def _receive_loop(self) -> None:
        try:
            while True:
                if self.warm and not self.text_count:
                    raw_message = await self._step_ws.recv()
                else:
                    raw_message = await asyncio.wait_for(self._step_ws.recv(), timeout=self.timeout)
                event = _json_object(raw_message)
                event_type = str(event.get("type") or "")
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                if event_type == "tts.response.error":
                    self.error_detail = _stepfun_error_detail(data)
                    return
                if event_type == "tts.response.sentence.start":
                    self.sentence_count += 1
                    if not self.first_chunk_abs_ms:
                        self.first_chunk_abs_ms = _elapsed_ms(self.started)
                    continue
                if event_type == "tts.response.audio.delta":
                    await self._send_audio_delta(data)
                    continue
                if event_type == "tts.response.audio.done":
                    if not self.audio_bytes:
                        await self._send_audio_delta(data)
                    self.done_seen = True
                    return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.error_detail = self.error_detail or "StepFun WS TTS timeout"
            return
        except (OSError, ConnectionClosed, ValueError, json.JSONDecodeError) as exc:
            self.error_detail = self.error_detail or _ws_exception_detail("StepFun WS TTS", exc)
            _stepfun_ws_tts_mark_failure(self.error_detail)
            return

    async def _send_audio_delta(self, data: dict[str, Any]) -> None:
        if self.warm and not self.text_count:
            return
        pcm = _stepfun_audio_delta(data)
        if not pcm:
            return
        device_audio = _tts_audio_for_device(pcm, source_rate=self.source_rate)
        frame_count = (len(device_audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES
        await send_tts_pcm_stream(
            self.websocket,
            self.turn_id,
            device_audio,
            stream_id=self.stream_id,
            is_final=False,
            timing=self.audio_timing,
        )
        self.audio_bytes += len(device_audio)
        self.audio_chunk_count += frame_count
        if not self.first_audio_abs_ms:
            self.first_audio_abs_ms = _elapsed_ms(self.started)
            self.first_audio_event.set()


async def start_stepfun_ws_tts_session(
    websocket: Any,
    runtime_config: Any,
    turn_id: int,
    *,
    stream_id: int,
    started: float,
    warm: bool = False,
) -> StepfunWsTtsSession:
    session = StepfunWsTtsSession(websocket, runtime_config, turn_id, stream_id=stream_id, started=started, warm=warm)
    return await session.start()


def _stepfun_ws_tts_session_is_healthy(session: Any) -> bool:
    health_check = getattr(session, "is_healthy", None)
    if callable(health_check):
        return bool(health_check())
    if getattr(session, "error_detail", ""):
        return False
    receiver = getattr(session, "_receiver_task", None)
    if receiver is not None and hasattr(receiver, "done") and receiver.done():
        return False
    return True


def _stepfun_ws_tts_session_bind_turn(session: Any, turn_id: int, *, stream_id: int) -> None:
    bind_turn = getattr(session, "bind_turn", None)
    if callable(bind_turn):
        bind_turn(turn_id, stream_id=stream_id)
        return
    try:
        session.turn_id = turn_id
        session.stream_id = stream_id
    except Exception:
        pass


async def _stepfun_ws_wait_session_id(step_ws: Any, *, timeout: float) -> str:
    while True:
        event = _json_object(await asyncio.wait_for(step_ws.recv(), timeout=timeout))
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "tts.connection.done":
            session_id = str(data.get("session_id") or "").strip()
            if session_id:
                return session_id
            raise ValueError("missing StepFun session_id")
        if event_type == "tts.response.error":
            raise ValueError(_stepfun_error_detail(data))


async def _stepfun_ws_wait_created(step_ws: Any, *, timeout: float) -> None:
    while True:
        event = _json_object(await asyncio.wait_for(step_ws.recv(), timeout=timeout))
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "tts.response.created":
            return
        if event_type == "tts.response.error":
            raise ValueError(_stepfun_error_detail(data))


def _stepfun_ws_create_event(runtime_config: Any, session_id: str, *, sample_rate: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "session_id": session_id,
        "voice_id": str(getattr(runtime_config, "tts_voice", "") or "").strip() or "aura",
        "response_format": "pcm",
        "volume_ratio": STEPFUN_WS_TTS_VOLUME_RATIO,
        "text_normalization": STEPFUN_WS_TTS_TEXT_NORMALIZATION,
        "speed_ratio": STEPFUN_WS_TTS_SPEED_RATIO,
        "sample_rate": sample_rate,
        "mode": STEPFUN_WS_TTS_MODE,
        "markdown_filter": True,
    }
    instruction = STEPFUN_WS_TTS_INSTRUCTION
    if instruction and str(getattr(runtime_config, "tts_model", "") or "").strip() == "stepaudio-2.5-tts":
        data["instruction"] = instruction
    return {"type": "tts.create", "data": data}


def _stepfun_audio_delta(data: dict[str, Any]) -> bytes:
    raw = str(data.get("audio") or "").strip()
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=False)
    except (ValueError, TypeError):
        return b""


def _stepfun_realtime_audio_delta(event: dict[str, Any]) -> bytes:
    if not isinstance(event, dict):
        return b""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    candidates = [
        event.get("audio"),
        event.get("delta"),
        data.get("audio"),
        data.get("delta"),
    ]
    for value in candidates:
        raw = str(value or "").strip()
        if not raw:
            continue
        try:
            return base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            continue
    return b""


def _stepfun_realtime_text_delta(event: dict[str, Any]) -> str:
    if not isinstance(event, dict):
        return ""
    event_type = str(event.get("type") or event.get("event") or "").lower()
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for response in (event.get("response"), data.get("response")):
        text = _stepfun_realtime_text_from_response(response)
        if text:
            return text
    for item in (event.get("item"), data.get("item")):
        text = _stepfun_realtime_text_from_item(item)
        if text:
            return text
    if "text" not in event_type and "transcript" not in event_type and "content_part" not in event_type:
        return ""
    for value in (
        event.get("text"),
        event.get("delta"),
        event.get("transcript"),
        event.get("part"),
        event.get("content"),
        data.get("text"),
        data.get("delta"),
        data.get("transcript"),
        data.get("part"),
        data.get("content"),
    ):
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            text = _stepfun_realtime_text_from_item(value)
            if text:
                return text
    return ""


def _stepfun_realtime_text_from_response(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    output = value.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        text = _stepfun_realtime_text_from_item(item)
        if text:
            parts.append(text)
    return "".join(parts).strip()


def _stepfun_realtime_text_from_item(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("text", "transcript"):
        text = value.get(key)
        if isinstance(text, str) and text:
            return text
    content = value.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _stepfun_realtime_text_from_item(item)
                if text:
                    parts.append(text)
        return "".join(parts).strip()
    return ""


def stepfun_realtime_instructions(runtime_config: Any) -> str:
    language = normalize_conversation_language(getattr(runtime_config, "asr_language", "zh"))
    weather_line = ""
    if runtime_config is not None and cached_weather_snapshot is not None:
        try:
            weather = cached_weather_snapshot(runtime_config)
        except Exception:
            weather = {}
        display = str(weather.get("display") or "").strip() if isinstance(weather, dict) else ""
        status = str(weather.get("status") or "").strip() if isinstance(weather, dict) else ""
        if display:
            weather_line = f"当前可用天气缓存：{display}；状态：{status or 'unknown'}。"
    language_instruction = {
        "zh": "只用自然中文回答。",
        "en": "Reply only in natural English.",
        "ja": "自然な日本語だけで答えてください。",
    }[language]
    return (
        "你是 Lily/Aura 的实时语音人格，像在近距离通话。"
        f"{language_instruction}"
        "优先快：通常一句话，最多两小句；先回答核心问题。"
        "这是普通语音对话，不是翻译、复述、改写或朗读任务；除非用户明确要求翻译，否则绝对不要说“帮你翻译成英文”。"
        "如果用户问天气、温度或今天怎么样，必须直接根据已给的天气缓存回答；不要说“我来查/这就帮你查”。"
        "没有缓存时才说暂时没拿到实时天气。"
        f"{weather_line}"
        "不要输出括号动作、舞台提示、心理描写、Markdown 或项目符号。"
        "不要自称 AI、助手或模型。"
        "除非用户明确问你在哪/你那边/你今天在干嘛，不要主动提具体地点、商场、店铺或食物。"
        "如果没听清，就简短请用户再说一遍。"
    )


def _stepfun_error_detail(data: dict[str, Any]) -> str:
    code = str(data.get("code") or "").strip()
    message = str(data.get("message") or data.get("error") or "StepFun WS TTS error").strip()
    return f"{code} {message}".strip()


def _stepfun_ws_source_sample_rate() -> int:
    sample_rate = int(STEPFUN_WS_TTS_SAMPLE_RATE or DEVICE_SAMPLE_RATE)
    if sample_rate in {8000, 16000, 22050, 24000, 48000}:
        return sample_rate
    return DEVICE_SAMPLE_RATE


def _tts_source_sample_rate(runtime_config: Any) -> int:
    sample_rate = _coerce_int(getattr(runtime_config, "tts_sample_rate", 0), 0)
    provider = str(getattr(runtime_config, "tts_provider", "") or "").strip().lower()
    # NLS and Baidu PCM endpoints use documented 8/16 kHz output variants.
    if provider in {"aliyun-nls", "baidu"}:
        return 8_000 if sample_rate <= 8_000 else DEVICE_SAMPLE_RATE
    return sample_rate if sample_rate >= 8000 else DEVICE_SAMPLE_RATE


def _tts_audio_for_device(audio: bytes, *, source_rate: int) -> bytes:
    if source_rate == DEVICE_SAMPLE_RATE:
        return audio
    return resample_pcm16_mono(audio, source_rate=source_rate, target_rate=DEVICE_SAMPLE_RATE)


def device_spoken_text(text: str, *, allow_fallback: bool = True) -> str:
    if normalize_spoken_reply is not None:
        reply = normalize_spoken_reply(text)
        if reply.fallback_used and not allow_fallback:
            return ""
        return _dedupe_repeated_spoken_text(_strip_low_value_voice_openers(reply.text, drop_pure_filler=not allow_fallback))
    clean = _strip_stage_directions(str(text or ""))
    clean = re.sub(r"^\s*(Aura|Lily|AI|助手)\s*[:：]\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"[ \t\r\n]+", " ", clean).strip()
    clean = clean.strip(" -—:：")
    if not clean and not allow_fallback:
        return ""
    return _dedupe_repeated_spoken_text(_strip_low_value_voice_openers(clean or "我在。", drop_pure_filler=not allow_fallback))


def _strip_low_value_voice_openers(text: str, *, drop_pure_filler: bool = False) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if _starts_with_natural_repeated_opener(value):
        return value
    if drop_pure_filler and re.fullmatch(
        r"(?:嗯+咯?|嗯嗯|好哒|好呀|好啦|好咯|好嘛|是滴|晓得啦|那就聊聊|那就聊|聊聊|嘛|呢|呀|啊|唔|呃|额)(?:嘛|呢|呀|啊|吧|啦|咯)?[\s,，。.!！?？、;；:：]*",
        value,
    ):
        return ""
    hard_filler_re = re.compile(
        r"^(?:好哒|好呀|好啦|好咯|好嘛|是滴|晓得啦)"
        r"(?![呀哒啦咯嘛哦喔呦哟呢吧啊了])(?=.)"
        r"[\s,，。.!！?？、;；:：]*"
    )
    soft_filler_re = re.compile(
        r"^(?:唔|呃|啊|额|嘛|呢|呀|好的|好|可以|收到|晓得|那就聊聊|那就聊|聊聊)"
        r"(?:嘛|呢|呀|啊|吧|啦|咯)?"
        r"(?=[\s,，。.!！?？、;；:：]|我|咱|你|先|那|聊|说|从|不)"
        r"[\s,，。.!！?？、;；:：]*"
    )
    for _ in range(3):
        stripped = hard_filler_re.sub("", value, count=1).lstrip()
        if stripped == value:
            stripped = soft_filler_re.sub("", value, count=1).lstrip()
        if stripped == value:
            break
        if not _has_substantive_spoken_text(stripped):
            break
        value = stripped
    return value


def _starts_with_natural_repeated_opener(text: str) -> bool:
    return bool(re.match(r"^\s*(好|嗯|啊|哦|唔|呃|额)[,，、]\s*\1(?=[,，、])", str(text or "")))


def _has_substantive_spoken_text(text: str) -> bool:
    return bool(re.search(r"[\w\u3400-\u9fff]", str(text or "")))


def _dedupe_repeated_spoken_text(text: str) -> str:
    clean = str(text or "").strip()
    if len(clean) < 8:
        return clean
    sentence_deduped = _dedupe_repeated_spoken_sentences(clean)
    if sentence_deduped != clean:
        return sentence_deduped
    if len(clean) % 2:
        return clean
    half = len(clean) // 2
    left = clean[:half].strip()
    right = clean[half:].strip()
    return left if left and left == right else clean


def _dedupe_repeated_spoken_sentences(text: str) -> str:
    value = str(text or "").strip()
    parts = [part.strip() for part in re.split(r"([^。！？!?]+[。！？!?]?)", value) if part.strip()]
    if len(parts) <= 1:
        return value
    kept: list[str] = []
    keys: list[str] = []
    changed = False
    for part in parts:
        key = _compact_transcript_for_confidence(part)
        if not key:
            continue
        repeated = any(
            key == previous
            or (len(key) >= 8 and previous.endswith(key))
            or (len(previous) >= 8 and key.endswith(previous))
            for previous in keys
        )
        if repeated:
            changed = True
            continue
        kept.append(part)
        keys.append(key)
    return "".join(kept).strip() if changed else value


def _strip_stage_directions(text: str) -> str:
    value = _strip_bracketed_stage_directions(str(text or ""))

    def replace_starred(match: re.Match[str]) -> str:
        body = match.group(1)
        return " " if _looks_like_stage_direction(body) else match.group(0)

    value = re.sub(r"(?<!\w)\*([^*\n]{1,160})\*(?!\w)", replace_starred, value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _strip_bracketed_stage_directions(text: str) -> str:
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]"}
    openers = set(pairs)
    out: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch not in openers:
            out.append(ch)
            index += 1
            continue
        closer = pairs[ch]
        close_index = text.find(closer, index + 1)
        if close_index < 0:
            out.append(ch)
            index += 1
            continue
        body = text[index + 1:close_index]
        if _looks_like_stage_direction(body):
            out.append(" ")
        else:
            out.append(text[index:close_index + 1])
        index = close_index + 1
    return "".join(out)


def _looks_like_stage_direction(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if len(body) > 180:
        return False
    return any(hint in body for hint in STAGE_DIRECTION_HINTS)


def tts_text_chunks(text: str, *, max_chars: int = TTS_TEXT_CHUNK_CHARS) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", str(text or "").replace("\r", "\n")).strip()
    if not clean:
        return []
    raw_parts = re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", clean)
    parts = [part.strip() for part in raw_parts if part.strip()]
    if not parts:
        parts = [clean]
    chunks: list[str] = []
    limit = max(8, int(max_chars or TTS_TEXT_CHUNK_CHARS))
    for index, part in enumerate(parts):
        if index == 0:
            chunks.extend(_split_first_tts_part(part, first_max_chars=min(limit, TTS_FIRST_CHUNK_CHARS), max_chars=limit))
        else:
            chunks.extend(_split_long_tts_part(part, max_chars=limit))
    return chunks


def _split_first_tts_part(text: str, *, first_max_chars: int, max_chars: int) -> list[str]:
    value = str(text or "").strip()
    if len(value) <= first_max_chars:
        return [value] if value else []
    min_first_chars = min(max(2, TTS_FIRST_CHUNK_MIN_CHARS), first_max_chars)
    for index, ch in enumerate(value[:first_max_chars], start=1):
        if ch in "，,、 " and index >= min_first_chars:
            first = value[:index].strip()
            rest = value[index:].strip()
            return [first, *_split_long_tts_part(rest, max_chars=max_chars)] if rest else [first]
    return _split_long_tts_part(value, max_chars=first_max_chars)


def _split_long_tts_part(text: str, *, max_chars: int) -> list[str]:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return [value] if value else []
    chunks: list[str] = []
    remaining = value
    soft_breaks = "，,、 "
    while len(remaining) > max_chars:
        cut = max((remaining.rfind(mark, 0, max_chars + 1) for mark in soft_breaks), default=-1)
        if cut < max_chars // 2:
            cut = max_chars
        chunk = remaining[:cut + 1].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut + 1:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def call_bridge(config: GatewayConfig, state: TurnState, transcript: str = "") -> dict[str, Any]:
    goal = transcript.strip() or config.placeholder_goal
    payload = {
        "goal": goal,
        "metadata": bridge_metadata(state, transcript, streamed=False),
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if state.client_ip:
        headers["x-forwarded-for"] = state.client_ip
    req = request.Request(
        config.bridge_url,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(1.0, config.bridge_timeout_seconds)) as res:
            return json.loads(res.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "response": f"本地网关已启动，但 Aura bridge 暂不可用：{exc.__class__.__name__}",
        }


def bridge_metadata(state: TurnState, transcript: str, *, streamed: bool) -> dict[str, Any]:
    metadata = {
        "source": "aura-lily-gateway",
        "device_id": state.device_id,
        "boot_id": state.boot_id,
        "turn_id": state.turn_id,
        "language": state.language,
        "audio_bytes": state.audio_bytes,
        "asr_configured": bool(transcript.strip()),
        "transcript": transcript.strip(),
    }
    if streamed:
        metadata["streamed"] = True
    if state.client_ip:
        metadata["client_ip"] = state.client_ip
    if state.device_public_ip:
        metadata["device_public_ip"] = state.device_public_ip
    start_metadata = state.metadata if isinstance(state.metadata, dict) else {}
    user_geo = start_metadata.get("user_geo") if isinstance(start_metadata.get("user_geo"), dict) else {}
    if user_geo:
        metadata["user_geo"] = dict(user_geo)
    for key in ("device_public_ip", "public_ip", "wan_ip"):
        value = str(start_metadata.get(key) or "").strip()
        if value and "device_public_ip" not in metadata:
            metadata["device_public_ip"] = value.split(",", 1)[0].strip()
    return metadata


def should_stream_bridge(runtime_config: Any) -> bool:
    if not BRIDGE_STREAM_ENABLED:
        return False
    if not runtime_config:
        return False
    return str(getattr(runtime_config, "aura_model_mode", "") or "").strip() in {"aura_model", "direct_llm"}


async def stream_dialogue_and_tts_from_bridge(
    websocket: Any,
    config: GatewayConfig,
    runtime_config: Any,
    state: TurnState,
    transcript: str,
    *,
    preface: TtsResult | None = None,
    prefetched_events: list[dict[str, Any]] | None = None,
    prefetched_event_source: AsyncIterator[dict[str, Any]] | None = None,
) -> bool:
    bridge_started = time.monotonic()
    stream_turn_id = state.turn_id
    first_delta_ms = 0
    raw_response = ""
    pending_text = ""
    deferred_local_preface = ""
    deferred_local_voice_reply = ""
    local_audio_id = ""
    final_payload: dict[str, Any] | None = None
    tts_results: list[TtsResult] = []
    sent_any_audio = False
    first_segment_http_used = False
    saw_event = False
    first_tts_started_ms = 0
    final_tts_frame_sent = False
    tts_segment_count = 0
    tts_text_chars = 0
    queued_tts_texts: list[str] = []
    tts_queue: asyncio.Queue[StreamTtsItem | None] = asyncio.Queue()
    tts_semaphore = asyncio.Semaphore(max(1, TTS_PREFETCH_CONCURRENCY))
    stepfun_warm_task = state.stepfun_tts_warm_task if STEPFUN_WS_TTS_WARM_ENABLED else None
    if stepfun_warm_task is not None:
        state.stepfun_tts_warm_task = None
        state.stepfun_tts_warm_started_at = 0.0
    stepfun_tts_task: asyncio.Task[StepfunWsTtsSession] | None = (
        stepfun_warm_task
        if stepfun_warm_task is not None
        else (
            asyncio.create_task(start_stepfun_ws_tts_session(
                websocket,
                runtime_config,
                stream_turn_id,
                stream_id=1,
                started=bridge_started,
            ))
            if stepfun_ws_tts_available(runtime_config)
            else None
        )
    )
    state.stream_tts_turn_id = stream_turn_id
    state.stream_tts_stepfun_task = stepfun_tts_task
    state.stream_tts_stepfun_session = None
    state.stream_tts_tasks = []
    stepfun_tts_session: StepfunWsTtsSession | None = None
    stepfun_session_failed = False
    stepfun_tts_warmed = bool(stepfun_warm_task)
    ws_tts_eager_followup_limit = (
        BRIDGE_STREAM_WS_TTS_CHUNK_CHARS
        if stepfun_ws_tts_available(runtime_config)
        else None
    )

    async def cleanup_stream_tts_on_cancel() -> None:
        if state.stream_tts_turn_id != stream_turn_id:
            return
        for task in list(state.stream_tts_tasks):
            if not task.done():
                task.cancel()
        for task in list(state.stream_tts_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if tts_sender_task is not None and not tts_sender_task.done():
            tts_sender_task.cancel()
            try:
                await tts_sender_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if stepfun_tts_session is not None:
            try:
                await stepfun_tts_session.close()
            except Exception:
                pass
        elif stepfun_tts_task is not None:
            if not stepfun_tts_task.done():
                stepfun_tts_task.cancel()
            try:
                session = await stepfun_tts_task
            except asyncio.CancelledError:
                session = None
            except Exception:
                session = None
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
        clear_stream_tts_resource_refs(state, owner_turn_id=stream_turn_id)

    def stream_turn_is_current() -> bool:
        return state.turn_id == stream_turn_id

    async def synthesize_stream_segment(text: str) -> TtsResult:
        async with tts_semaphore:
            return await asyncio.to_thread(synthesize_tts, runtime_config, text)

    async def queue_stream_segment(segment: str, *, is_final: bool, source: str = "") -> bool:
        nonlocal first_tts_started_ms, tts_segment_count, tts_text_chars
        spoken_segment = device_spoken_text(segment, allow_fallback=False)
        if not spoken_segment:
            return False
        tts_segment_count += 1
        tts_text_chars += len(spoken_segment)
        queued_tts_texts.append(spoken_segment)
        queued_ms = _elapsed_ms(bridge_started)
        if tts_segment_count == 1:
            first_tts_started_ms = queued_ms
        task = None if stepfun_ws_tts_available(runtime_config) else asyncio.create_task(synthesize_stream_segment(spoken_segment))
        if task is not None:
            state.stream_tts_tasks.append(task)
        await tts_queue.put(StreamTtsItem(
            index=tts_segment_count,
            text=spoken_segment,
            is_final=is_final,
            queued_ms=queued_ms,
            task=task,
            source=source,
        ))
        log_gateway(
            "bridge_stream_tts_queued",
            turn_id=stream_turn_id,
            index=tts_segment_count,
            chars=len(spoken_segment),
            queued_ms=queued_ms,
            final=is_final,
            source=source,
            prefetch_concurrency=max(1, TTS_PREFETCH_CONCURRENCY),
        )
        return True

    async def send_queued_tts() -> None:
        nonlocal sent_any_audio, final_tts_frame_sent, stepfun_tts_session, stepfun_session_failed, stepfun_tts_task, stepfun_tts_warmed, first_segment_http_used
        while True:
            item = await tts_queue.get()
            if not stream_turn_is_current():
                log_gateway("bridge_stream_tts_stale_drop", turn_id=stream_turn_id, current_turn_id=state.turn_id)
                return
            if item is None:
                if stepfun_tts_session is not None:
                    result = await stepfun_tts_session.finish(is_final=True)
                    tts_session_offset_ms = (
                        _elapsed_ms(stepfun_tts_session.started) - _elapsed_ms(bridge_started)
                        if getattr(stepfun_tts_session, "started", 0)
                        else 0
                    )
                    first_audio_since_bridge = (
                        max(0, stepfun_tts_session.first_audio_abs_ms - tts_session_offset_ms)
                        if stepfun_tts_session.first_audio_abs_ms
                        else 0
                    )
                    tts_results.append(result)
                    sent_any_audio = sent_any_audio or result.audio_bytes > 0
                    final_tts_frame_sent = final_tts_frame_sent or result.audio_bytes > 0
                    if not result.ok and not sent_any_audio:
                        await send_tts_failure(websocket, stream_turn_id, result, stream_id=1)
                        final_tts_frame_sent = True
                    log_gateway(
                        "bridge_stream_tts_sent",
                        turn_id=stream_turn_id,
                        index="all",
                        chars=tts_text_chars,
                        synth_ms=result.latency_ms,
                        first_audio_ms=result.first_audio_ms,
                        first_audio_since_bridge_ms=first_audio_since_bridge,
                        audio_bytes=result.audio_bytes,
                        audio_chunks=result.audio_chunk_count,
                        **tts_chunk_timing_payload(result),
                        final=True,
                        provider_stream="stepfun_ws_session",
                    )
                elif stepfun_tts_task is not None:
                    if not stepfun_tts_task.done():
                        stepfun_tts_task.cancel()
                    try:
                        maybe_session = await stepfun_tts_task
                    except asyncio.CancelledError:
                        maybe_session = None
                    except Exception:
                        maybe_session = None
                    if maybe_session is not None:
                        await maybe_session.close()
                    stepfun_tts_task = None
                    state.stream_tts_stepfun_task = None
                return
            started_ms = item.queued_ms
            if (
                item.index == 1
                and stepfun_tts_task is not None
                and stepfun_tts_session is None
                and not stepfun_session_failed
                and STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS > 0
                and STEPFUN_WS_TTS_FIRST_SEGMENT_HTTP_POLICY == "auto"
            ):
                try:
                    stepfun_tts_session = await asyncio.wait_for(
                        asyncio.shield(stepfun_tts_task),
                        timeout=STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS / 1000,
                    )
                    if _stepfun_ws_tts_session_is_healthy(stepfun_tts_session):
                        _stepfun_ws_tts_session_bind_turn(stepfun_tts_session, stream_turn_id, stream_id=1)
                        state.stream_tts_stepfun_session = stepfun_tts_session
                        if stepfun_tts_warmed:
                            log_gateway(
                                "stepfun_ws_tts_warm_ready",
                                turn_id=stream_turn_id,
                                warm_wait_ms=_elapsed_ms(bridge_started),
                                warm_age_ms=(
                                    _elapsed_ms(stepfun_tts_session.started)
                                    if getattr(stepfun_tts_session, "started", 0)
                                    else 0
                                ),
                            )
                    else:
                        detail = getattr(stepfun_tts_session, "error_detail", "") or "receiver_stopped"
                        await stepfun_tts_session.close()
                        stepfun_tts_session = None
                        state.stream_tts_stepfun_session = None
                        raise RuntimeError(f"StepFun WS TTS session unhealthy: {detail}")
                except asyncio.TimeoutError:
                    log_gateway(
                        "bridge_stream_tts_first_segment_http",
                        turn_id=stream_turn_id,
                        index=item.index,
                        chars=len(item.text),
                        queued_ms=item.queued_ms,
                        wait_ms=STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS,
                        reason="ws_not_ready",
                    )
                    try:
                        result = await synthesize_and_stream_tts(
                            websocket,
                            runtime_config,
                            stream_turn_id,
                            item.text,
                            stream_id=1,
                            is_final=False,
                            allow_stepfun_ws=False,
                            dialogue_segment_index=item.index - 1,
                            dialogue_segment_text=item.text,
                        )
                    except Exception as exc:
                        result = TtsResult(ok=False, detail=exc.__class__.__name__)
                    if result.ok and result.audio_bytes:
                        sent_any_audio = True
                        first_segment_http_used = True
                        tts_results.append(result)
                        log_gateway(
                            "bridge_stream_tts_sent",
                            turn_id=stream_turn_id,
                            index=item.index,
                            chars=len(item.text),
                            synth_ms=result.latency_ms,
                            first_audio_ms=result.first_audio_ms,
                            first_audio_since_bridge_ms=_elapsed_ms(bridge_started),
                            audio_bytes=result.audio_bytes,
                            audio_chunks=result.audio_chunk_count,
                            **tts_chunk_timing_payload(result),
                            final=False,
                            provider_stream="http_first_segment",
                        )
                        continue
                except Exception as exc:
                    log_gateway(
                        "bridge_stream_tts_first_segment_ws_unavailable",
                        turn_id=stream_turn_id,
                        index=item.index,
                        detail=exc.__class__.__name__,
                    )
            if (
                item.index == 1
                and STEPFUN_WS_TTS_FIRST_SEGMENT_HTTP_POLICY == "always"
            ):
                log_gateway(
                    "bridge_stream_tts_first_segment_http",
                    turn_id=stream_turn_id,
                    index=item.index,
                    chars=len(item.text),
                    queued_ms=item.queued_ms,
                    wait_ms=0,
                    reason="policy_always",
                )
                try:
                    result = await synthesize_and_stream_tts(
                        websocket,
                        runtime_config,
                        stream_turn_id,
                        item.text,
                        stream_id=1,
                        is_final=False,
                        allow_stepfun_ws=False,
                        dialogue_segment_index=item.index - 1,
                        dialogue_segment_text=item.text,
                    )
                except Exception as exc:
                    result = TtsResult(ok=False, detail=exc.__class__.__name__)
                if result.ok and result.audio_bytes:
                    sent_any_audio = True
                    first_segment_http_used = True
                    tts_results.append(result)
                    log_gateway(
                        "bridge_stream_tts_sent",
                        turn_id=stream_turn_id,
                        index=item.index,
                        chars=len(item.text),
                        synth_ms=result.latency_ms,
                        first_audio_ms=result.first_audio_ms,
                        first_audio_since_bridge_ms=_elapsed_ms(bridge_started),
                        audio_bytes=result.audio_bytes,
                        audio_chunks=result.audio_chunk_count,
                        **tts_chunk_timing_payload(result),
                        final=False,
                        provider_stream="http_first_segment",
                    )
                    continue
            if stepfun_tts_task is not None and not stepfun_session_failed:
                try:
                    if stepfun_tts_session is None:
                        stepfun_tts_session = await stepfun_tts_task
                        if not _stepfun_ws_tts_session_is_healthy(stepfun_tts_session):
                            detail = getattr(stepfun_tts_session, "error_detail", "") or "receiver_stopped"
                            await stepfun_tts_session.close()
                            if stepfun_tts_warmed:
                                log_gateway(
                                    "stepfun_ws_tts_warm_discard",
                                    turn_id=stream_turn_id,
                                    reason="adopt",
                                    status="unhealthy",
                                    detail=detail,
                                )
                                stepfun_tts_task = asyncio.create_task(start_stepfun_ws_tts_session(
                                    websocket,
                                    runtime_config,
                                    stream_turn_id,
                                    stream_id=1,
                                    started=bridge_started,
                                ))
                                state.stream_tts_stepfun_task = stepfun_tts_task
                                stepfun_tts_warmed = False
                                stepfun_tts_session = await stepfun_tts_task
                            else:
                                stepfun_tts_session = None
                                state.stream_tts_stepfun_session = None
                                raise RuntimeError(f"StepFun WS TTS session unhealthy: {detail}")
                        if not _stepfun_ws_tts_session_is_healthy(stepfun_tts_session):
                            detail = getattr(stepfun_tts_session, "error_detail", "") or "receiver_stopped"
                            await stepfun_tts_session.close()
                            state.stream_tts_stepfun_session = None
                            raise RuntimeError(f"StepFun WS TTS session unhealthy: {detail}")
                        _stepfun_ws_tts_session_bind_turn(stepfun_tts_session, stream_turn_id, stream_id=1)
                        state.stream_tts_stepfun_session = stepfun_tts_session
                        if stepfun_tts_warmed:
                            log_gateway(
                                "stepfun_ws_tts_warm_ready",
                                turn_id=stream_turn_id,
                                warm_wait_ms=_elapsed_ms(bridge_started),
                                warm_age_ms=(
                                    _elapsed_ms(stepfun_tts_session.started)
                                    if getattr(stepfun_tts_session, "started", 0)
                                    else 0
                                ),
                            )
                    await stepfun_tts_session.send_text(item.text)
                    text_sent_ms = _elapsed_ms(bridge_started)
                    if (
                        STEPFUN_WS_TTS_WAIT_FIRST_AUDIO
                        and item.index == 1
                        and STEPFUN_WS_TTS_FIRST_AUDIO_WAIT_MS > 0
                    ):
                        try:
                            await asyncio.wait_for(
                                stepfun_tts_session.first_audio_event.wait(),
                                timeout=STEPFUN_WS_TTS_FIRST_AUDIO_WAIT_MS / 1000,
                            )
                        except asyncio.TimeoutError:
                            pass
                    log_gateway(
                        "bridge_stream_tts_ws_text",
                        turn_id=stream_turn_id,
                        index=item.index,
                        chars=len(item.text),
                        queued_ms=item.queued_ms,
                        sent_ms=text_sent_ms,
                        after_wait_ms=_elapsed_ms(bridge_started),
                        final=item.is_final,
                        provider_stream="stepfun_ws_session",
                    )
                    continue
                except Exception as exc:
                    stepfun_session_failed = True
                    stepfun_tts_task = None
                    if stepfun_tts_session is not None:
                        await stepfun_tts_session.close()
                        stepfun_tts_session = None
                        state.stream_tts_stepfun_session = None
                    detail = _ws_exception_detail("StepFun WS TTS", exc)
                    if not STEPFUN_WS_TTS_FALLBACK_HTTP:
                        result = TtsResult(ok=False, detail=detail, latency_ms=_elapsed_ms(bridge_started))
                    else:
                        log_gateway(
                            "stepfun_ws_tts_fallback_http",
                            turn_id=stream_turn_id,
                            detail=detail,
                            latency_ms=_elapsed_ms(bridge_started),
                            streamed_http=True,
                        )
                        try:
                            result = await synthesize_and_stream_tts(
                                websocket,
                                runtime_config,
                                stream_turn_id,
                                item.text,
                                stream_id=1,
                                is_final=item.is_final,
                                allow_stepfun_ws=False,
                                dialogue_segment_index=item.index - 1,
                                dialogue_segment_text=item.text,
                            )
                        except Exception as exc2:  # pragma: no cover - defensive boundary for provider/runtime failures
                            result = TtsResult(ok=False, detail=exc2.__class__.__name__)
                        if result.ok and result.audio_bytes:
                            sent_any_audio = True
                            final_tts_frame_sent = final_tts_frame_sent or item.is_final
                            tts_results.append(result)
                            log_gateway(
                                "bridge_stream_tts_sent",
                                turn_id=stream_turn_id,
                                index=item.index,
                                chars=len(item.text),
                                synth_ms=result.latency_ms,
                                first_audio_ms=result.first_audio_ms,
                                first_audio_since_bridge_ms=_elapsed_ms(bridge_started),
                                audio_bytes=result.audio_bytes,
                                audio_chunks=result.audio_chunk_count,
                                **tts_chunk_timing_payload(result),
                                final=item.is_final,
                                provider_stream="http_fallback_stream",
                            )
                            continue
            else:
                try:
                    result = await (item.task or asyncio.create_task(synthesize_stream_segment(item.text)))
                except Exception as exc:  # pragma: no cover - defensive boundary for provider/runtime failures
                    result = TtsResult(ok=False, detail=exc.__class__.__name__)
            if not stream_turn_is_current():
                log_gateway("bridge_stream_tts_stale_drop", turn_id=stream_turn_id, current_turn_id=state.turn_id)
                return
            if not result.ok or not result.audio:
                failed = TtsResult(
                    ok=False,
                    detail=f"segment {item.index}: {result.detail or 'empty audio'}",
                    chunk_count=result.chunk_count,
                    audio_chunk_count=sum(max(0, done.audio_chunk_count) for done in tts_results),
                    audio_bytes=sum(max(0, done.audio_bytes) for done in tts_results),
                    latency_ms=max(0, _elapsed_ms(bridge_started) - started_ms),
                    first_chunk_ms=result.first_chunk_ms,
                    first_audio_ms=0,
                    source_sample_rate=result.source_sample_rate,
                    device_sample_rate=DEVICE_SAMPLE_RATE,
                    streamed=True,
                    **combined_tts_result_timing_fields(tts_results),
                )
                tts_results.append(failed)
                await send_tts_failure(websocket, stream_turn_id, failed, stream_id=1)
                final_tts_frame_sent = True
                log_gateway(
                    "bridge_stream_tts_failed",
                    turn_id=stream_turn_id,
                    index=item.index,
                    detail=failed.detail,
                )
                return

            frame_count = (len(result.audio) + TTS_CHUNK_BYTES - 1) // TTS_CHUNK_BYTES
            audio_timing = AudioChunkTiming(bridge_started)
            await send_dialogue_segment_timing(
                websocket,
                turn_id=stream_turn_id,
                segment_index=item.index - 1,
                text=item.text,
                audio_bytes=len(result.audio),
            )
            await send_tts_pcm_stream(
                websocket,
                stream_turn_id,
                result.audio,
                stream_id=1,
                is_final=item.is_final,
                timing=audio_timing,
            )
            first_audio_ms = max(0, audio_timing.first_audio_ms - started_ms)
            final_tts_frame_sent = final_tts_frame_sent or item.is_final
            sent_any_audio = True
            streamed_result = TtsResult(
                ok=True,
                audio=b"",
                detail=result.detail,
                chunk_count=result.chunk_count,
                audio_chunk_count=frame_count,
                audio_bytes=len(result.audio),
                latency_ms=max(0, _elapsed_ms(bridge_started) - started_ms),
                first_chunk_ms=result.first_chunk_ms or result.latency_ms,
                first_audio_ms=first_audio_ms,
                source_sample_rate=result.source_sample_rate,
                device_sample_rate=DEVICE_SAMPLE_RATE,
                streamed=True,
                **audio_chunk_timing_summary(audio_timing),
            )
            tts_results.append(streamed_result)
            log_gateway(
                "bridge_stream_tts_sent",
                turn_id=stream_turn_id,
                index=item.index,
                chars=len(item.text),
                synth_ms=result.latency_ms,
                first_audio_ms=first_audio_ms,
                first_audio_since_bridge_ms=_elapsed_ms(bridge_started),
                audio_bytes=len(result.audio),
                audio_chunks=frame_count,
                **tts_chunk_timing_payload(streamed_result),
                final=item.is_final,
            )

    tts_sender_task = asyncio.create_task(send_queued_tts())
    state.stream_tts_sender_task = tts_sender_task

    try:
        async def event_source() -> AsyncIterator[dict[str, Any]]:
            for prefetched in prefetched_events or []:
                yield prefetched
            if prefetched_event_source is not None:
                async for prefetched_live in prefetched_event_source:
                    yield prefetched_live
            elif not prefetched_events:
                async for streamed_event in bridge_stream_events(config, state, transcript):
                    yield streamed_event

        async for event in event_source():
            saw_event = True
            event_type = str(event.get("type") or "")
            if event_type == "delta":
                delta = str(event.get("text") or "")
                if not delta:
                    continue
                if not first_delta_ms:
                    first_delta_ms = _elapsed_ms(bridge_started)
                    await send_json(websocket, {
                        "type": "status",
                        "text": "开始流式回复",
                        "payload": {"turn_id": stream_turn_id, "first_delta_ms": first_delta_ms},
                    })
                raw_response += delta
                event_source = str(event.get("source") or "")
                if event_source in {"local_preface", "local_voice_reply"}:
                    spoken_delta = device_spoken_text(delta, allow_fallback=False)
                    if event_source == "local_voice_reply" and state.supports_local_audio:
                        deferred_local_voice_reply += delta
                        continue
                    should_defer_local_preface = (
                        event_source == "local_preface"
                        and not tts_segment_count
                        and not pending_text.strip()
                        and BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS > 0
                        and 0 < len(spoken_delta) <= BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS
                    )
                    if should_defer_local_preface:
                        deferred_local_preface += delta
                        continue
                    if pending_text.strip():
                        for final_segment in flush_stream_tts_segments(pending_text):
                            await queue_stream_segment(final_segment, is_final=False)
                        pending_text = ""
                    await queue_stream_segment(delta, is_final=False, source=event_source)
                    await asyncio.sleep(0)
                    continue
                if deferred_local_preface:
                    delta = merge_deferred_local_preface_for_tts(deferred_local_preface, delta)
                    deferred_local_preface = ""
                pending_text += delta
                if stream_bridge_response_requires_guard(
                    pending_text,
                    transcript=transcript,
                ):
                    continue
                if stream_bridge_should_wait_first_sentence(
                    pending_text,
                    transcript=transcript,
                    first_segment=tts_segment_count == 0,
                ):
                    continue
                while True:
                    segment, pending_text = pop_stream_tts_segment(
                        pending_text,
                        force=False,
                        first_segment=tts_segment_count == 0,
                        require_sentence_end=stream_bridge_requires_complete_first_sentence(
                            transcript=transcript,
                            first_segment=tts_segment_count == 0,
                        ),
                        followup_limit_chars=ws_tts_eager_followup_limit,
                    )
                    if not segment:
                        break
                    if stream_bridge_response_requires_guard(
                        segment,
                        transcript=transcript,
                    ):
                        continue
                    await queue_stream_segment(segment, is_final=False)
                continue
            if event_type == "final":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                final_payload = dict(payload)
                continue
            if event_type == "error":
                final_payload = {
                    "ok": False,
                    "status": "failed",
                    "response": str(event.get("response") or event.get("error") or "流式回复失败了，你再试一次。"),
                    "evidence": {"streamed": True, "stream_error": str(event.get("error") or "")},
                }
    except asyncio.CancelledError:
        await cleanup_stream_tts_on_cancel()
        raise

    if not saw_event:
        await tts_queue.put(None)
        await tts_sender_task
        clear_stream_tts_resource_refs(state, owner_turn_id=stream_turn_id)
        clear_bridge_speculative_adoption(state)
        return False

    state.bridge_latency_ms = _elapsed_ms(bridge_started)
    evidence = (final_payload or {}).get("evidence") if isinstance((final_payload or {}).get("evidence"), dict) else {}
    if bool(evidence.get("silent")) or str((final_payload or {}).get("status") or "") == "ignored":
        await tts_queue.put(None)
        await tts_sender_task
        timing_breakdown = voice_latency_breakdown(
            state,
            bridge_first_delta_ms=first_delta_ms,
            tts_first_audio_since_bridge_ms=0,
        )
        log_gateway(
            "turn_audio_timing",
            turn_id=stream_turn_id,
            status="ignored",
            reason="silent_drop",
            asr_ms=state.asr_latency_ms,
            streaming_asr_first_delta_ms=state.streaming_asr_first_delta_ms,
            streaming_asr_final_ms=state.streaming_asr_final_ms,
            streaming_asr_final_reason=state.streaming_asr_final_reason,
            streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
            streaming_asr_forwarded_frames=state.streaming_asr_forwarded_frames,
            **asr_diagnostic_payload(state),
            **streaming_asr_quality_gate_payload(state),
            **turn_trigger_payload(state),
            **streaming_asr_prefetch_log_fields(state),
            **bridge_speculative_log_fields(state),
            bridge_ms=state.bridge_latency_ms,
            bridge_first_delta_ms=first_delta_ms,
            **timing_breakdown,
            streamed_bridge=True,
        )
        await send_json(websocket, {
            "type": "system",
            "payload": {
                "action": "turn_silent_drop",
                "status": "ignored",
                "turn_id": stream_turn_id,
                "reason": "silent_drop",
                "asr_ms": state.asr_latency_ms,
                **asr_diagnostic_payload(state),
                "bridge_ms": state.bridge_latency_ms,
                "bridge_first_delta_ms": first_delta_ms,
                **streaming_asr_quality_gate_payload(state),
                **turn_trigger_payload(state),
                **bridge_speculative_payload(state),
                **timing_breakdown,
                "streamed_bridge": True,
            },
        })
        clear_stream_tts_resource_refs(state, owner_turn_id=stream_turn_id)
        clear_bridge_speculative_adoption(state)
        return True
    response = str((final_payload or {}).get("response") or raw_response).strip()
    fallback_response = stream_bridge_fallback_response(transcript)
    queued_response = "".join(queued_tts_texts).strip()
    quality_stop_reason = str(evidence.get("stop_reason") or "").strip()
    prefer_terminal_quality_response = bool(
        final_payload
        and quality_stop_reason == "voice_quality_guard_after_partial"
        and response
        and not stream_bridge_response_requires_guard(
            response,
            transcript=transcript,
        )
    )
    if (
        queued_response
        and not stream_bridge_response_requires_guard(
            queued_response,
            transcript=transcript,
        )
        and not prefer_terminal_quality_response
        and (not final_payload or not _text_prefix_equivalent(response, queued_response))
    ):
        response = queued_response
    if (not final_payload and stream_bridge_response_requires_guard(
        response,
        transcript=transcript,
    )) or (
        final_payload and stream_bridge_response_requires_guard(
            response,
            transcript=transcript,
        )
    ):
        response = (
            queued_response
            if queued_response and not stream_bridge_response_requires_guard(
                queued_response,
                transcript=transcript,
            )
            else fallback_response
        )
    if not response:
        response = "回复生成失败了，你再试一次。"
    spoken_response = device_spoken_text(response)
    if state.supports_local_audio:
        localized = match_local_audio(spoken_response, state.language)
        if localized:
            spoken_response, local_audio_id = localized
            deferred_local_voice_reply = ""
        elif deferred_local_voice_reply:
            pending_text = spoken_response if not tts_segment_count else deferred_local_voice_reply
            deferred_local_voice_reply = ""
    if deferred_local_preface:
        pending_text = merge_deferred_local_preface_for_tts(deferred_local_preface, pending_text or spoken_response)
        deferred_local_preface = ""
    final_segments = flush_stream_tts_segments(pending_text)
    if (
        final_segments
        and stream_bridge_response_requires_guard(
            "".join(final_segments),
            transcript=transcript,
        )
    ):
        final_segments = []
        if tts_segment_count == 0:
            spoken_response = fallback_response
    if local_audio_id:
        final_segments = []
    elif final_segments:
        for index, final_segment in enumerate(final_segments, start=1):
            await queue_stream_segment(final_segment, is_final=index == len(final_segments))
    elif (
        tts_segment_count > 0
        and prefer_terminal_quality_response
        and not _text_prefix_equivalent(spoken_response, queued_response)
    ):
        await queue_stream_segment(spoken_response, is_final=True, source="quality_fallback")
    elif tts_segment_count == 0:
        await queue_stream_segment(spoken_response, is_final=True)

    ok = bool((final_payload or {}).get("ok", True))
    request_id = str((final_payload or {}).get("request_id") or "")
    voice_turn_payload = (
        (final_payload or {}).get("voice_turn")
        if isinstance((final_payload or {}).get("voice_turn"), dict)
        else None
    )
    quality_trace = voice_quality_trace_payload(
        transcript=transcript,
        response=spoken_response,
        evidence=evidence,
        voice_turn=voice_turn_payload,
    )
    persona_prompt_trace = persona_prompt_trace_payload(final_payload)
    # 字幕先行：dialogue 文本要赶在 TTS 合成收尾之前送到设备。
    # 设备侧收到后只缓冲文字，等音频真正开播才上屏；之前放在
    # await tts_sender_task 之后发，文字常常比语音晚一整句。
    await send_json(websocket, {
        "type": "dialogue",
        "payload": {
            "turn_id": stream_turn_id,
            "request_id": request_id,
            "text": spoken_response,
            "segments": dialogue_segments_from_parts(queued_tts_texts) or dialogue_segments(spoken_response),
            "pose": voice_turn_pose(voice_turn_payload),
            "scene": dialogue_scene(final_payload),
            "coins_earned": 0,
            "deferred": bool(evidence.get("deferred")),
            "continue_listening": continue_listening_for_device(state),
            **({"local_audio_id": local_audio_id} if local_audio_id else {}),
            "timing": {
                "asr_ms": state.asr_latency_ms,
                "streaming_asr_first_delta_ms": state.streaming_asr_first_delta_ms,
                "streaming_asr_final_ms": state.streaming_asr_final_ms,
                "streaming_asr_final_reason": state.streaming_asr_final_reason,
                **asr_diagnostic_payload(state),
                **streaming_asr_quality_gate_payload(state),
                **turn_trigger_payload(state),
                **streaming_asr_prefetch_payload(state),
                **bridge_speculative_payload(state),
                **quality_trace,
                **persona_prompt_trace,
                "bridge_ms": state.bridge_latency_ms,
                "first_delta_ms": first_delta_ms,
                "streamed": True,
            },
        },
    })
    # 流式回合同样在对话文本落地后推一次数值状态。
    start_status_update_task(websocket, runtime_config)

    await tts_queue.put(None)
    await tts_sender_task
    if sent_any_audio and not final_tts_frame_sent:
        if stream_turn_is_current():
            await send_tts_binary(websocket, stream_turn_id, b"", stream_id=1, is_final=True)
        final_tts_frame_sent = True
    else:
        final_tts_frame_sent = final_tts_frame_sent or not sent_any_audio
    first_tts = next((item for item in tts_results if item.first_audio_ms), None)
    tts_first_audio_ms = first_tts.first_audio_ms if first_tts else 0
    tts_first_chunk_ms = first_tts.first_chunk_ms if first_tts else 0
    http_first_audio_since_bridge_ms = (
        first_tts_started_ms + tts_first_audio_ms
        if first_tts is not None and tts_first_audio_ms
        else 0
    )
    tts_provider_stream = "stepfun_ws_session" if stepfun_tts_session is not None else ""
    tts_session_offset_ms = (
        _elapsed_ms(stepfun_tts_session.started) - _elapsed_ms(bridge_started)
        if stepfun_tts_session is not None and getattr(stepfun_tts_session, "started", 0)
        else 0
    )
    ws_tts_first_text_ms = (
        max(0, stepfun_tts_session.first_text_ms - tts_session_offset_ms)
        if stepfun_tts_session is not None
        else 0
    )
    ws_tts_first_audio_since_bridge_ms = (
        max(0, stepfun_tts_session.first_audio_abs_ms - tts_session_offset_ms)
        if stepfun_tts_session is not None and stepfun_tts_session.first_audio_abs_ms
        else 0
    )
    tts_first_text_ms = _min_nonzero_or_zero(
        first_tts_started_ms if first_tts is not None else None,
        ws_tts_first_text_ms if ws_tts_first_text_ms else None,
    )
    tts_first_audio_since_bridge_ms = _min_positive(
        http_first_audio_since_bridge_ms,
        ws_tts_first_audio_since_bridge_ms,
    )
    if stepfun_tts_session is not None and first_segment_http_used:
        tts_provider_stream = "http_first_segment+stepfun_ws_session"
    elif first_segment_http_used:
        tts_provider_stream = "http_first_segment"
    tts_ws_text_count = stepfun_tts_session.text_count if stepfun_tts_session is not None else 0
    tts_ws_audio_chunks = stepfun_tts_session.audio_chunk_count if stepfun_tts_session is not None else 0
    first_audio_since_bridge_ms = (
        tts_first_audio_since_bridge_ms
        if tts_first_audio_since_bridge_ms
        else first_tts_started_ms + tts_first_audio_ms
        if first_tts is not None and tts_first_audio_ms
        else 0
    )
    timing_breakdown = voice_latency_breakdown(
        state,
        bridge_first_delta_ms=first_delta_ms,
        tts_first_audio_since_bridge_ms=first_audio_since_bridge_ms,
    )
    first_text_to_first_audio_ms = (
        max(0, first_audio_since_bridge_ms - tts_first_text_ms)
        if first_audio_since_bridge_ms and tts_first_text_ms
        else 0
    )
    first_delta_to_first_text_ms = (
        max(0, tts_first_text_ms - first_delta_ms)
        if tts_first_text_ms and first_delta_ms
        else 0
    )
    tts_total_ms = sum(max(0, item.latency_ms) for item in tts_results)
    tts_chunk_count = sum(max(0, item.chunk_count) for item in tts_results)
    tts_gap_payload = combine_tts_chunk_timing_payload(tts_results)
    diagnosis = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=first_delta_ms,
        tts_first_text_ms=tts_first_text_ms,
        tts_first_audio_since_bridge_ms=first_audio_since_bridge_ms,
        tts_first_text_to_audio_ms=first_text_to_first_audio_ms,
        audio_send_realtime_x100=int(tts_gap_payload.get("tts_audio_send_realtime_x100") or 0),
        audio_chunk_stall_count=int(tts_gap_payload.get("tts_audio_chunk_stall_count") or 0),
        streamed_bridge=True,
        tts_provider_stream=tts_provider_stream,
    )
    log_gateway(
        "turn_audio_timing",
        turn_id=stream_turn_id,
        status="ok" if sent_any_audio or local_audio_id else "failed",
        asr_ms=state.asr_latency_ms,
        streaming_asr_first_delta_ms=state.streaming_asr_first_delta_ms,
        streaming_asr_final_ms=state.streaming_asr_final_ms,
        streaming_asr_final_reason=state.streaming_asr_final_reason,
        streaming_asr_audio_bytes=state.streaming_asr_audio_bytes,
        streaming_asr_forwarded_frames=state.streaming_asr_forwarded_frames,
        **asr_diagnostic_payload(state),
        **streaming_asr_quality_gate_payload(state),
        **turn_trigger_payload(state),
        **streaming_asr_prefetch_log_fields(state),
        **bridge_speculative_log_fields(state),
        **quality_trace,
        **persona_prompt_trace,
        bridge_ms=state.bridge_latency_ms,
        bridge_first_delta_ms=first_delta_ms,
        **timing_breakdown,
        **diagnosis,
        tts_provider_stream=tts_provider_stream,
        tts_first_text_ms=tts_first_text_ms,
        tts_first_chunk_ms=tts_first_chunk_ms,
        tts_first_audio_ms=tts_first_audio_ms,
        tts_first_audio_since_bridge_ms=tts_first_audio_since_bridge_ms,
        tts_first_text_to_audio_ms=first_text_to_first_audio_ms,
        bridge_first_delta_to_tts_first_text_ms=first_delta_to_first_text_ms,
        tts_ws_text_count=tts_ws_text_count,
        tts_ws_audio_chunks=tts_ws_audio_chunks,
        first_audio_since_bridge_ms=first_audio_since_bridge_ms,
        **tts_gap_payload,
        tts_total_ms=tts_total_ms,
        tts_chunk_count=tts_chunk_count,
        response_chars=len(spoken_response),
        preface_ms=preface.first_audio_ms if preface and preface.ok else 0,
        streamed_bridge=True,
    )
    await send_json(websocket, {
        "type": "system",
        "payload": {
            "action": "turn_audio_timing",
            "status": "ok" if sent_any_audio or local_audio_id else "failed",
            "turn_id": stream_turn_id,
            "asr_ms": state.asr_latency_ms,
            "streaming_asr_first_delta_ms": state.streaming_asr_first_delta_ms,
            "streaming_asr_final_ms": state.streaming_asr_final_ms,
            "streaming_asr_final_reason": state.streaming_asr_final_reason,
            "streaming_asr_audio_bytes": state.streaming_asr_audio_bytes,
            "streaming_asr_forwarded_frames": state.streaming_asr_forwarded_frames,
            **asr_diagnostic_payload(state),
            **streaming_asr_quality_gate_payload(state),
            **turn_trigger_payload(state),
            **streaming_asr_prefetch_payload(state),
            **bridge_speculative_payload(state),
            **quality_trace,
            **persona_prompt_trace,
            "bridge_ms": state.bridge_latency_ms,
            "bridge_first_delta_ms": first_delta_ms,
            **timing_breakdown,
            **diagnosis,
            "tts_provider_stream": tts_provider_stream,
            "tts_first_text_ms": tts_first_text_ms,
            "tts_first_chunk_ms": tts_first_chunk_ms,
            "tts_first_audio_ms": tts_first_audio_ms,
            "tts_first_audio_since_bridge_ms": tts_first_audio_since_bridge_ms,
            "tts_first_text_to_audio_ms": first_text_to_first_audio_ms,
            "bridge_first_delta_to_tts_first_text_ms": first_delta_to_first_text_ms,
            "tts_ws_text_count": tts_ws_text_count,
            "tts_ws_audio_chunks": tts_ws_audio_chunks,
            "first_audio_since_bridge_ms": first_audio_since_bridge_ms,
            **tts_gap_payload,
            "tts_total_ms": tts_total_ms,
            "tts_chunk_count": tts_chunk_count,
            "tts_preface_ms": preface.first_audio_ms if preface and preface.ok else 0,
            "streamed_bridge": True,
        },
    })
    log_gateway(
        "bridge_stream_result",
        turn_id=stream_turn_id,
        ok=ok,
        latency_ms=state.bridge_latency_ms,
        first_delta_ms=first_delta_ms,
        response_chars=len(spoken_response),
    )
    clear_stream_tts_resource_refs(state, owner_turn_id=stream_turn_id)
    clear_bridge_speculative_adoption(state)
    maybe_schedule_voice_reminder(evidence, voice_turn_payload)
    task_id = str(evidence.get("task_id") or "").strip()
    if bool(evidence.get("deferred")) and task_id:
        await poll_and_send_background_result(websocket, config, runtime_config, state, task_id)
    return True


def bridge_speculative_payload(state: TurnState) -> dict[str, Any]:
    return {
        "bridge_speculative_status": state.bridge_speculative_status,
        "bridge_speculative_decision": state.bridge_speculative_decision,
        "bridge_speculative_reason": state.bridge_speculative_reason,
        "bridge_speculative_started_ms": state.bridge_speculative_started_ms,
        "bridge_speculative_event_count": state.bridge_speculative_event_count,
        "bridge_speculative_delta_chars": state.bridge_speculative_delta_chars,
    }


def bridge_speculative_log_fields(state: TurnState) -> dict[str, Any]:
    return bridge_speculative_payload(state)


def clear_bridge_speculative_adoption(state: TurnState) -> None:
    if not state.bridge_speculative_adopted:
        return
    state.bridge_speculative_task = None
    state.bridge_speculative_queue = None
    state.bridge_speculative_adopted = False


async def iter_bridge_speculative_queue(state: TurnState, queue: asyncio.Queue[dict[str, Any] | None]) -> AsyncIterator[dict[str, Any]]:
    while True:
        item = await queue.get()
        if item is None:
            return
        yield dict(item)


def bridge_speculative_can_start(state: TurnState, text: str, runtime_config: Any) -> tuple[bool, str]:
    if not BRIDGE_SPECULATIVE_ENABLED:
        return False, "disabled"
    if not should_stream_bridge(runtime_config):
        return False, "bridge_stream_disabled"
    if state.bridge_speculative_task is not None:
        return False, "already_started"
    if state.streaming_asr_final_ready:
        return False, "final_ready"
    value = _normalized_asr_text(text)
    if transcript_is_low_confidence_fragment(value):
        return False, "low_confidence_fragment"
    if streaming_asr_deterministic_partial_plan(value, runtime_config):
        return False, "deterministic"
    if bridge_speculative_should_skip_fast_local_intent(value):
        return False, "fast_local_intent"
    if len(value) < BRIDGE_SPECULATIVE_MIN_CHARS:
        return False, "too_short"
    if streaming_asr_audio_ms(state) < BRIDGE_SPECULATIVE_MIN_AUDIO_MS:
        return False, "audio_too_short"
    if _streaming_asr_has_correction_marker(value):
        return False, "correction_marker"
    if value.endswith(("，", "、", ",", "；", ";", "：", ":")):
        return False, "trailing_clause"
    return True, "ok"


def bridge_speculative_should_skip_fast_local_intent(text: str) -> bool:
    value = _normalized_asr_text(text)
    if not value:
        return False
    if any(token in value for token in ("最近状态", "复盘", "工作状态", "自然回应", "想聊聊", "陪我聊", "聊两句")):
        return False
    fast_tokens = (
        "天气",
        "多少度",
        "温度",
        "几点",
        "现在时间",
        "今天几号",
        "星期几",
        "你那边",
        "在哪",
        "在干嘛",
        "干什么",
        "测试一下",
        "测试下",
        "听得到吗",
        "能听到吗",
    )
    return any(token in value for token in fast_tokens)


def maybe_start_bridge_speculative(state: TurnState, config: GatewayConfig, text: str, runtime_config: Any) -> None:
    allowed, reason = bridge_speculative_can_start(state, text, runtime_config)
    if not allowed:
        if reason not in {"too_short", "audio_too_short", "already_started"}:
            log_gateway(
                "bridge_speculative_blocked",
                turn_id=state.turn_id,
                reason=reason,
                text_chars=len(_normalized_asr_text(text)),
                audio_ms=streaming_asr_audio_ms(state),
            )
        return
    state.bridge_speculative_text = str(text or "").strip()
    state.bridge_speculative_status = "started"
    state.bridge_speculative_decision = ""
    state.bridge_speculative_reason = ""
    state.bridge_speculative_event_count = 0
    state.bridge_speculative_delta_chars = 0
    state.bridge_speculative_queue = asyncio.Queue()
    state.bridge_speculative_adopted = False
    state.bridge_speculative_started_ms = _elapsed_ms(state.streaming_asr_started_at) if state.streaming_asr_started_at else 0
    state.bridge_speculative_task = asyncio.create_task(run_bridge_speculative(config, state, state.bridge_speculative_text))
    log_gateway(
        "bridge_speculative_started",
        turn_id=state.turn_id,
        started_ms=state.bridge_speculative_started_ms,
        text_chars=len(_normalized_asr_text(state.bridge_speculative_text)),
        audio_ms=streaming_asr_audio_ms(state),
    )


async def run_bridge_speculative(config: GatewayConfig, state: TurnState, text: str) -> BridgeSpeculativeResult:
    result = BridgeSpeculativeResult(text=text, started_ms=state.bridge_speculative_started_ms)
    queue = state.bridge_speculative_queue
    try:
        async for event in bridge_stream_events(
            config,
            state,
            text,
            metadata_extra={"speculative": True, "speculative_text": text},
        ):
            item = dict(event)
            result.events.append(item)
            state.bridge_speculative_event_count = len(result.events)
            if item.get("type") == "delta":
                result.delta_chars += len(str(item.get("text") or ""))
                state.bridge_speculative_delta_chars = result.delta_chars
            if queue is not None:
                await queue.put(dict(item))
            if (
                not state.bridge_speculative_adopted
                and (
                    len(result.events) >= BRIDGE_SPECULATIVE_MAX_EVENTS
                    or result.delta_chars >= BRIDGE_SPECULATIVE_MAX_CHARS
                )
            ):
                result.status = "buffer_limit"
                state.bridge_speculative_status = result.status
                if queue is not None:
                    await queue.put(None)
                return result
        result.status = "completed"
        state.bridge_speculative_status = result.status
        result.completed_ms = _elapsed_ms(state.streaming_asr_started_at) if state.streaming_asr_started_at else 0
        if queue is not None:
            await queue.put(None)
        return result
    except asyncio.CancelledError:
        result.status = "cancelled"
        state.bridge_speculative_status = "cancelled"
        if queue is not None and state.bridge_speculative_adopted:
            await queue.put({
                "type": "error",
                "error": "bridge_speculative_cancelled",
                "response": "流式回复连接中断了，你再试一次。",
            })
            await queue.put(None)
        raise
    except Exception as exc:
        result.status = "failed"
        result.error = exc.__class__.__name__
        state.bridge_speculative_status = "failed"
        state.bridge_speculative_reason = result.error
        if queue is not None:
            await queue.put({
                "type": "error",
                "error": result.error,
                "response": "流式回复连接失败了，你再试一次。",
            })
            await queue.put(None)
        return result


async def cancel_bridge_speculative(state: TurnState, *, reason: str, clear: bool = True) -> None:
    task = state.bridge_speculative_task
    state.bridge_speculative_task = None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        log_gateway("bridge_speculative_cancelled", turn_id=state.turn_id, reason=reason)
    elif task is None and state.bridge_speculative_adopted:
        queue = state.bridge_speculative_queue
        if queue is not None:
            await queue.put({
                "type": "error",
                "error": f"bridge_speculative_{reason}",
                "response": "流式回复连接中断了，你再试一次。",
            })
            await queue.put(None)
    if clear:
        state.bridge_speculative_text = ""
        state.bridge_speculative_started_ms = 0
        state.bridge_speculative_status = ""
        state.bridge_speculative_decision = ""
        state.bridge_speculative_reason = ""
        state.bridge_speculative_event_count = 0
        state.bridge_speculative_delta_chars = 0
    state.bridge_speculative_queue = None
    state.bridge_speculative_adopted = False


async def resolve_bridge_speculative_reuse(state: TurnState, final_text: str) -> BridgeSpeculativeReuse:
    task = state.bridge_speculative_task
    if task is None:
        return BridgeSpeculativeReuse(events=None, decision="", reason="not_started")
    reuse_allowed, reuse_reason = bridge_speculative_reuse_decision(state, final_text)
    if not reuse_allowed:
        state.bridge_speculative_decision = "miss"
        state.bridge_speculative_reason = reuse_reason
        await cancel_bridge_speculative(state, reason=reuse_reason, clear=False)
        return BridgeSpeculativeReuse(events=None, decision="miss", reason=reuse_reason)
    if not task.done():
        queue = state.bridge_speculative_queue
        if queue is None:
            state.bridge_speculative_decision = "miss"
            state.bridge_speculative_reason = "queue_missing"
            await cancel_bridge_speculative(state, reason="queue_missing", clear=False)
            return BridgeSpeculativeReuse(events=None, decision="miss", reason="queue_missing")
        state.bridge_speculative_adopted = True
        state.bridge_speculative_decision = "hit"
        state.bridge_speculative_reason = reuse_reason
        state.bridge_speculative_status = state.bridge_speculative_status or "adopted"
        log_gateway(
            "bridge_speculative_adopted",
            turn_id=state.turn_id,
            reason=reuse_reason,
            events=state.bridge_speculative_event_count,
            delta_chars=state.bridge_speculative_delta_chars,
        )
        return BridgeSpeculativeReuse(
            events=None,
            decision="hit",
            reason=reuse_reason,
            event_source=iter_bridge_speculative_queue(state, queue),
        )
    state.bridge_speculative_task = None
    state.bridge_speculative_queue = None
    state.bridge_speculative_adopted = False
    try:
        speculative_result = task.result()
    except Exception as exc:
        reason = exc.__class__.__name__
        state.bridge_speculative_decision = "miss"
        state.bridge_speculative_reason = reason
        return BridgeSpeculativeReuse(events=None, decision="miss", reason=reason)
    if not isinstance(speculative_result, BridgeSpeculativeResult):
        state.bridge_speculative_decision = "miss"
        state.bridge_speculative_reason = "invalid_result"
        return BridgeSpeculativeReuse(events=None, decision="miss", reason="invalid_result")
    if not speculative_result.events:
        state.bridge_speculative_decision = "miss"
        state.bridge_speculative_reason = "empty_result"
        return BridgeSpeculativeReuse(events=None, decision="miss", reason="empty_result")
    if speculative_result.status in {"failed", "cancelled"}:
        reason = speculative_result.status if not speculative_result.error else speculative_result.error
        state.bridge_speculative_decision = "miss"
        state.bridge_speculative_reason = reason
        return BridgeSpeculativeReuse(events=None, decision="miss", reason=reason)
    state.bridge_speculative_status = speculative_result.status
    state.bridge_speculative_event_count = len(speculative_result.events)
    state.bridge_speculative_delta_chars = speculative_result.delta_chars
    state.bridge_speculative_decision = "hit"
    state.bridge_speculative_reason = reuse_reason
    return BridgeSpeculativeReuse(
        events=[dict(item) for item in speculative_result.events],
        decision="hit",
        reason=reuse_reason,
    )


def bridge_speculative_reuse_decision(state: TurnState, final_text: str) -> tuple[bool, str]:
    if not BRIDGE_SPECULATIVE_REUSE_ENABLED:
        return False, "reuse_disabled"
    partial = _normalized_asr_text(state.bridge_speculative_text)
    final = _normalized_asr_text(final_text)
    if not partial or not final:
        return False, "empty"
    if final == partial:
        return True, "exact"
    if final.startswith(partial):
        extra = final[len(partial):]
        if len(extra) <= max(2, int(len(partial) * (1.0 - BRIDGE_SPECULATIVE_MIN_PREFIX_RATIO))):
            return True, "prefix_minor_tail"
        return False, "final_has_new_tail"
    if partial.startswith(final) and len(final) / max(1, len(partial)) >= BRIDGE_SPECULATIVE_MIN_PREFIX_RATIO:
        return True, "partial_superset"
    return False, "not_prefix"


async def bridge_stream_events(
    config: GatewayConfig,
    state: TurnState,
    transcript: str,
    *,
    metadata_extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            for event in iter_bridge_stream_events(config, state, transcript, metadata_extra=metadata_extra):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "error",
                "error": exc.__class__.__name__,
                "response": "流式回复连接失败了，你再试一次。",
            })
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    Thread(target=worker, name=f"aura-bridge-stream-{state.turn_id}", daemon=True).start()
    while True:
        event = await queue.get()
        if event is None:
            return
        yield event


def iter_bridge_stream_events(
    config: GatewayConfig,
    state: TurnState,
    transcript: str,
    *,
    metadata_extra: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    endpoint = bridge_stream_url(config.bridge_url)
    if not endpoint:
        return
    payload = {
        "goal": transcript.strip() or config.placeholder_goal,
        "metadata": {**bridge_metadata(state, transcript, streamed=True), **dict(metadata_extra or {})},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    if state.client_ip:
        headers["x-forwarded-for"] = state.client_ip
    req = request.Request(endpoint, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=max(1.0, config.bridge_timeout_seconds)) as res:
        for raw_line in res:
            line = bytes(raw_line or b"").strip()
            if not line:
                continue
            payload = json.loads(line.decode("utf-8"))
            if isinstance(payload, dict):
                yield payload


def bridge_stream_url(bridge_url: str) -> str:
    parsed = urlsplit(str(bridge_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = (parsed.path or "/turn").rstrip("/")
    if path.endswith("/stream"):
        stream_path = path
    else:
        stream_path = f"{path}/stream"
    return urlunsplit((parsed.scheme, parsed.netloc, stream_path, parsed.query, ""))


def pop_stream_tts_segment(
    text: str,
    *,
    force: bool,
    first_segment: bool = True,
    require_sentence_end: bool = False,
    followup_limit_chars: int | None = None,
) -> tuple[str, str]:
    value = str(text or "")
    if not value.strip():
        return "", ""
    if force:
        return value.strip(), ""
    if _has_unclosed_stage_wrapper(value):
        return "", value
    stripped_left = value.lstrip()
    leading = len(value) - len(stripped_left)
    leading_wrapper_chars = _leading_wrapped_segment_chars(stripped_left)
    if leading_wrapper_chars:
        cut = leading + leading_wrapper_chars
        return value[:cut].strip(), value[cut:]
    # Streaming deltas can end in the middle of a Chinese word. A character
    # count cutoff can turn one word into separate TTS requests and create an
    # audible pause. Wait for punctuation; the final flush sends any remaining
    # unpunctuated text when the model stream ends.
    min_chars = max(2, BRIDGE_STREAM_MIN_CHARS)
    strong_punctuation = "。！？!?；;\n"
    soft_punctuation = "，,、：: "
    for index, ch in enumerate(stripped_left, start=1):
        if index < min_chars:
            continue
        if ch in strong_punctuation:
            cut = leading + index
            if _has_unclosed_stage_wrapper(value[:cut]):
                continue
            if _stream_tts_segment_is_incomplete(value[:cut], remainder=value[cut:]):
                continue
            if first_segment and _stream_tts_segment_too_short_after_cleaning(value[:cut]):
                continue
            return value[:cut].strip(), value[cut:]
        if ch in soft_punctuation:
            if require_sentence_end:
                continue
            cut = leading + index
            if _has_unclosed_stage_wrapper(value[:cut]):
                continue
            if _stream_tts_segment_is_incomplete(value[:cut], remainder=value[cut:]):
                continue
            if first_segment and _stream_tts_segment_too_short_after_cleaning(value[:cut]):
                continue
            return value[:cut].strip(), value[cut:]
    return "", value


def merge_deferred_local_preface_for_tts(local_preface: str, model_text: str) -> str:
    preface = str(local_preface or "").strip()
    body = str(model_text or "").lstrip()
    if not preface:
        return body
    if not body:
        return preface
    softened = preface.rstrip("。！？!?；;").rstrip()
    if not softened:
        return body
    if softened.endswith(("，", ",", "、", "：", ":")):
        return f"{softened}{body}"
    return f"{softened}，{body}"


def _text_prefix_equivalent(full_text: str, prefix_text: str) -> bool:
    full_key = re.sub(r"[\s,，。.!！?？、~～…·:：;；\"'“”‘’（）()【】\[\]{}<>《》\\-—]+", "", str(full_text or ""))
    prefix_key = re.sub(r"[\s,，。.!！?？、~～…·:：;；\"'“”‘’（）()【】\[\]{}<>《》\\-—]+", "", str(prefix_text or ""))
    return bool(prefix_key) and full_key.startswith(prefix_key)


def _stream_tts_segment_too_short_after_cleaning(text: str) -> bool:
    raw = str(text or "").strip()
    cleaned = device_spoken_text(text, allow_fallback=False)
    if cleaned == raw:
        return False
    key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", cleaned)
    return bool(cleaned) and len(key) < 2


def flush_stream_tts_segments(text: str) -> list[str]:
    pending = str(text or "")
    segments: list[str] = []
    while pending.strip():
        segment, pending = pop_stream_tts_segment(pending, force=False, first_segment=not segments)
        if segment:
            if device_spoken_text(segment, allow_fallback=False):
                segments.append(segment)
            continue
        if _has_unclosed_stage_wrapper(pending):
            spoken_prefix = _strip_unclosed_stage_tail(pending).strip()
            if spoken_prefix:
                segments.extend(flush_stream_tts_segments(spoken_prefix))
            return _merge_short_tts_tail(segments)
        cleaned = device_spoken_text(pending.strip(), allow_fallback=False)
        if _stream_tts_segment_is_incomplete(cleaned, remainder=""):
            return _merge_short_tts_tail(segments)
        if cleaned:
            segments.append(pending.strip())
        break
    return _merge_short_tts_tail(segments)


def _stream_tts_segment_is_incomplete(text: str, *, remainder: str = "") -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    value = value.rstrip("，,；;、：: ")
    if not value:
        return False
    if _stream_tts_response_has_malformed_punctuation(value):
        return True
    incomplete_suffixes = (
        "其实我",
        "其实你",
        "其实看你",
        "感觉你",
        "看你",
        "我看",
        "我觉得",
        "我也觉得该理",
        "觉得该理",
        "该理",
        "我也觉得",
        "刚好我也觉得",
        "我猜你",
        "是那种",
        "那种",
        "比如",
        "像是",
        "因为",
        "所以",
        "但是",
        "不过",
        "听得出来",
        "看得出来",
        "你心里",
        "是觉得生活节奏",
    )
    if any(value.endswith(suffix) for suffix in incomplete_suffixes):
        return True
    remainder_text = str(remainder or "")
    if value.endswith(".") and remainder_text.startswith("."):
        return True
    if re.search(r"(?:\.\.\.|…+)$", value):
        return True
    last_ellipsis = max(value.rfind("..."), value.rfind("…"))
    if last_ellipsis >= 0 and not re.search(r"[。！？!?]", value[last_ellipsis:]):
        return True
    if remainder_text.strip() and re.search(r"(?:状态|工作|复盘|天气|位置|速度|时间|心情|计划)[啊呀呢嘛吧]*$", value):
        return True
    return False


def _stream_tts_response_has_malformed_punctuation(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"[，,、；;：:]\s*[。！？!?]", value):
        return True
    if re.search(r"[。！？!?]\s*[，,、；;：:]", value):
        return True
    return False


def _stream_tts_response_has_unfinished_tail(text: str, *, min_chars: int = 8) -> bool:
    value = str(text or "").strip().rstrip("，,；;、：: ")
    if not value or re.search(r"[。！？!?]$", value):
        return False
    if not re.search(r"[\u3400-\u9fff]", value):
        return False
    last_end = -1
    for ch in "。！？!?":
        last_end = max(last_end, value.rfind(ch))
    tail = value[last_end + 1:].strip() if last_end >= 0 else value
    key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", tail)
    return len(key) >= max(1, min_chars)


def _stream_tts_response_makes_unfounded_user_claim(text: str, *, transcript: str = "") -> bool:
    key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", str(text or "").strip())
    if not key:
        return False
    user_key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", str(transcript or "").strip())
    direct_claim_markers = (
        "你这几天",
        "你这两天",
        "你最近",
        "你一直",
        "一直在看你",
        "一直在跟我",
        "你心里",
        "看你这",
        "比上周",
        "那个结",
        "结还没解开",
        "没解开",
    )
    if any(re.sub(r"[\s，。！？!?、；;：:,.…]+", "", marker) in key for marker in direct_claim_markers):
        return True
    inferred_state_markers = (
        "听得出来",
        "看得出来",
        "感觉你",
        "精神头",
        "念叨",
        "琐碎",
        "绷得太紧",
        "隐形加班",
        "熬大夜",
        "赶项目",
        "觉得累",
        "让你觉得累",
        "节奏乱",
        "整个人都在飘",
        "找点重心",
        "透透气",
        "刚醒",
        "我刚醒",
        "脑子最清醒",
        "脑子清醒",
        "最清醒",
        "这一大早",
        "一大早",
        "今晚这时间点",
        "这时间点儿",
        "这时间点",
        "钻牛角尖",
        "突然想通",
        "想通了",
        "电量还剩",
        "往哪儿充",
        "往哪充",
        "充电",
        "事情太多",
        "堆得太满",
        "事情堆得太满",
        "有点乱",
        "太乱",
        "乱了",
        "歇歇脚",
        "歇脚",
    )
    for marker in inferred_state_markers:
        marker_key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", marker)
        if marker_key and marker_key in key and marker_key not in user_key:
            return True
    return False


def _stream_tts_response_is_vague_status(text: str) -> bool:
    key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", str(text or "").strip())
    if not key:
        return False
    if _stream_tts_status_reply_is_topic_echo(text):
        return True
    vague_markers = (
        "最近状态确实该理理",
        "最近状态确实要理",
        "状态确实该理理",
        "状态确实要复盘",
        "确实该理理",
        "确实要理理",
        "确实该复盘",
        "确实要复盘",
        "该理理",
        "该理一理",
        "我也觉得该理",
        "觉得该理",
    )
    return any(re.sub(r"[\s，。！？!?、；;：:,.…]+", "", marker) in key for marker in vague_markers)


def _stream_tts_status_reply_is_topic_echo(text: str) -> bool:
    key = re.sub(r"[\s，。！？!?、；;：:,.…啊呀呢嘛吧哦嗯哈呗喽]+", "", str(text or "").strip())
    if not key:
        return False
    if any(token in key for token in ("节奏", "睡眠", "情绪", "提不起劲", "太满", "最累", "卡住", "事情", "项目", "压力")):
        return False
    return key in {
        "最近状态",
        "状态",
        "复盘",
        "工作状态",
        "想聊最近状态是",
    }


def stream_bridge_response_needs_guard(text: str, *, transcript: str = "") -> bool:
    if not str(text or "").strip():
        return False
    if _stream_tts_segment_is_incomplete(text, remainder=""):
        return True
    if _stream_tts_response_is_empty_opening(text, transcript=transcript):
        return True
    if _stream_tts_response_makes_unfounded_user_claim(text, transcript=transcript):
        return True
    user_text = str(transcript or "")
    if _stream_bridge_status_like_transcript(user_text):
        if _stream_tts_response_has_unfinished_tail(text):
            return True
        if _stream_tts_status_reply_lacks_actionable_anchor(text):
            return True
        if _stream_tts_response_is_vague_status(text):
            return True
        cleaned = str(text or "").strip()
        if re.search(r"(?:其实[你我]?|感觉[你我]?|看你这|我看|我觉得|我猜你|听得出来|看得出来)[，,。！？!?、；;：: ]*$", cleaned):
            return True
    return False


def stream_bridge_response_requires_guard(
    text: str,
    *,
    transcript: str = "",
) -> bool:
    return stream_bridge_response_needs_guard(text, transcript=transcript)


def _stream_tts_response_is_empty_opening(text: str, *, transcript: str = "") -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    key = re.sub(r"[\s，。！？!?、；;：:,.…]+", "", value)
    if not key:
        return False
    if len(key) <= 16 and re.search(
        r"^(?:那咱们|那我们|咱们|我们|直奔主题|进入正题|开始吧|说吧|聊吧|你说|我听着|我在听)",
        key,
    ):
        return True
    if len(key) <= 18 and re.search(
        r"^(?:聊啥|想聊啥|聊点什么|聊点儿什么|想聊点什么|想聊点儿什么|从哪儿聊起|从哪聊起|从哪里聊起|先从哪儿聊起|先从哪聊起|先从哪里聊起)",
        key,
    ):
        return True
    if len(key) <= 24 and re.search(r"(?:想聊啥|聊点什么|聊点儿什么|从哪儿聊起|从哪里聊起|你想从哪儿|你想从哪|想从哪儿|想从哪)$", key):
        return True
    if any(token in str(transcript or "") for token in ("想聊聊", "陪我聊", "聊两句")):
        if key in {"那就聊", "那就聊嘛", "聊聊", "嘛", "好哒", "好呀"}:
            return True
        if re.search(r"(?:我在这儿陪着你|我在这陪着你|就在这儿陪着你|就在这陪着你|陪着你)(?:你想从哪儿|你想从哪|想从哪儿|想从哪)?$", key):
            return True
        if key in {"正好我也闲着", "我也闲着"}:
            return True
    if len(key) <= 18 and re.search(
        r"^(?:行呀|行啊|好呀|好啊|可以|可以呀|可以啊)(?:那咱们|那我们|咱们|我们|直奔主题|进入正题|开始|说|聊)",
        key,
    ):
        return True
    user_text = str(transcript or "")
    if _stream_bridge_status_like_transcript(user_text):
        if key in {"行呀那咱们就直奔主题", "行啊那咱们就直奔主题", "那咱们就直奔主题", "那我们就直奔主题", "直奔主题"}:
            return True
    return False


def _stream_tts_status_reply_lacks_actionable_anchor(text: str) -> bool:
    key = re.sub(r"[\s，。！？!?、；;：:,.…啊呀呢嘛吧哦嗯哈呗喽]+", "", str(text or "").strip())
    if not key:
        return False
    if (
        "节奏" in key
        and not any(token in key for token in ("先看", "从", "说起", "复盘", "拆", "太满", "提不起劲", "哪", "哪里", "哪块", "卡"))
    ):
        return True
    anchors = (
        "工作",
        "节奏",
        "睡眠",
        "情绪",
        "压力",
        "事情",
        "项目",
        "太满",
        "提不起劲",
        "最累",
        "卡住",
        "哪里",
        "哪块",
        "复盘",
        "状态",
    )
    if any(anchor in key for anchor in anchors):
        return False
    return True


def stream_bridge_should_wait_first_sentence(
    text: str,
    *,
    transcript: str = "",
    first_segment: bool,
) -> bool:
    if not first_segment:
        return False
    user_text = str(transcript or "")
    if not _stream_bridge_requires_complete_first_sentence_transcript(user_text):
        return False
    value = str(text or "").strip()
    if not value:
        return False
    return not any(ch in value for ch in "。！？!?")


def stream_bridge_requires_complete_first_sentence(*, transcript: str = "", first_segment: bool) -> bool:
    if not first_segment:
        return False
    user_text = str(transcript or "")
    return _stream_bridge_requires_complete_first_sentence_transcript(user_text)


def stream_bridge_fallback_response(transcript: str) -> str:
    value = str(transcript or "")
    if _stream_bridge_status_like_transcript(value):
        return "从工作节奏说起：是事情太满，还是提不起劲？"
    if any(token in value for token in ("换工作", "跳槽", "离职", "找工作", "新工作")):
        return "换工作这件事先拆开看：是现在耗着难受，还是新机会更吸引你？"
    if any(token in value for token in ("想聊聊", "聊聊", "想找你聊", "想跟你说")):
        return "先说你最想聊的那一件。"
    return "我在。"


def _stream_bridge_status_like_transcript(text: str) -> bool:
    return any(token in str(text or "") for token in ("最近状态", "复盘", "工作状态", "工作节奏"))


def _stream_bridge_requires_complete_first_sentence_transcript(text: str) -> bool:
    value = str(text or "")
    if _stream_bridge_status_like_transcript(value):
        return True
    complete_first_sentence_topics = (
        "加班",
        "压力",
        "焦虑",
        "难过",
        "低落",
        "烦",
        "累",
        "睡不着",
        "陪我聊",
        "聊两句",
        "说说话",
        "安慰",
        "换工作",
        "跳槽",
        "离职",
        "找工作",
        "新工作",
        "想聊聊",
        "想找你聊",
        "想跟你说",
    )
    return any(token in value for token in complete_first_sentence_topics)


def _merge_short_tts_tail(segments: list[str]) -> list[str]:
    if len(segments) < 2:
        return segments
    min_chars = max(2, BRIDGE_STREAM_MIN_CHARS)
    tail = segments[-1].strip()
    if re.fullmatch(r"[。！？!?；;，,、：:\s]+", tail or ""):
        merged = [*segments[:-2], (segments[-2].rstrip() + tail).strip()]
        return merged
    if 0 < len(tail) < min_chars:
        merged = [*segments[:-2], (segments[-2].rstrip() + tail).strip()]
        return merged
    return segments


def _strip_unclosed_stage_tail(text: str) -> str:
    value = str(text or "")
    cut_at: int | None = None
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]"}
    stack: list[tuple[str, int]] = []
    for index, ch in enumerate(value):
        if ch in pairs:
            stack.append((pairs[ch], index))
        elif stack and ch == stack[-1][0]:
            stack.pop()
    if stack:
        cut_at = min(index for _, index in stack)
    if value.count("*") % 2 == 1:
        star_cut = value.rfind("*")
        cut_at = star_cut if cut_at is None else min(cut_at, star_cut)
    return value[:cut_at].rstrip() if cut_at is not None else value


def _has_unclosed_stage_wrapper(text: str) -> bool:
    value = str(text or "")
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]"}
    stack: list[str] = []
    for ch in value:
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
    if stack:
        return True
    return value.count("*") % 2 == 1


def _leading_wrapped_segment_chars(text: str) -> int:
    value = str(text or "")
    if not value:
        return 0
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]"}
    opener = value[0]
    if opener in pairs:
        close_index = value.find(pairs[opener], 1)
        return close_index + 1 if close_index >= 0 else 0
    if opener == "*":
        close_index = value.find("*", 1)
        return close_index + 1 if close_index >= 0 else 0
    return 0


def websocket_client_ip(websocket: Any) -> str:
    remote = getattr(websocket, "remote_address", None)
    if isinstance(remote, (tuple, list)) and remote:
        return str(remote[0] or "").strip()
    if isinstance(remote, str):
        return remote.strip()
    return ""


def fetch_background_task_result(config: GatewayConfig, task_id: str) -> dict[str, Any]:
    task = str(task_id or "").strip()
    if not task:
        return {"ok": False, "status": "failed"}
    endpoint = background_task_result_url(config.bridge_url, task)
    if not endpoint:
        return {"ok": False, "status": "failed"}
    req = request.Request(endpoint, method="GET")
    try:
        with request.urlopen(req, timeout=5.0) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return {"ok": False, "status": "pending", "task_id": task}
    return payload if isinstance(payload, dict) else {"ok": False, "status": "failed", "task_id": task}


def background_task_result_url(bridge_url: str, task_id: str) -> str:
    parsed = urlsplit(str(bridge_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        f"/persona/background-task/{quote(str(task_id or '').strip(), safe='')}",
        "",
        "",
    ))


async def send_tts_pcm_stream(
    websocket: Any,
    turn_id: int,
    audio: bytes,
    *,
    stream_id: int,
    is_final: bool = True,
    timing: AudioChunkTiming | None = None,
) -> None:
    if not audio:
        await send_tts_binary(websocket, turn_id, b"", stream_id=stream_id, is_final=True)
        return
    pos = 0
    total = len(audio)
    started = time.monotonic()
    pacing_enabled = bool(TTS_AUDIO_SEND_PACING_ENABLED)
    pacing_rate = max(1.0, float(TTS_AUDIO_SEND_PACING_RATE or 1.0))
    prefill_bytes = int(
        max(0, TTS_AUDIO_SEND_PACING_PREFILL_MS)
        * DEVICE_SAMPLE_RATE
        * DEVICE_SAMPLE_WIDTH
        * DEVICE_CHANNELS
        / 1000
    )
    direct_packets = max(0, int(TTS_AUDIO_SEND_DIRECT_PACKETS or 0))
    chunk_count = 0
    while pos < total:
        chunk = audio[pos:pos + TTS_CHUNK_BYTES]
        pos += len(chunk)
        chunk_count += 1
        await send_tts_binary(websocket, turn_id, chunk, stream_id=stream_id, is_final=is_final and pos >= total)
        if timing is not None:
            timing.record_chunk(byte_count=len(chunk))
        if pacing_enabled and pos < total and pos > prefill_bytes and chunk_count > direct_packets:
            audio_seconds = (pos - prefill_bytes) / max(1, DEVICE_SAMPLE_RATE * DEVICE_SAMPLE_WIDTH * DEVICE_CHANNELS)
            target_elapsed = audio_seconds / pacing_rate
            elapsed = time.monotonic() - started
            delay = target_elapsed - elapsed
            if TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS > 0:
                delay = min(delay, TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS / 1000)
            if delay > 0:
                await asyncio.sleep(delay)
                if timing is not None:
                    timing.record_pacing_sleep(delay)
    if timing is not None:
        timing.finish()


async def send_tts_binary(websocket: Any, turn_id: int, audio: bytes, *, stream_id: int, is_final: bool) -> None:
    flags = TTS_BINARY_FLAG_FINAL if is_final else 0
    header = bytearray(TTS_BINARY_HEADER_SIZE)
    header[0:4] = TTS_BINARY_MAGIC
    header[4:8] = int(stream_id).to_bytes(4, "little", signed=False)
    header[8:12] = int(turn_id or 0).to_bytes(4, "little", signed=False)
    header[12] = flags
    await websocket.send(bytes(header) + bytes(audio or b""))


async def send_system(
    websocket: Any,
    action: str,
    *,
    status: str = "ok",
    turn_id: int = 0,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {"action": action, "status": status}
    if turn_id:
        payload["turn_id"] = turn_id
    for key, value in extra.items():
        if value not in {"", None, False}:
            payload[str(key)] = value
    await send_json(websocket, {"type": "system", "payload": payload})


async def send_json(websocket: Any, payload: dict[str, Any]) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def mark_turn_trigger(
    state: TurnState,
    reason: str,
    *,
    detail: str = "",
    audio_ms: int = 0,
    silence_ms: int = 0,
) -> None:
    if state.turn_trigger_reason:
        return
    state.turn_trigger_reason = str(reason or "").strip() or "unknown"
    state.turn_trigger_detail = str(detail or "").strip()
    state.turn_triggered_at = time.monotonic()
    state.turn_trigger_ms = _elapsed_ms(state.started_at) if state.started_at else 0
    state.turn_trigger_audio_ms = max(0, int(audio_ms or 0))
    state.turn_trigger_silence_ms = max(0, int(silence_ms or 0))


def turn_trigger_payload(state: TurnState) -> dict[str, Any]:
    return {
        "turn_trigger_reason": state.turn_trigger_reason or "",
        "turn_trigger_detail": state.turn_trigger_detail or "",
        "turn_trigger_ms": max(0, int(state.turn_trigger_ms or 0)),
        "turn_trigger_audio_ms": max(0, int(state.turn_trigger_audio_ms or 0)),
        "turn_trigger_silence_ms": max(0, int(state.turn_trigger_silence_ms or 0)),
    }


def audio_receive_payload(state: TurnState) -> dict[str, Any]:
    stop_gap = 0
    if state.audio_stop_received_ms and state.audio_last_packet_ms:
        stop_gap = max(0, int(state.audio_stop_received_ms - state.audio_last_packet_ms))
    return {
        "audio_packet_count": max(0, int(state.audio_packet_count or 0)),
        "audio_first_packet_ms": max(0, int(state.audio_first_packet_ms or 0)),
        "audio_last_packet_ms": max(0, int(state.audio_last_packet_ms or 0)),
        "audio_stop_received_ms": max(0, int(state.audio_stop_received_ms or 0)),
        "audio_stop_gap_ms": stop_gap,
    }


def voice_quality_trace_payload(
    *,
    transcript: str = "",
    response: str = "",
    evidence: dict[str, Any] | None = None,
    voice_turn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_evidence = evidence if isinstance(evidence, dict) else {}
    safe_voice_turn = voice_turn if isinstance(voice_turn, dict) else {}
    voice_debug = safe_voice_turn.get("debug") if isinstance(safe_voice_turn.get("debug"), dict) else {}
    reply_contract = safe_evidence.get("reply_contract") if isinstance(safe_evidence.get("reply_contract"), dict) else {}
    return {
        "transcript_chars": len(str(transcript or "")),
        "transcript_preview": _transcript_preview(transcript),
        "spoken_response_chars": len(str(response or "")),
        "response_preview": _transcript_preview(response),
        "voice_decision_path": str(voice_debug.get("decision_path") or ""),
        "voice_route": str(voice_debug.get("route") or ""),
        "model_skipped": bool(safe_evidence.get("model_skipped")),
        "local_voice_reply": bool(safe_evidence.get("local_voice_reply")),
        "local_preface": bool(safe_evidence.get("local_preface")),
        "reply_contract_changed": bool(reply_contract.get("changed")),
        "reply_contract_fallback_used": bool(reply_contract.get("fallback_used")),
    }


def persona_prompt_trace_payload(final_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = final_payload if isinstance(final_payload, dict) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
    context = debug.get("context") if isinstance(debug.get("context"), dict) else {}
    runtime = debug.get("aura_runtime") if isinstance(debug.get("aura_runtime"), dict) else {}
    trace: dict[str, Any] = {
        "persona_prompt_chars": _coerce_int(context.get("prompt_chars"), 0),
        "persona_compact_prompt_chars": _coerce_int(context.get("compact_prompt_chars"), 0),
        "persona_voice_low_latency": bool(context.get("voice_low_latency")),
        "persona_recent_message_limit": _coerce_int(context.get("recent_message_limit"), 0),
        "persona_turn_latency_ms": _coerce_int(evidence.get("persona_turn_latency_ms"), 0),
        "persona_context_build_ms": _coerce_int(evidence.get("persona_context_build_ms"), 0),
        "aura_llm_prompt_chars": _coerce_int(evidence.get("aura_llm_prompt_chars"), 0),
        "aura_llm_user_prompt_chars": _coerce_int(evidence.get("aura_llm_user_prompt_chars"), 0),
        "aura_llm_system_prompt_chars": _coerce_int(evidence.get("aura_llm_system_prompt_chars"), 0),
        "aura_llm_max_tokens": _coerce_int(evidence.get("aura_llm_max_tokens"), 0),
        "aura_llm_response_open_ms": _coerce_int(evidence.get("aura_llm_response_open_ms"), 0),
        "aura_llm_first_delta_ms": _coerce_int(evidence.get("aura_llm_first_delta_ms"), 0),
        "aura_llm_response_to_first_delta_ms": _coerce_int(evidence.get("aura_llm_response_to_first_delta_ms"), 0),
        "aura_llm_http_keepalive": bool(evidence.get("aura_llm_http_keepalive")),
        "aura_llm_http_keepalive_retry": bool(evidence.get("aura_llm_http_keepalive_retry")),
        "aura_llm_first_raw_delta_ms": _coerce_int(evidence.get("aura_llm_first_raw_delta_ms"), 0),
        "aura_llm_first_audible_delta_ms": _coerce_int(evidence.get("aura_llm_first_audible_delta_ms"), 0),
        "aura_llm_complete_ms": _coerce_int(evidence.get("aura_llm_complete_ms"), 0),
        "aura_model_mode": str(runtime.get("aura_model_mode") or ""),
        "aura_model_provider": str(runtime.get("aura_model_provider") or ""),
        "aura_model_model": str(runtime.get("aura_model_model") or ""),
        "aura_model_route": str(runtime.get("model_route") or ""),
    }
    reasoning_effort = str(evidence.get("aura_llm_reasoning_effort") or "").strip()
    if reasoning_effort:
        trace["aura_llm_reasoning_effort"] = reasoning_effort
    stop_reason = str(evidence.get("stop_reason") or "").strip()
    if stop_reason:
        trace["aura_llm_stop_reason"] = stop_reason
    return {key: value for key, value in trace.items() if value not in {"", 0, False}}


def streaming_asr_quality_gate_payload(state: TurnState) -> dict[str, Any]:
    return {
        "streaming_asr_early_turn_triggered": bool(state.streaming_asr_early_turn_triggered),
        "streaming_asr_early_turn_blocked": bool(state.streaming_asr_early_turn_blocked),
        "streaming_asr_finish_qsize_at_stop": state.streaming_asr_finish_qsize_at_stop,
        "streaming_asr_finish_queue_ms": state.streaming_asr_finish_queue_ms,
        "streaming_asr_finish_queue_timeout": bool(state.streaming_asr_finish_queue_timeout),
        "streaming_asr_sender_drain_ms": state.streaming_asr_sender_drain_ms,
        "streaming_asr_receiver_wait_ms": state.streaming_asr_receiver_wait_ms,
        "streaming_asr_commit_sent": bool(state.streaming_asr_commit_sent),
        "streaming_asr_commit_to_final_ms": state.streaming_asr_commit_to_final_ms,
    }


def asr_diagnostic_payload(state: TurnState) -> dict[str, Any]:
    return {
        "asr_decode_ms": max(0, int(state.asr_decode_ms or 0)),
        "asr_backend_ms": max(0, int(state.asr_backend_ms or 0)),
        "asr_wav_bytes": max(0, int(state.asr_wav_bytes or 0)),
        "asr_pcm_ms": max(0, int(state.asr_pcm_ms or 0)),
        "asr_pcm_rms": max(0, int(state.asr_pcm_rms or 0)),
        "asr_pcm_peak": max(0, int(state.asr_pcm_peak or 0)),
        "asr_pcm_clipping_ratio_x10000": max(0, int(state.asr_pcm_clipping_ratio_x10000 or 0)),
    }


def pcm_to_wav_bytes(pcm: bytes, *, sample_rate: int) -> bytes:
    import io

    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(DEVICE_CHANNELS)
        wav.setsampwidth(DEVICE_SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()


def resample_pcm16_mono(pcm: bytes, *, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or not pcm:
        return pcm
    sample_count = len(pcm) // 2
    if sample_count <= 1:
        return pcm
    samples = [int.from_bytes(pcm[i * 2:i * 2 + 2], "little", signed=True) for i in range(sample_count)]
    target_count = max(1, int(sample_count * target_rate / source_rate))
    out = bytearray(target_count * 2)
    for i in range(target_count):
        src = i * (source_rate / target_rate)
        left = int(src)
        right = min(left + 1, sample_count - 1)
        frac = src - left
        value = int(samples[left] * (1.0 - frac) + samples[right] * frac)
        out[i * 2:i * 2 + 2] = int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


def multipart_form_data(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"----AuraLily{int(time.time() * 1000)}"
    parts: list[bytes] = []
    for key, value in fields.items():
        if value:
            parts.extend([
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ])
    parts.extend([
        f"--{boundary}\r\n".encode("ascii"),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
        file_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ])
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def dialogue_segments(text: str) -> list[dict[str, Any]]:
    clean = " ".join(str(text or "").split())
    if not clean:
        return []
    raw_parts = re.split(r"(?<=[。！？!?])|(?<=[.])(?=\s|$)", clean)
    parts = [part.strip() for part in raw_parts if part.strip()][:DIALOGUE_MAX_SEGMENTS]
    if not parts:
        parts = [clean[:120]]
    return [
        {"text": part[:120], "duration_ms": estimate_dialogue_duration_ms(part)}
        for part in parts
    ]


DIALOGUE_MAX_SEGMENTS = 12


def dialogue_segments_from_parts(parts: list[str]) -> list[dict[str, Any]]:
    clean_parts = [" ".join(str(part or "").split()) for part in parts]
    clean_parts = [part for part in clean_parts if part]
    return [
        {"text": part[:120], "duration_ms": estimate_dialogue_duration_ms(part)}
        for part in clean_parts[:DIALOGUE_MAX_SEGMENTS]
    ]


def estimate_dialogue_duration_ms(text: str) -> int:
    clean = str(text or "").strip()
    latin_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", clean)
    cjk_chars = re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", clean)
    duration = 650 + len(latin_words) * 330 + len(cjk_chars) * 180
    return min(12_000, max(1_200, duration))


def pcm16_mono_duration_ms(audio_bytes: int, *, sample_rate: int = DEVICE_SAMPLE_RATE) -> int:
    if audio_bytes <= 0 or sample_rate <= 0:
        return 0
    return max(1, int(audio_bytes * 1000 / (sample_rate * DEVICE_SAMPLE_WIDTH)))


async def send_dialogue_segment_timing(
    websocket: Any,
    *,
    turn_id: int,
    segment_index: int,
    text: str,
    audio_bytes: int,
) -> None:
    duration_ms = pcm16_mono_duration_ms(audio_bytes)
    if duration_ms <= 0 or segment_index < 0 or segment_index >= DIALOGUE_MAX_SEGMENTS:
        return
    await send_json(websocket, {
        "type": "dialogue_segment",
        "payload": {
            "turn_id": turn_id,
            "segment_index": segment_index,
            "text": str(text or "")[:120],
            "duration_ms": duration_ms,
        },
    })


def asr_transcription_url(base_url: str, *, provider: str = "") -> str:
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    provider_id = str(provider or "").strip().lower()
    path = (parsed.path or "").rstrip("/")
    if (
        path.endswith("/audio/transcriptions")
        or path.endswith("/speech-to-text")
        or path.endswith("/audio/asr")
        or path.endswith("/audio/asr/sse")
    ):
        return text
    if provider_id == "stepfun":
        path = "/v1/audio/asr/sse" if not path else f"{path}/audio/asr/sse"
    elif provider_id == "elevenlabs":
        path = "/v1/speech-to-text" if not path else f"{path}/speech-to-text"
    elif not path:
        path = "/v1/audio/transcriptions"
    elif path.endswith("/v1"):
        path = f"{path}/audio/transcriptions"
    else:
        path = f"{path}/audio/transcriptions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def tts_speech_url(base_url: str, *, provider: str = "") -> str:
    text = str(base_url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    provider_id = str(provider or "").strip().lower()
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/audio/speech"):
        return text
    if provider_id in {"custom-http", "voxcpm", "voxcpm-aura"} and path:
        return text
    if not path:
        path = "/v1/audio/speech"
    elif path.endswith("/v1"):
        path = f"{path}/audio/speech"
    else:
        path = f"{path}/audio/speech"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _min_positive(*values: int) -> int:
    positive = [max(0, int(value or 0)) for value in values if int(value or 0) > 0]
    return min(positive) if positive else 0


def _min_nonzero_or_zero(*values: int | None) -> int:
    present = [max(0, int(value)) for value in values if value is not None]
    return min(present) if present else 0


def _percentile_ms(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value)) for value in values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def _signed_percentile_ms(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def audio_chunk_timing_summary(timing: AudioChunkTiming | None) -> dict[str, Any]:
    gaps = list(timing.gaps_ms) if timing is not None else []
    buffer_leads = list(timing.buffer_leads_ms) if timing is not None else []
    threshold = int(timing.stall_threshold_ms if timing is not None else TTS_AUDIO_CHUNK_STALL_MS)
    audio_bytes = max(0, int(timing.audio_bytes if timing is not None else 0))
    send_ms = 0
    realtime_x100 = 0
    if timing is not None and timing.send_started_at and timing.finished_at:
        send_ms = max(0, int((timing.finished_at - timing.send_started_at) * 1000))
    if audio_bytes > 0 and send_ms > 0:
        audio_ms = audio_bytes * 1000 / max(1, DEVICE_SAMPLE_RATE * DEVICE_SAMPLE_WIDTH * DEVICE_CHANNELS)
        realtime_x100 = max(1, int((audio_ms / send_ms) * 100))
    return {
        "audio_chunk_gap_count": len(gaps),
        "audio_chunk_gap_p50_ms": _percentile_ms(gaps, 0.50),
        "audio_chunk_gap_p95_ms": _percentile_ms(gaps, 0.95),
        "audio_chunk_gap_max_ms": max(gaps, default=0),
        "audio_chunk_stall_count": sum(1 for gap in gaps if gap >= threshold),
        "audio_chunk_gaps_ms": tuple(gaps),
        "audio_send_bytes": audio_bytes,
        "audio_send_ms": send_ms,
        "audio_send_realtime_x100": realtime_x100,
        "audio_send_pacing_enabled": bool(timing.pacing_enabled) if timing is not None else False,
        "audio_send_pacing_rate_x100": max(0, int(timing.pacing_rate_x100 if timing is not None else 0)),
        "audio_send_pacing_prefill_ms": max(0, int(timing.pacing_prefill_ms if timing is not None else 0)),
        "audio_send_pacing_sleep_count": max(0, int(timing.pacing_sleep_count if timing is not None else 0)),
        "audio_send_pacing_sleep_ms": max(0, int(timing.pacing_sleep_ms if timing is not None else 0)),
        "audio_buffer_lead_min_ms": min(buffer_leads, default=0),
        "audio_buffer_lead_p50_ms": _signed_percentile_ms(buffer_leads, 0.50) if buffer_leads else 0,
        "audio_buffer_lead_final_ms": buffer_leads[-1] if buffer_leads else 0,
        "audio_buffer_leads_ms": tuple(buffer_leads),
    }


def tts_chunk_timing_payload(result: TtsResult | None) -> dict[str, int | bool]:
    if result is None:
        return {
            "tts_audio_chunk_gap_count": 0,
            "tts_audio_chunk_gap_p50_ms": 0,
            "tts_audio_chunk_gap_p95_ms": 0,
            "tts_audio_chunk_gap_max_ms": 0,
            "tts_audio_chunk_stall_count": 0,
            "tts_audio_send_bytes": 0,
            "tts_audio_send_ms": 0,
            "tts_audio_send_realtime_x100": 0,
            "tts_audio_send_pacing_enabled": bool(TTS_AUDIO_SEND_PACING_ENABLED),
            "tts_audio_send_pacing_rate_x100": int(float(TTS_AUDIO_SEND_PACING_RATE or 1.0) * 100),
            "tts_audio_send_pacing_prefill_ms": max(0, int(TTS_AUDIO_SEND_PACING_PREFILL_MS or 0)),
            "tts_audio_send_pacing_sleep_count": 0,
            "tts_audio_send_pacing_sleep_ms": 0,
            "tts_audio_buffer_lead_min_ms": 0,
            "tts_audio_buffer_lead_p50_ms": 0,
            "tts_audio_buffer_lead_final_ms": 0,
        }
    return {
        "tts_audio_chunk_gap_count": max(0, int(result.audio_chunk_gap_count or 0)),
        "tts_audio_chunk_gap_p50_ms": max(0, int(result.audio_chunk_gap_p50_ms or 0)),
        "tts_audio_chunk_gap_p95_ms": max(0, int(result.audio_chunk_gap_p95_ms or 0)),
        "tts_audio_chunk_gap_max_ms": max(0, int(result.audio_chunk_gap_max_ms or 0)),
        "tts_audio_chunk_stall_count": max(0, int(result.audio_chunk_stall_count or 0)),
        "tts_audio_send_bytes": max(0, int(result.audio_send_bytes or 0)),
        "tts_audio_send_ms": max(0, int(result.audio_send_ms or 0)),
        "tts_audio_send_realtime_x100": max(0, int(result.audio_send_realtime_x100 or 0)),
        "tts_audio_send_pacing_enabled": bool(result.audio_send_pacing_enabled),
        "tts_audio_send_pacing_rate_x100": max(0, int(result.audio_send_pacing_rate_x100 or 0)),
        "tts_audio_send_pacing_prefill_ms": max(0, int(result.audio_send_pacing_prefill_ms or 0)),
        "tts_audio_send_pacing_sleep_count": max(0, int(result.audio_send_pacing_sleep_count or 0)),
        "tts_audio_send_pacing_sleep_ms": max(0, int(result.audio_send_pacing_sleep_ms or 0)),
        "tts_audio_buffer_lead_min_ms": int(result.audio_buffer_lead_min_ms or 0),
        "tts_audio_buffer_lead_p50_ms": int(result.audio_buffer_lead_p50_ms or 0),
        "tts_audio_buffer_lead_final_ms": int(result.audio_buffer_lead_final_ms or 0),
    }


def combined_tts_result_timing_fields(results: list[TtsResult]) -> dict[str, Any]:
    gaps = [
        max(0, int(gap))
        for result in results
        for gap in tuple(result.audio_chunk_gaps_ms or ())
    ]
    buffer_leads = [
        int(value)
        for result in results
        for value in tuple(result.audio_buffer_leads_ms or ())
    ]
    if not buffer_leads:
        buffer_leads = [
            int(value)
            for result in results
            for value in (
                [result.audio_buffer_lead_min_ms, result.audio_buffer_lead_p50_ms, result.audio_buffer_lead_final_ms]
                if any(
                    int(candidate or 0)
                    for candidate in (
                        result.audio_buffer_lead_min_ms,
                        result.audio_buffer_lead_p50_ms,
                        result.audio_buffer_lead_final_ms,
                    )
                )
                else []
            )
        ]
    send_bytes = sum(max(0, int(result.audio_send_bytes or 0)) for result in results)
    send_ms = sum(max(0, int(result.audio_send_ms or 0)) for result in results)
    realtime_x100 = 0
    if send_bytes > 0 and send_ms > 0:
        audio_ms = send_bytes * 1000 / max(1, DEVICE_SAMPLE_RATE * DEVICE_SAMPLE_WIDTH * DEVICE_CHANNELS)
        realtime_x100 = max(1, int((audio_ms / send_ms) * 100))
    if gaps:
        gap_fields = {
            "audio_chunk_gap_count": len(gaps),
            "audio_chunk_gap_p50_ms": _percentile_ms(gaps, 0.50),
            "audio_chunk_gap_p95_ms": _percentile_ms(gaps, 0.95),
            "audio_chunk_gap_max_ms": max(gaps, default=0),
            "audio_chunk_stall_count": sum(1 for gap in gaps if gap >= TTS_AUDIO_CHUNK_STALL_MS),
            "audio_chunk_gaps_ms": tuple(gaps),
        }
    else:
        count = sum(max(0, int(result.audio_chunk_gap_count or 0)) for result in results)
        if count <= 0:
            gap_fields = {
                "audio_chunk_gap_count": 0,
                "audio_chunk_gap_p50_ms": 0,
                "audio_chunk_gap_p95_ms": 0,
                "audio_chunk_gap_max_ms": 0,
                "audio_chunk_stall_count": 0,
                "audio_chunk_gaps_ms": (),
            }
        else:
            weighted_p50 = sum(
                max(0, int(result.audio_chunk_gap_p50_ms or 0)) * max(0, int(result.audio_chunk_gap_count or 0))
                for result in results
            )
            weighted_p95 = sum(
                max(0, int(result.audio_chunk_gap_p95_ms or 0)) * max(0, int(result.audio_chunk_gap_count or 0))
                for result in results
            )
            gap_fields = {
                "audio_chunk_gap_count": count,
                "audio_chunk_gap_p50_ms": int(weighted_p50 / count) if count else 0,
                "audio_chunk_gap_p95_ms": int(weighted_p95 / count) if count else 0,
                "audio_chunk_gap_max_ms": max((max(0, int(result.audio_chunk_gap_max_ms or 0)) for result in results), default=0),
                "audio_chunk_stall_count": sum(max(0, int(result.audio_chunk_stall_count or 0)) for result in results),
                "audio_chunk_gaps_ms": (),
            }
    return {
        **gap_fields,
        "audio_send_bytes": send_bytes,
        "audio_send_ms": send_ms,
        "audio_send_realtime_x100": realtime_x100,
        "audio_send_pacing_enabled": any(bool(result.audio_send_pacing_enabled) for result in results) or bool(TTS_AUDIO_SEND_PACING_ENABLED),
        "audio_send_pacing_rate_x100": int(float(TTS_AUDIO_SEND_PACING_RATE or 1.0) * 100),
        "audio_send_pacing_prefill_ms": max(0, int(TTS_AUDIO_SEND_PACING_PREFILL_MS or 0)),
        "audio_send_pacing_sleep_count": sum(max(0, int(result.audio_send_pacing_sleep_count or 0)) for result in results),
        "audio_send_pacing_sleep_ms": sum(max(0, int(result.audio_send_pacing_sleep_ms or 0)) for result in results),
        "audio_buffer_lead_min_ms": min(buffer_leads, default=0),
        "audio_buffer_lead_p50_ms": _signed_percentile_ms(buffer_leads, 0.50) if buffer_leads else 0,
        "audio_buffer_lead_final_ms": buffer_leads[-1] if buffer_leads else 0,
        "audio_buffer_leads_ms": tuple(buffer_leads),
    }


def combine_tts_chunk_timing_payload(results: list[TtsResult]) -> dict[str, int | bool]:
    combined = combined_tts_result_timing_fields(results)
    return {
        "tts_audio_chunk_gap_count": max(0, int(combined.get("audio_chunk_gap_count") or 0)),
        "tts_audio_chunk_gap_p50_ms": max(0, int(combined.get("audio_chunk_gap_p50_ms") or 0)),
        "tts_audio_chunk_gap_p95_ms": max(0, int(combined.get("audio_chunk_gap_p95_ms") or 0)),
        "tts_audio_chunk_gap_max_ms": max(0, int(combined.get("audio_chunk_gap_max_ms") or 0)),
        "tts_audio_chunk_stall_count": max(0, int(combined.get("audio_chunk_stall_count") or 0)),
        "tts_audio_send_bytes": max(0, int(combined.get("audio_send_bytes") or 0)),
        "tts_audio_send_ms": max(0, int(combined.get("audio_send_ms") or 0)),
        "tts_audio_send_realtime_x100": max(0, int(combined.get("audio_send_realtime_x100") or 0)),
        "tts_audio_send_pacing_enabled": bool(combined.get("audio_send_pacing_enabled")),
        "tts_audio_send_pacing_rate_x100": max(0, int(combined.get("audio_send_pacing_rate_x100") or 0)),
        "tts_audio_send_pacing_prefill_ms": max(0, int(combined.get("audio_send_pacing_prefill_ms") or 0)),
        "tts_audio_send_pacing_sleep_count": max(0, int(combined.get("audio_send_pacing_sleep_count") or 0)),
        "tts_audio_send_pacing_sleep_ms": max(0, int(combined.get("audio_send_pacing_sleep_ms") or 0)),
        "tts_audio_buffer_lead_min_ms": int(combined.get("audio_buffer_lead_min_ms") or 0),
        "tts_audio_buffer_lead_p50_ms": int(combined.get("audio_buffer_lead_p50_ms") or 0),
        "tts_audio_buffer_lead_final_ms": int(combined.get("audio_buffer_lead_final_ms") or 0),
    }


def voice_latency_breakdown(
    state: TurnState,
    *,
    bridge_first_delta_ms: int = 0,
    tts_first_audio_since_bridge_ms: int = 0,
) -> dict[str, int]:
    asr_final_ms = int(state.streaming_asr_final_ms or 0)
    if not asr_final_ms and state.asr_latency_ms:
        asr_final_ms = int(state.asr_latency_ms)
    bridge_first = int(bridge_first_delta_ms or 0)
    first_audio_from_bridge = int(tts_first_audio_since_bridge_ms or 0)
    first_audio_from_turn = asr_final_ms + first_audio_from_bridge if first_audio_from_bridge else 0
    trigger_to_first_audio = 0
    if first_audio_from_turn and state.turn_trigger_ms:
        trigger_to_first_audio = max(0, first_audio_from_turn - int(state.turn_trigger_ms or 0))
    elif first_audio_from_bridge:
        trigger_to_first_audio = first_audio_from_bridge
    return {
        "asr_to_bridge_first_delta_ms": bridge_first if bridge_first else 0,
        "bridge_to_tts_first_audio_ms": first_audio_from_bridge,
        "asr_to_tts_first_audio_ms": first_audio_from_bridge,
        "turn_to_tts_first_audio_ms": first_audio_from_turn,
        "trigger_to_tts_first_audio_ms": trigger_to_first_audio,
    }


def voice_latency_diagnosis(
    state: TurnState,
    *,
    bridge_first_delta_ms: int = 0,
    tts_first_text_ms: int = 0,
    tts_first_audio_since_bridge_ms: int = 0,
    tts_first_text_to_audio_ms: int = 0,
    audio_send_realtime_x100: int = 0,
    audio_chunk_stall_count: int = 0,
    streamed_bridge: bool = False,
    tts_provider_stream: str = "",
) -> dict[str, Any]:
    asr_final_ms = int(state.streaming_asr_final_ms or state.asr_latency_ms or 0)
    first_delta_ms = int(bridge_first_delta_ms or 0)
    first_text_ms = int(tts_first_text_ms or 0)
    first_audio_ms = int(tts_first_audio_since_bridge_ms or 0)
    text_to_audio_ms = int(tts_first_text_to_audio_ms or 0)
    realtime_x100 = int(audio_send_realtime_x100 or 0)
    stall_count = int(audio_chunk_stall_count or 0)
    text_queue_ms = (
        max(0, first_text_ms - first_delta_ms)
        if first_text_ms and first_delta_ms
        else 0
    )
    bottleneck = "unknown"
    severity = "unknown"
    recommendation = "缺少完整 timing，先看 turn_audio_timing 原始字段。"
    primary_candidates: list[tuple[int, str, str]] = []
    if asr_final_ms >= 1800:
        primary_candidates.append((
            asr_final_ms,
            "asr_final",
            "ASR 定稿太慢；优先检查流式 ASR 是否启用、是否等了录音 stop、VAD 静音阈值是否过长。",
        ))
    if first_delta_ms >= 1000:
        primary_candidates.append((
            first_delta_ms,
            "llm_first_delta",
            "LLM 首 token 慢；优先测短 prompt、max_tokens/reasoning、模型/provider 和网络。",
        ))
    if text_queue_ms >= 500:
        primary_candidates.append((
            text_queue_ms,
            "tts_text_queue",
            "LLM 已有内容但迟迟没喂给 TTS；优先检查分句阈值和本地前导/舞台描写清洗。",
        ))
    if text_to_audio_ms >= 900 or (first_audio_ms >= 2200 and not first_delta_ms):
        primary_candidates.append((
            text_to_audio_ms or first_audio_ms,
            "tts_first_audio",
            "TTS 首音频慢；优先检查 StepFun WS 是否 fallback 到 HTTP、voice/model、TTS 连接预热。",
        ))

    if primary_candidates:
        _, bottleneck, recommendation = max(primary_candidates, key=lambda item: item[0])
    elif stall_count > 0:
        bottleneck = "tts_audio_stall"
        recommendation = "TTS 音频包中途有停顿；优先看 provider chunk gap、WebSocket 稳定性和设备播放缓冲。"
    elif realtime_x100 and realtime_x100 < 85:
        bottleneck = "audio_send"
        recommendation = "服务端发送音频慢于实时播放；优先检查发送 pacing、网络和设备消费速度。"
    elif first_audio_ms and first_audio_ms <= 1200:
        bottleneck = "ok"
        recommendation = "首音频已接近目标；若体感仍慢，继续看后续音频 stall 和设备端 first_pcm/speaker 日志。"
    elif first_audio_ms:
        bottleneck = "borderline"
        recommendation = "首音频可用但还不够快；按 LLM 首 token、TTS 首音频、发送 stall 三段继续压缩。"

    total_first_audio = asr_final_ms + first_audio_ms if first_audio_ms else 0
    if bottleneck == "ok":
        severity = "ok"
    elif total_first_audio >= 3500 or asr_final_ms >= 3000 or first_delta_ms >= 2500 or text_to_audio_ms >= 2200:
        severity = "high"
    elif total_first_audio >= 1800 or asr_final_ms >= 1800 or first_delta_ms >= 1000 or text_to_audio_ms >= 900 or stall_count > 0:
        severity = "medium"
    elif bottleneck in {"unknown", "borderline"}:
        severity = "low"
    else:
        severity = "low"

    return {
        "latency_bottleneck": bottleneck,
        "latency_severity": severity,
        "latency_recommendation": recommendation,
        "latency_total_first_audio_ms": total_first_audio,
        "latency_asr_final_ms": asr_final_ms,
        "latency_llm_first_delta_ms": first_delta_ms,
        "latency_tts_first_text_ms": first_text_ms,
        "latency_tts_text_queue_ms": text_queue_ms,
        "latency_tts_first_audio_since_bridge_ms": first_audio_ms,
        "latency_tts_text_to_audio_ms": text_to_audio_ms,
        "latency_audio_send_realtime_x100": realtime_x100,
        "latency_audio_stall_count": stall_count,
        "latency_streamed_bridge": bool(streamed_bridge),
        "latency_tts_provider_stream": str(tts_provider_stream or ""),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    config = GatewayConfig(
        host=str(args.host),
        port=int(args.port),
        bridge_url=str(args.bridge_url),
        placeholder_goal=str(args.placeholder_goal),
        bridge_timeout_seconds=float(args.bridge_timeout),
    )
    try:
        asyncio.run(run_gateway(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
