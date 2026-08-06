from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import io
import json
import os
import re
import signal
import socket
import sys
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread, current_thread, main_thread
from time import monotonic
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .bridge import (
    DEFAULT_TIMEOUT_SECONDS,
    HermesLilyBridge,
    HermesLilyConfig,
    command_from_string,
    tuple_from_csv,
)
from .runtime_config import (
    load_runtime_bridge_config,
    public_runtime_config,
    read_hermes_provider_secret,
    read_hermes_provider_config,
    save_runtime_bridge_config,
)
from .quota import query_quota
from .dialogue_quota import DEFAULT_LIMIT, DEFAULT_WINDOW_SECONDS, DialogueQuota

try:
    from integrations.aura_persona_gateway.admin import (
        aura_runtime,
        aura_runtime_secret,
        check_admin_token,
        persona_assets,
        persona_health,
        persona_state,
        persona_world,
        refresh_aura_weather,
        update_aura_runtime,
        update_persona_assets,
        update_persona_config,
        update_persona_state,
    )
    from integrations.aura_persona_gateway.city_names import normalize_city_name
    from integrations.aura_persona_gateway.config import load_persona_config
    from integrations.aura_persona_gateway.llm import (
        DirectLlmClient,
        DirectLlmConfig,
        close_direct_llm_http_pool,
        warm_direct_llm_http_pool,
    )
    from integrations.aura_persona_gateway.runtime import load_aura_runtime_config
    from integrations.aura_persona_gateway.store import LilyPersonaStore
    from integrations.aura_persona_gateway.turn import AuraPersonaGateway
except ImportError:  # pragma: no cover - persona gateway can be omitted in tiny builds
    aura_runtime = None
    aura_runtime_secret = None
    check_admin_token = None
    load_aura_runtime_config = None
    load_persona_config = None
    DirectLlmClient = None
    DirectLlmConfig = None
    close_direct_llm_http_pool = None
    warm_direct_llm_http_pool = None
    persona_assets = None
    persona_health = None
    persona_state = None
    persona_world = None
    refresh_aura_weather = None
    update_aura_runtime = None
    update_persona_assets = None
    update_persona_config = None
    update_persona_state = None
    AuraPersonaGateway = None
    LilyPersonaStore = None

    def normalize_city_name(value: Any) -> str:
        return str(value or "").strip()


MAX_REQUEST_BYTES = 64 * 1024
DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_QUEUE_TIMEOUT_SECONDS = 30.0
USER_GEO_CACHE_TTL_SECONDS = 900
ADMIN_TOKEN_ENV = "AURA_LILY_ADMIN_TOKEN"
LEGACY_ADMIN_TOKEN_ENV = "AURA_PERSONA_ADMIN_TOKEN"
ADMIN_USER_ENV = "AURA_LILY_ADMIN_USER"
LEGACY_ADMIN_USER_ENV = "AURA_PERSONA_ADMIN_USER"
ADMIN_PASSWORD_ENV = "AURA_LILY_ADMIN_PASSWORD"
LEGACY_ADMIN_PASSWORD_ENV = "AURA_PERSONA_ADMIN_PASSWORD"
GATEWAY_STATUS_PATH_ENV = "AURA_LILY_GATEWAY_STATUS_PATH"
DEFAULT_GATEWAY_STATUS_PATH = "/data/aura-persona/config/gateway_status.json"
DIALOGUE_QUOTA_DB_ENV = "AURA_DIALOGUE_QUOTA_DB"
DEFAULT_DIALOGUE_QUOTA_DB = "/data/aura-persona/config/dialogue_quota.sqlite3"
QUOTA_REFRESH_SECONDS_ENV = "AURA_PROVIDER_QUOTA_REFRESH_SECONDS"
DEFAULT_QUOTA_REFRESH_SECONDS = 30 * 60
DEVICE_SAMPLE_RATE = 16_000
MIN_AUDIO_SAMPLE_RATE = 8_000
_USER_GEO_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


class LilyServerConfig:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        bridge_config: HermesLilyConfig,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
    ) -> None:
        self.host = host
        self.port = port
        self.bridge_config = bridge_config
        self.max_concurrency = max(1, int(max_concurrency or DEFAULT_MAX_CONCURRENCY))
        self.queue_timeout_seconds = max(0.0, float(queue_timeout_seconds or 0.0))


