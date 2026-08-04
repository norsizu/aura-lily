#!/usr/bin/env python3
"""Measure Lily voice latency without an ESP32 device.

Default mode avoids writing to companion.db: it streams the configured Aura LLM
directly, splits early speakable text, and runs the configured TTS with the same
prefetch style used by the gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.util
import json
import sys
import time
import tempfile
import contextlib
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.aura_persona_gateway.llm import DirectLlmClient, DirectLlmConfig
from integrations.aura_persona_gateway.runtime import load_aura_runtime_config, voice_latency_path
from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.turn import AuraPersonaGateway
from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig
from integrations.hermes_lily_cli.gateway import (
    DEVICE_SAMPLE_RATE,
    GatewayConfig,
    TurnState,
    TTS_BINARY_FLAG_FINAL,
    TTS_BINARY_HEADER_SIZE,
    TTS_BINARY_MAGIC,
    TTS_PREFETCH_CONCURRENCY,
    BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS,
    TtsResult,
    device_spoken_text,
    flush_stream_tts_segments,
    pop_stream_tts_segment,
    handle_connection,
    stream_dialogue_and_tts_from_bridge,
    synthesize_and_stream_tts,
    synthesize_tts,
    merge_deferred_local_preface_for_tts,
)
from integrations.hermes_lily_cli import gateway as gateway_module
from websockets.asyncio.client import connect as ws_connect


STREAM_LOCAL_QUALITY_CASES = {
    "job_change_chat",
    "open_chat",
    "overtime_supportive_chat",
    "status_review_entry",
}


BENCHMARK_SUITES: dict[str, list[tuple[str, str]]] = {
    "quick": [
        ("weather_user", "今天天气怎么样？"),
        ("weather_aura", "你那边天气怎么样？"),
        ("time_user", "现在几点？"),
        ("grounded_activity", "你现在在干嘛？"),
        ("grounded_location", "你在哪？"),
        ("casual_chat", "我今天有点累，你陪我聊两句。"),
        ("long_question", "我想测试一下回复速度，你简单说说现在语音链路可能慢在哪里。"),
    ],
    "quality": [
        ("fixed_short", "测试流式速度，请用十个字以内回答我在。"),
        ("noise_drop", "嗯。"),
        ("grounded_activity", "你现在在干嘛？"),
        ("grounded_location", "你在哪？"),
        ("time_weather_aura", "你那边几点了，天气怎么样？"),
        ("time_weather_user", "现在几点，多少度？"),
        ("open_chat", "我想聊聊。"),
        ("job_change_chat", "最近想换工作，能聊聊吗？"),
        ("overtime_supportive_chat", "我最近加班有点烦，想聊聊。"),
        ("weather_advice", "你觉得我需要带伞吗？"),
        ("weather_reason", "你为什么建议我带伞？"),
        ("corrected_weather", "等一下我不是测试一下，我是问今天天气怎么样。"),
        ("latency_diagnostic", "我想测试一下回复速度，你简单说说现在语音链路可能慢在哪里。"),
        ("mood_chat", "你今天心情怎么样？"),
        ("status_review_entry", "我今天想聊聊最近状态，你自然回应一句。"),
        ("outing_chat", "我今天下午打算出门。"),
        ("time_user", "现在几点？"),
        ("time_aura", "你那边几点？"),
    ],
}

QUALITY_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "fixed_short": {"path": "explicit_fixed_reply", "contains": ("我在。",), "model_skipped": True},
    "noise_drop": {"path": "empty_or_noise", "status": "ignored", "silent": True},
    "grounded_activity": {"path": "grounded_current_activity", "contains_any": ("散步", "正好陪你说话", "听你说话"), "model_skipped": True},
    "grounded_location": {"path": "grounded_current_location", "contains_any": ("具体位置先不说", "我在"), "model_skipped": True},
    "time_weather_aura": {"path": "current_time_weather", "contains": ("我这边", "上海", "天气是"), "model_skipped": True},
    "time_weather_user": {"path": "current_time_weather", "contains": ("上海", "天气是"), "model_skipped": True},
    "open_chat": {
        "path": "casual_chat_preface",
        "contains_any": ("最想说", "最想聊", "最挂心", "占心"),
        "not_contains": ("我听着", "我在听", "你说"),
        "model_skipped": False,
    },
    "job_change_chat": {
        "path": "casual_chat_preface",
        "contains_any": ("换工作", "犹豫", "目标", "要不要走"),
        "not_contains": ("工作节奏", "最近状态啊", "我在。", "我听着", "我在听"),
        "model_skipped": False,
    },
    "overtime_supportive_chat": {
        "path": "supportive_chat",
        "contains_any": ("加班", "累", "烦", "憋着"),
        "not_contains": ("工作节奏", "最近状态啊", "我在。"),
        "model_skipped": False,
    },
    "weather_advice": {"path": "cached_weather_advice", "contains": ("上海",), "contains_any": ("带一把", "带伞", "不用特意带伞")},
    "weather_reason": {"path": "cached_weather_advice", "contains": ("依据", "上海")},
    "corrected_weather": {"path": "cached_weather", "contains": ("上海",), "not_contains": ("我是问", "上海")},
    "latency_diagnostic": {
        "path": "voice_latency_diagnostic",
        "contains": ("ASR", "模型首句", "TTS"),
        "not_contains": ("基站", "运营商", "手机信号"),
        "model_skipped": True,
    },
    "mood_chat": {"path": "state_mood", "contains": ("心情",), "model_skipped": True},
    "status_review_entry": {
        "path": "status_review_entry",
        "contains_any": ("最近状态", "复盘", "工作节奏", "哪一块最卡"),
        "not_contains": ("大悦城", "商场", "你说，我在听", "我听着", "我在听"),
        "model_skipped": False,
    },
    "outing_chat": {"path": "outing_weather_advice", "contains": ("上海",), "contains_any": ("防晒", "带伞", "路上慢点")},
    "time_user": {"path": "current_time", "contains": ("上海",)},
    "time_aura": {"path": "current_time", "contains": ("我这边",)},
}

LOCAL_QUALITY_FORBIDDEN_TOKENS = (
    "（",
    "）",
    "*",
    "基站",
    "运营商",
    "手机信号",
    "大悦城",
    "逛商场",
)


class CaptureWebsocket:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.frames: list[tuple[float, Any]] = []

    async def send(self, payload: Any) -> None:
        self.frames.append((time.monotonic(), payload))

    def elapsed_ms(self, at: float) -> int:
        return max(0, int((at - self.started) * 1000))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Aura Lily LLM/TTS streaming latency.")
    parser.add_argument(
        "--text",
        default="测试一下，请用一句自然中文回答你现在状态怎么样，控制在二十个字以内。",
        help="Transcript or user text to benchmark.",
    )
    parser.add_argument("--suite", choices=sorted(BENCHMARK_SUITES), default="", help="Run a fixed benchmark suite instead of one text.")
    parser.add_argument("--persona-home", default=".docker/aura-persona")
    parser.add_argument(
        "--mode",
        choices=[
            "direct",
            "direct-stream-tts",
            "persona-llm",
            "tts-only",
            "persona-stream-tts",
            "bridge",
            "voice-sim",
            "voice-ws",
            "local-quality",
        ],
        default="direct",
    )
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8765/turn")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8787/ws")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--audio-ms", type=int, default=900, help="voice-sim uploaded PCM duration.")
    parser.add_argument("--frame-ms", type=int, default=40, help="voice-ws PCM frame duration.")
    parser.add_argument("--audio-format", choices=["pcm", "opus"], default="pcm", help="voice-ws upload format.")
    parser.add_argument("--warm-tts-ms", type=int, default=0, help="persona-stream-tts: pre-open StepFun TTS WS this many milliseconds before the turn.")
    parser.add_argument("--user-city", default="", help="Inject user_geo.city into simulated voice turns.")
    parser.add_argument("--user-timezone", default="", help="Inject user_geo.timezone into simulated voice turns.")
    parser.add_argument("--user-latitude", default="", help="Inject user_geo.latitude into simulated voice turns.")
    parser.add_argument("--user-longitude", default="", help="Inject user_geo.longitude into simulated voice turns.")
    parser.add_argument("--open-platform-temporary", action="store_true", help="Temporarily benchmark StepFun /v1 Open Platform ASR/LLM/TTS without saving runtime config.")
    parser.add_argument("--model", default="", help="Temporarily override Aura LLM model for benchmark only.")
    parser.add_argument("--tts-model", default="", help="Temporarily override TTS model for benchmark only.")
    parser.add_argument("--max-tokens", type=int, default=0, help="Temporarily override Aura LLM max tokens for benchmark only.")
    parser.add_argument("--realtime-upload", action=argparse.BooleanOptionalAction, default=True, help="voice-ws sleeps between frames like a real device.")
    parser.add_argument("--fake-streaming-asr", action="store_true", help="Use a fake StepFun WS ASR server for voice-sim.")
    parser.add_argument("--fake-streaming-asr-early-final", action="store_true", help="Fake ASR final immediately after the first audio append, before client stop.")
    parser.add_argument("--fake-streaming-asr-speech-stop", action="store_true", help="Fake ASR partial text followed by speech_stopped before client stop.")
    parser.add_argument("--json", action="store_true", help="Print JSON only.")
    return parser.parse_args()


def runtime_for_benchmark(runtime_config: Any, args: argparse.Namespace) -> Any:
    updates: dict[str, Any] = {}
    if args.open_platform_temporary:
        key = (
            str(getattr(runtime_config, "aura_model_api_key", "") or "").strip()
            or str(getattr(runtime_config, "tts_api_key", "") or "").strip()
            or str(getattr(runtime_config, "asr_api_key", "") or "").strip()
        )
        updates.update({
            "aura_model_mode": "aura_model",
            "aura_model_provider": "stepfun",
            "aura_model_model": "stepaudio-2.5-chat",
            "aura_model_base_url": "https://api.stepfun.com/v1",
            "aura_model_api_key": key,
            "aura_model_reasoning_effort": "",
            "aura_model_max_tokens": 96,
            "tts_enabled": True,
            "tts_provider": "stepfun",
            "tts_model": "stepaudio-2.5-tts",
            "tts_base_url": "https://api.stepfun.com/v1",
            "tts_api_key": key,
            "asr_enabled": True,
            "asr_mode": "api",
            "asr_provider": "stepfun",
            "asr_model": "stepaudio-2.5-asr-stream",
            "asr_base_url": "https://api.stepfun.com/v1",
            "asr_api_key": key,
        })
    if args.model:
        updates["aura_model_model"] = str(args.model).strip()
    if args.tts_model:
        updates["tts_model"] = str(args.tts_model).strip()
    if args.max_tokens:
        updates["aura_model_max_tokens"] = max(16, int(args.max_tokens))
    return replace(runtime_config, **updates) if updates else runtime_config


async def run_direct_once(runtime_config: Any, text: str) -> dict[str, Any]:
    started = time.monotonic()
    first_delta_ms = 0
    final_llm_ms = 0
    raw_response = ""
    pending_text = ""
    segment_index = 0
    tts_results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max(1, TTS_PREFETCH_CONCURRENCY))
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def synthesize_segment(segment_text: str) -> TtsResult:
        async with semaphore:
            return await asyncio.to_thread(synthesize_tts, runtime_config, segment_text)

    async def enqueue_segment(segment: str, *, final: bool) -> None:
        nonlocal segment_index
        spoken = device_spoken_text(segment)
        if not spoken:
            return
        segment_index += 1
        queued_ms = elapsed_ms(started)
        task = asyncio.create_task(synthesize_segment(spoken))
        await queue.put({
            "index": segment_index,
            "text": spoken,
            "queued_ms": queued_ms,
            "final": final,
            "task": task,
        })

    async def consume_tts() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            result = await item["task"]
            done_ms = elapsed_ms(started)
            tts_results.append({
                "index": item["index"],
                "chars": len(item["text"]),
                "queued_ms": item["queued_ms"],
                "done_ms": done_ms,
                "latency_ms": max(0, done_ms - int(item["queued_ms"])),
                "ok": result.ok,
                "audio_bytes": len(result.audio or b""),
                "chunk_count": result.chunk_count,
                "final": item["final"],
                "detail": result.detail if not result.ok else "",
            })

    consumer = asyncio.create_task(consume_tts())
    client = DirectLlmClient(
        DirectLlmConfig(
            provider=runtime_config.aura_model_provider,
            model=runtime_config.aura_model_model,
            base_url=runtime_config.aura_model_base_url,
            api_key=runtime_config.aura_model_api_key,
            timeout_seconds=float(runtime_config.aura_model_timeout_seconds or 90),
        )
    )
    final_event: dict[str, Any] = {}
    async for event in iter_direct_llm_events(client, text):
        event_type = str(event.get("type") or "")
        if event_type == "delta":
            delta = str(event.get("text") or "")
            if not delta:
                continue
            if not first_delta_ms:
                first_delta_ms = elapsed_ms(started)
            raw_response += delta
            pending_text += delta
            while True:
                segment, pending_text = pop_stream_tts_segment(
                    pending_text,
                    force=False,
                    first_segment=segment_index == 0,
                )
                if not segment:
                    break
                await enqueue_segment(segment, final=False)
        elif event_type == "final":
            final_event = dict(event)
            final_llm_ms = elapsed_ms(started)

    final_text = pending_text.strip()
    if final_text:
        await enqueue_segment(final_text, final=True)
    elif segment_index == 0:
        await enqueue_segment(str(final_event.get("response") or raw_response), final=True)
    await queue.put(None)
    await consumer

    first_audio_ms = min((item["done_ms"] for item in tts_results if item.get("ok")), default=0)
    return {
        "mode": "direct",
        "ok": bool(final_event.get("ok")) and any(item.get("ok") for item in tts_results),
        "llm_model": runtime_config.aura_model_model,
        "tts_model": runtime_config.tts_model,
        "tts_voice": runtime_config.tts_voice,
        "first_delta_ms": first_delta_ms,
        "final_llm_ms": final_llm_ms,
        "first_audio_ready_ms": first_audio_ms,
        "total_ms": elapsed_ms(started),
        "response_chars": len(str(final_event.get("response") or raw_response)),
        "segments": tts_results,
    }


async def run_direct_stream_tts_once(runtime_config: Any, text: str) -> dict[str, Any]:
    started = time.monotonic()
    first_delta_ms = 0
    final_llm_ms = 0
    raw_response = ""
    pending_text = ""
    segment_index = 0
    ws = CaptureWebsocket()
    segment_results: list[dict[str, Any]] = []

    async def synthesize_segment(segment: str, *, final: bool) -> None:
        nonlocal segment_index
        spoken = device_spoken_text(segment)
        if not spoken:
            return
        segment_index += 1
        queued_ms = elapsed_ms(started)
        before_frames = len(ws.frames)
        result = await synthesize_and_stream_tts(
            ws,
            runtime_config,
            1,
            spoken,
            stream_id=1,
            is_final=final,
        )
        first_audio_ms = min(
            (
                ws.elapsed_ms(at)
                for at, payload in ws.frames[before_frames:]
                if isinstance(payload, bytes)
                and (info := parse_tts_binary(payload))
                and info.get("audio_bytes", 0) > 0
            ),
            default=0,
        )
        segment_results.append({
            "index": segment_index,
            "chars": len(spoken),
            "queued_ms": queued_ms,
            "first_audio_ms": first_audio_ms,
            "done_ms": elapsed_ms(started),
            "latency_ms": max(0, elapsed_ms(started) - queued_ms),
            "ok": result.ok,
            "streamed": result.streamed,
            "audio_bytes": result.audio_bytes,
            "audio_chunk_count": result.audio_chunk_count,
            "final": final,
            "detail": result.detail if not result.ok else "",
        })

    client = DirectLlmClient(
        DirectLlmConfig(
            provider=runtime_config.aura_model_provider,
            model=runtime_config.aura_model_model,
            base_url=runtime_config.aura_model_base_url,
            api_key=runtime_config.aura_model_api_key,
            timeout_seconds=float(runtime_config.aura_model_timeout_seconds or 90),
        )
    )
    final_event: dict[str, Any] = {}
    async for event in iter_direct_llm_events(client, text):
        event_type = str(event.get("type") or "")
        if event_type == "delta":
            delta = str(event.get("text") or "")
            if not delta:
                continue
            if not first_delta_ms:
                first_delta_ms = elapsed_ms(started)
            raw_response += delta
            pending_text += delta
            while True:
                segment, pending_text = pop_stream_tts_segment(
                    pending_text,
                    force=False,
                    first_segment=segment_index == 0,
                )
                if not segment:
                    break
                await synthesize_segment(segment, final=False)
        elif event_type == "final":
            final_event = dict(event)
            final_llm_ms = elapsed_ms(started)

    final_text = pending_text.strip()
    if final_text:
        await synthesize_segment(final_text, final=True)
    elif segment_index == 0:
        await synthesize_segment(str(final_event.get("response") or raw_response), final=True)

    ws_summary = summarize_ws_frames(ws, ok=bool(final_event.get("ok")))
    return {
        "mode": "direct-stream-tts",
        "ok": bool(final_event.get("ok")) and any(item.get("ok") for item in segment_results),
        "llm_model": runtime_config.aura_model_model,
        "tts_model": runtime_config.tts_model,
        "tts_voice": runtime_config.tts_voice,
        "first_delta_ms": first_delta_ms,
        "final_llm_ms": final_llm_ms,
        "first_audio_sent_ms": ws_summary["first_audio_sent_ms"],
        "final_audio_frame_ms": ws_summary["final_audio_frame_ms"],
        "total_ms": elapsed_ms(started),
        "response_chars": len(str(final_event.get("response") or raw_response)),
        "binary_frames": ws_summary["binary_frames"],
        "audio_bytes": ws_summary["audio_bytes"],
        "segments": segment_results,
    }


async def iter_direct_llm_events(client: DirectLlmClient, text: str):
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def worker() -> None:
        try:
            for event in client.stream(text, metadata={"source": "voice_latency_benchmark", "mode": "direct"}):
                loop.call_soon_threadsafe(queue.put_nowait, dict(event))
        except Exception as exc:  # pragma: no cover - diagnostic tool boundary
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "final",
                "ok": False,
                "status": "failed",
                "response": f"benchmark stream failed: {exc.__class__.__name__}",
                "evidence": {"error_type": exc.__class__.__name__},
            })
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    Thread(target=worker, name="voice-latency-llm-stream", daemon=True).start()
    while True:
        event = await queue.get()
        if event is None:
            return
        yield event


async def run_bridge_once(
    runtime_config: Any,
    text: str,
    *,
    bridge_url: str,
    timeout: float,
    user_geo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ws = CaptureWebsocket()
    config = GatewayConfig(host="127.0.0.1", port=0, bridge_url=bridge_url, bridge_timeout_seconds=timeout)
    metadata: dict[str, Any] = {}
    if user_geo:
        metadata["user_geo"] = dict(user_geo)
    state = TurnState(turn_id=1, audio_chunks=[b"benchmark"], audio_bytes=0, asr_latency_ms=0, metadata=metadata)
    ok = await stream_dialogue_and_tts_from_bridge(ws, config, runtime_config, state, text)
    summary = summarize_ws_frames(ws, ok=ok)
    if user_geo:
        summary["user_geo"] = dict(user_geo)
    return summary


async def run_persona_stream_tts_once(
    runtime_config: Any,
    text: str,
    *,
    persona_home: str,
    user_geo: dict[str, Any] | None = None,
    warm_tts_ms: int = 0,
) -> dict[str, Any]:
    started = time.monotonic()
    ws = CaptureWebsocket()
    with tempfile.TemporaryDirectory(prefix="aura-persona-stream-") as tmp:
        tmp_path = Path(tmp)
        config = PersonaGatewayConfig(
            enabled=True,
            persona_home=str(Path(persona_home).expanduser()),
            companion_home=str(tmp_path / "companion-home"),
            hermes_home=str(tmp_path / "hermes-home"),
            include_debug_context=True,
            recent_message_limit=4,
            max_context_chars=3200,
        )
        store = LilyPersonaStore(config.companion_db_path)
        bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
        gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime_config)
        metadata: dict[str, Any] = {
            "source": "aura-lily-gateway",
            "audio_bytes": DEVICE_SAMPLE_RATE * 2,
            "streamed": True,
            "speculative": True,
        }
        if user_geo:
            metadata["user_geo"] = dict(user_geo)

        async def persona_events() -> Any:
            for event in gateway.run_direct_turn_stream(text, metadata=metadata):
                yield event

        state = TurnState(
            turn_id=1,
            audio_chunks=[b"benchmark"],
            audio_bytes=DEVICE_SAMPLE_RATE * 2,
            asr_latency_ms=0,
            streaming_asr_first_delta_ms=1,
            streaming_asr_final_ms=1,
            streaming_asr_final_reason="benchmark_text",
            streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2,
            streaming_asr_forwarded_frames=1,
            streaming_asr_early_turn_triggered=True,
            turn_trigger_reason="benchmark_text",
            turn_trigger_detail="persona_stream_tts",
            turn_trigger_ms=1,
            metadata=metadata,
        )
        warm_ms = max(0, int(warm_tts_ms or 0))
        if warm_ms:
            await gateway_module.maybe_start_stepfun_tts_warm_session(ws, state, runtime_config, reason="benchmark")
            await asyncio.sleep(warm_ms / 1000)
        ok = await stream_dialogue_and_tts_from_bridge(
            ws,
            GatewayConfig(host="127.0.0.1", port=0, bridge_url="local-persona-stream", bridge_timeout_seconds=90),
            runtime_config,
            state,
            text,
            prefetched_event_source=persona_events(),
        )
    summary = summarize_ws_frames(ws, ok=ok)
    summary["mode"] = "persona-stream-tts"
    summary["ok"] = bool(summary.get("audio_bytes") and (summary.get("timing") or {}).get("status") == "ok")
    summary["total_ms"] = elapsed_ms(started)
    if warm_tts_ms:
        summary["warm_tts_ms"] = max(0, int(warm_tts_ms or 0))
    if user_geo:
        summary["user_geo"] = dict(user_geo)
    return summary


async def run_persona_llm_once(
    runtime_config: Any,
    text: str,
    *,
    persona_home: str,
    user_geo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Measure Aura SOUL/persona prompt -> LLM streaming without synthesizing TTS."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aura-persona-llm-") as tmp:
        tmp_path = Path(tmp)
        config = PersonaGatewayConfig(
            enabled=True,
            persona_home=str(Path(persona_home).expanduser()),
            companion_home=str(tmp_path / "companion-home"),
            hermes_home=str(tmp_path / "hermes-home"),
            include_debug_context=True,
            recent_message_limit=4,
            max_context_chars=3200,
        )
        store = LilyPersonaStore(config.companion_db_path)
        bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
        gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime_config)
        metadata: dict[str, Any] = {
            "source": "aura-lily-gateway",
            "audio_bytes": DEVICE_SAMPLE_RATE * 2,
            "streamed": True,
            "speculative": True,
        }
        if user_geo:
            metadata["user_geo"] = dict(user_geo)

        first_delta_ms = 0
        first_model_delta_ms = 0
        first_tts_text_ms = 0
        pending_text = ""
        deferred_local_preface = ""
        emitted_text = ""
        delta_sources: dict[str, int] = {}
        tts_segments: list[dict[str, Any]] = []
        final_payload: dict[str, Any] = {}

        for event in gateway.run_direct_turn_stream(text, metadata=metadata):
            if not isinstance(event, dict):
                continue
            if event.get("type") == "delta":
                delta = str(event.get("text") or "")
                if not delta:
                    continue
                now_ms = elapsed_ms(started)
                source = str(event.get("source") or "model")
                delta_sources[source] = delta_sources.get(source, 0) + len(delta)
                if not first_delta_ms:
                    first_delta_ms = now_ms
                if source not in {"local_preface", "local_voice_reply"} and not first_model_delta_ms:
                    first_model_delta_ms = now_ms
                emitted_text += delta
                if source in {"local_preface", "local_voice_reply"}:
                    spoken = device_spoken_text(delta, allow_fallback=False)
                    should_defer_local_preface = (
                        source == "local_preface"
                        and BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS > 0
                        and not tts_segments
                        and not pending_text.strip()
                        and 0 < len(spoken) <= BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS
                    )
                    if should_defer_local_preface:
                        deferred_local_preface += delta
                        continue
                    if pending_text.strip():
                        for segment in flush_stream_tts_segments(pending_text):
                            spoken = device_spoken_text(segment, allow_fallback=False)
                            if not spoken:
                                continue
                            if not first_tts_text_ms:
                                first_tts_text_ms = now_ms
                            tts_segments.append({
                                "index": len(tts_segments) + 1,
                                "ms": now_ms,
                                "chars": len(spoken),
                                "text": spoken[:80],
                            })
                        pending_text = ""
                    if spoken:
                        if not first_tts_text_ms:
                            first_tts_text_ms = now_ms
                        tts_segments.append({
                            "index": len(tts_segments) + 1,
                            "ms": now_ms,
                            "chars": len(spoken),
                            "text": spoken[:80],
                    })
                    continue
                if deferred_local_preface:
                    delta = merge_deferred_local_preface_for_tts(deferred_local_preface, delta)
                    deferred_local_preface = ""
                pending_text += delta
                while True:
                    segment, pending_text = pop_stream_tts_segment(
                        pending_text,
                        force=False,
                        first_segment=not tts_segments,
                    )
                    if not segment:
                        break
                    spoken = device_spoken_text(segment, allow_fallback=False)
                    if not spoken:
                        break
                    if not first_tts_text_ms:
                        first_tts_text_ms = now_ms
                    tts_segments.append({
                        "index": len(tts_segments) + 1,
                        "ms": now_ms,
                        "chars": len(spoken),
                        "text": spoken[:80],
                    })
                continue
            if event.get("type") == "final":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                final_payload = dict(payload)

        if not tts_segments:
            final_text = str(final_payload.get("response") or emitted_text).strip()
            if deferred_local_preface:
                pending_text = merge_deferred_local_preface_for_tts(deferred_local_preface, pending_text or final_text)
                deferred_local_preface = ""
            for segment in flush_stream_tts_segments(pending_text or final_text):
                spoken = device_spoken_text(segment)
                if not spoken:
                    continue
                now_ms = elapsed_ms(started)
                if not first_tts_text_ms:
                    first_tts_text_ms = now_ms
                tts_segments.append({
                    "index": len(tts_segments) + 1,
                    "ms": now_ms,
                    "chars": len(spoken),
                    "text": spoken[:80],
                })

    evidence = final_payload.get("evidence") if isinstance(final_payload.get("evidence"), dict) else {}
    quality_guard = evidence.get("quality_guard") if isinstance(evidence.get("quality_guard"), dict) else {}
    if quality_guard:
        guarded_final_text = str(final_payload.get("response") or "").strip()
        if guarded_final_text and guarded_final_text != emitted_text.strip():
            tts_segments = []
            first_tts_text_ms = int(final_payload.get("latency_ms") or elapsed_ms(started))
            for segment in flush_stream_tts_segments(guarded_final_text):
                spoken = device_spoken_text(segment)
                if not spoken:
                    continue
                tts_segments.append({
                    "index": len(tts_segments) + 1,
                    "ms": first_tts_text_ms,
                    "chars": len(spoken),
                    "text": spoken[:80],
                })
    debug = final_payload.get("debug") if isinstance(final_payload.get("debug"), dict) else {}
    context = debug.get("context") if isinstance(debug.get("context"), dict) else {}
    runtime = debug.get("aura_runtime") if isinstance(debug.get("aura_runtime"), dict) else {}
    voice_turn = final_payload.get("voice_turn") if isinstance(final_payload.get("voice_turn"), dict) else {}
    voice_debug = voice_turn.get("debug") if isinstance(voice_turn.get("debug"), dict) else {}
    billing = voice_latency_path(runtime_config)
    result = {
        "mode": "persona-llm",
        "ok": bool(final_payload.get("ok")),
        "status": str(final_payload.get("status") or ""),
        "text": text,
        "response": str(final_payload.get("response") or emitted_text),
        "response_chars": len(str(final_payload.get("response") or emitted_text)),
        "decision_path": str(voice_debug.get("decision_path") or ""),
        "model_skipped": bool(evidence.get("model_skipped")),
        "local_preface": bool(evidence.get("local_preface")),
        "quality_guard": quality_guard,
        "llm_model": runtime_config.aura_model_model,
        "llm_provider": runtime_config.aura_model_provider,
        "llm_billing_scope": billing.get("llm_billing_scope", ""),
        "persona_context_build_ms": int(evidence.get("persona_context_build_ms") or context.get("context_build_ms") or 0),
        "persona_turn_latency_ms": int(evidence.get("persona_turn_latency_ms") or final_payload.get("latency_ms") or 0),
        "first_delta_ms": first_delta_ms,
        "first_model_delta_ms": first_model_delta_ms,
        "first_tts_text_ms": first_tts_text_ms,
        "aura_llm_first_delta_ms": int(evidence.get("aura_llm_first_delta_ms") or 0),
        "aura_llm_first_raw_delta_ms": int(evidence.get("aura_llm_first_raw_delta_ms") or 0),
        "aura_llm_first_audible_delta_ms": int(evidence.get("aura_llm_first_audible_delta_ms") or 0),
        "aura_llm_complete_ms": int(evidence.get("aura_llm_complete_ms") or 0),
        "persona_prompt_chars": int(context.get("prompt_chars") or evidence.get("aura_llm_prompt_chars") or 0),
        "persona_compact_prompt_chars": int(context.get("compact_prompt_chars") or 0),
        "aura_llm_prompt_chars": int(evidence.get("aura_llm_prompt_chars") or 0),
        "aura_llm_system_prompt_chars": int(evidence.get("aura_llm_system_prompt_chars") or 0),
        "aura_llm_max_tokens": int(evidence.get("aura_llm_max_tokens") or 0),
        "aura_llm_response_open_ms": int(evidence.get("aura_llm_response_open_ms") or 0),
        "aura_llm_response_to_first_delta_ms": int(evidence.get("aura_llm_response_to_first_delta_ms") or 0),
        "aura_llm_http_keepalive": bool(evidence.get("aura_llm_http_keepalive")),
        "aura_llm_http_keepalive_retry": bool(evidence.get("aura_llm_http_keepalive_retry")),
        "aura_llm_stop_reason": str(evidence.get("stop_reason") or ""),
        "delta_sources": delta_sources,
        "tts_segments": tts_segments[:4],
        "tts_segment_count": len(tts_segments),
        "total_ms": elapsed_ms(started),
        "aura_model_mode": str(runtime.get("aura_model_mode") or runtime_config.aura_model_mode),
        "aura_model_route": str(runtime.get("model_route") or evidence.get("route") or ""),
    }
    provider_status = evidence.get("status")
    if provider_status:
        result["provider_status"] = int(provider_status) if isinstance(provider_status, int) else str(provider_status)
    provider_error = _provider_error_summary(evidence)
    if provider_error:
        result["provider_error"] = provider_error
    if user_geo:
        result["user_geo"] = dict(user_geo)
    if not result["ok"]:
        result["error"] = str(final_payload.get("error") or final_payload.get("response") or "")[:240]
    return result


async def run_tts_only_once(runtime_config: Any, text: str) -> dict[str, Any]:
    """Measure configured TTS text -> first audio without running Aura LLM."""
    started = time.monotonic()
    ws = CaptureWebsocket()
    result = await synthesize_and_stream_tts(
        ws,
        runtime_config,
        1,
        text,
        stream_id=1,
        is_final=True,
    )
    summary = summarize_ws_frames(ws, ok=result.ok)
    billing = voice_latency_path(runtime_config)
    return {
        "mode": "tts-only",
        "ok": bool(result.ok and summary.get("audio_bytes")),
        "text": text,
        "tts_provider": runtime_config.tts_provider,
        "tts_model": runtime_config.tts_model,
        "tts_voice": runtime_config.tts_voice,
        "tts_billing_scope": billing.get("tts_billing_scope", ""),
        "first_audio_sent_ms": int(summary.get("first_audio_sent_ms") or 0),
        "final_audio_frame_ms": int(summary.get("final_audio_frame_ms") or 0),
        "tts_first_chunk_ms": int(result.first_chunk_ms or 0),
        "tts_first_audio_ms": int(result.first_audio_ms or 0),
        "tts_latency_ms": int(result.latency_ms or 0),
        "total_ms": elapsed_ms(started),
        "binary_frames": int(summary.get("binary_frames") or 0),
        "audio_bytes": int(summary.get("audio_bytes") or result.audio_bytes or len(result.audio or b"")),
        "audio_chunk_count": int(result.audio_chunk_count or 0),
        "audio_chunk_gap_p95_ms": int(result.audio_chunk_gap_p95_ms or 0),
        "audio_chunk_gap_max_ms": int(result.audio_chunk_gap_max_ms or 0),
        "audio_chunk_stall_count": int(result.audio_chunk_stall_count or 0),
        "detail": result.detail if not result.ok else "",
        "streamed": bool(result.streamed),
    }


def run_local_quality_once(
    runtime_config: Any,
    text: str,
    *,
    case_name: str,
    persona_home: str,
    user_geo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="aura-quality-") as tmp:
        tmp_path = Path(tmp)
        config = PersonaGatewayConfig(
            enabled=True,
            persona_home=str(Path(persona_home).expanduser()),
            companion_home=str(tmp_path / "companion-home"),
            hermes_home=str(tmp_path / "hermes-home"),
            include_debug_context=True,
        )
        runtime = _runtime_for_local_quality(runtime_config)
        store = LilyPersonaStore(config.companion_db_path)
        state = store.get_or_create_state(config.scope)
        state["mood"] = 84
        state["energy"] = 58
        state["trust"] = 76
        state["stress"] = 12
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        metadata.update({
            "current_activity": "散步",
            "current_location": "park",
            "location_label": "附近公园",
        })
        state["metadata"] = metadata
        store.save_state(config.scope, state)
        bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",)))
        gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
        metadata_payload: dict[str, Any] = {
            "source": "aura-lily-gateway",
            "audio_bytes": 1234,
            "streamed": True,
        }
        if user_geo:
            metadata_payload["user_geo"] = dict(user_geo)
        if case_name in STREAM_LOCAL_QUALITY_CASES:
            result_dict = _run_stream_local_quality_case(gateway, text, metadata=metadata_payload, case_name=case_name)
        else:
            result = gateway.run_turn(text, metadata=metadata_payload)
            result_dict = result.to_dict()

    voice_turn = result_dict.get("voice_turn") if isinstance(result_dict.get("voice_turn"), dict) else {}
    voice_debug = voice_turn.get("debug") if isinstance(voice_turn.get("debug"), dict) else {}
    decision_path = str(voice_debug.get("decision_path") or "")
    evidence = result_dict.get("evidence") if isinstance(result_dict.get("evidence"), dict) else {}
    quality = evaluate_local_quality(case_name, result_dict, decision_path)
    return {
        "mode": "local-quality",
        "ok": bool(quality["ok"]),
        "case": case_name,
        "text": text,
        "status": str(result_dict.get("status") or ""),
        "response": str(result_dict.get("response") or ""),
        "response_chars": len(str(result_dict.get("response") or "")),
        "decision_path": decision_path,
        "model_skipped": bool(evidence.get("model_skipped")),
        "local_preface": bool(evidence.get("local_preface")),
        "quality_guard": evidence.get("quality_guard") if isinstance(evidence.get("quality_guard"), dict) else {},
        "silent": bool(evidence.get("silent")),
        "latency_ms": int(result_dict.get("latency_ms") or 0),
        "total_ms": elapsed_ms(started),
        "quality": quality,
    }


def _run_stream_local_quality_case(gateway: AuraPersonaGateway, text: str, *, metadata: dict[str, Any], case_name: str) -> dict[str, Any]:
    with _patched_local_quality_direct_stream(case_name):
        events = list(gateway.run_direct_turn_stream(text, metadata=metadata))
    final_payload: dict[str, Any] = {}
    for event in events:
        if isinstance(event, dict) and event.get("type") == "final":
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            final_payload = dict(payload)
    if not final_payload:
        final_payload = {
            "ok": False,
            "status": "failed",
            "response": "",
            "latency_ms": 0,
            "voice_turn": {},
            "evidence": {"error": "missing_stream_final"},
        }
    final_payload["stream_events"] = [
        {"type": event.get("type"), "source": event.get("source", ""), "text": event.get("text", "")}
        for event in events
        if isinstance(event, dict) and event.get("type") == "delta"
    ]
    return final_payload


@contextlib.contextmanager
def _patched_local_quality_direct_stream(case_name: str) -> Iterable[None]:
    original = DirectLlmClient.stream

    def fake_stream(self: DirectLlmClient, prompt: str, *, metadata: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
        responses = {
            "job_change_chat": "可以，先说你是在犹豫要不要走，还是已经有目标了？",
            "open_chat": "先从你最想说的那件事开始。",
            "overtime_supportive_chat": "加班这件事先别自己憋着，是累还是烦哪一点更多？",
            "status_review_entry": "你说，我在听。",
        }
        response = responses.get(case_name, "你说，我在听。")
        yield {"type": "delta", "text": response}
        yield {
            "type": "final",
            "ok": True,
            "status": "completed",
            "response": response,
            "request_id": "local-quality-casual",
            "latency_ms": 1,
            "evidence": {
                "stop_reason": "local_quality_fake_stream",
                "route": "direct_llm",
                "streamed": True,
            },
        }

    DirectLlmClient.stream = fake_stream
    try:
        yield
    finally:
        DirectLlmClient.stream = original


def _runtime_for_local_quality(runtime_config: Any) -> Any:
    now = int(time.time())
    cache = (
        {
            "key": "open_meteo|上海||",
            "city": "上海",
            "temperature": "34.2",
            "condition": "多云",
            "weather_icon": 1,
            "humidity": "80",
            "updated_at": now,
            "ttl_seconds": 3600,
            "display": "上海，34.2度，多云，湿度80%",
            "source": "open_meteo",
            "observed_at": "2026-07-02T15:00",
        },
    )
    return replace(
        runtime_config,
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
        aura_model_mode="aura_model",
        aura_model_provider="local-quality",
        aura_model_model="local-quality-model",
        aura_model_base_url="https://local-quality.invalid/v1",
        aura_model_api_key="local-quality-key",
        cached_weather_enabled=True,
        cached_weather_city="上海",
        cached_weather_temperature="24.3",
        cached_weather_condition="多云",
        cached_weather_icon=1,
        cached_weather_humidity="99",
        cached_weather_updated_at=now,
        cached_weather_ttl_seconds=3600,
        user_weather_cache=cache,
    )


def _provider_error_summary(evidence: dict[str, Any]) -> str:
    detail = str(evidence.get("detail") or evidence.get("error") or "").strip()
    if not detail:
        return ""
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:240]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            error_type = str(error.get("type") or "").strip()
            if message and error_type:
                return f"{error_type}: {message}"[:240]
            if message:
                return message[:240]
        message = str(payload.get("message") or "").strip()
        if message:
            return message[:240]
    return detail[:240]


def evaluate_local_quality(case_name: str, result: dict[str, Any], decision_path: str) -> dict[str, Any]:
    expectation = QUALITY_EXPECTATIONS.get(str(case_name or ""))
    if not expectation:
        return {"ok": True, "checks": [], "note": "no expectation"}
    response = str(result.get("response") or "")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    expected_path = str(expectation.get("path") or "")
    if expected_path:
        add("decision_path", decision_path == expected_path, f"{decision_path} != {expected_path}")
    expected_status = str(expectation.get("status") or "")
    if expected_status:
        add("status", str(result.get("status") or "") == expected_status, str(result.get("status") or ""))
    if "model_skipped" in expectation:
        add("model_skipped", bool(evidence.get("model_skipped")) is bool(expectation["model_skipped"]), str(evidence.get("model_skipped")))
    if "silent" in expectation:
        add("silent", bool(evidence.get("silent")) is bool(expectation["silent"]), str(evidence.get("silent")))
    for token in tuple(expectation.get("contains") or ()):
        add(f"contains:{token}", str(token) in response, response[:120])
    for token in tuple(expectation.get("not_contains") or ()):
        add(f"not_contains:{token}", str(token) not in response, response[:120])
    contains_any = tuple(expectation.get("contains_any") or ())
    if contains_any:
        add("contains_any", any(str(token) in response for token in contains_any), " / ".join(str(token) for token in contains_any))
    for token in LOCAL_QUALITY_FORBIDDEN_TOKENS:
        add(f"global_not_contains:{token}", str(token) not in response, response[:120])
    return {"ok": all(item["ok"] for item in checks), "checks": checks}


class SimulatedDeviceWebsocket(CaptureWebsocket):
    def __init__(
        self,
        incoming: list[Any],
        *,
        stop_after_server_vad: bool = False,
        wait_after_stop: bool = False,
        stop_timeout: float = 5.0,
        done_timeout: float = 12.0,
    ) -> None:
        super().__init__()
        self.incoming = incoming
        self.remote_address = ("127.0.0.1", 43210)
        self.stop_after_server_vad = stop_after_server_vad
        self.wait_after_stop = wait_after_stop
        self.stop_timeout = stop_timeout
        self.done_timeout = done_timeout
        self.server_vad_stop_event = asyncio.Event()
        self.turn_done_event = asyncio.Event()

    def __aiter__(self) -> "SimulatedDeviceWebsocket":
        return self

    async def __anext__(self) -> Any:
        if not self.incoming:
            raise StopAsyncIteration
        item = self.incoming.pop(0)
        if item == "__wait_server_vad_stop__":
            try:
                await asyncio.wait_for(self.server_vad_stop_event.wait(), timeout=self.stop_timeout)
            except asyncio.TimeoutError:
                pass
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
        if item == "__wait_turn_done__":
            try:
                await asyncio.wait_for(self.turn_done_event.wait(), timeout=self.done_timeout)
            except asyncio.TimeoutError:
                pass
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return item

    async def send(self, payload: Any) -> None:
        await super().send(payload)
        if self.stop_after_server_vad and isinstance(payload, str):
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                return
            body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if body.get("action") == "server_vad_stop":
                self.server_vad_stop_event.set()
            if body.get("action") == "turn_audio_timing" or item.get("type") == "dialogue":
                self.turn_done_event.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class FakeStepfunAsrSocket:
    def __init__(self, transcript: str, *, early_final: bool = False, speech_stop: bool = False) -> None:
        self.transcript = transcript
        self.early_final = early_final
        self.speech_stop = speech_stop
        self.sent: list[dict[str, Any]] = []
        self.recv_queue: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        item = json.loads(payload)
        self.sent.append(item)
        if (self.early_final or self.speech_stop) and item.get("type") == "input_audio_buffer.append":
            event_type = (
                "conversation.item.input_audio_transcription.delta"
                if self.speech_stop
                else "conversation.item.input_audio_transcription.completed"
            )
            self.recv_queue.put_nowait(json.dumps({
                "type": event_type,
                "text": self.transcript,
            }, ensure_ascii=False))
            if self.speech_stop:
                self.recv_queue.put_nowait(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
        elif item.get("type") == "input_audio_buffer.commit":
            self.recv_queue.put_nowait(json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "text": self.transcript,
            }, ensure_ascii=False))

    async def recv(self) -> str:
        return await self.recv_queue.get()


class FakeStepfunAsrConnect:
    def __init__(
        self,
        url: str,
        *,
        transcript: str,
        early_final: bool = False,
        speech_stop: bool = False,
        **kwargs: Any,
    ) -> None:
        self.url = url
        self.kwargs = kwargs
        self.socket = FakeStepfunAsrSocket(transcript, early_final=early_final, speech_stop=speech_stop)

    async def __aenter__(self) -> FakeStepfunAsrSocket:
        return self.socket

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


async def run_voice_sim_once(
    runtime_config: Any,
    text: str,
    *,
    bridge_url: str,
    timeout: float,
    audio_ms: int,
    fake_streaming_asr: bool,
    fake_streaming_asr_early_final: bool,
    fake_streaming_asr_speech_stop: bool,
    user_geo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if fake_streaming_asr or fake_streaming_asr_early_final or fake_streaming_asr_speech_stop:
        runtime_config = replace(
            runtime_config,
            asr_enabled=True,
            asr_mode="api",
            asr_provider="stepfun",
            asr_model="stepaudio-2.5-asr-stream",
            asr_base_url="https://api.stepfun.com/v1",
            asr_api_key="fake-stepfun-asr-key",
        )
    original_ws_connect = gateway_module.ws_connect
    original_load_runtime = gateway_module.load_runtime_config_for_gateway
    fake_connections: list[FakeStepfunAsrConnect] = []
    if fake_streaming_asr or fake_streaming_asr_early_final or fake_streaming_asr_speech_stop:
        def fake_ws_connect(url: str, **kwargs: Any):
            if "realtime/asr/stream" in str(url):
                conn = FakeStepfunAsrConnect(
                    url,
                    transcript=text,
                    early_final=bool(fake_streaming_asr_early_final),
                    speech_stop=bool(fake_streaming_asr_speech_stop),
                    **kwargs,
                )
                fake_connections.append(conn)
                return conn
            return original_ws_connect(url, **kwargs)

        gateway_module.ws_connect = fake_ws_connect
        gateway_module.load_runtime_config_for_gateway = lambda: runtime_config

    sample_count = max(1, int(DEVICE_SAMPLE_RATE * max(20, audio_ms) / 1000))
    pcm = b"".join((1200 if index % 2 == 0 else -1200).to_bytes(2, "little", signed=True) for index in range(sample_count))
    start_payload: dict[str, Any] = {"turn_id": 1, "server_vad": False}
    if user_geo:
        start_payload["user_geo"] = dict(user_geo)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": start_payload,
    }, ensure_ascii=False)
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 1}}, ensure_ascii=False)
    incoming: list[Any] = [start, pcm]
    if fake_streaming_asr_early_final or fake_streaming_asr_speech_stop:
        incoming.append("__wait_server_vad_stop__")
    incoming.append(stop)
    if fake_streaming_asr_early_final or fake_streaming_asr_speech_stop:
        incoming.append("__wait_turn_done__")
    ws = SimulatedDeviceWebsocket(
        incoming,
        stop_after_server_vad=bool(fake_streaming_asr_early_final or fake_streaming_asr_speech_stop),
        wait_after_stop=bool(fake_streaming_asr_early_final or fake_streaming_asr_speech_stop),
        stop_timeout=5.0 if fake_streaming_asr_early_final else 0.2 if fake_streaming_asr_speech_stop else 5.0,
    )
    config = GatewayConfig(host="127.0.0.1", port=0, bridge_url=bridge_url, bridge_timeout_seconds=timeout)
    try:
        await handle_connection(ws, config)
    finally:
        gateway_module.ws_connect = original_ws_connect
        gateway_module.load_runtime_config_for_gateway = original_load_runtime
    summary = summarize_ws_frames(ws, ok=True)
    timing = summary.get("timing") if isinstance(summary.get("timing"), dict) else {}
    summary["ok"] = bool(summary.get("audio_bytes") and timing.get("status") == "ok")
    summary["mode"] = "voice-sim"
    summary["fake_streaming_asr"] = bool(fake_streaming_asr or fake_streaming_asr_early_final or fake_streaming_asr_speech_stop)
    summary["fake_streaming_asr_early_final"] = bool(fake_streaming_asr_early_final)
    summary["fake_streaming_asr_speech_stop"] = bool(fake_streaming_asr_speech_stop)
    summary["sim_audio_ms"] = max(20, audio_ms)
    if user_geo:
        summary["user_geo"] = dict(user_geo)
    summary["asr_ws_sent_types"] = (
        [item.get("type") for item in fake_connections[0].socket.sent]
        if fake_connections else []
    )
    return summary


async def run_voice_ws_once(
    *,
    ws_url: str,
    audio_ms: int,
    frame_ms: int,
    audio_format: str,
    realtime_upload: bool,
    user_geo: dict[str, Any] | None = None,
    turn_id: int = 1,
) -> dict[str, Any]:
    started = time.monotonic()
    sample_count = max(1, int(DEVICE_SAMPLE_RATE * max(20, audio_ms) / 1000))
    pcm = b"".join((1200 if index % 2 == 0 else -1200).to_bytes(2, "little", signed=True) for index in range(sample_count))
    upload_format = str(audio_format or "pcm").strip().lower()
    upload_frame_ms = 60 if upload_format == "opus" else max(10, frame_ms)
    packets = opus_packets_from_pcm(pcm, frame_ms=upload_frame_ms) if upload_format == "opus" else pcm_frames_from_pcm(pcm, frame_ms=upload_frame_ms)
    frames: list[tuple[float, Any]] = []
    timing_payload: dict[str, Any] = {}
    dialogue_payload: dict[str, Any] = {}
    stop_sent_at = 0.0
    first_audio_ms = 0
    final_audio_ms = 0
    audio_bytes = 0
    binary_frames = 0
    upload_done_at = 0.0

    def ms(at: float) -> int:
        return max(0, int((at - started) * 1000))

    async with ws_connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "payload": {"device_id": "codex-ws-benchmark", "boot_id": f"bench-{turn_id}"},
        }, ensure_ascii=False))
        start_payload: dict[str, Any] = {"turn_id": turn_id, "server_vad": False}
        if user_geo:
            start_payload["user_geo"] = dict(user_geo)
        await ws.send(json.dumps({
            "type": "start",
            "sample_rate": DEVICE_SAMPLE_RATE,
            "format": upload_format,
            "frame_duration": upload_frame_ms,
            "payload": start_payload,
        }, ensure_ascii=False))
        for index, packet in enumerate(packets):
            await ws.send(packet)
            if realtime_upload and index + 1 < len(packets):
                await asyncio.sleep(max(0.0, upload_frame_ms / 1000))
        upload_done_at = time.monotonic()
        await ws.send(json.dumps({"type": "stop", "payload": {"turn_id": turn_id}}, ensure_ascii=False))
        stop_sent_at = time.monotonic()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                payload = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            now = time.monotonic()
            frames.append((now, payload))
            if isinstance(payload, bytes):
                info = parse_tts_binary(payload)
                if not info:
                    continue
                binary_frames += 1
                audio_bytes += int(info.get("audio_bytes") or 0)
                if info.get("audio_bytes") and not first_audio_ms:
                    first_audio_ms = ms(now)
                if info.get("final"):
                    final_audio_ms = ms(now)
                    if timing_payload:
                        break
                continue
            if not isinstance(payload, str):
                continue
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("type") == "dialogue":
                dialogue_payload = dict(body)
            if body.get("action") == "turn_audio_timing":
                timing_payload = dict(body)
                if final_audio_ms:
                    break

    stop_ms = ms(stop_sent_at) if stop_sent_at else 0
    return {
        "mode": "voice-ws",
        "ok": bool(audio_bytes and timing_payload.get("status") == "ok"),
        "ws_url": ws_url,
        "sim_audio_ms": max(20, audio_ms),
        "frame_ms": upload_frame_ms,
        "audio_format": upload_format,
        "uploaded_frames": len(packets),
        "realtime_upload": bool(realtime_upload),
        "upload_done_ms": ms(upload_done_at) if upload_done_at else 0,
        "stop_sent_ms": stop_ms,
        "first_audio_sent_ms": first_audio_ms,
        "first_audio_after_stop_ms": max(0, first_audio_ms - stop_ms) if first_audio_ms and stop_ms else 0,
        "first_audio_after_upload_ms": max(0, first_audio_ms - ms(upload_done_at)) if first_audio_ms and upload_done_at else 0,
        "final_audio_frame_ms": final_audio_ms,
        "total_ms": ms(frames[-1][0]) if frames else stop_ms,
        "binary_frames": binary_frames,
        "audio_bytes": audio_bytes,
        "timing": timing_payload,
        "dialogue_text": str(dialogue_payload.get("text") or "")[:120],
    }


def pcm_frames_from_pcm(pcm: bytes, *, frame_ms: int) -> list[bytes]:
    frame_bytes = max(2, int(DEVICE_SAMPLE_RATE * max(10, frame_ms) / 1000) * 2)
    return [pcm[pos:pos + frame_bytes] for pos in range(0, len(pcm), frame_bytes)]


def opus_packets_from_pcm(pcm: bytes, *, frame_ms: int) -> list[bytes]:
    encoder = OpusPacketEncoder(sample_rate=DEVICE_SAMPLE_RATE, channels=1, frame_ms=frame_ms)
    try:
        frame_samples = encoder.frame_samples
        samples = memoryview(pcm).cast("h")
        packets: list[bytes] = []
        for pos in range(0, len(samples), frame_samples):
            frame = samples[pos:pos + frame_samples]
            if len(frame) < frame_samples:
                padded = bytearray(frame_samples * 2)
                padded[: len(frame) * 2] = frame.tobytes()
                frame_bytes = bytes(padded)
            else:
                frame_bytes = frame.tobytes()
            packets.append(encoder.encode(frame_bytes))
        return packets
    finally:
        encoder.close()


class OpusPacketEncoder:
    def __init__(self, *, sample_rate: int, channels: int, frame_ms: int) -> None:
        lib_name = ctypes.util.find_library("opus") or "libopus.so.0"
        self.lib = ctypes.CDLL(lib_name)
        self.sample_rate = int(sample_rate or DEVICE_SAMPLE_RATE)
        self.channels = int(channels or 1)
        self.frame_ms = max(10, int(frame_ms or 60))
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000)
        err = ctypes.c_int()
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
        ]
        self.lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.encoder = self.lib.opus_encoder_create(self.sample_rate, self.channels, 2048, ctypes.byref(err))
        if not self.encoder or err.value != 0:
            raise RuntimeError(f"opus encoder init failed: {err.value}")
        self.lib.opus_encoder_ctl(self.encoder, 4002, 24000)  # OPUS_SET_BITRATE
        self.lib.opus_encoder_ctl(self.encoder, 4010, 3)      # OPUS_SET_COMPLEXITY
        self.lib.opus_encoder_ctl(self.encoder, 4016, 0)      # OPUS_SET_DTX

    def close(self) -> None:
        if self.encoder:
            self.lib.opus_encoder_destroy(self.encoder)
            self.encoder = None

    def encode(self, pcm_frame: bytes) -> bytes:
        if not self.encoder:
            raise RuntimeError("opus encoder is closed")
        if len(pcm_frame) != self.frame_samples * 2:
            raise ValueError("pcm frame size mismatch")
        in_type = ctypes.c_int16 * self.frame_samples
        in_buf = in_type.from_buffer_copy(pcm_frame)
        out_buf = (ctypes.c_ubyte * 4000)()
        packet_len = self.lib.opus_encode(self.encoder, in_buf, self.frame_samples, out_buf, len(out_buf))
        if packet_len < 0:
            raise RuntimeError(f"opus encode failed: {packet_len}")
        return ctypes.string_at(out_buf, packet_len)


def summarize_ws_frames(ws: CaptureWebsocket, *, ok: bool) -> dict[str, Any]:
    binary_frames = []
    json_frames = []
    timing_payload: dict[str, Any] = {}
    silent_payload: dict[str, Any] = {}
    dialogue_ms = 0
    dialogue_text = ""
    first_stream_status_ms = 0
    for at, payload in ws.frames:
        ms = ws.elapsed_ms(at)
        if isinstance(payload, bytes):
            info = parse_tts_binary(payload)
            if info:
                info["ms"] = ms
                binary_frames.append(info)
            continue
        if isinstance(payload, str):
            try:
                item = json.loads(payload)
            except json.JSONDecodeError:
                continue
            json_frames.append({"ms": ms, "type": item.get("type"), "payload": item.get("payload")})
            if item.get("type") == "status" and "流式" in str(item.get("text") or ""):
                first_stream_status_ms = ms
            body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if item.get("type") == "dialogue":
                dialogue_ms = ms
                dialogue_text = str(body.get("text") or "")
            if body.get("action") == "turn_audio_timing":
                timing_payload = dict(body)
            if body.get("action") == "turn_silent_drop":
                silent_payload = dict(body)
    first_audio_ms = min((item["ms"] for item in binary_frames if item["audio_bytes"] > 0), default=0)
    final_audio_ms = max((item["ms"] for item in binary_frames if item["final"]), default=0)
    return {
        "mode": "bridge",
        "ok": bool(ok),
        "first_stream_status_ms": first_stream_status_ms,
        "first_audio_sent_ms": first_audio_ms,
        "final_audio_frame_ms": final_audio_ms,
        "dialogue_ms": dialogue_ms,
        "dialogue_text": dialogue_text[:160],
        "silent_drop": bool(silent_payload),
        "total_ms": ws.elapsed_ms(ws.frames[-1][0]) if ws.frames else 0,
        "binary_frames": len(binary_frames),
        "audio_bytes": sum(item["audio_bytes"] for item in binary_frames),
        "timing": timing_payload,
        "silent": silent_payload,
    }


def parse_tts_binary(payload: bytes) -> dict[str, Any]:
    if len(payload) < TTS_BINARY_HEADER_SIZE or payload[:4] != TTS_BINARY_MAGIC:
        return {}
    flags = payload[12]
    return {
        "stream_id": int.from_bytes(payload[4:8], "little"),
        "turn_id": int.from_bytes(payload[8:12], "little"),
        "final": bool(flags & TTS_BINARY_FLAG_FINAL),
        "audio_bytes": max(0, len(payload) - TTS_BINARY_HEADER_SIZE),
    }


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(value)) for value in values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * pct + 0.999999) - 1))
    return ordered[index]


def metric_value(result: dict[str, Any], key: str) -> int:
    if key in result:
        return max(0, int(result.get(key) or 0))
    timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
    return max(0, int(timing.get(key) or 0))


def summarize_suite(results: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [
        "first_audio_sent_ms",
        "first_audio_ready_ms",
        "final_audio_frame_ms",
        "total_ms",
        "asr_ms",
        "bridge_ms",
        "turn_to_tts_first_audio_ms",
        "first_model_delta_ms",
        "first_tts_text_ms",
        "persona_context_build_ms",
        "persona_turn_latency_ms",
        "aura_llm_response_open_ms",
        "aura_llm_response_to_first_delta_ms",
        "aura_llm_first_delta_ms",
        "aura_llm_first_raw_delta_ms",
        "aura_llm_first_audible_delta_ms",
        "aura_llm_complete_ms",
        "tts_first_text_ms",
        "tts_first_audio_ms",
        "tts_first_audio_since_bridge_ms",
        "tts_first_text_to_audio_ms",
        "tts_latency_ms",
        "audio_chunk_gap_p95_ms",
        "audio_chunk_gap_max_ms",
        "audio_chunk_stall_count",
        "bridge_first_delta_to_tts_first_text_ms",
        "tts_audio_send_ms",
        "tts_audio_send_realtime_x100",
        "tts_audio_buffer_lead_min_ms",
        "tts_audio_buffer_lead_p50_ms",
        "tts_audio_buffer_lead_final_ms",
        "tts_audio_send_pacing_sleep_ms",
        "tts_audio_chunk_gap_p95_ms",
        "tts_audio_chunk_gap_max_ms",
        "tts_audio_chunk_stall_count",
    ]
    summary: dict[str, Any] = {
        "suite": results[0].get("suite") if results else "",
        "runs": len(results),
        "ok": sum(1 for result in results if result.get("ok")),
        "metrics": {},
        "quality_guard_reasons": {},
        "model_skipped": sum(1 for result in results if result.get("model_skipped")),
        "local_preface": sum(1 for result in results if result.get("local_preface")),
    }
    for metric in metrics:
        values = [metric_value(result, metric) for result in results if metric_value(result, metric)]
        if not values:
            continue
        summary["metrics"][metric] = {
            "p50": percentile(values, 0.50),
            "p95": percentile(values, 0.95),
            "max": max(values),
        }
    by_case: dict[str, dict[str, Any]] = {}
    for result in results:
        case = str(result.get("case") or "single")
        rows = by_case.setdefault(case, {"runs": 0, "ok": 0, "silent": 0, "first_audio_ms": [], "total_ms": [], "samples": []})
        rows["runs"] += 1
        rows["ok"] += 1 if result.get("ok") else 0
        rows["silent"] += 1 if result.get("silent_drop") or bool(result.get("silent")) else 0
        first_audio = metric_value(result, "first_audio_sent_ms") or metric_value(result, "first_audio_ready_ms")
        if first_audio:
            rows["first_audio_ms"].append(first_audio)
        total = metric_value(result, "total_ms")
        if total:
            rows["total_ms"].append(total)
        sample = str(result.get("dialogue_text") or result.get("response") or "")[:80]
        if sample and len(rows["samples"]) < 3:
            rows["samples"].append(sample)
        quality = result.get("quality_guard") if isinstance(result.get("quality_guard"), dict) else {}
        if not quality:
            evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
            quality = evidence.get("quality_guard") if isinstance(evidence.get("quality_guard"), dict) else {}
        reason = str((quality or {}).get("reason") or "")
        if reason:
            summary["quality_guard_reasons"][reason] = int(summary["quality_guard_reasons"].get(reason, 0)) + 1
    summary["cases"] = {
        case: {
            "runs": rows["runs"],
            "ok": rows["ok"],
            "silent": rows["silent"],
            "samples": rows["samples"],
            "first_audio_p50_ms": percentile(rows["first_audio_ms"], 0.50),
            "first_audio_p95_ms": percentile(rows["first_audio_ms"], 0.95),
            "total_p50_ms": percentile(rows["total_ms"], 0.50),
            "total_p95_ms": percentile(rows["total_ms"], 0.95),
        }
        for case, rows in sorted(by_case.items())
    }
    return summary


def print_result(result: dict[str, Any], *, json_only: bool) -> None:
    if json_only:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("mode") == "direct":
        print(
            "summary: first_delta={first_delta_ms}ms first_audio_ready={first_audio_ready_ms}ms "
            "final_llm={final_llm_ms}ms total={total_ms}ms segments={segments}".format(
                **{**result, "segments": len(result.get("segments") or [])}
            )
        )
    elif result.get("mode") == "persona-llm":
        print(
            "summary: context={persona_context_build_ms}ms llm_first_delta={aura_llm_first_delta_ms}ms "
            "llm_open={aura_llm_response_open_ms}ms llm_open_to_delta={aura_llm_response_to_first_delta_ms}ms "
            "keepalive={aura_llm_http_keepalive} retry={aura_llm_http_keepalive_retry} "
            "first_audible={aura_llm_first_audible_delta_ms}ms first_tts_text={first_tts_text_ms}ms "
            "persona_turn={persona_turn_latency_ms}ms total={total_ms}ms prompt_chars={persona_prompt_chars} "
            "model_skipped={model_skipped}".format(**result)
        )
    elif result.get("mode") == "tts-only":
        print(
            "summary: tts_first_audio={first_audio_sent_ms}ms tts_latency={tts_latency_ms}ms "
            "final_audio={final_audio_frame_ms}ms total={total_ms}ms bytes={audio_bytes} "
            "streamed={streamed}".format(**result)
        )
    elif result.get("mode") == "direct-stream-tts":
        print(
            "summary: first_delta={first_delta_ms}ms first_audio_sent={first_audio_sent_ms}ms "
            "final_llm={final_llm_ms}ms final_audio={final_audio_frame_ms}ms "
            "total={total_ms}ms frames={binary_frames}".format(**result)
        )
    elif result.get("mode") in {"voice-sim", "voice-ws"}:
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        print(
            "summary: {mode} first_audio={first_audio_sent_ms}ms first_after_stop={first_audio_after_stop_ms}ms "
            "final_audio={final_audio_frame_ms}ms asr={asr_ms}ms streaming_asr_final={streaming_asr_final_ms}ms "
            "bridge={bridge_ms}ms tts_first_text={tts_first_text_ms}ms "
            "tts_first_audio={tts_first_audio_since_bridge_ms}ms tts_text_to_audio={tts_first_text_to_audio_ms}ms "
            "tts_gap_p95={tts_audio_chunk_gap_p95_ms}ms tts_gap_max={tts_audio_chunk_gap_max_ms}ms "
            "tts_stalls={tts_audio_chunk_stall_count} buffer_lead_min={tts_audio_buffer_lead_min_ms}ms "
            "buffer_lead_final={tts_audio_buffer_lead_final_ms}ms pacing_sleep={tts_audio_send_pacing_sleep_ms}ms "
            "realtime_first_after_response={realtime_first_audio_after_response_ms}ms "
            "total={total_ms}ms frames={binary_frames}".format(
                **{
                    **result,
                    "first_audio_after_stop_ms": result.get("first_audio_after_stop_ms", 0),
                    "asr_ms": timing.get("asr_ms", 0),
                    "streaming_asr_final_ms": timing.get("streaming_asr_final_ms", 0),
                    "bridge_ms": timing.get("bridge_ms", 0),
                    "tts_first_text_ms": timing.get("tts_first_text_ms", 0),
                    "tts_first_audio_since_bridge_ms": timing.get("tts_first_audio_since_bridge_ms", 0),
                    "tts_first_text_to_audio_ms": timing.get("tts_first_text_to_audio_ms", 0),
                    "tts_audio_chunk_gap_p95_ms": timing.get("tts_audio_chunk_gap_p95_ms", 0),
                    "tts_audio_chunk_gap_max_ms": timing.get("tts_audio_chunk_gap_max_ms", 0),
                    "tts_audio_chunk_stall_count": timing.get("tts_audio_chunk_stall_count", 0),
                    "tts_audio_send_pacing_sleep_ms": timing.get("tts_audio_send_pacing_sleep_ms", 0),
                    "realtime_first_audio_after_response_ms": timing.get("realtime_first_audio_after_response_ms", 0),
                }
            )
        )
    else:
        print(
            "summary: first_audio_sent={first_audio_sent_ms}ms final_audio={final_audio_frame_ms}ms "
            "dialogue={dialogue_ms}ms total={total_ms}ms frames={binary_frames}".format(**result)
        )


async def main_async() -> int:
    args = parse_args()
    runtime = load_aura_runtime_config(persona_home=str(Path(args.persona_home).expanduser()))
    runtime = runtime_for_benchmark(runtime, args)
    user_geo = benchmark_user_geo(args)
    results = []
    cases = BENCHMARK_SUITES.get(args.suite) if args.suite else [("single", args.text)]
    for iteration in range(1, max(1, int(args.iterations or 1)) + 1):
        for case_name, text in cases:
            result: dict[str, Any]
            if args.mode == "direct":
                result = await run_direct_once(runtime, text)
            elif args.mode == "direct-stream-tts":
                result = await run_direct_stream_tts_once(runtime, text)
            elif args.mode == "persona-llm":
                result = await run_persona_llm_once(
                    runtime,
                    text,
                    persona_home=args.persona_home,
                    user_geo=user_geo,
                )
            elif args.mode == "tts-only":
                result = await run_tts_only_once(runtime, text)
            elif args.mode == "persona-stream-tts":
                result = await run_persona_stream_tts_once(
                    runtime,
                    text,
                    persona_home=args.persona_home,
                    user_geo=user_geo,
                    warm_tts_ms=args.warm_tts_ms,
                )
            elif args.mode == "voice-sim":
                result = await run_voice_sim_once(
                    runtime,
                    text,
                    bridge_url=args.bridge_url,
                    timeout=args.timeout,
                    audio_ms=args.audio_ms,
                    fake_streaming_asr=bool(args.fake_streaming_asr),
                    fake_streaming_asr_early_final=bool(args.fake_streaming_asr_early_final),
                    fake_streaming_asr_speech_stop=bool(args.fake_streaming_asr_speech_stop),
                    user_geo=user_geo,
                )
            elif args.mode == "voice-ws":
                result = await run_voice_ws_once(
                    ws_url=args.ws_url,
                    audio_ms=args.audio_ms,
                    frame_ms=args.frame_ms,
                    audio_format=args.audio_format,
                    realtime_upload=bool(args.realtime_upload),
                    user_geo=user_geo,
                    turn_id=len(results) + 1,
                )
            elif args.mode == "local-quality":
                quality_geo = user_geo or {"city": "上海", "timezone": "Asia/Shanghai"}
                result = run_local_quality_once(
                    runtime,
                    text,
                    case_name=case_name,
                    persona_home=args.persona_home,
                    user_geo=quality_geo,
                )
            else:
                result = await run_bridge_once(
                    runtime,
                    text,
                    bridge_url=args.bridge_url,
                    timeout=args.timeout,
                    user_geo=user_geo,
                )
            result["case"] = case_name
            result["text"] = text
            result["iteration"] = iteration
            if args.suite:
                result["suite"] = args.suite
            results.append(result)
    for result in results:
        print_result(result, json_only=bool(args.json))
    if args.suite or len(results) > 1:
        summary = summarize_suite(results)
        if args.mode == "direct":
            summary["note"] = "direct mode measures LLM/TTS without gateway ASR."
        if args.json:
            print(json.dumps({"type": "summary", **summary}, ensure_ascii=False, separators=(",", ":")))
        else:
            print("suite summary:")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def benchmark_user_geo(args: argparse.Namespace) -> dict[str, Any]:
    geo = {
        "city": str(getattr(args, "user_city", "") or "").strip(),
        "timezone": str(getattr(args, "user_timezone", "") or "").strip(),
        "latitude": str(getattr(args, "user_latitude", "") or "").strip(),
        "longitude": str(getattr(args, "user_longitude", "") or "").strip(),
    }
    cleaned = {key: value for key, value in geo.items() if value}
    if cleaned:
        cleaned.setdefault("source", "benchmark")
    return cleaned


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