class LilyRuntime:
    def __init__(self, config: LilyServerConfig) -> None:
        self.bridge = HermesLilyBridge(load_runtime_bridge_config(config.bridge_config))
        self.slots = BoundedSemaphore(config.max_concurrency)
        self.queue_timeout_seconds = config.queue_timeout_seconds
        self.persona_config = load_persona_config() if load_persona_config else None
        self.aura_runtime_config = (
            load_aura_runtime_config(persona_home=self.persona_config.persona_home)
            if self.persona_config and load_aura_runtime_config
            else None
        )
        self._aura_llm_warm_signature = ""
        self._aura_llm_warm_thread: Thread | None = None
        self.persona_store = (
            LilyPersonaStore(self.persona_config.companion_db_path)
            if self.persona_config and LilyPersonaStore
            else None
        )
        configured_quota_db = str(os.environ.get(DIALOGUE_QUOTA_DB_ENV) or "").strip()
        quota_home = str(getattr(self.persona_config, "persona_home", "") or "").strip()
        quota_path = configured_quota_db or (
            str(Path(quota_home).expanduser() / "config" / "dialogue_quota.sqlite3")
            if quota_home
            else DEFAULT_DIALOGUE_QUOTA_DB
        )
        self.dialogue_quota = DialogueQuota(quota_path)
        self._provider_quota_lock = Lock()
        self._provider_quota_cache: dict[str, Any] | None = None
        self._provider_quota_checked_at = 0.0
        self._provider_quota_checked_at_wall = 0
        try:
            refresh_value = (
                os.environ.get(QUOTA_REFRESH_SECONDS_ENV)
                or os.environ.get("AURA_QUOTA_CACHE_TTL_SECONDS")
                or str(DEFAULT_QUOTA_REFRESH_SECONDS)
            )
            self._provider_quota_refresh_seconds = max(60.0, float(refresh_value))
        except (TypeError, ValueError):
            self._provider_quota_refresh_seconds = float(DEFAULT_QUOTA_REFRESH_SECONDS)
        self._schedule_aura_llm_warm(reason="init")

    def _refresh_aura_runtime_config(self) -> Any:
        if not load_aura_runtime_config:
            self.aura_runtime_config = None
            return None
        persona_home = self.persona_config.persona_home if self.persona_config else ""
        self.aura_runtime_config = load_aura_runtime_config(persona_home=persona_home)
        self._schedule_aura_llm_warm(reason="refresh")
        return self.aura_runtime_config

    def _direct_llm_warm_config(self) -> DirectLlmConfig | None:
        config = self.aura_runtime_config
        if not config or not DirectLlmConfig:
            return None
        if str(config.aura_model_mode or "").strip() not in {"aura_model", "direct_llm"}:
            return None
        if not str(config.aura_model_base_url or "").strip() or not str(config.aura_model_api_key or "").strip():
            return None
        return DirectLlmConfig(
            provider=config.aura_model_provider,
            model=config.aura_model_model,
            base_url=config.aura_model_base_url,
            api_key=config.aura_model_api_key,
            timeout_seconds=float(config.aura_model_timeout_seconds or 90),
            max_tokens=int(config.aura_model_max_tokens or 96),
            temperature=float(config.aura_model_temperature or 0.4),
            reasoning_effort=config.aura_model_reasoning_effort,
        )

    def _schedule_aura_llm_warm(self, *, reason: str) -> None:
        if not warm_direct_llm_http_pool:
            return
        warm_config = self._direct_llm_warm_config()
        signature = ""
        if warm_config is not None:
            signature = "|".join((
                str(warm_config.provider or ""),
                str(warm_config.model or ""),
                str(warm_config.base_url or ""),
                "key" if str(warm_config.api_key or "").strip() else "",
            ))
        if signature != self._aura_llm_warm_signature:
            if self._aura_llm_warm_signature and close_direct_llm_http_pool:
                close_direct_llm_http_pool()
            self._aura_llm_warm_signature = signature
        if not warm_config or not signature:
            return
        if self._aura_llm_warm_thread is not None and self._aura_llm_warm_thread.is_alive():
            return

        def worker() -> None:
            try:
                result = warm_direct_llm_http_pool(warm_config, timeout_seconds=1.5)
            except Exception as exc:  # pragma: no cover - defensive background path
                sys.stderr.write(f"aura-lily-server: aura llm warm failed: {exc.__class__.__name__}; reason={reason}\n")
                return
            status = str(result.get("status") or "")
            latency_ms = int(result.get("latency_ms") or 0)
            endpoint_host = str(result.get("endpoint_host") or "")
            sys.stderr.write(
                "aura-lily-server: aura_llm_http_warm "
                f"ok={bool(result.get('ok'))} status={status} latency_ms={latency_ms} "
                f"reason={reason} endpoint_host={endpoint_host}\n"
            )

        self._aura_llm_warm_thread = Thread(target=worker, name="aura-llm-http-warm", daemon=True)
        self._aura_llm_warm_thread.start()

    def run_turn(self, goal: str, *, metadata: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self.persona_config and self.persona_config.enabled:
            return self.run_persona_turn(goal, metadata=metadata)
        return self.run_plain_turn(goal, metadata=metadata)

    def run_plain_turn(self, goal: str, *, metadata: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        queued_started = monotonic()
        acquired = self.slots.acquire(timeout=self.queue_timeout_seconds)
        queued_ms = max(0, int((monotonic() - queued_started) * 1000))
        if not acquired:
            return 429, {
                "ok": False,
                "status": "failed",
                "error": "server is busy; retry later",
                "queued_ms": queued_ms,
            }
        try:
            quota = self.reserve_dialogue_quota(metadata)
            if quota is not None and not quota.get("allowed", True):
                return 429, self.dialogue_quota_error(
                    quota,
                    queued_ms=queued_ms,
                    language=metadata.get("language") if isinstance(metadata, dict) else "",
                )
            result = self.bridge.run(goal, metadata=metadata)
            payload = result.to_dict()
            payload["queued_ms"] = queued_ms
            if quota is not None:
                payload["dialogue_quota"] = quota
            return (200 if result.ok else 500), payload
        finally:
            self.slots.release()

    def run_persona_turn(self, goal: str, *, metadata: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not self.persona_config or not AuraPersonaGateway:
            return 503, {"ok": False, "status": "failed", "error": "persona gateway is unavailable"}
        queued_started = monotonic()
        acquired = self.slots.acquire(timeout=self.queue_timeout_seconds)
        queued_ms = max(0, int((monotonic() - queued_started) * 1000))
        if not acquired:
            return 429, {
                "ok": False,
                "status": "failed",
                "error": "server is busy; retry later",
                "queued_ms": queued_ms,
            }
        try:
            quota = self.reserve_dialogue_quota(metadata)
            if quota is not None and not quota.get("allowed", True):
                return 429, self.dialogue_quota_error(
                    quota,
                    queued_ms=queued_ms,
                    language=metadata.get("language") if isinstance(metadata, dict) else "",
                )
            runtime_config = self._refresh_aura_runtime_config()
            gateway = AuraPersonaGateway(
                config=self.persona_config,
                bridge=self.bridge,
                store=self.persona_store,
                runtime_config=runtime_config,
            )
            result = gateway.run_turn(goal, metadata=metadata)
            self.aura_runtime_config = gateway.runtime_config
            payload = result.to_dict()
            payload["queued_ms"] = queued_ms
            payload["persona"] = True
            if quota is not None:
                payload["dialogue_quota"] = quota
            return (200 if result.ok else 500), payload
        finally:
            self.slots.release()

    def stream_turn(self, goal: str, *, metadata: dict[str, Any], persona_only: bool = False) -> Iterator[dict[str, Any]]:
        if self.persona_config and self.persona_config.enabled:
            yield from self.stream_persona_turn(goal, metadata=metadata)
            return
        if persona_only:
            yield {"type": "final", "status": 503, "payload": {"ok": False, "status": "failed", "error": "persona gateway is unavailable"}}
            return
        status, payload = self.run_plain_turn(goal, metadata=metadata)
        yield {"type": "final", "status": status, "payload": payload}

    def stream_persona_turn(self, goal: str, *, metadata: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if not self.persona_config or not AuraPersonaGateway:
            yield {"type": "final", "status": 503, "payload": {"ok": False, "status": "failed", "error": "persona gateway is unavailable"}}
            return
        queued_started = monotonic()
        acquired = self.slots.acquire(timeout=self.queue_timeout_seconds)
        queued_ms = max(0, int((monotonic() - queued_started) * 1000))
        if not acquired:
            yield {
                "type": "final",
                "status": 429,
                "payload": {
                    "ok": False,
                    "status": "failed",
                    "error": "server is busy; retry later",
                    "queued_ms": queued_ms,
                },
            }
            return
        try:
            quota = self.reserve_dialogue_quota(metadata)
            if quota is not None and not quota.get("allowed", True):
                yield {
                    "type": "final",
                    "status": 429,
                    "payload": self.dialogue_quota_error(
                        quota,
                        queued_ms=queued_ms,
                        language=metadata.get("language") if isinstance(metadata, dict) else "",
                    ),
                }
                return
            runtime_config = self._refresh_aura_runtime_config()
            gateway = AuraPersonaGateway(
                config=self.persona_config,
                bridge=self.bridge,
                store=self.persona_store,
                runtime_config=runtime_config,
            )
            for event in gateway.run_direct_turn_stream(goal, metadata=metadata):
                if event.get("type") == "final" and isinstance(event.get("payload"), dict):
                    event["payload"]["queued_ms"] = queued_ms
                    event["payload"]["persona"] = True
                    if quota is not None:
                        event["payload"]["dialogue_quota"] = quota
                    event.setdefault("status", 200 if event["payload"].get("ok") else 500)
                yield event
            self.aura_runtime_config = gateway.runtime_config
        finally:
            self.slots.release()

    def persona_health(self) -> dict[str, Any]:
        if not self.persona_config or not self.persona_store or not persona_health:
            return {"ok": False, "enabled": False, "error": "persona gateway is unavailable"}
        return persona_health(self.persona_config, self.persona_store)

    def update_persona_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not self.persona_config or not update_persona_config:
            return {"ok": False, "error": "persona gateway is unavailable"}
        self.persona_config = update_persona_config(self.persona_config, updates)
        self.aura_runtime_config = (
            load_aura_runtime_config(persona_home=self.persona_config.persona_home)
            if load_aura_runtime_config
            else None
        )
        self.persona_store = LilyPersonaStore(self.persona_config.companion_db_path) if LilyPersonaStore else None
        self._schedule_aura_llm_warm(reason="persona_config_update")
        return {"ok": True, "config": self.persona_config.public_dict()}

    def aura_runtime(self) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not aura_runtime:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        return aura_runtime(self.aura_runtime_config)

    def aura_runtime_secret(self, key: str) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not aura_runtime_secret:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        return aura_runtime_secret(self.aura_runtime_config, key)

    def update_aura_runtime(self, updates: dict[str, Any]) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not update_aura_runtime:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        self.aura_runtime_config = update_aura_runtime(self.aura_runtime_config, updates)
        self._invalidate_provider_quota_cache()
        self._schedule_aura_llm_warm(reason="update")
        return {"ok": True, "config": self.aura_runtime_config.public_dict()}

    def copy_stepfun_plan_key_to_asr(self) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not update_aura_runtime:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        config = self.aura_runtime_config
        candidates = [
            (
                "Aura LLM",
                str(config.aura_model_api_key or "").strip(),
                str(config.aura_model_provider or "").strip().lower(),
                str(config.aura_model_base_url or "").strip(),
            ),
            (
                "TTS",
                str(config.tts_api_key or "").strip(),
                str(config.tts_provider or "").strip().lower(),
                str(config.tts_base_url or "").strip(),
            ),
        ]
        source = ""
        api_key = ""
        for label, key, provider, base_url in candidates:
            if key and provider == "stepfun" and "step_plan" in base_url:
                source = label
                api_key = key
                break
        if not api_key:
            return {
                "ok": False,
                "error": "没有可复用的已保存 StepFun Step Plan Key；请先保存 Aura LLM 或 TTS 的 StepFun Plan Key。",
            }
        provider = str(config.asr_provider or "").strip().lower()
        model = str(config.asr_model or "").strip()
        base_url = str(config.asr_base_url or "").strip()
        if provider == "stepfun" and model == "stepaudio-2.5-asr" and "step_plan" in base_url:
            asr_provider = "stepfun"
            asr_model = "stepaudio-2.5-asr"
            asr_base_url = base_url
        else:
            asr_provider = "stepfun"
            asr_model = "stepaudio-2.5-asr"
            asr_base_url = "https://api.stepfun.com/step_plan/v1"
        self.aura_runtime_config = update_aura_runtime(self.aura_runtime_config, {
            "asr_enabled": True,
            "asr_mode": "api",
            "asr_provider": asr_provider,
            "asr_model": asr_model,
            "asr_base_url": asr_base_url,
            "asr_api_key": api_key,
            "asr_language": config.asr_language or "zh",
        })
        self._schedule_aura_llm_warm(reason="copy_stepfun_plan_key")
        return {
            "ok": True,
            "source": source,
            "config": self.aura_runtime_config.public_dict(),
        }

    def apply_stepfun_open_platform(self) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not update_aura_runtime:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        config = self.aura_runtime_config
        candidates = [
            ("Aura LLM", str(config.aura_model_api_key or "").strip()),
            ("TTS", str(config.tts_api_key or "").strip()),
            ("ASR", str(config.asr_api_key or "").strip()),
        ]
        source = ""
        api_key = ""
        for label, key in candidates:
            if key:
                source = label
                api_key = key
                break
        if not api_key:
            return {
                "ok": False,
                "error": "没有可复用的 StepFun API Key；请先在 Aura LLM、TTS 或 ASR 任一处保存 Key。",
            }
        self.aura_runtime_config = update_aura_runtime(self.aura_runtime_config, {
            "aura_model_mode": "aura_model",
            "aura_model_provider": "stepfun",
            "aura_model_model": "stepaudio-2.5-chat",
            "aura_model_base_url": "https://api.stepfun.com/v1",
            "aura_model_api_key": api_key,
            "aura_model_reasoning_effort": "",
            "aura_model_max_tokens": 96,
            "tts_enabled": True,
            "tts_provider": "stepfun",
            "tts_model": "stepaudio-2.5-tts",
            "tts_base_url": "https://api.stepfun.com/v1",
            "tts_api_key": api_key,
            "asr_enabled": True,
            "asr_mode": "api",
            "asr_provider": "stepfun",
            "asr_model": "stepaudio-2.5-asr-stream",
            "asr_base_url": "https://api.stepfun.com/v1",
            "asr_api_key": api_key,
            "asr_language": config.asr_language or "zh",
        })
        self._schedule_aura_llm_warm(reason="apply_stepfun_open_platform")
        return {
            "ok": True,
            "source": source,
            "billing_scope": "open_platform",
            "config": self.aura_runtime_config.public_dict(),
        }

    def refresh_aura_weather(self, updates: dict[str, Any]) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config or not refresh_aura_weather:
            return {"ok": False, "error": "aura runtime config is unavailable"}
        city = str(updates.get("city") or "").strip()
        force = _coerce_bool(updates.get("force"), True)
        self.aura_runtime_config, result = refresh_aura_weather(self.aura_runtime_config, city=city, force=force)
        return {"ok": bool(result.get("ok")), "result": result, "config": self.aura_runtime_config.public_dict()}

    def persona_assets(self) -> dict[str, Any]:
        if not self.persona_config or not persona_assets:
            return {"ok": False, "error": "persona gateway is unavailable"}
        return persona_assets(self.persona_config)

    def update_persona_assets(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not self.persona_config or not update_persona_assets:
            return {"ok": False, "error": "persona gateway is unavailable"}
        return update_persona_assets(self.persona_config, updates)

    def persona_state(self) -> dict[str, Any]:
        if not self.persona_config or not self.persona_store or not persona_state:
            return {"ok": False, "error": "persona gateway is unavailable"}
        return persona_state(self.persona_config, self.persona_store)

    def persona_world(self) -> dict[str, Any]:
        if not self.persona_config or not self.persona_store or not persona_world:
            return {"ok": False, "error": "persona gateway is unavailable"}
        return persona_world(self.persona_config, self.persona_store)

    def update_persona_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not self.persona_config or not self.persona_store or not update_persona_state:
            return {"ok": False, "error": "persona gateway is unavailable"}
        return update_persona_state(self.persona_config, self.persona_store, updates)

    def background_task_result(self, task_id: str) -> dict[str, Any]:
        if not self.persona_config or not self.persona_store:
            return {"ok": False, "status": "unavailable", "error": "persona gateway is unavailable"}
        result = self.persona_store.background_task_result(self.persona_config.scope, task_id=task_id)
        if not result:
            return {"ok": False, "status": "pending", "task_id": str(task_id or "").strip()}
        return {
            "ok": str(result.get("status") or "") == "sent",
            "status": str(result.get("status") or ""),
            "task_id": str(result.get("task_id") or task_id or ""),
            "body": str(result.get("body") or ""),
            "created_at": result.get("created_at"),
        }

    def hermes_config(self) -> dict[str, Any]:
        return public_runtime_config(self.bridge.config)

    def model_quotas(self, *, force: bool = False) -> dict[str, Any]:
        """Query both configured model routes without returning credentials."""
        current = monotonic()
        with self._provider_quota_lock:
            if (
                self._provider_quota_cache
                and not force
                and current - self._provider_quota_checked_at < self._provider_quota_refresh_seconds
            ):
                provider_payload = dict(self._provider_quota_cache)
            else:
                hermes = read_hermes_provider_config(self.bridge.config)
                hermes_result = query_quota(
                    provider=self.bridge.config.provider,
                    model=self.bridge.config.model,
                    base_url=hermes.get("base_url", ""),
                    api_key=read_hermes_provider_secret(self.bridge.config, "api_key").get("value", ""),
                )
                aura_config = self._refresh_aura_runtime_config()
                aura_result = query_quota(
                    provider=aura_config.aura_model_provider if aura_config else "",
                    model=aura_config.aura_model_model if aura_config else "",
                    base_url=aura_config.aura_model_base_url if aura_config else "",
                    api_key=aura_config.aura_model_api_key if aura_config else "",
                )
                provider_payload = {"aura": aura_result, "hermes": hermes_result}
                self._provider_quota_cache = dict(provider_payload)
                self._provider_quota_checked_at = current
                self._provider_quota_checked_at_wall = int(time.time())
        return {
            "ok": True,
            **provider_payload,
            "dialogue": self.dialogue_quota_snapshot(),
            "provider_quota_checked_at": self._provider_quota_checked_at_wall,
            "provider_quota_refresh_seconds": int(self._provider_quota_refresh_seconds),
        }

    def dialogue_quota_snapshot(self) -> dict[str, Any]:
        config = self.aura_runtime_config
        limit = int(getattr(config, "dialogue_quota_limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)
        window = int(getattr(config, "dialogue_quota_window_seconds", DEFAULT_WINDOW_SECONDS) or DEFAULT_WINDOW_SECONDS)
        return self.dialogue_quota.snapshot(limit=limit, window_seconds=window)

    def reserve_dialogue_quota(self, metadata: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(metadata, dict) and bool(metadata.get("speculative")):
            return None
        config = self.aura_runtime_config
        limit = int(getattr(config, "dialogue_quota_limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)
        window = int(getattr(config, "dialogue_quota_window_seconds", DEFAULT_WINDOW_SECONDS) or DEFAULT_WINDOW_SECONDS)
        return self.dialogue_quota.consume(limit=limit, window_seconds=window)

    @staticmethod
    def dialogue_quota_error(
        quota: dict[str, Any],
        *,
        queued_ms: int = 0,
        language: Any = "zh",
    ) -> dict[str, Any]:
        language_text = str(language or "zh").strip().lower()
        if language_text.startswith("en"):
            detail = "This chat window is used up for now. Please wait for it to reset before we continue."
            response = "We've had a lovely chat, but this window is used up. Let's continue when it resets, okay?"
        elif language_text.startswith("ja"):
            detail = "この時間帯の会話回数を使い切りました。回復するまで少し待ってください。"
            response = "たくさん話せてうれしかったよ。この時間帯の回数を使い切ったから、回復したらまた話そうね。"
        else:
            detail = "这段时间的对话次数已用完，请等额度窗口恢复后再继续。"
            response = "这段时间聊得很开心，不过次数用完啦。额度恢复后，我们再继续，好吗？"
        return {
            "ok": False,
            "status": "quota_exceeded",
            "error": "dialogue quota exceeded",
            "detail": detail,
            "response": response,
            "voice_text": response,
            "queued_ms": int(queued_ms),
            "dialogue_quota": quota,
        }

    def location_summary(self) -> dict[str, Any]:
        return build_location_summary(self.persona_config)

    def hermes_secret(self, key: str) -> dict[str, Any]:
        return read_hermes_provider_secret(self.bridge.config, key)

    def update_hermes_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        bridge_config = save_runtime_bridge_config(self.bridge.config, updates)
        self.bridge = HermesLilyBridge(bridge_config)
        self._invalidate_provider_quota_cache()
        return {"ok": True, "config": self.hermes_config()}

    def _invalidate_provider_quota_cache(self) -> None:
        with self._provider_quota_lock:
            self._provider_quota_cache = None
            self._provider_quota_checked_at = 0.0
            self._provider_quota_checked_at_wall = 0

    def test_hermes(self) -> dict[str, Any]:
        prompt = "请只回复：Lily Hermes ok"
        result = self.bridge.run(prompt, metadata={"source": "admin_test", "kind": "hermes_llm"})
        return _test_result(
            ok=result.ok,
            kind="hermes_llm",
            provider=self.bridge.config.provider,
            model=self.bridge.config.model,
            latency_ms=result.latency_ms,
            detail="Hermes 主模型连通。" if result.ok else result.response,
        )

    def test_aura_model(self) -> dict[str, Any]:
        self._refresh_aura_runtime_config()
        if not self.aura_runtime_config:
            return _test_result(ok=False, kind="aura_llm", detail="Aura runtime config is unavailable")
        if self.persona_config and AuraPersonaGateway:
            gateway = AuraPersonaGateway(
                config=self.persona_config,
                bridge=self.bridge,
                store=self.persona_store,
                runtime_config=self.aura_runtime_config,
            )
            result = gateway._run_aura_model(
                "请只回复：Lily Aura ok",
                metadata={"source": "admin_test", "kind": "aura_llm"},
            )
            provider = self.aura_runtime_config.aura_model_provider if result.evidence.get("route") == "direct_llm" else gateway._aura_model_bridge().config.provider
            model = self.aura_runtime_config.aura_model_model if result.evidence.get("route") == "direct_llm" else gateway._aura_model_bridge().config.model
        elif self.aura_runtime_config and self.aura_runtime_config.aura_model_mode in {"aura_model", "direct_llm"} and DirectLlmClient and DirectLlmConfig:
            result = DirectLlmClient(
                DirectLlmConfig(
                    provider=self.aura_runtime_config.aura_model_provider,
                    model=self.aura_runtime_config.aura_model_model,
                    base_url=self.aura_runtime_config.aura_model_base_url,
                    api_key=self.aura_runtime_config.aura_model_api_key,
                    timeout_seconds=float(self.aura_runtime_config.aura_model_timeout_seconds or 90),
                    max_tokens=int(self.aura_runtime_config.aura_model_max_tokens or 96),
                    temperature=float(self.aura_runtime_config.aura_model_temperature or 0.4),
                    reasoning_effort=self.aura_runtime_config.aura_model_reasoning_effort,
                )
            ).run("请只回复：Lily Aura ok", metadata={"source": "admin_test", "kind": "aura_llm"})
            provider = self.aura_runtime_config.aura_model_provider
            model = self.aura_runtime_config.aura_model_model
        else:
            result = self.bridge.run(
                "请只回复：Lily Aura ok",
                metadata={"source": "admin_test", "kind": "aura_llm"},
            )
            provider = self.bridge.config.provider
            model = self.bridge.config.model
        return _test_result(
            ok=result.ok,
            kind="aura_llm",
            provider=provider,
            model=model,
            latency_ms=result.latency_ms,
            detail="Aura 主模型连通。" if result.ok else result.response,
        )

    def test_tts(self) -> dict[str, Any]:
        config = self._refresh_aura_runtime_config()
        if not config:
            return _test_result(ok=False, kind="tts", detail="Aura runtime config is unavailable")
        if not config.tts_enabled:
            return _test_result(ok=False, kind="tts", provider=config.tts_provider, model=config.tts_model, detail="TTS 未启用")
        started = monotonic()
        probe = _probe_tts_endpoint(
            config.tts_base_url,
            provider=config.tts_provider,
            model=config.tts_model,
            voice=config.tts_voice,
            api_key=config.tts_api_key,
            audio_format=config.tts_format,
            sample_rate=int(config.tts_sample_rate or 0),
            timeout=float(config.tts_timeout_seconds or 15),
        )
        payload = _test_result(
            ok=probe.get("ok", False),
            kind="tts",
            provider=config.tts_provider,
            model=config.tts_model,
            latency_ms=max(0, int((monotonic() - started) * 1000)),
            detail=probe.get("detail") or "",
            endpoint_host=probe.get("endpoint_host") or "",
            stage=probe.get("stage") or "",
        )
        for key in (
            "requested_sample_rate",
            "source_sample_rate",
            "device_sample_rate",
            "resampled_for_device",
            "audio_format",
            "audio_bytes",
            "device_audio_bytes",
            "audio_data_url",
            "audio_mime_type",
        ):
            if key in probe:
                payload[key] = probe[key]
        return payload

    def test_asr(self) -> dict[str, Any]:
        config = self._refresh_aura_runtime_config()
        if not config:
            return _test_result(ok=False, kind="asr", detail="Aura runtime config is unavailable")
        if not config.asr_enabled:
            return _test_result(ok=False, kind="asr", provider=config.asr_provider, model=config.asr_model, detail="ASR 未启用")
        if config.asr_mode == "local":
            return _test_result(
                ok=True,
                kind="asr",
                provider=config.asr_provider,
                model=config.asr_model,
                detail="本地 ASR 配置已保存；实际模型加载会在语音链路接入时验证。",
            )
        if config.asr_provider == "stepfun" and "stream" in str(config.asr_model or "").lower():
            started = monotonic()
            probe = _probe_stepfun_realtime_asr_ws(
                config.asr_base_url,
                model=config.asr_model,
                language=config.asr_language,
                api_key=config.asr_api_key,
                timeout=float(config.asr_timeout_seconds or 30),
            )
            return _test_result(
                ok=probe.get("ok", False),
                kind="asr",
                provider=config.asr_provider,
                model=config.asr_model,
                latency_ms=max(0, int((monotonic() - started) * 1000)),
                detail=probe.get("detail") or "",
                endpoint_host=probe.get("endpoint_host") or "",
                stage=probe.get("stage") or "",
            )
        if config.asr_provider in {"stepfun-realtime", "stepfun_realtime"}:
            started = monotonic()
            probe = _probe_stepfun_step_plan_realtime_ws(
                config.asr_base_url,
                model=config.asr_model,
                api_key=config.asr_api_key,
                timeout=float(config.asr_timeout_seconds or 30),
            )
            return _test_result(
                ok=probe.get("ok", False),
                kind="asr",
                provider=config.asr_provider,
                model=config.asr_model,
                latency_ms=max(0, int((monotonic() - started) * 1000)),
                detail=probe.get("detail") or "",
                endpoint_host=probe.get("endpoint_host") or "",
                stage=probe.get("stage") or "",
            )
        started = monotonic()
        probe = _probe_asr_endpoint(
            config.asr_base_url,
            provider=config.asr_provider,
            timeout=float(config.asr_timeout_seconds or 30),
        )
        return _test_result(
            ok=probe.get("ok", False),
            kind="asr",
            provider=config.asr_provider,
            model=config.asr_model,
            latency_ms=max(0, int((monotonic() - started) * 1000)),
            detail=probe.get("detail") or "",
            endpoint_host=probe.get("endpoint_host") or "",
            stage=probe.get("stage") or "",
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aura-lily-server",
        description="HTTP bridge from ESP32/Mini requests to Hermes CLI.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--toolsets", default=",".join(HermesLilyConfig().toolsets))
    parser.add_argument("--skills", default="")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--hermes-command", default="hermes")
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--queue-timeout", type=float, default=DEFAULT_QUEUE_TIMEOUT_SECONDS)
    parser.add_argument("--ignore-rules", action="store_true")
    parser.add_argument("--no-accept-hooks", action="store_true")
    parser.add_argument("--yolo", action="store_true")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> LilyServerConfig:
    bridge_config = HermesLilyConfig(
        command=command_from_string(args.hermes_command),
        provider=str(args.provider or ""),
        model=str(args.model or ""),
        cwd=str(args.cwd or ""),
        hermes_home=str(args.hermes_home or ""),
        toolsets=tuple_from_csv(args.toolsets),
        skills=tuple_from_csv(args.skills),
        timeout_seconds=float(args.timeout),
        accept_hooks=not bool(args.no_accept_hooks),
        ignore_rules=bool(args.ignore_rules),
        yolo=bool(args.yolo),
    )
    return LilyServerConfig(
        host=str(args.host),
        port=int(args.port),
        bridge_config=bridge_config,
        max_concurrency=int(args.max_concurrency),
        queue_timeout_seconds=float(args.queue_timeout),
    )


def make_handler(config: LilyServerConfig) -> type[BaseHTTPRequestHandler]:
    runtime = LilyRuntime(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "AuraLilyHermes/0.1"

        def do_GET(self) -> None:
            request_path = urlsplit(self.path).path
            if request_path in {"/admin", "/admin/"}:
                self._send_html(render_admin_page())
                return
            if request_path in {"/admin/style.css", "/admin/app.js"}:
                content, content_type = render_admin_asset(request_path)
                if not content:
                    self._send_json({"ok": False, "error": "not_found"}, status=404)
                    return
                self._send_text(content, content_type=content_type)
                return
            if self.path == "/health":
                self._send_json({
                    "ok": True,
                    "service": "aura-lily-hermes",
                    "provider": runtime.bridge.config.provider,
                    "model": runtime.bridge.config.model,
                    "persona_enabled": bool(runtime.persona_config and runtime.persona_config.enabled),
                })
                return
            if self.path == "/admin/summary":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                self._send_json({
                    "ok": True,
                    "health": {
                        "ok": True,
                        "service": "aura-lily-hermes",
                        "provider": runtime.bridge.config.provider,
                        "model": runtime.bridge.config.model,
                        "persona_enabled": bool(runtime.persona_config and runtime.persona_config.enabled),
                    },
                    "hermes": runtime.hermes_config(),
                    "aura_runtime": runtime.aura_runtime_config.public_dict() if runtime.aura_runtime_config else {},
                    "persona": runtime.persona_config.public_dict() if runtime.persona_config else {},
                    "location": runtime.location_summary(),
                })
                return
            if request_path == "/admin/quota":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                query = parse_qs(urlsplit(self.path).query)
                force_refresh = str((query.get("refresh") or [""])[0]).strip().lower() in {"1", "true", "yes"}
                response = runtime.model_quotas(force=force_refresh)
                self._send_json(response, status=200)
                return
            if self.path == "/admin/hermes/config":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                self._send_json({"ok": True, "config": runtime.hermes_config()})
                return
            if self.path.startswith("/admin/hermes/secret/"):
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                key = self.path.rsplit("/", 1)[-1]
                response = runtime.hermes_secret(key)
                self._send_json(response, status=200 if response.get("ok") else 404)
                return
            if self.path == "/admin/aura/runtime":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.aura_runtime()
                self._send_json(response, status=200 if response.get("ok") else 500)
                return
            if self.path == "/admin/aura/weather/refresh":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.refresh_aura_weather({})
                self._send_json(response, status=200 if response.get("ok") else 502)
                return
            if self.path == "/admin/aura/copy-stepfun-plan-key":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.copy_stepfun_plan_key_to_asr()
                self._send_json(response, status=200 if response.get("ok") else 400)
                return
            if self.path == "/admin/aura/apply-stepfun-open-platform":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.apply_stepfun_open_platform()
                self._send_json(response, status=200 if response.get("ok") else 400)
                return
            if self.path == "/admin/test/hermes":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.test_hermes()
                self._send_json(response, status=200 if response.get("ok") else 502)
                return
            if self.path == "/admin/test/aura-model":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.test_aura_model()
                self._send_json(response, status=200 if response.get("ok") else 502)
                return
            if self.path == "/admin/test/tts":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.test_tts()
                self._send_json(response, status=200 if response.get("ok") else 502)
                return
            if self.path == "/admin/test/asr":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.test_asr()
                self._send_json(response, status=200 if response.get("ok") else 502)
                return
            if self.path.startswith("/admin/aura/secret/"):
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                key = self.path.rsplit("/", 1)[-1]
                response = runtime.aura_runtime_secret(key)
                self._send_json(response, status=200 if response.get("ok") else 404)
                return
            if self.path == "/persona/health":
                self._send_json(runtime.persona_health())
                return
            if self.path.startswith("/persona/background-task/"):
                task_id = unquote(self.path.rsplit("/", 1)[-1])
                response = runtime.background_task_result(task_id)
                self._send_json(response, status=200 if response.get("ok") or response.get("status") == "pending" else 500)
                return
            if self.path == "/persona/config":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                payload = runtime.persona_config.public_dict() if runtime.persona_config else {}
                self._send_json({"ok": True, "config": payload})
                return
            if self.path == "/persona/assets":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.persona_assets()
                self._send_json(response, status=200 if response.get("ok") else 500)
                return
            if self.path == "/persona/state":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.persona_state()
                self._send_json(response, status=200 if response.get("ok") else 500)
                return
            if self.path == "/persona/world":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.persona_world()
                self._send_json(response, status=200 if response.get("ok") else 500)
                return
            self._send_json({"ok": False, "error": "not_found"}, status=404)

        def do_POST(self) -> None:
            if self.path not in {
                "/turn",
                "/turn/stream",
                "/persona/turn",
                "/persona/turn/stream",
                "/persona/config",
                "/persona/assets",
                "/persona/state",
                "/admin/hermes/config",
                "/admin/aura/runtime",
                "/admin/aura/weather/refresh",
            }:
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            try:
                payload = self._read_json(limit=MAX_REQUEST_BYTES)
            except ValueError as exc:
                self._send_json({"ok": False, "status": "failed", "error": str(exc)}, status=400)
                return

            if self.path == "/persona/config":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.update_persona_config(payload)
                self._send_json(response, status=200 if response.get("ok") else 500)
                return

            if self.path == "/persona/assets":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.update_persona_assets(payload)
                self._send_json(response, status=200 if response.get("ok") else 400)
                return

            if self.path == "/persona/state":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.update_persona_state(payload)
                self._send_json(response, status=200 if response.get("ok") else 500)
                return

            if self.path == "/admin/hermes/config":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.update_hermes_config(payload)
                self._send_json(response, status=200 if response.get("ok") else 500)
                return

            if self.path == "/admin/aura/runtime":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.update_aura_runtime(payload)
                self._send_json(response, status=200 if response.get("ok") else 500)
                return

            if self.path == "/admin/aura/weather/refresh":
                if not self._admin_allowed():
                    self._send_json({"ok": False, "error": "unauthorized"}, status=401)
                    return
                response = runtime.refresh_aura_weather(payload)
                self._send_json(response, status=200 if response.get("ok") else 502)
                return

            goal = str(payload.get("goal") or payload.get("text") or payload.get("transcript") or "").strip()
            if not goal:
                self._send_json(
                    {"ok": False, "status": "failed", "error": "goal is required"},
                    status=400,
                )
                return
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            metadata = _metadata_with_user_geo(metadata, self, runtime.persona_config)
            if self.path in {"/turn/stream", "/persona/turn/stream"}:
                self._send_json_stream(
                    runtime.stream_turn(
                        goal,
                        metadata=metadata,
                        persona_only=self.path == "/persona/turn/stream",
                    )
                )
                return

            if self.path == "/persona/turn":
                status, response = runtime.run_persona_turn(goal, metadata=metadata)
            else:
                status, response = runtime.run_turn(goal, metadata=metadata)
            self._send_json(response, status=status)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("aura-lily-server: " + (fmt % args) + "\n")

        def _read_json(self, *, limit: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length <= 0:
                raise ValueError("request body is required")
            if length > limit:
                raise ValueError(f"request body is too large; max {limit} bytes")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid json: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("json object is required")
            return payload

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json_stream(self, events: Iterator[dict[str, Any]], *, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                for event in events:
                    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                    self.wfile.write(body)
                    self.wfile.flush()
            except Exception as exc:
                self.log_message(
                    "stream error: %s: %s\n%s",
                    exc.__class__.__name__,
                    exc,
                    traceback.format_exc(),
                )
                body = json.dumps(
                    {
                        "type": "error",
                        "status": 500,
                        "error": exc.__class__.__name__,
                        "detail": str(exc),
                        "response": "本地人格服务临时不可用，请再试一次。",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                self.wfile.write(body)
                self.wfile.flush()

        def _send_html(self, content: str, *, status: int = 200) -> None:
            self._send_text(content, content_type="text/html; charset=utf-8", status=status)

        def _send_text(self, content: str, *, content_type: str, status: int = 200) -> None:
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _admin_allowed(self) -> bool:
            headers = {key.lower(): value for key, value in self.headers.items()}
            if runtime.persona_config and check_admin_token:
                return check_admin_token(runtime.persona_config, headers)
            if _basic_admin_allowed(headers):
                return True
            token = os.environ.get(ADMIN_TOKEN_ENV) or os.environ.get(LEGACY_ADMIN_TOKEN_ENV) or ""
            if not token:
                return False
            supplied = headers.get("x-aura-admin-token") or _bearer_token(headers)
            return hmac.compare_digest(supplied, token)

    return Handler


def _basic_admin_allowed(headers: dict[str, str]) -> bool:
    expected_password = (
        os.environ.get(ADMIN_PASSWORD_ENV)
        or os.environ.get(LEGACY_ADMIN_PASSWORD_ENV)
        or os.environ.get(ADMIN_TOKEN_ENV)
        or os.environ.get(LEGACY_ADMIN_TOKEN_ENV)
        or ""
    )
    if not expected_password:
        return False
    expected_user = os.environ.get(ADMIN_USER_ENV) or os.environ.get(LEGACY_ADMIN_USER_ENV) or "admin"
    authorization = headers.get("authorization", "").strip()
    if not authorization.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(authorization.split(" ", 1)[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    supplied_user, sep, supplied_password = raw.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(supplied_user, expected_user) and hmac.compare_digest(supplied_password, expected_password)


def _bearer_token(headers: dict[str, str]) -> str:
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


def build_location_summary(persona_config: Any | None = None) -> dict[str, Any]:
    mode = str(getattr(persona_config, "user_location_mode", "") or os.environ.get("AURA_USER_LOCATION_MODE", "device_ip")).strip() or "device_ip"
    manual_geo = _configured_user_geo(persona_config)
    auto_enabled = _user_geo_auto_enabled(persona_config)
    gateway_status = _read_gateway_status()
    device_public_ip = str(gateway_status.get("device_public_ip") or "").strip()
    client_ip = str(gateway_status.get("client_ip") or "").strip()
    effective_geo = dict(manual_geo)
    source = str(effective_geo.get("source") or "")
    if not effective_geo and auto_enabled and device_public_ip and not _is_private_or_loopback_ip(device_public_ip):
        effective_geo = _lookup_user_geo(device_public_ip)
        source = str(effective_geo.get("source") or "device_ip")
    normalized_mode = str(mode).strip().lower()
    if normalized_mode in {"manual", "fixed", "configured"}:
        status = "manual" if manual_geo else "manual_missing"
    elif effective_geo:
        status = "auto_ready"
    else:
        status = "auto_waiting"
    if normalized_mode in {"disabled", "off", "none"}:
        status = "disabled"
    return {
        "ok": True,
        "mode": mode,
        "status": status,
        "auto_enabled": auto_enabled,
        "manual_configured": bool(manual_geo),
        "manual_geo": manual_geo,
        "effective_geo": effective_geo,
        "effective_source": source,
        "gateway_status": {
            "available": bool(gateway_status),
            "updated_at": gateway_status.get("updated_at"),
            "age_seconds": _age_seconds(gateway_status.get("updated_at")),
            "device_id": str(gateway_status.get("device_id") or ""),
            "boot_id": str(gateway_status.get("boot_id") or ""),
            "client_ip": _mask_ip(client_ip),
            "client_ip_private": _is_private_or_loopback_ip(client_ip) if client_ip else None,
            "device_public_ip_configured": bool(device_public_ip),
            "device_public_ip": _mask_ip(device_public_ip),
            "source_event": str(gateway_status.get("source_event") or ""),
        },
        "notes": [
            "device_ip mode uses only the ESP32-reported public IP.",
            "Client private IP is intentionally ignored for geolocation.",
            "manual mode is the reliable fallback when the device cannot report a public IP.",
        ],
    }


def _gateway_status_path() -> Path:
    return Path(os.environ.get(GATEWAY_STATUS_PATH_ENV, DEFAULT_GATEWAY_STATUS_PATH)).expanduser()


def _read_gateway_status() -> dict[str, Any]:
    path = _gateway_status_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _age_seconds(timestamp: Any) -> int | None:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None
    return max(0, int(time.time() - value))


def _mask_ip(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return ".".join(parts[:3] + ["*"])
    return text


def _metadata_with_user_geo(
    metadata: dict[str, Any],
    handler: BaseHTTPRequestHandler,
    persona_config: Any | None = None,
) -> dict[str, Any]:
    enriched = dict(metadata or {})
    if isinstance(enriched.get("user_geo"), dict) and (
        enriched["user_geo"].get("city") or enriched["user_geo"].get("timezone")
    ):
        enriched["user_geo"] = _normalized_user_geo(enriched["user_geo"])
        return enriched
    geo = _configured_user_geo(persona_config)
    if not geo and _user_geo_auto_enabled(persona_config):
        geo = _request_user_geo(handler, metadata=enriched)
    if geo:
        enriched["user_geo"] = _normalized_user_geo(geo)
    return enriched


def _request_user_geo(handler: BaseHTTPRequestHandler, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {key.lower(): value for key, value in handler.headers.items()}
    client_ip = _device_public_ip(metadata)
    if not client_ip:
        client_ip = _client_ip(headers, handler.client_address[0] if handler.client_address else "", metadata=metadata)
    if not client_ip or _is_private_or_loopback_ip(client_ip):
        return {}
    return _lookup_user_geo(client_ip)


def _client_ip(headers: dict[str, str], fallback: str, *, metadata: dict[str, Any] | None = None) -> str:
    for key in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip"):
        value = str(headers.get(key) or "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    if isinstance(metadata, dict):
        value = str(metadata.get("client_ip") or metadata.get("device_ip") or "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    return str(fallback or "").strip()


def _configured_user_geo(persona_config: Any | None = None) -> dict[str, Any]:
    if persona_config and hasattr(persona_config, "configured_user_geo"):
        geo = persona_config.configured_user_geo()
        if geo:
            return _normalized_user_geo(geo)
    city = str(os.environ.get("AURA_USER_HOME_CITY", "") or "").strip()
    timezone = str(os.environ.get("AURA_USER_TIMEZONE", "") or "").strip()
    latitude = _float_text(os.environ.get("AURA_USER_LATITUDE", ""))
    longitude = _float_text(os.environ.get("AURA_USER_LONGITUDE", ""))
    if not (city or timezone or latitude or longitude):
        return {}
    geo = {
        "city": city,
        "timezone": timezone,
        "latitude": latitude,
        "longitude": longitude,
        "source": "manual",
    }
    return _normalized_user_geo({key: value for key, value in geo.items() if value not in {"", None}})


def _normalized_user_geo(geo: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(geo or {})
    if normalized.get("city"):
        normalized["city"] = normalize_city_name(normalized.get("city"))
    return normalized


def _user_geo_auto_enabled(persona_config: Any | None = None) -> bool:
    if persona_config and hasattr(persona_config, "user_location_mode"):
        mode = str(getattr(persona_config, "user_location_mode", "") or "").strip().lower()
    else:
        mode = str(os.environ.get("AURA_USER_LOCATION_MODE", "device_ip") or "").strip().lower()
    if mode in {"", "device_ip", "device-public-ip", "public_ip", "ip", "auto"}:
        return True
    return False


def _device_public_ip(metadata: dict[str, Any] | None = None) -> str:
    if not isinstance(metadata, dict):
        return ""
    for key in ("device_public_ip", "public_ip", "wan_ip"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    device = metadata.get("device")
    if isinstance(device, dict):
        value = str(device.get("public_ip") or device.get("device_public_ip") or "").strip()
        if value:
            return value.split(",", 1)[0].strip()
    return ""


def _float_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(float(text))
    except (TypeError, ValueError):
        return ""


def _lookup_user_geo(ip_address: str) -> dict[str, Any]:
    if not ip_address or _is_private_or_loopback_ip(ip_address):
        return {}
    provider = os.environ.get("AURA_USER_GEO_PROVIDER", "ipapi").strip().lower()
    if provider in {"", "off", "disabled", "none"}:
        return {}
    cache_key = ip_address
    cached = _USER_GEO_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < USER_GEO_CACHE_TTL_SECONDS:
        return dict(cached[1])
    timeout = _float_env("AURA_USER_GEO_TIMEOUT_SECONDS", 2.5)
    if provider not in {"ipapi", "ip-api", "ip-api.com"}:
        return {}
    url = "http://ip-api.com/json/" + ip_address
    url += "?fields=status,message,country,regionName,city,lat,lon,timezone,query"
    try:
        with urlopen(Request(url, headers={"accept": "application/json"}), timeout=max(1.0, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return {}
    geo = {
        "city": normalize_city_name(payload.get("city") or ""),
        "region": str(payload.get("regionName") or "").strip(),
        "country": str(payload.get("country") or "").strip(),
        "latitude": payload.get("lat"),
        "longitude": payload.get("lon"),
        "timezone": str(payload.get("timezone") or "").strip(),
        "source": "ip",
    }
    geo = {key: value for key, value in geo.items() if value not in {"", None}}
    if geo.get("city") or geo.get("timezone"):
        _USER_GEO_CACHE[cache_key] = (now, geo)
        return dict(geo)
    return {}


def _is_private_or_loopback_ip(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(text)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)
    except ValueError:
        return False


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _test_result(
    *,
    ok: bool,
    kind: str,
    provider: str = "",
    model: str = "",
    latency_ms: int = 0,
    detail: str = "",
    endpoint_host: str = "",
    stage: str = "",
) -> dict[str, Any]:
    safe_detail = _safe_test_detail(detail)
    if not ok:
        # 常见失败翻译成可操作提示（缺 key/缺 SDK/网络等），原始错误跟在后面。
        hint = _model_failure_hint(detail)
        if hint:
            safe_detail = f"{hint} ── 原始错误：{safe_detail}"
    payload = {
        "ok": bool(ok),
        "kind": kind,
        "provider": provider,
        "model": model,
        "latency_ms": max(0, int(latency_ms or 0)),
        "detail": safe_detail,
        "endpoint_host": endpoint_host,
    }
    if stage:
        payload["stage"] = stage
    if not ok:
        payload["error"] = safe_detail or "test_failed"
    return payload


def _model_failure_hint(detail: str) -> str:
    """把 hermes/模型测试的常见失败翻译成能直接照做的中文提示。

    换订阅/换 provider 最常踩的三个坑：环境变量 key 名不对、镜像缺协议 SDK、
    key 本身无效。原始 traceback 对用户没有行动价值，这里给出下一步。
    """
    text = str(detail or "")
    match = re.search(r"Set the ([A-Z][A-Z0-9_]+) environment variable", text)
    if match or "no API key was found" in text:
        var = match.group(1) if match else "对应 provider 的 XXX_API_KEY"
        return f"缺少 API Key：把 {var}=<你的key> 写进项目 .env，重启原生服务后再点一次测试"
    match = re.search(r"The '([A-Za-z0-9_\-]+)' package is required", text)
    if match:
        pkg = match.group(1)
        return f"当前 Python 环境缺少 {pkg} SDK：在项目虚拟环境中运行 python -m pip install {pkg}，然后重启原生服务"
    if re.search(r"(?i)incorrect api key|invalid[ _]?api[ _-]?key|\b401\b|unauthorized", text):
        return "API Key 无效或过期：检查项目 .env 里的 key 是否填对、是否是该平台的 key"
    if re.search(r"(?i)unknown provider|invalid provider|not a valid provider|unsupported provider", text):
        return "provider 名称不被 hermes 识别：确认拼写（如 kimi-for-coding、deepseek），可在当前终端执行 hermes model 查看可用列表"
    if re.search(r"(?i)rate.?limit|quota|exhausted|insufficient|\b429\b", text):
        return "配额或频率限制：该订阅额度可能用完了，稍后再试或检查套餐"
    if re.search(r"(?i)timed? ?out|connection|resolve|unreachable|refused", text):
        return "网络连不通模型服务：检查网络与 base_url，稍后重试"
    return ""


def _safe_test_detail(value: str) -> str:
    text = str(value or "").strip().replace("\n", " ")
    text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "<redacted>", text)
    text = re.sub(
        r"(?i)(api[_-]?key|apikey|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s,;}]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._\-]+",
        r"\1<redacted>",
        text,
    )
    if len(text) > 240:
        return text[:240] + "..."
    return text


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _probe_tts_endpoint(
    base_url: str,
    *,
    provider: str = "",
    model: str = "",
    voice: str = "",
    api_key: str = "",
    audio_format: str = "pcm",
    sample_rate: int = 0,
    timeout: float,
) -> dict[str, Any]:
    text = str(base_url or "").strip()
    if not text:
        return {"ok": False, "detail": "TTS Base URL 为空"}
    endpoint_url = _tts_speech_url(text, provider=provider)
    if not endpoint_url:
        return {"ok": False, "detail": "TTS Base URL 需要是 http/https 地址"}
    if str(provider or "").strip().lower() not in {"voxcpm", "voxcpm-aura"}:
        speech = _probe_tts_speech(
            endpoint_url,
            model=model,
            voice=voice,
            api_key=api_key,
            audio_format=audio_format,
            sample_rate=sample_rate,
            timeout=timeout,
        )
        speech["stage"] = "speech"
        if speech.get("ok"):
            speech["detail"] = "TTS speech generation reachable."
            return speech
        speech["detail"] = f"TTS speech generation failed: {speech.get('detail') or 'unknown'}"
        return speech
    health_url = _health_url_for(text)
    health = _probe_http_endpoint(health_url, timeout=timeout)
    if not health.get("ok"):
        health["stage"] = "health"
        detail = health.get("detail") or "unknown"
        health["detail"] = f"TTS health check failed: {detail}"
        return health
    speech = _probe_tts_speech(
        endpoint_url,
        model=model,
        voice=voice,
        api_key=api_key,
        audio_format=audio_format,
        sample_rate=sample_rate,
        timeout=timeout,
    )
    if speech.get("ok"):
        speech["stage"] = "speech"
        speech["detail"] = "TTS health and speech generation reachable."
        return speech
    speech["stage"] = "speech"
    speech["detail"] = f"health ok; speech failed: {speech.get('detail') or 'unknown'}"
    return speech


def _probe_asr_endpoint(
    base_url: str,
    *,
    provider: str = "",
    timeout: float,
) -> dict[str, Any]:
    text = str(base_url or "").strip()
    if not text:
        return {"ok": False, "detail": "ASR Base URL 为空"}
    endpoint_url = _asr_transcription_url(text, provider=provider)
    if not endpoint_url:
        return {"ok": False, "detail": "ASR Base URL 需要是 http/https 地址"}
    provider_id = str(provider or "").strip().lower()
    if provider_id in {"custom", "local", "local-whisper", "local-whisper-http"}:
        health_url = _health_url_for(text)
        health = _probe_http_endpoint(health_url, timeout=min(max(1.0, timeout), 5.0))
        health["stage"] = "health"
        if health.get("ok"):
            health["detail"] = "ASR health endpoint reachable."
            return health
    probe = _probe_http_endpoint(endpoint_url, timeout=min(max(1.0, timeout), 5.0))
    probe["stage"] = "stepfun_sse" if provider_id == "stepfun" else "transcriptions"
    if probe.get("ok"):
        probe["detail"] = (
            "StepFun ASR SSE endpoint reachable."
            if provider_id == "stepfun"
            else "ASR transcription endpoint reachable."
        )
    return probe


def _stepfun_realtime_asr_ws_url(base_url: str) -> str:
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


def _stepfun_step_plan_realtime_ws_url(base_url: str, *, model: str) -> str:
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


def _probe_stepfun_step_plan_realtime_ws(
    base_url: str,
    *,
    model: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    ws_url = _stepfun_step_plan_realtime_ws_url(base_url, model=model)
    parsed = urlsplit(ws_url)
    host = parsed.hostname or ""
    if not ws_url:
        return {
            "ok": False,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": "StepFun Step Plan Realtime Base URL 需要是 http/https/ws/wss 地址",
            "endpoint_host": host,
        }
    if not str(api_key or "").strip():
        return {
            "ok": False,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": "StepFun Step Plan Realtime API Key 未配置",
            "endpoint_host": host,
        }
    started = monotonic()
    try:
        return asyncio.run(_probe_stepfun_step_plan_realtime_ws_async(
            ws_url,
            api_key=api_key,
            timeout=max(1.0, min(float(timeout or 5.0), 10.0)),
            started=started,
        ))
    except RuntimeError as exc:
        return {
            "ok": False,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


async def _probe_stepfun_step_plan_realtime_ws_async(
    ws_url: str,
    *,
    api_key: str,
    timeout: float,
    started: float,
) -> dict[str, Any]:
    parsed = urlsplit(ws_url)
    host = parsed.hostname or ""
    headers = {"Authorization": f"Bearer {str(api_key or '').strip()}"}
    try:
        async with ws_connect(
            ws_url,
            additional_headers=headers,
            open_timeout=min(5.0, timeout),
            ping_interval=None,
            max_size=2 * 1024 * 1024,
        ) as step_ws:
            await step_ws.send(json.dumps(
                _stepfun_step_plan_realtime_session_update(),
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            while True:
                event = _json_object(await asyncio.wait_for(step_ws.recv(), timeout=timeout))
                event_type = str(event.get("type") or event.get("event") or "").lower()
                if any(marker in event_type for marker in ("error", "failed")):
                    data = event.get("data") if isinstance(event.get("data"), dict) else event
                    return {
                        "ok": False,
                        "stage": "stepfun_step_plan_realtime_ws",
                        "detail": _stepfun_ws_error_detail(data),
                        "endpoint_host": host,
                        "latency_ms": max(0, int((monotonic() - started) * 1000)),
                    }
                if event_type:
                    return {
                        "ok": True,
                        "stage": "stepfun_step_plan_realtime_ws",
                        "detail": f"StepFun Step Plan Realtime WebSocket reachable ({event_type}).",
                        "endpoint_host": host,
                        "latency_ms": max(0, int((monotonic() - started) * 1000)),
                    }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": "StepFun Step Plan Realtime WebSocket timeout",
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }
    except (OSError, ConnectionClosed, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


def _stepfun_step_plan_realtime_session_update(*, voice: str = "") -> dict[str, Any]:
    session: dict[str, Any] = {
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "turn_detection": {
            "type": "server_vad",
            "prefix_padding_ms": 500,
            "silence_duration_ms": 800,
            "energy_awakeness_threshold": 2500,
        },
    }
    if str(voice or "").strip():
        session["voice"] = str(voice).strip()
    return {
        "type": "session.update",
        "session": session,
    }


def _probe_stepfun_realtime_asr_ws(
    base_url: str,
    *,
    model: str,
    language: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    ws_url = _stepfun_realtime_asr_ws_url(base_url)
    parsed = urlsplit(ws_url)
    host = parsed.hostname or ""
    if not ws_url:
        return {
            "ok": False,
            "stage": "stepfun_realtime_ws",
            "detail": "StepFun realtime ASR Base URL 需要是 http/https/ws/wss 地址",
            "endpoint_host": host,
        }
    if not str(api_key or "").strip():
        return {
            "ok": False,
            "stage": "stepfun_realtime_ws",
            "detail": "StepFun realtime ASR API Key 未配置",
            "endpoint_host": host,
        }
    started = monotonic()
    try:
        return asyncio.run(_probe_stepfun_realtime_asr_ws_async(
            ws_url,
            model=model,
            language=language,
            api_key=api_key,
            timeout=max(1.0, min(float(timeout or 5.0), 10.0)),
            started=started,
        ))
    except RuntimeError as exc:
        return {
            "ok": False,
            "stage": "stepfun_realtime_ws",
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


async def _probe_stepfun_realtime_asr_ws_async(
    ws_url: str,
    *,
    model: str,
    language: str,
    api_key: str,
    timeout: float,
    started: float,
) -> dict[str, Any]:
    parsed = urlsplit(ws_url)
    host = parsed.hostname or ""
    headers = {"Authorization": f"Bearer {str(api_key or '').strip()}"}
    try:
        async with ws_connect(
            ws_url,
            additional_headers=headers,
            open_timeout=min(5.0, timeout),
            ping_interval=None,
            max_size=2 * 1024 * 1024,
        ) as step_ws:
            await step_ws.send(json.dumps(
                _stepfun_realtime_asr_session_update(model=model, language=language),
                ensure_ascii=False,
                separators=(",", ":"),
            ))
            while True:
                event = _json_object(await asyncio.wait_for(step_ws.recv(), timeout=timeout))
                event_type = str(event.get("type") or event.get("event") or "").lower()
                if any(marker in event_type for marker in ("error", "failed")):
                    data = event.get("data") if isinstance(event.get("data"), dict) else event
                    return {
                        "ok": False,
                        "stage": "stepfun_realtime_ws",
                        "detail": _stepfun_ws_error_detail(data),
                        "endpoint_host": host,
                        "latency_ms": max(0, int((monotonic() - started) * 1000)),
                    }
                if event_type in {"session.created", "session.updated"} or event_type:
                    return {
                        "ok": True,
                        "stage": "stepfun_realtime_ws",
                        "detail": f"StepFun realtime ASR WebSocket reachable ({event_type or 'connected'}).",
                        "endpoint_host": host,
                        "latency_ms": max(0, int((monotonic() - started) * 1000)),
                    }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "stage": "stepfun_realtime_ws",
            "detail": "StepFun realtime ASR WebSocket timeout",
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }
    except (OSError, ConnectionClosed, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "stage": "stepfun_realtime_ws",
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


def _stepfun_realtime_asr_session_update(*, model: str, language: str) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "audio": {
                "input": {
                    "format": {
                        "type": "pcm",
                        "codec": "pcm_s16le",
                        "rate": DEVICE_SAMPLE_RATE,
                        "bits": 16,
                        "channel": 1,
                    },
                    "transcription": {
                        "model": str(model or "").strip() or "stepaudio-2.5-asr-stream",
                        "language": str(language or "").strip() or "zh",
                        "prompt": "请记录下你所听到的语音内容。",
                        "full_rerun_on_commit": True,
                        "enable_itn": True,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 800,
                        "threshold": 0.5,
                    },
                },
            },
        },
    }


def _json_object(message: Any) -> dict[str, Any]:
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    payload = json.loads(str(message or "{}"))
    return payload if isinstance(payload, dict) else {}


def _stepfun_ws_error_detail(data: dict[str, Any]) -> str:
    code = str(data.get("code") or "").strip()
    message = str(data.get("message") or data.get("error") or "StepFun realtime ASR WebSocket error").strip()
    return f"{code} {message}".strip()


def _probe_tts_speech(
    url: str,
    *,
    model: str,
    voice: str,
    audio_format: str,
    sample_rate: int = 0,
    api_key: str = "",
    timeout: float,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    normalized_format = str(audio_format or "pcm").strip().lower()
    requested_rate = _audio_sample_rate(sample_rate)
    body_payload = {
        "model": model or "tts-test",
        "input": "你好，我是 Lily 的语音测试。",
        "voice": voice or "aura",
        "response_format": normalized_format or "pcm",
    }
    if normalized_format == "pcm":
        body_payload["sample_rate"] = requested_rate
    body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json"}
    secret = str(api_key or "").strip()
    if secret:
        headers["authorization"] = f"Bearer {secret}"
    request = Request(url, data=body, method="POST", headers=headers)
    started = monotonic()
    try:
        with urlopen(request, timeout=max(1.0, float(timeout or 4.0))) as response:
            audio = response.read()
            ok = 200 <= int(response.status) < 300 and bool(audio)
            payload = {
                "ok": ok,
                "detail": f"HTTP {int(response.status)}; bytes={len(audio)}",
                "endpoint_host": host,
                "latency_ms": max(0, int((monotonic() - started) * 1000)),
                "audio_format": normalized_format,
                "audio_bytes": len(audio),
                "requested_sample_rate": requested_rate if normalized_format == "pcm" else 0,
            }
            if ok and normalized_format == "pcm":
                device_audio = _resample_pcm16_mono(audio, source_rate=requested_rate, target_rate=DEVICE_SAMPLE_RATE)
                wav_bytes = _pcm_to_wav_bytes(device_audio, sample_rate=DEVICE_SAMPLE_RATE)
                payload.update({
                    "source_sample_rate": requested_rate,
                    "device_sample_rate": DEVICE_SAMPLE_RATE,
                    "resampled_for_device": requested_rate != DEVICE_SAMPLE_RATE,
                    "device_audio_bytes": len(device_audio),
                    "audio_mime_type": "audio/wav",
                    "audio_data_url": "data:audio/wav;base64," + base64.b64encode(wav_bytes).decode("ascii"),
                })
            return {
                **payload,
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "detail": f"HTTP {exc.code}",
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }
    except (OSError, URLError) as exc:
        return {
            "ok": False,
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


def _audio_sample_rate(value: int) -> int:
    sample_rate = int(value or 0)
    return sample_rate if sample_rate >= MIN_AUDIO_SAMPLE_RATE else DEVICE_SAMPLE_RATE


def _pcm_to_wav_bytes(pcm: bytes, *, sample_rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()


def _resample_pcm16_mono(pcm: bytes, *, source_rate: int, target_rate: int) -> bytes:
    if source_rate == target_rate or not pcm:
        return pcm
    sample_count = len(pcm) // 2
    if sample_count <= 1:
        return pcm
    samples = [int.from_bytes(pcm[i * 2:i * 2 + 2], "little", signed=True) for i in range(sample_count)]
    target_count = max(1, int(sample_count * target_rate / source_rate))
    out = bytearray(target_count * 2)
    for index in range(target_count):
        src = index * (source_rate / target_rate)
        left = int(src)
        right = min(left + 1, sample_count - 1)
        frac = src - left
        value = int(samples[left] * (1.0 - frac) + samples[right] * frac)
        out[index * 2:index * 2 + 2] = int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


def _tts_speech_url(base_url: str, *, provider: str = "") -> str:
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


def _asr_transcription_url(base_url: str, *, provider: str = "") -> str:
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


def _health_url_for(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))


def _probe_http_endpoint(url: str, *, timeout: float) -> dict[str, Any]:
    text = str(url or "").strip()
    if not text:
        return {"ok": False, "detail": "Base URL 为空"}
    parsed = urlsplit(text)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        return {"ok": False, "detail": "Base URL 需要是 http/https 地址", "endpoint_host": host}
    request = Request(text, method="GET")
    started = monotonic()
    try:
        with urlopen(request, timeout=max(1.0, float(timeout or 3.0))) as response:
            status = int(response.status)
            return {
                "ok": 200 <= status < 500,
                "detail": f"HTTP {status}",
                "endpoint_host": host,
                "latency_ms": max(0, int((monotonic() - started) * 1000)),
            }
    except HTTPError as exc:
        return {
            "ok": exc.code < 500,
            "detail": f"HTTP {exc.code}",
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }
    except (OSError, URLError) as exc:
        return {
            "ok": False,
            "detail": _network_error_detail(exc),
            "endpoint_host": host,
            "latency_ms": max(0, int((monotonic() - started) * 1000)),
        }


def _network_error_detail(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    candidates = [reason, exc]
    for item in candidates:
        if isinstance(item, (TimeoutError, socket.timeout)):
            return "connection timed out; service may be offline or unreachable from this container"
        if isinstance(item, ConnectionRefusedError):
            return "connection refused; host is reachable but the service port is not listening"
    text = str(reason or exc)
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "connection timed out; service may be offline or unreachable from this container"
    if "connection refused" in lowered:
        return "connection refused; host is reachable but the service port is not listening"
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return "network unreachable; check VPN/Tailscale/LAN route to the service host"
    return f"{exc.__class__.__name__}: {text}"


def render_admin_page() -> str:
    return _admin_asset_path("index.html").read_text(encoding="utf-8")


def render_admin_asset(path: str) -> tuple[str, str]:
    asset_name = path.rsplit("/", 1)[-1]
    if asset_name not in {"style.css", "app.js"}:
        return "", "text/plain; charset=utf-8"
    content_type = "text/css; charset=utf-8" if asset_name.endswith(".css") else "application/javascript; charset=utf-8"
    asset_path = _admin_asset_path(asset_name)
    if not asset_path.exists():
        return "", content_type
    return asset_path.read_text(encoding="utf-8"), content_type


def _admin_asset_path(name: str) -> Path:
    return Path(__file__).with_name("admin") / name

def install_shutdown_handlers(server: ThreadingHTTPServer) -> None:
    def request_shutdown(signum: int, _frame: Any) -> None:
        print(f"aura-lily-server received signal {signum}; shutting down", file=sys.stderr, flush=True)
        Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def run_server(config: LilyServerConfig) -> None:
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    if current_thread() is main_thread():
        install_shutdown_handlers(server)
    print(
        f"aura-lily-server listening on http://{config.host}:{config.port} "
        f"(max_concurrency={config.max_concurrency})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv or sys.argv[1:]))
    run_server(build_config(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
