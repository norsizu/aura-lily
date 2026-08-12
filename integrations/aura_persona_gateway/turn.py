from __future__ import annotations

import datetime as dt
import re
import time
import uuid
from dataclasses import dataclass, field
from threading import Thread
from typing import Any, Iterator

from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyResult
from integrations.hermes_lily_cli.runtime_config import load_hermes_provider_catalog

from .assets import load_persona_assets
from .config import PersonaGatewayConfig, load_persona_config
from .context import PersonaContext, build_persona_context
from .grounded_intent import classify_grounded_current_intent
from .llm import AGENT_TASK_MARKER, DirectLlmClient, DirectLlmConfig
from .outlets import evaluate_lily_outlets
from .query_context import correction_focus_text, resolve_query_context
from .response_contract import (
    FALLBACK_SPOKEN_REPLY,
    normalize_spoken_reply,
)
from .runtime import CONFIGURED_VALUE_MARKER, AuraRuntimeConfig, cached_weather_snapshot, load_aura_runtime_config
from .state_rules import apply_agent_reply_delta, apply_user_interaction_delta
from .store import LilyPersonaStore
from .voice_turn import (
    BackgroundTask,
    VoiceTurnResult,
    VoiceTurnVerdict,
    background_task_from_llm_marker,
    execute_voice_turn,
)
from .weather import cached_user_weather_snapshot, refresh_cached_weather_if_needed, refresh_user_weather_if_needed
from .world import build_world_snapshot

VOICE_STREAM_MAX_TOKENS = 128
VOICE_STREAM_DETAIL_MAX_TOKENS = 384
STEPFUN_REASONING_STREAM_MIN_TOKENS = 384
STEPFUN_REASONING_MODELS = {"step-3.7-flash", "step-3.5-flash", "step-3.5-flash-2603", "step-router-v1"}


def _conversation_language(metadata: dict[str, Any] | None) -> str:
    raw = str((metadata or {}).get("language") or "zh").strip().lower().replace("_", "-")
    if raw == "jp" or raw == "ja" or raw.startswith("ja-"):
        return "ja"
    if raw == "en" or raw.startswith("en-"):
        return "en"
    return "zh"


def _context_with_conversation_language(context: PersonaContext, language: str) -> PersonaContext:
    instruction = {
        "zh": "只用自然中文回答。",
        "en": "Reply only in natural English. Do not include Chinese or mention translation.",
        "ja": "自然な日本語だけで答えてください。中国語を混ぜず、翻訳設定にも触れないでください。",
    }[language]
    prompt = context.prompt + "\n\n## 本轮最高优先级语言规则\n" + instruction
    return PersonaContext(
        prompt=prompt,
        state_summary=context.state_summary,
        debug={**context.debug, "conversation_language": language, "prompt_chars": len(prompt)},
    )


@dataclass(frozen=True)
class PersonaTurnResult:
    ok: bool
    status: str
    response: str
    request_id: str
    latency_ms: int
    voice_turn: dict[str, Any]
    debug: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "response": self.response,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "voice_turn": dict(self.voice_turn),
            "debug": dict(self.debug),
            "evidence": dict(self.evidence),
        }


class AuraPersonaGateway:
    def __init__(
        self,
        *,
        config: PersonaGatewayConfig | None = None,
        bridge: HermesLilyBridge,
        store: LilyPersonaStore | None = None,
        runtime_config: AuraRuntimeConfig | None = None,
    ) -> None:
        self.config = config or load_persona_config()
        self.runtime_config = runtime_config or load_aura_runtime_config(persona_home=self.config.persona_home)
        self.bridge = bridge
        self.store = store or LilyPersonaStore(self.config.companion_db_path)

    def build_context(self, user_text: str, *, metadata: dict[str, Any] | None = None) -> tuple[PersonaContext, VoiceTurnResult, dict[str, Any]]:
        scope = self.config.scope
        conversation_language = _conversation_language(metadata)
        voice_low_latency = _is_voice_gateway_turn(metadata)
        state = self.store.get_or_create_state(scope)
        speculative = _is_speculative_turn(metadata)
        updated_state = state if speculative else apply_user_interaction_delta(state)
        if not speculative:
            self.store.save_state(scope, updated_state)
        assets = load_persona_assets(self.config)
        user_geo = self._effective_user_geo(metadata)
        focused_user_text = correction_focus_text(user_text)
        query_context = resolve_query_context(
            user_text,
            aura_home_city=self.config.aura_home_city,
            user_home_city=self.config.user_home_city,
            user_geo=user_geo,
        )
        query_context_dict = query_context.to_dict()
        local_cache = self._local_cache_for_query(query_context_dict, user_geo=user_geo, user_text=user_text)
        state_summary = self._summary_for_outlets(updated_state)
        voice_turn = execute_voice_turn(
            focused_user_text,
            fastpath=query_context_dict,
            runtime_config=self.runtime_config,
            state_summary=state_summary,
            local_cache=local_cache,
        )
        recent_limit = min(self.config.recent_message_limit, 4) if voice_low_latency else self.config.recent_message_limit
        recent = [] if voice_low_latency and _query_needs_fresh_world_answer(query_context_dict) else self.store.list_recent_messages(scope, limit=recent_limit)
        moment = None if voice_low_latency else self.store.latest_moment(scope)
        day_key = dt.datetime.now().date().isoformat()
        plan = [] if voice_low_latency else self.store.today_plan(scope, day_key=day_key)
        world_snapshot = build_world_snapshot(
            config=self.config,
            store=self.store,
            state=updated_state,
            query_context=query_context_dict,
            user_geo=user_geo,
            voice_low_latency=voice_low_latency,
            recent_messages=recent,
            persist=not speculative,
        )
        voice_turn = _with_grounded_current_voice_reply(
            voice_turn,
            user_text=user_text,
            query_context=query_context_dict,
            world_snapshot=world_snapshot,
            voice_low_latency=voice_low_latency,
        )
        updated_state = self.store.get_or_create_state(scope)
        plan = world_snapshot.get("today_plan") if isinstance(world_snapshot.get("today_plan"), list) else plan
        state_summary = self._summary_for_outlets(updated_state)
        outlets = evaluate_lily_outlets(
            state_summary,
            proactive_enabled=self.config.proactive_enabled,
            spend_enabled=self.config.spend_enabled,
        )
        context = build_persona_context(
            user_text=user_text,
            config=self.config,
            assets=assets,
            state=updated_state,
            recent_messages=recent,
            latest_moment=moment,
            today_plan=plan,
            query_context=query_context,
            outlet_signals=outlets,
            local_cache=local_cache,
            world_snapshot=world_snapshot,
            compact_voice=voice_low_latency,
        )
        if voice_low_latency:
            base_prompt_chars = len(context.prompt)
            if _is_detail_request(user_text):
                length_instruction = (
                    "用户这轮点名要详细讲。忽略上文“一句短答”的默认限制："
                    "先给结论，再分几句把要点讲全，信息讲完就停，不要为了凑字数拖长。"
                    "仍然全程口语，不要列表符号、编号或括号动作。"
                )
            else:
                length_instruction = "这轮要直接播成语音。最多一句或两小句，优先回答用户核心问题。"
            voice_prompt = (
                context.prompt
                + "\n\n## 实时语音限制\n"
                + length_instruction
                + "除非用户明确问你在哪/在干嘛/吃什么，不要主动展开位置、动态、食物或长背景。"
                + "普通回复不要提具体地点、附近店铺或最近动态；这些只在世界状态策略允许时才用。"
                + "不要用括号动作、舞台提示或心理描写。"
                + "第一句必须承接用户本轮的具体话题；不要只说“我在/我听着/那我听着/陪着你/你想从哪儿开始讲”。"
                + "用户没有问日期或时间时，不要拿当前时刻、凌晨、早上、晚上、半夜作为开场判断。"
                + "用户只是说“想聊聊”时，不要编你自己“闲着/刚醒/脑子清醒/一大早/今晚这时间点/还没睡/大半夜清醒着”，也不要编用户最近琐碎、熬夜、赶项目、钻牛角尖、这段时间挺拼或压力。"
                + "如果用户说想聊最近状态，要先回应“最近状态/状态/复盘”本身，再给一个简短切入点。"
                + "用户说“我最近状态/聊聊最近状态/复盘工作状态”时，默认是在说用户自己；不要编 Aura 自己在后台、没睡、忙完或喘口气。"
                + "示例：用户说“我想复盘最近的状态”，可以答“从工作节奏说起：是事情太满，还是提不起劲？”；禁止只复读成“最近状态啊？”"
                + "用户问你当前状态、位置或今天安排时，世界状态是唯一事实来源；不要复述最近对话里的旧地点、旧活动或旧计划。"
            )
            context = PersonaContext(
                prompt=voice_prompt,
                state_summary=context.state_summary,
                debug={
                    **context.debug,
                    "voice_low_latency": True,
                    "focused_user_text": focused_user_text if focused_user_text != user_text else "",
                    "recent_message_limit": recent_limit,
                    "compact_prompt_chars": base_prompt_chars,
                    "prompt_chars": len(voice_prompt),
                },
            )
        context = _context_with_conversation_language(context, conversation_language)
        debug = {
            "state_before_id": state.get("id"),
            "scope": self.config.scope.as_tuple(),
            "focused_user_text": focused_user_text if focused_user_text != user_text else "",
        }
        return context, voice_turn, debug

    def run_turn(self, user_text: str, *, metadata: dict[str, Any] | None = None) -> PersonaTurnResult:
        started = time.monotonic()
        request_id = f"persona-{uuid.uuid4().hex[:12]}"
        context, voice_turn, setup_debug = self.build_context(user_text, metadata=metadata)
        context_world = context.debug.get("world_snapshot") if isinstance(context.debug.get("world_snapshot"), dict) else {}
        context_current = context_world.get("current") if isinstance(context_world.get("current"), dict) else {}
        world_location_key = str(context_current.get("location_key") or "home")
        conversation_language = _conversation_language(metadata)
        voice_low_latency = _is_voice_gateway_turn(metadata) or _is_speculative_turn(metadata)
        user_message_id = self.store.save_im_message(
            self.config.scope,
            direction="user",
            message_type="user_text",
            body=user_text,
            metadata={"source": "lily_persona_gateway", "request_id": request_id},
        )
        if conversation_language == "zh" and self._should_enqueue_background_task(voice_turn):
            return self._finish_deferred_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
        if voice_turn.verdict == VoiceTurnVerdict.SILENT_DROP:
            return self._finish_silent_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
        if conversation_language == "zh" and self._should_use_local_voice_reply(voice_turn, voice_low_latency=voice_low_latency):
            return self._finish_local_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
        model_result = self._run_aura_model(
            context.prompt,
            metadata={
                "persona_gateway": True,
                "request_id": request_id,
                "aura_model_mode": self.runtime_config.aura_model_mode,
                "language": conversation_language,
            },
        )
        model_quality_guard = (
            model_result.evidence.get("quality_guard")
            if isinstance(model_result.evidence.get("quality_guard"), dict)
            else {}
        )
        model_stop_reason = str(model_result.evidence.get("stop_reason") or "")
        reply = normalize_spoken_reply(model_result.response)
        if model_quality_guard and model_stop_reason.startswith("voice_quality_guard"):
            response = reply.text
            quality_guard = dict(model_quality_guard)
        else:
            response, quality_guard = _guard_model_spoken_reply(reply.text, context=context, voice_turn=voice_turn)
        reply_debug = reply.to_debug(raw_response=model_result.response)
        if quality_guard:
            reply_debug["quality_guard"] = quality_guard
        self.store.save_im_message(
            self.config.scope,
            direction="aura",
            message_type="aura_text",
            body=response,
            status="sent" if model_result.ok else "failed",
            metadata={
                "source": "lily_persona_gateway",
                "request_id": request_id,
                "reply_to_message_id": user_message_id,
                "voice_turn": voice_turn.to_dict(),
                "reply_contract": reply_debug,
            },
        )
        state = self.store.get_or_create_state(self.config.scope)
        self.store.save_state(self.config.scope, apply_agent_reply_delta(state, ok=model_result.ok))
        debug = {
            "request_id": request_id,
            "context": context.debug,
            "voice_turn": voice_turn.to_dict(),
            "aura_runtime": {
                "fast_reply_enabled": self.runtime_config.fast_reply_enabled,
                "fast_reply_mode": self.runtime_config.fast_reply_mode,
                "voice_turn_enabled": self.runtime_config.voice_turn_enabled,
                "tts_enabled": self.runtime_config.tts_enabled,
                "tts_provider": self.runtime_config.tts_provider,
                "aura_model_mode": self.runtime_config.aura_model_mode,
                "aura_model_provider": self.runtime_config.aura_model_provider,
                "aura_model_model": self.runtime_config.aura_model_model,
                "model_route": model_result.evidence.get("route", "hermes_agent"),
            },
            "setup": setup_debug,
            "reply_contract": reply_debug,
            "hermes": {
                "ok": model_result.ok,
                "status": model_result.status,
                "latency_ms": model_result.latency_ms,
            },
        }
        if self.config.debug_enabled:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona turn",
                trace_id=request_id,
                payload=debug,
            )
        return PersonaTurnResult(
            ok=model_result.ok,
            status=model_result.status,
            response=response,
            request_id=request_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            voice_turn=voice_turn.to_dict(),
            debug=debug if self.config.include_debug_context else {},
            evidence={**dict(model_result.evidence or {}), "world_location_key": world_location_key},
        )

    def run_direct_turn_stream(self, user_text: str, *, metadata: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        started = time.monotonic()
        request_id = f"persona-{uuid.uuid4().hex[:12]}"
        context, voice_turn, setup_debug = self.build_context(user_text, metadata=metadata)
        context_world = context.debug.get("world_snapshot") if isinstance(context.debug.get("world_snapshot"), dict) else {}
        context_current = context_world.get("current") if isinstance(context_world.get("current"), dict) else {}
        world_location_key = str(context_current.get("location_key") or "home")
        conversation_language = _conversation_language(metadata)
        context_build_ms = max(0, int((time.monotonic() - started) * 1000))
        setup_debug = {**setup_debug, "context_build_ms": context_build_ms}
        voice_max_sentences, voice_char_limit = _voice_reply_budget(user_text)
        voice_detail_request = _is_detail_request(user_text)
        speculative = _is_speculative_turn(metadata)
        local_preface = ""
        local_preface_debug: dict[str, Any] = {}
        user_message_id = 0
        if not speculative:
            user_message_id = self.store.save_im_message(
                self.config.scope,
                direction="user",
                message_type="user_text",
                body=user_text,
                metadata={"source": "lily_persona_gateway", "request_id": request_id, "streamed": True},
            )
        if conversation_language == "zh" and self._should_enqueue_background_task(voice_turn) and not self._llm_marker_overrides_regex_routing(voice_turn):
            if speculative:
                result = _speculative_skipped_result(
                    request_id=request_id,
                    started=started,
                    status="deferred",
                    response=voice_turn.speak_text.strip(),
                    voice_turn=voice_turn,
                    context=context,
                    setup_debug=setup_debug,
                    stop_reason="speculative_background_task",
                )
                yield {"type": "final", "payload": result.to_dict()}
                return
            result = self._finish_deferred_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
            yield {"type": "final", "payload": result.to_dict()}
            return
        if voice_turn.verdict == VoiceTurnVerdict.SILENT_DROP:
            if speculative:
                result = _speculative_skipped_result(
                    request_id=request_id,
                    started=started,
                    status="ignored",
                    response="",
                    voice_turn=voice_turn,
                    context=context,
                    setup_debug=setup_debug,
                    stop_reason="silent_drop",
                )
                yield {"type": "final", "payload": result.to_dict()}
                return
            result = self._finish_silent_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
            yield {"type": "final", "payload": result.to_dict()}
            return
        mode = self.runtime_config.aura_model_mode.strip()
        if conversation_language == "zh" and mode in {"aura_model", "direct_llm"} and self._should_stream_with_local_preface(voice_turn):
            local_preface = _local_preface_text_for_stream(voice_turn)
            local_preface_debug = {"decision_path": str((voice_turn.debug or {}).get("decision_path") or "")}
            if local_preface:
                yield {"type": "delta", "text": local_preface, "source": "local_preface"}
                context = _context_with_local_preface(context, local_preface, voice_turn=voice_turn)
        elif conversation_language == "zh" and self._should_use_local_voice_reply(voice_turn):
            local_text = voice_turn.speak_text.strip()
            if local_text:
                yield {"type": "delta", "text": local_text, "source": "local_voice_reply"}
            if speculative:
                result = _speculative_local_voice_result(
                    request_id=request_id,
                    started=started,
                    response=local_text,
                    voice_turn=voice_turn,
                    context=context,
                    setup_debug=setup_debug,
                )
                yield {"type": "final", "payload": result.to_dict()}
                return
            result = self._finish_local_voice_turn(
                request_id=request_id,
                started=started,
                user_message_id=user_message_id,
                voice_turn=voice_turn,
                context=context,
                setup_debug=setup_debug,
            )
            yield {"type": "final", "payload": result.to_dict()}
            return

        if mode not in {"aura_model", "direct_llm"}:
            model_result = self._run_aura_model(
                context.prompt,
                metadata={
                    "persona_gateway": True,
                    "request_id": request_id,
                    "aura_model_mode": self.runtime_config.aura_model_mode,
                    "language": conversation_language,
                },
            )
            model_delta = _model_delta_after_preface(
                local_preface,
                model_result.response,
                decision_path=str(local_preface_debug.get("decision_path") or ""),
            )
            if model_delta:
                yield {"type": "delta", "text": model_delta}
            if local_preface:
                model_result = _model_result_with_local_preface(
                    model_result,
                    local_preface,
                    decision_path=str(local_preface_debug.get("decision_path") or ""),
                )
        else:
            final_event: dict[str, Any] | None = None
            model_stream_text = ""
            emitted_model_chars = 0
            stream_timing: dict[str, Any] = {}
            first_raw_delta_ms = 0
            first_audible_delta_ms = 0
            # 快模型意图判断：模型判定自己答不准时从首字符输出 [后台]任务描述，
            # 这里截住这行（不进 TTS），转后台 agent。见 llm.AGENT_TASK_STREAM_INSTRUCTION。
            # 本地前置句已经开播的轮次不再转后台（话都说一半了）。
            agent_marker_enabled = bool(self.runtime_config.ack_and_enqueue_enabled)
            agent_marker_watch = agent_marker_enabled and not local_preface
            agent_marker_hit = False
            agent_marker_buffer = ""

            def stream_evidence(extra: dict[str, Any]) -> dict[str, Any]:
                payload = {
                    "route": "direct_llm",
                    "provider": self.runtime_config.aura_model_provider,
                    "model": self.runtime_config.aura_model_model,
                    "streamed": True,
                    "persona_context_build_ms": context_build_ms,
                    "world_location_key": world_location_key,
                    "aura_llm_first_raw_delta_ms": first_raw_delta_ms,
                    "aura_llm_first_audible_delta_ms": first_audible_delta_ms,
                    "aura_llm_prompt_chars": len(context.prompt),
                    **stream_timing,
                    **extra,
                }
                return {
                    key: value
                    for key, value in payload.items()
                    if str(key).startswith("aura_llm_")
                    or (value is not None and value is not False and value != "" and value != 0)
                }

            for event in DirectLlmClient(
                DirectLlmConfig(
                    provider=self.runtime_config.aura_model_provider,
                    model=self.runtime_config.aura_model_model,
                    base_url=self.runtime_config.aura_model_base_url,
                    api_key=self.runtime_config.aura_model_api_key,
                    timeout_seconds=float(self.runtime_config.aura_model_timeout_seconds or 90),
                    max_tokens=_voice_stream_max_tokens(
                        self.runtime_config,
                        enumeration=voice_max_sentences > 2 and not voice_detail_request,
                        detail=voice_detail_request,
                    ),
                    temperature=float(self.runtime_config.aura_model_temperature or 0.4),
                    reasoning_effort=self.runtime_config.aura_model_reasoning_effort,
                    agent_marker_enabled=agent_marker_enabled,
                )
            ).stream(
                context.prompt,
                metadata={
                    "persona_gateway": True,
                    "request_id": request_id,
                    "aura_model_mode": self.runtime_config.aura_model_mode,
                    "streamed": True,
                    "language": conversation_language,
                },
            ):
                if event.get("type") == "delta":
                    raw_delta = str(event.get("text") or "")
                    if isinstance(event.get("timing"), dict):
                        stream_timing.update(dict(event["timing"]))
                    if raw_delta and not first_raw_delta_ms:
                        first_raw_delta_ms = max(0, int((time.monotonic() - started) * 1000))
                    if agent_marker_hit:
                        # 已命中 [后台]：继续吃完任务描述，不产生任何可播文本。
                        agent_marker_buffer += raw_delta
                        if len(agent_marker_buffer) > 200:
                            break
                        continue
                    if agent_marker_watch:
                        agent_marker_buffer += raw_delta
                        # 模型偶尔把半角括号写成全角，探测时归一化；放回正常流时用原文。
                        probe = agent_marker_buffer.lstrip().replace("【", "[").replace("】", "]")
                        if not probe:
                            continue
                        if len(probe) < len(AGENT_TASK_MARKER) and AGENT_TASK_MARKER.startswith(probe):
                            # 还可能是标记前缀（如 "[后"），先按住不放出去。
                            continue
                        if probe.startswith(AGENT_TASK_MARKER):
                            agent_marker_hit = True
                            continue
                        # 不是标记：把按住的字放回正常流处理。
                        agent_marker_watch = False
                        raw_delta = agent_marker_buffer
                        agent_marker_buffer = ""
                    if local_preface:
                        model_stream_text += raw_delta
                        raw_guarded_text, raw_stream_guard = _guard_model_spoken_reply(
                            _prepare_streaming_voice_model_text(model_stream_text, allow_empty_filler=True),
                            context=context,
                            voice_turn=voice_turn,
                        )
                        if raw_stream_guard and not _stream_quality_guard_should_wait(raw_stream_guard):
                            final_event = {
                                "type": "final",
                                "ok": True,
                                "status": "completed",
                                "response": raw_guarded_text.strip() or local_preface,
                                "request_id": request_id,
                                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                "evidence": stream_evidence({
                                    "stop_reason": "voice_quality_guard",
                                    "local_preface": True,
                                    "quality_guard": raw_stream_guard,
                                }),
                            }
                            break
                        audible_text = _model_text_after_preface(
                            local_preface,
                            model_stream_text,
                            decision_path=str(local_preface_debug.get("decision_path") or ""),
                        )
                        if (
                            not audible_text.strip()
                            and _local_preface_stream_should_stop(
                                local_preface,
                                model_stream_text,
                                decision_path=str(local_preface_debug.get("decision_path") or ""),
                            )
                        ):
                            final_event = {
                                "type": "final",
                                "ok": True,
                                "status": "completed",
                                "response": "",
                                "request_id": request_id,
                                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                "evidence": stream_evidence({
                                    "stop_reason": "local_preface_unsafe_continuation",
                                    "local_preface": True,
                                    "quality_guard": {
                                        "reason": "blocked_unsafe_continuation",
                                        "fallback_used": True,
                                        "forced_terminal_fallback": True,
                                        "decision_path": str(local_preface_debug.get("decision_path") or ""),
                                    },
                                }),
                            }
                            break
                        if _local_preface_continuation_ready(audible_text, raw_text=model_stream_text):
                            guarded_text, stream_guard = _guard_model_spoken_reply(
                                audible_text,
                                context=context,
                                voice_turn=voice_turn,
                            )
                            if stream_guard:
                                final_event = {
                                    "type": "final",
                                    "ok": True,
                                    "status": "completed",
                                    "response": guarded_text.strip() or local_preface,
                                    "request_id": request_id,
                                    "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                    "evidence": stream_evidence({
                                        "stop_reason": "voice_quality_guard",
                                        "local_preface": True,
                                        "quality_guard": stream_guard,
                                    }),
                                }
                                break
                            delta, emitted_model_chars = _next_stable_stream_delta(audible_text, emitted_model_chars)
                        else:
                            delta = ""
                    else:
                        model_stream_text += raw_delta
                        audible_text = _compact_streaming_voice_model_text(
                            model_stream_text,
                            max_sentences=voice_max_sentences,
                            limit=voice_char_limit,
                        )
                        if not audible_text.strip():
                            raw_guarded_text, raw_stream_guard = _guard_model_spoken_reply(
                                _prepare_streaming_voice_model_text(model_stream_text),
                                context=context,
                                voice_turn=voice_turn,
                            )
                            if raw_stream_guard and emitted_model_chars == 0:
                                if _stream_quality_guard_should_wait(raw_stream_guard):
                                    delta = ""
                                else:
                                    audible_text = raw_guarded_text
                                    delta, emitted_model_chars = _next_safe_voice_stream_delta(
                                        audible_text,
                                        emitted_model_chars,
                                        voice_turn=voice_turn,
                                    )
                                    final_event = {
                                        "type": "final",
                                        "ok": bool(audible_text.strip()),
                                        "status": "completed" if audible_text.strip() else "failed",
                                        "response": audible_text.strip(),
                                        "request_id": request_id,
                                        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                        "evidence": stream_evidence({
                                            "stop_reason": "voice_quality_guard",
                                            "voice_compacted": True,
                                            "quality_guard": raw_stream_guard,
                                        }),
                                    }
                            else:
                                delta = ""
                        else:
                            guarded_text, stream_guard = _guard_model_spoken_reply(
                                audible_text,
                                context=context,
                                voice_turn=voice_turn,
                            )
                            if stream_guard and emitted_model_chars == 0:
                                if _stream_quality_guard_should_wait(stream_guard):
                                    if _stream_quality_guard_allows_waiting_delta(stream_guard, audible_text):
                                        delta, emitted_model_chars = _next_safe_voice_stream_delta(
                                            audible_text,
                                            emitted_model_chars,
                                            voice_turn=voice_turn,
                                        )
                                    else:
                                        delta = ""
                                else:
                                    audible_text = guarded_text
                                    delta, emitted_model_chars = _next_safe_voice_stream_delta(
                                        audible_text,
                                        emitted_model_chars,
                                        voice_turn=voice_turn,
                                    )
                                    final_event = {
                                        "type": "final",
                                        "ok": bool(audible_text.strip()),
                                        "status": "completed" if audible_text.strip() else "failed",
                                        "response": audible_text.strip(),
                                        "request_id": request_id,
                                        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                        "evidence": stream_evidence({
                                            "stop_reason": "voice_quality_guard",
                                            "voice_compacted": True,
                                            "quality_guard": stream_guard,
                                        }),
                                    }
                            else:
                                if (
                                    not stream_guard
                                    and emitted_model_chars == 0
                                    and _casual_chat_stream_should_wait_first_sentence(audible_text, voice_turn=voice_turn)
                                ):
                                    delta = ""
                                elif stream_guard and _stream_quality_guard_should_wait(stream_guard):
                                    if _stream_quality_guard_allows_waiting_delta(stream_guard, audible_text):
                                        delta, emitted_model_chars = _next_safe_voice_stream_delta(
                                            audible_text,
                                            emitted_model_chars,
                                            voice_turn=voice_turn,
                                        )
                                    else:
                                        delta = ""
                                elif stream_guard:
                                    safe_response = _safe_streamed_prefix_or_fallback(
                                        audible_text,
                                        emitted_model_chars,
                                        context=context,
                                        voice_turn=voice_turn,
                                    )
                                    final_event = {
                                        "type": "final",
                                        "ok": bool(safe_response.strip()),
                                        "status": "completed" if safe_response.strip() else "failed",
                                        "response": safe_response.strip() or FALLBACK_SPOKEN_REPLY,
                                        "request_id": request_id,
                                        "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                                        "evidence": stream_evidence({
                                            "stop_reason": "voice_quality_guard_after_partial",
                                            "voice_compacted": True,
                                            "quality_guard": stream_guard,
                                        }),
                                    }
                                elif _status_review_stream_should_wait_for_completion(
                                    audible_text,
                                    raw_text=model_stream_text,
                                    voice_turn=voice_turn,
                                ):
                                    delta = ""
                                else:
                                    delta, emitted_model_chars = _next_safe_voice_stream_delta(
                                        audible_text,
                                        emitted_model_chars,
                                        voice_turn=voice_turn,
                                    )
                    if delta:
                        if not first_audible_delta_ms:
                            first_audible_delta_ms = max(0, int((time.monotonic() - started) * 1000))
                        yield {"type": "delta", "text": delta}
                    if final_event is not None and str((final_event.get("evidence") or {}).get("stop_reason") or "").startswith("voice_quality_guard"):
                        break
                    if (
                        not local_preface
                        and _streaming_voice_model_text_is_complete(
                            audible_text,
                            raw_text=model_stream_text,
                            max_sentences=voice_max_sentences,
                            limit=voice_char_limit,
                        )
                    ):
                        final_event = {
                            "type": "final",
                            "ok": bool(audible_text.strip()),
                            "status": "completed" if audible_text.strip() else "failed",
                            "response": audible_text.strip(),
                            "request_id": request_id,
                            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                            "evidence": stream_evidence({
                                "stop_reason": "voice_compact_limit",
                                "voice_compacted": True,
                                "raw_chars": len(model_stream_text),
                                "spoken_chars": len(audible_text),
                            }),
                        }
                        break
                    continue
                if event.get("type") == "final":
                    final_event = event
            if agent_marker_hit:
                # 快模型判定这轮它答不准 → 转后台 agent，当场只播一句确认。
                task_text = agent_marker_buffer.lstrip().replace("【", "[").replace("】", "]")
                task_text = task_text[len(AGENT_TASK_MARKER):] if task_text.startswith(AGENT_TASK_MARKER) else task_text
                task_text = task_text.strip().splitlines()[0].strip() if task_text.strip() else ""
                deferred_voice_turn = _voice_turn_with_llm_background_task(
                    voice_turn,
                    task_text=task_text,
                    source_text=user_text,
                    ack_text=self.runtime_config.background_ack_reply,
                )
                ack_text = deferred_voice_turn.speak_text.strip()
                if speculative:
                    result = _speculative_skipped_result(
                        request_id=request_id,
                        started=started,
                        status="deferred",
                        response=ack_text,
                        voice_turn=deferred_voice_turn,
                        context=context,
                        setup_debug=setup_debug,
                        stop_reason="speculative_background_task",
                    )
                    yield {"type": "final", "payload": result.to_dict()}
                    return
                if ack_text:
                    yield {"type": "delta", "text": ack_text, "source": "local_voice_reply"}
                result = self._finish_deferred_voice_turn(
                    request_id=request_id,
                    started=started,
                    user_message_id=user_message_id,
                    voice_turn=deferred_voice_turn,
                    context=context,
                    setup_debug=setup_debug,
                )
                yield {"type": "final", "payload": result.to_dict()}
                return
            stop_reason = str(((final_event or {}).get("evidence") or {}).get("stop_reason") or "")
            final_response = str((final_event or {}).get("response") or "")
            final_evidence = dict((final_event or {}).get("evidence") or {})
            if final_evidence:
                stream_timing.update({
                    key: value
                    for key, value in final_evidence.items()
                    if str(key).startswith("aura_llm_")
                })
            if not stop_reason.startswith("voice_quality_guard") and not local_preface:
                compacted_final = _compact_streaming_voice_model_text(
                    final_response,
                    max_sentences=voice_max_sentences,
                    limit=voice_char_limit,
                )
                final_repair_reason = ""
                original_compacted_final = compacted_final
                if _looks_like_provider_error_reply(final_response):
                    compacted_final = _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn)
                    final_repair_reason = "blocked_provider_error_reply"
                elif _voice_stream_requires_complete_sentence(voice_turn):
                    repaired_final = _safe_streamed_prefix_or_fallback(
                        compacted_final,
                        len(compacted_final),
                        context=context,
                        voice_turn=voice_turn,
                    )
                    if repaired_final.strip() != compacted_final.strip():
                        final_repair_reason = _streaming_final_repair_reason_for_original(
                            compacted_final
                        )
                        compacted_final = repaired_final
                if final_repair_reason or _streaming_text_ends_with_incomplete_phrase(compacted_final):
                    if not final_repair_reason:
                        compacted_final = _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn)
                        final_repair_reason = _streaming_final_repair_reason_for_original(
                            original_compacted_final
                        )
                    repair_guard = {
                        "reason": final_repair_reason,
                        "fallback_used": compacted_final == _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn),
                        "final_response_repaired": True,
                        "decision_path": str((voice_turn.debug or {}).get("decision_path") or ""),
                    }
                    if _stream_quality_guard_should_force_fallback(repair_guard, stop_reason="voice_quality_guard"):
                        compacted_final = _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn)
                        repair_guard = {
                            **repair_guard,
                            "fallback_used": True,
                            "forced_terminal_fallback": True,
                        }
                    final_evidence = {
                        **final_evidence,
                        **stream_evidence({
                            "stop_reason": "voice_quality_guard",
                            "voice_compacted": True,
                            "quality_guard": repair_guard,
                        }),
                    }
                    final_event = {
                        **(final_event or {}),
                        "ok": bool((final_event or {}).get("ok")) if str((final_event or {}).get("status") or "") == "failed" else bool(compacted_final.strip()),
                        "status": str((final_event or {}).get("status") or "") if str((final_event or {}).get("status") or "") == "failed" else ("completed" if compacted_final.strip() else "failed"),
                        "response": compacted_final,
                        "evidence": final_evidence,
                    }
                    final_response = compacted_final
                    stop_reason = "voice_quality_guard"
            elif stop_reason.startswith("voice_quality_guard") and not local_preface:
                raw_quality_guard = (
                    final_evidence.get("quality_guard")
                    if isinstance(final_evidence.get("quality_guard"), dict)
                    else {}
                )
                guarded_final, final_guard = _guard_model_spoken_reply(
                    final_response,
                    context=context,
                    voice_turn=voice_turn,
                )
                if _stream_quality_guard_should_force_fallback(raw_quality_guard, stop_reason=stop_reason):
                    stable_fallback = _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn)
                    final_guard = {
                        **dict(raw_quality_guard),
                        "reason": str(raw_quality_guard.get("reason") or "voice_quality_guard"),
                        "fallback_used": True,
                        "final_response_repaired": True,
                        "forced_terminal_fallback": True,
                    }
                    guarded_final = stable_fallback
                if final_guard and guarded_final.strip() != final_response.strip():
                    final_evidence = {
                        **final_evidence,
                        **stream_evidence({
                            "stop_reason": stop_reason,
                            "voice_compacted": True,
                            "quality_guard": {
                                **dict(final_guard),
                                "final_response_repaired": True,
                            },
                        }),
                    }
                    final_event = {
                        **(final_event or {}),
                        "ok": bool(guarded_final.strip()),
                        "status": "completed" if guarded_final.strip() else "failed",
                        "response": guarded_final,
                        "evidence": final_evidence,
                    }
                    final_response = guarded_final
            model_result = HermesLilyResult(
                ok=bool((final_event or {}).get("ok")),
                status=str((final_event or {}).get("status") or "failed"),
                response=(
                    final_response
                    if local_preface or stop_reason.startswith("voice_quality_guard")
                    else _compact_streaming_voice_model_text(
                        final_response,
                        max_sentences=voice_max_sentences,
                        limit=voice_char_limit,
                    )
                ),
                request_id=str((final_event or {}).get("request_id") or request_id),
                latency_ms=int((final_event or {}).get("latency_ms") or 0),
                evidence={
                    **stream_evidence({}),
                    **dict((final_event or {}).get("evidence") or {}),
                },
            )
            if local_preface:
                model_result = _model_result_with_local_preface(
                    model_result,
                    local_preface,
                    decision_path=str(local_preface_debug.get("decision_path") or ""),
                )

        model_quality_guard = (
            model_result.evidence.get("quality_guard")
            if isinstance(model_result.evidence.get("quality_guard"), dict)
            else {}
        )
        model_stop_reason = str(model_result.evidence.get("stop_reason") or "")
        reply = normalize_spoken_reply(model_result.response)
        if model_quality_guard and model_stop_reason.startswith("voice_quality_guard"):
            response = reply.text
            quality_guard = dict(model_quality_guard)
        else:
            response, quality_guard = _guard_model_spoken_reply(reply.text, context=context, voice_turn=voice_turn)
        reply_debug = reply.to_debug(raw_response=model_result.response)
        if quality_guard:
            reply_debug["quality_guard"] = quality_guard
        if not speculative:
            self.store.save_im_message(
                self.config.scope,
                direction="aura",
                message_type="aura_text",
                body=response,
                status="sent" if model_result.ok else "failed",
                metadata={
                    "source": "lily_persona_gateway",
                    "request_id": request_id,
                    "reply_to_message_id": user_message_id,
                    "voice_turn": voice_turn.to_dict(),
                    "reply_contract": reply_debug,
                    "streamed": True,
                    "local_preface": bool(local_preface),
                },
            )
            state = self.store.get_or_create_state(self.config.scope)
            self.store.save_state(self.config.scope, apply_agent_reply_delta(state, ok=model_result.ok))
        debug = {
            "request_id": request_id,
            "context": context.debug,
            "voice_turn": voice_turn.to_dict(),
            "aura_runtime": {
                "fast_reply_enabled": self.runtime_config.fast_reply_enabled,
                "fast_reply_mode": self.runtime_config.fast_reply_mode,
                "voice_turn_enabled": self.runtime_config.voice_turn_enabled,
                "tts_enabled": self.runtime_config.tts_enabled,
                "tts_provider": self.runtime_config.tts_provider,
                "aura_model_mode": self.runtime_config.aura_model_mode,
                "aura_model_provider": self.runtime_config.aura_model_provider,
                "aura_model_model": self.runtime_config.aura_model_model,
                "model_route": model_result.evidence.get("route", "hermes_agent"),
                "streamed": True,
                "speculative": speculative,
            },
            "setup": setup_debug,
            "reply_contract": reply_debug,
            "hermes": {
                "ok": model_result.ok,
                "status": model_result.status,
                "latency_ms": model_result.latency_ms,
            },
        }
        if local_preface:
            debug["local_preface"] = {
                **local_preface_debug,
                "chars": len(local_preface),
            }
        if self.config.debug_enabled and not speculative:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona streamed turn",
                trace_id=request_id,
                payload=debug,
            )
        persona_turn_latency_ms = max(0, int((time.monotonic() - started) * 1000))
        result = PersonaTurnResult(
            ok=model_result.ok,
            status=model_result.status,
            response=response,
            request_id=request_id,
            latency_ms=persona_turn_latency_ms,
            voice_turn=voice_turn.to_dict(),
            debug=debug if self.config.include_debug_context else {},
            evidence={
                **dict(model_result.evidence or {}),
                "world_location_key": world_location_key,
                "streamed": True,
                "persona_turn_latency_ms": persona_turn_latency_ms,
                "local_preface": bool(local_preface),
                "speculative": speculative,
                **({"quality_guard": quality_guard} if quality_guard else {}),
            },
        )
        yield {"type": "final", "payload": result.to_dict()}

    def _should_stream_with_local_preface(self, voice_turn: VoiceTurnResult) -> bool:
        if not voice_turn.speak_text.strip():
            return False
        if voice_turn.background_task:
            return False
        if _voice_turn_has_local_complete(voice_turn):
            return False
        if not self.runtime_config.fast_reply_enabled:
            return False
        decision_path = str((voice_turn.debug or {}).get("decision_path") or "")
        return decision_path in {
            "cached_weather_advice",
            "state_mood",
            "outing_weather_advice",
            "local_social",
        } or _voice_turn_can_use_conversational_preface(voice_turn)

    def _should_use_local_voice_reply(self, voice_turn: VoiceTurnResult, *, voice_low_latency: bool = True) -> bool:
        if not voice_turn.speak_text.strip():
            return False
        if voice_turn.background_task:
            return False
        decision_path = str((voice_turn.debug or {}).get("decision_path") or "")
        if not voice_low_latency and decision_path == "local_social":
            return False
        if _voice_turn_has_local_complete(voice_turn):
            return True
        if decision_path in {
            "cached_weather",
            "cached_weather_advice",
            "weather_unavailable",
            "weather_advice_unavailable",
            "current_time",
            "current_time_weather",
            "explicit_fixed_reply",
            "grounded_current_activity",
            "grounded_current_location",
            "state_mood",
            "outing_weather_advice",
            # 定时提醒必须用本地确定性话术：模型自由发挥会答应根本没调度的时间。
            "reminder_set",
            "reminder_cancel",
            "reminder_time_unclear",
        }:
            return True
        if decision_path in {"supportive_chat"}:
            return False
        if not self.runtime_config.fast_reply_enabled:
            return False
        if self.runtime_config.fast_reply_mode != "local_rule":
            return False
        return voice_turn.verdict in {
            VoiceTurnVerdict.SPEAK_NOW,
            VoiceTurnVerdict.CLARIFY_NOW,
            VoiceTurnVerdict.REFUSE_NOW,
        }

    def _should_enqueue_background_task(self, voice_turn: VoiceTurnResult) -> bool:
        if not self.runtime_config.ack_and_enqueue_enabled:
            return False
        if voice_turn.verdict != VoiceTurnVerdict.ACK_AND_ENQUEUE:
            return False
        return voice_turn.background_task is not None

    def _llm_marker_overrides_regex_routing(self, voice_turn: VoiceTurnResult) -> bool:
        """流式路径上由快模型自己做意图判断（[后台] 标记），关键词正则只留兜底。

        只放行 agent_lookup/agent_create 这两条靠"查/搜/找"等关键词猜出来的
        路由，让模型先试着当场答、答不准再自己打标转后台；forecast_lookup
        是能力边界判断（本地只有天气实况缓存，未来预报必须联网），保持
        确定性路由，不交给模型赌。
        """
        if self.runtime_config.aura_model_mode.strip() not in {"aura_model", "direct_llm"}:
            return False
        if voice_turn.background_task is None:
            return False
        decision_path = str((voice_turn.debug or {}).get("decision_path") or "")
        return decision_path in {"agent_lookup", "agent_create"}

    def _run_aura_model(self, prompt: str, *, metadata: dict[str, Any]) -> HermesLilyResult:
        mode = self.runtime_config.aura_model_mode.strip()
        if mode in {"aura_model", "direct_llm"}:
            return DirectLlmClient(
                DirectLlmConfig(
                    provider=self.runtime_config.aura_model_provider,
                    model=self.runtime_config.aura_model_model,
                    base_url=self.runtime_config.aura_model_base_url,
                    api_key=self.runtime_config.aura_model_api_key,
                    timeout_seconds=float(self.runtime_config.aura_model_timeout_seconds or 90),
                    max_tokens=int(self.runtime_config.aura_model_max_tokens or 96),
                    temperature=float(self.runtime_config.aura_model_temperature or 0.4),
                    reasoning_effort=self.runtime_config.aura_model_reasoning_effort,
                )
            ).run(prompt, metadata=metadata)
        return self._aura_model_bridge().run(prompt, metadata=metadata)

    def _aura_model_bridge(self) -> HermesLilyBridge:
        if self.runtime_config.aura_model_mode not in {"hermes_main", "hermes_agent"}:
            return self.bridge
        base = self.bridge.config
        provider = self.runtime_config.aura_model_provider.strip()
        model = self.runtime_config.aura_model_model.strip()
        base_url = self.runtime_config.aura_model_base_url.strip()
        api_key = self.runtime_config.aura_model_api_key.strip()
        extra_env = dict(base.extra_env or {})
        if api_key and api_key != CONFIGURED_VALUE_MARKER:
            extra_env.setdefault("AURA_MODEL_API_KEY", api_key)
            extra_env.setdefault("HERMES_INFERENCE_API_KEY", api_key)
            extra_env.setdefault("OPENAI_API_KEY", api_key)
        if base_url:
            extra_env.setdefault("AURA_MODEL_BASE_URL", base_url)
            extra_env.setdefault("HERMES_INFERENCE_BASE_URL", base_url)
            extra_env.setdefault("OPENAI_BASE_URL", base_url)
        self._add_provider_env(extra_env, provider=provider, api_key=api_key, base_url=base_url)
        return HermesLilyBridge(
            base.__class__(
                command=base.command,
                provider=provider or base.provider,
                model=model or base.model,
                cwd=base.cwd,
                hermes_home=base.hermes_home,
                toolsets=base.toolsets,
                skills=base.skills,
                timeout_seconds=float(self.runtime_config.aura_model_timeout_seconds or base.timeout_seconds),
                accept_hooks=base.accept_hooks,
                ignore_rules=base.ignore_rules,
                yolo=base.yolo,
                extra_args=base.extra_args,
                extra_env=extra_env,
            )
        )

    def _add_provider_env(self, extra_env: dict[str, str], *, provider: str, api_key: str, base_url: str) -> None:
        if not provider:
            return
        provider_key = provider.strip().lower()
        try:
            catalog = load_hermes_provider_catalog()
        except (FileNotFoundError, ValueError):
            catalog = ()
        for item in catalog:
            item_id = str(item.get("id") or "").strip().lower()
            aliases = {str(alias).strip().lower() for alias in item.get("aliases") or []}
            if provider_key != item_id and provider_key not in aliases:
                continue
            if api_key and api_key != CONFIGURED_VALUE_MARKER:
                for env_name in item.get("api_key_env_vars") or ():
                    if env_name:
                        extra_env.setdefault(str(env_name), api_key)
            if base_url and item.get("base_url_env"):
                extra_env.setdefault(str(item["base_url_env"]), base_url)
            return

    def _finish_local_voice_turn(
        self,
        *,
        request_id: str,
        started: float,
        user_message_id: int,
        voice_turn: VoiceTurnResult,
        context: PersonaContext,
        setup_debug: dict[str, Any],
    ) -> PersonaTurnResult:
        response = voice_turn.speak_text.strip()
        evidence: dict[str, Any] = {"stop_reason": "local_voice_reply", "model_skipped": True, "local_voice_reply": True}
        reminder_payload = (voice_turn.debug or {}).get("reminder")
        if isinstance(reminder_payload, dict) and reminder_payload:
            # 网关靠 evidence["reminder"] 真正调度到点播报，别弄丢。
            evidence["reminder"] = dict(reminder_payload)
        self.store.save_im_message(
            self.config.scope,
            direction="aura",
            message_type="aura_text",
            body=response,
            status="sent",
            metadata={
                "source": "lily_persona_gateway",
                "request_id": request_id,
                "reply_to_message_id": user_message_id,
                "voice_turn": voice_turn.to_dict(),
                "local_voice_reply": True,
                "streamed": True,
                "local_preface": False,
                "evidence": dict(evidence),
            },
        )
        state = self.store.get_or_create_state(self.config.scope)
        self.store.save_state(self.config.scope, apply_agent_reply_delta(state, ok=True))
        debug = {
            "request_id": request_id,
            "context": context.debug,
            "voice_turn": voice_turn.to_dict(),
            "aura_runtime": {
                "fast_reply_enabled": self.runtime_config.fast_reply_enabled,
                "fast_reply_mode": self.runtime_config.fast_reply_mode,
                "voice_turn_enabled": self.runtime_config.voice_turn_enabled,
                "tts_enabled": self.runtime_config.tts_enabled,
                "tts_provider": self.runtime_config.tts_provider,
            },
            "setup": setup_debug,
            "hermes": {
                "ok": True,
                "status": "skipped",
                "latency_ms": 0,
                "reason": "local_voice_reply",
            },
        }
        if self.config.debug_enabled:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona local voice turn",
                trace_id=request_id,
                payload=debug,
            )
        return PersonaTurnResult(
            ok=True,
            status="completed",
            response=response,
            request_id=request_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            voice_turn=voice_turn.to_dict(),
            debug=debug if self.config.include_debug_context else {},
            evidence=dict(evidence),
        )

    def _finish_silent_voice_turn(
        self,
        *,
        request_id: str,
        started: float,
        user_message_id: int,
        voice_turn: VoiceTurnResult,
        context: PersonaContext,
        setup_debug: dict[str, Any],
    ) -> PersonaTurnResult:
        metadata = {
            "source": "lily_persona_gateway",
            "request_id": request_id,
            "reply_to_message_id": user_message_id,
            "voice_turn": voice_turn.to_dict(),
            "silent_drop": True,
        }
        self.store.record_life_event(
            self.config.scope,
            event_type="lily.voice.silent_drop",
            title="Lily voice turn ignored",
            description=str((voice_turn.debug or {}).get("decision_path") or "silent_drop"),
            visibility="debug",
            payload=metadata,
        )
        debug = {
            "request_id": request_id,
            "context": context.debug,
            "voice_turn": voice_turn.to_dict(),
            "aura_runtime": {
                "voice_turn_enabled": self.runtime_config.voice_turn_enabled,
                "aura_model_mode": self.runtime_config.aura_model_mode,
                "tts_enabled": self.runtime_config.tts_enabled,
            },
            "setup": setup_debug,
            "hermes": {
                "ok": True,
                "status": "skipped",
                "latency_ms": 0,
                "reason": "silent_drop",
            },
        }
        if self.config.debug_enabled:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona silent voice turn",
                trace_id=request_id,
                payload=debug,
            )
        return PersonaTurnResult(
            ok=True,
            status="ignored",
            response="",
            request_id=request_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            voice_turn=voice_turn.to_dict(),
            debug=debug if self.config.include_debug_context else {},
            evidence={"stop_reason": "silent_drop", "model_skipped": True, "silent": True},
        )

    def _finish_deferred_voice_turn(
        self,
        *,
        request_id: str,
        started: float,
        user_message_id: int,
        voice_turn: VoiceTurnResult,
        context: PersonaContext,
        setup_debug: dict[str, Any],
    ) -> PersonaTurnResult:
        task = voice_turn.background_task
        response = voice_turn.speak_text.strip() or self.runtime_config.background_ack_reply
        if task is None:
            response = response or "好，我先处理。"
        task_id = task.task_id if task else f"voice-{uuid.uuid4().hex[:12]}"
        task_payload = task.to_dict() if task else {}
        self.store.save_im_message(
            self.config.scope,
            direction="aura",
            message_type="aura_text",
            body=response,
            status="sent",
            metadata={
                "source": "lily_persona_gateway",
                "request_id": request_id,
                "reply_to_message_id": user_message_id,
                "voice_turn": voice_turn.to_dict(),
                "deferred": True,
                "background_task": task_payload,
            },
            task_id=task_id,
            reply_to_id=user_message_id,
        )
        self.store.record_life_event(
            self.config.scope,
            event_type="lily.background_task.queued",
            title="Lily background task queued",
            description=str(task.source_text if task else response),
            visibility="debug",
            payload={
                "request_id": request_id,
                "task_id": task_id,
                "voice_turn": voice_turn.to_dict(),
            },
        )
        state = self.store.get_or_create_state(self.config.scope)
        self.store.save_state(self.config.scope, apply_agent_reply_delta(state, ok=True))
        debug = {
            "request_id": request_id,
            "context": context.debug,
            "voice_turn": voice_turn.to_dict(),
            "aura_runtime": {
                "fast_reply_enabled": self.runtime_config.fast_reply_enabled,
                "fast_reply_mode": self.runtime_config.fast_reply_mode,
                "ack_and_enqueue_enabled": self.runtime_config.ack_and_enqueue_enabled,
                "voice_turn_enabled": self.runtime_config.voice_turn_enabled,
                "tts_enabled": self.runtime_config.tts_enabled,
                "tts_provider": self.runtime_config.tts_provider,
                "aura_model_mode": self.runtime_config.aura_model_mode,
            },
            "setup": setup_debug,
            "hermes": {
                "ok": True,
                "status": "queued",
                "latency_ms": 0,
                "reason": "background_task",
            },
        }
        if self.config.debug_enabled:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona background task queued",
                trace_id=request_id,
                payload=debug,
            )
        if task is not None:
            self._start_background_task(
                task,
                request_id=request_id,
                user_message_id=user_message_id,
            )
        return PersonaTurnResult(
            ok=True,
            status="deferred",
            response=response,
            request_id=request_id,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            voice_turn=voice_turn.to_dict(),
            debug=debug if self.config.include_debug_context else {},
            evidence={
                "stop_reason": "background_task_queued",
                "model_skipped": True,
                "deferred": True,
                "task_id": task_id,
                "task_kind": task.task_kind if task else "",
            },
        )

    def _start_background_task(
        self,
        task: BackgroundTask,
        *,
        request_id: str,
        user_message_id: int,
    ) -> None:
        thread = Thread(
            target=self._run_background_task,
            kwargs={
                "task": task,
                "request_id": request_id,
                "user_message_id": user_message_id,
            },
            name=f"aura-bg-{task.task_id}",
            daemon=True,
        )
        thread.start()

    def _run_background_task(
        self,
        *,
        task: BackgroundTask,
        request_id: str,
        user_message_id: int,
    ) -> None:
        result = self.bridge.run(
            self._background_task_goal(task),
            metadata={
                "persona_gateway": True,
                "request_id": request_id,
                "background_task": task.to_dict(),
                "aura_model_mode": "hermes_agent",
            },
        )
        body = result.response.strip() or task.fallback_text
        status = "sent" if result.ok else "failed"
        payload = {
            "source": "lily_persona_gateway",
            "request_id": request_id,
            "background_task": task.to_dict(),
            "hermes": {
                "ok": result.ok,
                "status": result.status,
                "latency_ms": result.latency_ms,
                "evidence": dict(result.evidence or {}),
            },
        }
        self.store.save_im_message(
            self.config.scope,
            direction="aura",
            message_type="background_task_result",
            body=body,
            status=status,
            metadata=payload,
            task_id=task.task_id,
            reply_to_id=user_message_id,
        )
        self.store.record_life_event(
            self.config.scope,
            event_type="lily.background_task.completed" if result.ok else "lily.background_task.failed",
            title="Lily background task completed" if result.ok else "Lily background task failed",
            description=body[:500],
            visibility="debug",
            payload=payload,
        )
        if self.config.debug_enabled:
            self.store.record_debug_event(
                self.config.scope,
                title="Lily persona background task finished",
                trace_id=f"{request_id}:{task.task_id}",
                payload=payload,
            )

    def _background_task_goal(self, task: BackgroundTask) -> str:
        """组装后台 agent 任务的完整 goal。

        hermes 每次是全新进程，只发 source_text 会让它既没上下文（上一轮
        自己报过的股票代码这轮就"没记到"）、又不知道自己是谁（把
        `hermes tools`、"配置 provider" 这类运维话直接讲给用户听）。
        这里补上最近对话历史 + Lily 人设/输出约束。
        """
        lines: list[str] = []
        try:
            recent = self.store.list_recent_messages(self.config.scope, limit=8)
        except Exception:
            recent = []
        for item in recent:
            body = str(item.get("body") or "").strip().replace("\n", " ")
            if not body:
                continue
            role = "用户" if item.get("direction") == "user" else "你"
            lines.append(f"{role}：{body[:200]}")
        history = "\n".join(lines[-8:])
        parts = [
            "你是 Lily（用户叫她的语音伙伴），正在后台替用户跑一个查证/执行任务，结果会直接念给用户听。",
            "可以使用你的全部工具（联网搜索、终端、读写文件、代码执行、记忆）完成任务；先动手做，不要先解释方法。",
        ]
        if history:
            parts.append("## 最近对话（含你自己说过的话，视为你的记忆，不要否认或复述询问）\n" + history)
        parts.append("## 用户这轮的任务\n" + task.source_text)
        parts.append(
            "## 输出要求\n"
            "- 用口语中文直接给结论和关键信息，像跟朋友汇报，不超过三四句。\n"
            "- 禁止出现工具名、命令、配置项、provider、hermes 之类实现细节；工具失败就说“这个我暂时查不到”，并给替代建议。\n"
            "- 数据带上来源名称（如 Yahoo Finance），不要贴长链接。"
        )
        return "\n\n".join(parts)

    @staticmethod
    def _summary_for_outlets(state: dict[str, Any]) -> dict[str, Any]:
        from .state_rules import state_context_summary

        return state_context_summary(state)

    def _effective_user_geo(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if isinstance((metadata or {}).get("user_geo"), dict):
            geo = dict((metadata or {}).get("user_geo") or {})
            if geo.get("city") or geo.get("timezone") or geo.get("latitude") or geo.get("longitude"):
                return geo
        return self.config.configured_user_geo()

    def _local_cache_for_query(
        self,
        query_context: dict[str, Any],
        *,
        user_geo: dict[str, Any] | None = None,
        user_text: str = "",
    ) -> dict[str, Any]:
        intent = str(query_context.get("intent") or "")
        if intent in {"time", "time_weather"}:
            from .time_context import current_time_snapshot, unknown_time_snapshot

            subject = str(query_context.get("subject_entity") or "").strip()
            target = str(query_context.get("target_location") or "").strip()
            timezone = str(query_context.get("timezone") or "").strip()
            if subject == "aura":
                snapshot = current_time_snapshot(city=target or self.config.aura_home_city)
            elif subject == "user":
                if not (target or timezone):
                    snapshot = unknown_time_snapshot(city=target)
                else:
                    snapshot = current_time_snapshot(city=target, timezone_name=timezone)
            else:
                snapshot = current_time_snapshot()
            query_context["time_snapshot"] = snapshot
            local_cache = {"current_time": snapshot}
            if intent == "time_weather":
                weather_snapshot = self._weather_snapshot_for_query(query_context, user_geo=user_geo or {})
                query_context["weather_snapshot"] = weather_snapshot
                local_cache["cached_weather"] = weather_snapshot
            return local_cache
        if intent == "chat" and _looks_like_outing_weather_context(user_text, user_geo or {}):
            snapshot = self._cached_user_weather_snapshot_for_geo(user_geo or {})
            if snapshot:
                return {"cached_weather": snapshot}
        if intent not in {"weather", "weather_advice"}:
            return {}
        snapshot = self._weather_snapshot_for_query(query_context, user_geo=user_geo or {})
        query_context["weather_snapshot"] = snapshot
        return {"cached_weather": snapshot}

    def _weather_snapshot_for_query(self, query_context: dict[str, Any], *, user_geo: dict[str, Any]) -> dict[str, Any]:
        subject = str(query_context.get("subject_entity") or "").strip()
        target = str(query_context.get("target_location") or "").strip()
        location_source = str(query_context.get("location_source") or "").strip()
        if subject == "aura" or location_source == "aura_home":
            city = target or self.config.aura_home_city or self.runtime_config.cached_weather_city
            self.runtime_config, refresh = refresh_cached_weather_if_needed(self.runtime_config, city=city)
            return dict(refresh.get("weather") or cached_weather_snapshot(self.runtime_config))
        if subject in {"user", "location"} and (
            target
            or (str(user_geo.get("latitude") or user_geo.get("lat") or "").strip()
                and str(user_geo.get("longitude") or user_geo.get("lon") or user_geo.get("lng") or "").strip())
        ):
            self.runtime_config, snapshot = refresh_user_weather_if_needed(
                self.runtime_config,
                city=target,
                latitude=str(user_geo.get("latitude") or user_geo.get("lat") or ""),
                longitude=str(user_geo.get("longitude") or user_geo.get("lon") or user_geo.get("lng") or ""),
            )
            return snapshot
        if subject == "user":
            return {
                "enabled": bool(self.runtime_config.cached_weather_enabled),
                "status": "unknown_location",
                "city": "",
                "temperature": "",
                "condition": "",
                "weather_icon": 0,
                "humidity": "",
                "updated_at": 0,
                "ttl_seconds": 0,
                "age_seconds": None,
                "has_content": False,
                "display": "",
                "source": "",
            }
        return cached_weather_snapshot(self.runtime_config)

    def _cached_user_weather_snapshot_for_geo(self, user_geo: dict[str, Any]) -> dict[str, Any]:
        city = str(user_geo.get("city") or "").strip()
        latitude = str(user_geo.get("latitude") or user_geo.get("lat") or "").strip()
        longitude = str(user_geo.get("longitude") or user_geo.get("lon") or user_geo.get("lng") or "").strip()
        if not (city or (latitude and longitude)):
            return {}
        snapshot = cached_user_weather_snapshot(
            self.runtime_config,
            city=city,
            latitude=latitude,
            longitude=longitude,
        )
        return snapshot if snapshot.get("status") == "fresh" else {}

def _is_voice_gateway_turn(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    source = str(metadata.get("source") or "").strip()
    if source == "aura-lily-gateway":
        return True
    if metadata.get("asr_configured") is not None:
        return True
    if metadata.get("audio_bytes"):
        return True
    return False


def _voice_stream_max_tokens(runtime_config: AuraRuntimeConfig, *, enumeration: bool = False, detail: bool = False) -> int:
    try:
        configured = int(runtime_config.aura_model_max_tokens or 96)
    except (TypeError, ValueError):
        configured = 96
    model = str(getattr(runtime_config, "aura_model_model", "") or "").strip().lower()
    if model in STEPFUN_REASONING_MODELS:
        # These models emit reasoning_content before visible text. The normal
        # 128-token voice cap can therefore finish with no audible response.
        return max(configured, STEPFUN_REASONING_STREAM_MIN_TOKENS)
    if detail:
        # 内容型回合（详细讲解/原因/步骤）要装下多句完整展开，
        # 默认 128 token 会把答案砍在半截。
        return max(configured, VOICE_STREAM_DETAIL_MAX_TOKENS)
    if enumeration:
        # 列举回合要装下 3~4 个完整选项，96 token（约 90 字）根本不够，
        # 会在第 2 个选项中间被 provider 截断。
        return max(configured, 192)
    return max(16, min(configured, VOICE_STREAM_MAX_TOKENS))


def _is_speculative_turn(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("speculative")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _speculative_local_voice_result(
    *,
    request_id: str,
    started: float,
    response: str,
    voice_turn: VoiceTurnResult,
    context: PersonaContext,
    setup_debug: dict[str, Any],
) -> PersonaTurnResult:
    debug = {
        "request_id": request_id,
        "context": context.debug,
        "voice_turn": voice_turn.to_dict(),
        "aura_runtime": {"model_route": "local_voice_reply", "streamed": True, "speculative": True},
        "setup": setup_debug,
        "hermes": {"ok": True, "status": "skipped", "latency_ms": 0, "reason": "speculative_local_voice_reply"},
    }
    return PersonaTurnResult(
        ok=True,
        status="completed",
        response=response,
        request_id=request_id,
        latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        voice_turn=voice_turn.to_dict(),
        debug=debug,
        evidence={
            "stop_reason": "local_voice_reply",
            "model_skipped": True,
            "streamed": True,
            "local_preface": False,
            "speculative": True,
        },
    )


def _voice_turn_with_llm_background_task(
    voice_turn: VoiceTurnResult,
    *,
    task_text: str,
    source_text: str,
    ack_text: str,
) -> VoiceTurnResult:
    """快模型输出 [后台] 标记后，把本轮改写成 ACK_AND_ENQUEUE 结果。

    保留原 voice_turn 的 debug 便于追查（decision_path 记为 llm_agent_marker，
    原路径挪到 regex_decision_path）。
    """
    task = background_task_from_llm_marker(task_text, source_text=source_text)
    debug = dict(voice_turn.debug or {})
    regex_path = str(debug.get("decision_path") or "")
    debug["decision_path"] = "llm_agent_marker"
    if regex_path:
        debug["regex_decision_path"] = regex_path
    debug["llm_task_text"] = task.source_text
    return VoiceTurnResult(
        verdict=VoiceTurnVerdict.ACK_AND_ENQUEUE,
        speak_text=str(ack_text or "").strip() or "好，我去查，弄完马上告诉你。",
        emotion="focused",
        background_task=task,
        continue_listening=voice_turn.continue_listening,
        debug=debug,
    )


def _speculative_skipped_result(
    *,
    request_id: str,
    started: float,
    status: str,
    response: str,
    voice_turn: VoiceTurnResult,
    context: PersonaContext,
    setup_debug: dict[str, Any],
    stop_reason: str,
) -> PersonaTurnResult:
    debug = {
        "request_id": request_id,
        "context": context.debug,
        "voice_turn": voice_turn.to_dict(),
        "aura_runtime": {"model_route": "skipped", "streamed": True, "speculative": True},
        "setup": setup_debug,
        "hermes": {"ok": True, "status": "skipped", "latency_ms": 0, "reason": stop_reason},
    }
    return PersonaTurnResult(
        ok=True,
        status=status,
        response=response,
        request_id=request_id,
        latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        voice_turn=voice_turn.to_dict(),
        debug=debug,
        evidence={"stop_reason": stop_reason, "streamed": True, "speculative": True},
    )


def _query_needs_fresh_world_answer(query_context: dict[str, Any]) -> bool:
    intent = str((query_context or {}).get("intent") or "").strip()
    subject = str((query_context or {}).get("subject_entity") or "").strip()
    return subject == "aura" and intent in {"activity_or_location", "day_plan"}


def _looks_like_outing_weather_context(text: str, user_geo: dict[str, Any]) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if not isinstance(user_geo, dict) or not (
        user_geo.get("city")
        or user_geo.get("latitude")
        or user_geo.get("lat")
        or user_geo.get("longitude")
        or user_geo.get("lon")
        or user_geo.get("lng")
    ):
        return False
    clean = "".join(ch for ch in value.lower() if not ch.isspace())
    has_outing = any(token in clean for token in ("出门", "出去", "外出", "去外面"))
    has_timing = any(token in clean for token in ("今天", "下午", "一会", "等会", "现在", "待会", "打算", "准备"))
    return has_outing and has_timing


def _with_grounded_current_voice_reply(
    voice_turn: VoiceTurnResult,
    *,
    user_text: str,
    query_context: dict[str, Any],
    world_snapshot: dict[str, Any],
    voice_low_latency: bool,
) -> VoiceTurnResult:
    if not voice_low_latency:
        return voice_turn
    debug = dict(voice_turn.debug or {})
    if debug.get("decision_path") == "voice_turn_disabled":
        return voice_turn
    if voice_turn.background_task or voice_turn.speak_text.strip():
        return voice_turn
    if voice_turn.verdict != VoiceTurnVerdict.SPEAK_NOW:
        return voice_turn
    if str(query_context.get("intent") or "") != "activity_or_location":
        return voice_turn
    if str(query_context.get("subject_entity") or "") != "aura":
        return voice_turn
    if not _is_simple_current_state_query(user_text):
        return voice_turn

    reply, path, reply_debug = _grounded_current_reply(user_text, world_snapshot)
    if not reply:
        return voice_turn
    return VoiceTurnResult(
        verdict=VoiceTurnVerdict.SPEAK_NOW,
        speak_text=reply,
        emotion=voice_turn.emotion or "calm",
        continue_listening=voice_turn.continue_listening,
        debug={
            **debug,
            "decision_path": path,
            "grounded_current": reply_debug,
        },
    )


def _grounded_current_reply(user_text: str, world_snapshot: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    current = world_snapshot.get("current") if isinstance(world_snapshot.get("current"), dict) else {}
    policy = world_snapshot.get("mention_policy") if isinstance(world_snapshot.get("mention_policy"), dict) else {}
    snapshot_debug = world_snapshot.get("debug") if isinstance(world_snapshot.get("debug"), dict) else {}
    source = str(current.get("source") or snapshot_debug.get("current_source") or "").strip()
    activity = _clean_reply_piece(current.get("activity_label") or "")
    location = _clean_reply_piece(current.get("location_label") or "")
    intent = classify_grounded_current_intent(user_text)
    asks_location = intent == "location"
    allow_activity = bool(policy.get("allow_activity"))
    allow_location = bool(policy.get("allow_location"))
    is_manual_current = source == "manual_state"
    can_speak_activity = allow_activity and activity and (
        source == "manual_state"
        or (source == "active_plan" and _safe_active_plan_activity(activity))
    )
    vague_reply, vague_category = _vague_current_activity_reply(current, source=source)
    debug = {
        "source": source,
        "allow_activity": allow_activity,
        "allow_location": allow_location,
        "asks_location": asks_location,
        "intent": intent or "",
        "used_activity": False,
        "used_location": False,
        "used_vague_activity": False,
        "activity_category": vague_category,
        "activity_source_allowed": bool(can_speak_activity),
    }

    if asks_location:
        if is_manual_current and allow_location and location:
            debug["used_location"] = True
            return f"我在{location}。", "grounded_current_location", debug
        if is_manual_current and allow_activity and activity:
            debug["used_activity"] = True
            return f"具体位置先不说，我{_activity_phrase(activity)}。", "grounded_current_location", debug
        if vague_reply:
            debug["used_vague_activity"] = True
            return f"具体位置先不说，{vague_reply}", "grounded_current_location", debug
        return "具体位置先不说，这会儿说话方便。", "grounded_current_location", debug

    if can_speak_activity:
        debug["used_activity"] = True
        return f"我{_activity_phrase(activity)}。", "grounded_current_activity", debug
    if vague_reply:
        debug["used_vague_activity"] = True
        return vague_reply, "grounded_current_activity", debug
    return "这会儿没什么大事，正好陪你说话。", "grounded_current_activity", debug


def _is_simple_current_state_query(text: str) -> bool:
    return classify_grounded_current_intent(text) is not None


def _activity_phrase(activity: str) -> str:
    value = _clean_reply_piece(activity)
    if not value:
        return "在这边听你说话"
    if value.startswith(("在", "正", "正在")):
        return value
    return f"在{value}"


def _safe_active_plan_activity(activity: str) -> bool:
    value = _clean_reply_piece(activity)
    if not value:
        return False
    unsafe_tokens = ("吃", "饭", "早餐", "午餐", "晚餐", "睡", "起床", "买", "购物", "店", "商场", "咖啡")
    if any(token in value for token in unsafe_tokens):
        return False
    return value in {"散步", "整理东西", "看点内容", "安静待着", "安静停留", "随便逛逛", "休息"}


def _vague_current_activity_reply(current: dict[str, Any], *, source: str) -> tuple[str, str]:
    if source == "manual_state":
        return "", ""
    category = _vague_activity_category(current, source=source)
    if category == "outing":
        return "刚活动了一下，现在正好陪你说话。", category
    if category == "errand":
        return "刚处理点小事，现在正好陪你说话。", category
    if category == "quiet":
        return "刚在捣鼓自己的小事，现在正好陪你说话。", category
    if category == "rest":
        return "刚缓了一会儿，现在正好陪你说话。", category
    return "这会儿没什么大事，正好陪你说话。", category


def _vague_activity_category(current: dict[str, Any], *, source: str) -> str:
    if source in {"fallback_home", "compact_voice_fallback", ""}:
        return "idle"
    value = " ".join(
        _clean_reply_piece(part)
        for part in (
            current.get("activity_key"),
            current.get("activity_label"),
            current.get("title"),
            current.get("slot_key"),
            current.get("location_key"),
            current.get("location_label"),
        )
        if part
    ).lower()
    if not value:
        return "idle"
    if any(token in value for token in ("walk", "browse", "outing", "散步", "出门", "外面", "逛", "走一")):
        return "outing"
    if any(token in value for token in ("errand", "shop", "买", "购物", "补点", "日用品", "便利")):
        return "errand"
    if any(token in value for token in ("quiet", "focus", "desk", "整理", "看点", "安静", "处理", "书桌")):
        return "quiet"
    if any(token in value for token in ("rest", "home", "wake", "settle", "睡", "起床", "休息", "放松", "缓")):
        return "rest"
    if any(token in value for token in ("meal", "breakfast", "lunch", "dinner", "吃", "饭", "餐")):
        return "rest"
    return "idle"


def _guard_model_spoken_reply(
    text: str,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None = None,
) -> tuple[str, dict[str, Any]]:
    response = str(text or "").strip()
    if not response:
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {"reason": "empty_after_contract", "fallback_used": True}
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if _looks_like_provider_error_reply(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_provider_error_reply",
            "fallback_used": True,
            "decision_path": path,
        }
    if path == "status_review_entry":
        stripped_status_opening = _strip_low_value_status_opening(response)
        if stripped_status_opening:
            stripped_guard = _quality_guard_reason_for_spoken_reply(
                stripped_status_opening,
                context=context,
                voice_turn=voice_turn,
                path=path,
            )
            if stripped_guard:
                return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
                    "reason": stripped_guard,
                    "fallback_used": True,
                    "decision_path": path,
                    "after_low_value_status_opening_removed": True,
                }
            return stripped_status_opening, {
                "reason": "removed_low_value_status_opening",
                "fallback_used": False,
                "decision_path": path,
            }
    if _reply_hallucinates_aura_self_state(response, context=context, voice_turn=voice_turn):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_aura_self_state_hallucination",
            "fallback_used": True,
            "decision_path": path,
        }
    if _reply_makes_unfounded_user_state_claim(response, context=context, voice_turn=voice_turn):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_unfounded_user_state_claim",
            "fallback_used": True,
            "decision_path": path,
        }
    if _streaming_text_ends_with_incomplete_phrase(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_incomplete_streaming_reply",
            "fallback_used": True,
            "decision_path": path,
        }
    if _looks_like_action_or_stage_direction(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_action_or_stage_direction",
            "fallback_used": True,
            "decision_path": path,
        }
    if path == "supportive_chat" and _supportive_continuation_is_unsafe(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_unsafe_supportive_reply",
            "fallback_used": True,
            "decision_path": path,
        }
    if _casual_continuation_is_placeholder(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_placeholder_reply",
            "fallback_used": True,
            "decision_path": path,
        }
    if _reply_is_too_vague_for_user_text(response, context=context, voice_turn=voice_turn):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_vague_reply",
            "fallback_used": True,
            "decision_path": path,
        }
    if path == "casual_chat_preface" and _casual_continuation_is_unfounded_guess(response):
        return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
            "reason": "blocked_unfounded_casual_guess",
            "fallback_used": True,
            "decision_path": path,
        }
    repeated_guard: dict[str, Any] = {}
    deduped_response = _dedupe_repeated_spoken_sentences(response)
    if deduped_response != response:
        response = deduped_response
        if not response:
            return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
                "reason": "empty_after_repeated_sentence_removed",
                "fallback_used": True,
                "decision_path": path,
            }
        repeated_guard = {
            "reason": "removed_repeated_sentence",
            "fallback_used": False,
            "decision_path": path,
        }
    forbidden = _world_forbidden_reply_tokens(context)
    matched = [token for token in forbidden if token and token in response]
    if not matched:
        return response, repeated_guard
    kept = _drop_sentences_with_tokens(response, matched)
    if kept:
        return kept, {
            "reason": "removed_world_background_sentence",
            "matched_tokens": matched[:8],
            "fallback_used": False,
            "decision_path": str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or ""),
        }
    return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn), {
        "reason": "blocked_world_background_leak",
        "matched_tokens": matched[:8],
        "fallback_used": True,
        "decision_path": str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or ""),
    }


def _quality_guard_reason_for_spoken_reply(
    text: str,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None,
    path: str,
) -> str:
    response = str(text or "").strip()
    if not response:
        return "empty_after_contract"
    if _reply_hallucinates_aura_self_state(response, context=context, voice_turn=voice_turn):
        return "blocked_aura_self_state_hallucination"
    if _reply_makes_unfounded_user_state_claim(response, context=context, voice_turn=voice_turn):
        return "blocked_unfounded_user_state_claim"
    if _streaming_text_ends_with_incomplete_phrase(response):
        return "blocked_incomplete_streaming_reply"
    if _looks_like_action_or_stage_direction(response):
        return "blocked_action_or_stage_direction"
    if path == "supportive_chat" and _supportive_continuation_is_unsafe(response):
        return "blocked_unsafe_supportive_reply"
    if _casual_continuation_is_placeholder(response):
        return "blocked_placeholder_reply"
    if _reply_is_too_vague_for_user_text(response, context=context, voice_turn=voice_turn):
        return "blocked_vague_reply"
    if path == "casual_chat_preface" and _casual_continuation_is_unfounded_guess(response):
        return "blocked_unfounded_casual_guess"
    if any(token for token in _world_forbidden_reply_tokens(context) if token and token in response):
        return "blocked_world_background_leak"
    return ""


def _stream_quality_guard_should_wait(guard: dict[str, Any]) -> bool:
    reason = str((guard or {}).get("reason") or "")
    if reason == "empty_after_contract":
        return True
    if reason == "blocked_incomplete_streaming_reply":
        return True
    path = str((guard or {}).get("decision_path") or "")
    if reason == "blocked_vague_reply" and path == "status_review_entry":
        return True
    if path == "casual_chat_preface" and reason in {
        "blocked_placeholder_reply",
        "blocked_vague_reply",
        "blocked_aura_self_state_hallucination",
        "blocked_unfounded_casual_guess",
        "blocked_unfounded_user_state_claim",
    }:
        return True
    return False


def _stream_quality_guard_should_force_fallback(guard: dict[str, Any], *, stop_reason: str = "") -> bool:
    reason = str((guard or {}).get("reason") or "")
    if not reason:
        return False
    if reason == "blocked_incomplete_streaming_reply":
        return True
    if str(stop_reason or "").strip() == "voice_quality_guard_after_partial":
        return reason in {
            "blocked_unfounded_user_state_claim",
            "blocked_unsafe_supportive_reply",
            "blocked_incomplete_streaming_reply",
            "blocked_aura_self_state_hallucination",
            "blocked_unfounded_casual_guess",
            "blocked_placeholder_reply",
            "blocked_vague_reply",
        }
    return bool(guard.get("fallback_used")) and reason in {
        "blocked_unfounded_user_state_claim",
        "blocked_unsafe_supportive_reply",
        "blocked_incomplete_streaming_reply",
        "blocked_aura_self_state_hallucination",
        "blocked_unfounded_casual_guess",
    }


def _stream_quality_guard_allows_waiting_delta(guard: dict[str, Any], text: str) -> bool:
    return False


def _status_review_stream_should_wait_for_completion(
    audible_text: str,
    *,
    raw_text: str,
    voice_turn: VoiceTurnResult | None,
) -> bool:
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if path != "status_review_entry":
        return False
    return not bool(_first_streaming_sentence(audible_text, include_partial=False))


def _fallback_spoken_reply_for_voice_turn(voice_turn: VoiceTurnResult | None) -> str:
    if voice_turn and _voice_turn_has_fallback_only(voice_turn):
        text = str(voice_turn.speak_text or "").strip()
        if text:
            return text
    return FALLBACK_SPOKEN_REPLY


def _quality_fallback_spoken_reply(
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None = None,
) -> str:
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if _user_topic_is_job_change(context):
        return _pick_quality_fallback(
            context,
            [
                "换工作这件事先拆开看：是现在耗着难受，还是新机会更吸引你？",
                "可以聊。先看动因：是不想留了，还是想要更好的机会？",
                "这事值得认真想，先说你最在意的：发展、收入，还是现在待着太消耗？",
            ],
        )
    if _user_topic_needs_specific_anchor(context):
        return _pick_quality_fallback(
            context,
            [
                "从工作节奏说起：是事情太满，还是提不起劲？",
                "先把状态拆小一点：工作量、睡眠，还是提不起劲？",
                "先看最卡的一块：事情太满，还是心劲不够？",
            ],
        )
    if path == "casual_chat_preface":
        text = str(voice_turn.speak_text or "").strip()
        options = [
            text,
            "先说你最想聊的那一件。",
            "先从最挂心的地方说。",
            "先说最近最占心的那一块。",
        ]
        return _pick_quality_fallback(context, options)
    if path == "supportive_chat":
        text = str(voice_turn.speak_text or "").strip()
        options = [
            text,
            "我在，先把话说慢一点。",
            "好，我陪你。先说最难受的那一处。",
            "先缓一口气，我听你说。",
        ]
        return _pick_quality_fallback(context, options)
    if path == "local_social":
        text = str(voice_turn.speak_text or "").strip()
        if text:
            return _pick_quality_fallback(context, [text])
        return FALLBACK_SPOKEN_REPLY
    if path in {"normal_chat", ""}:
        return _fallback_spoken_reply_for_voice_turn(voice_turn)
    fallback = _fallback_spoken_reply_for_voice_turn(voice_turn)
    if fallback and fallback != FALLBACK_SPOKEN_REPLY:
        return _pick_quality_fallback(context, [fallback])
    return _pick_quality_fallback(
        context,
        [
            "我刚才没接稳，你换个说法再问我一次。",
            "这句我没接准，你再说一遍。",
            FALLBACK_SPOKEN_REPLY,
        ],
    )


def _pick_quality_fallback(context: PersonaContext, options: list[str]) -> str:
    cleaned = []
    seen: set[str] = set()
    for option in options:
        text = str(option or "").strip()
        key = _dedupe_key(text)
        if not text or not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    if not cleaned:
        return FALLBACK_SPOKEN_REPLY
    recent_keys = _recent_aura_reply_keys(context)
    for text in cleaned:
        if _dedupe_key(text) not in recent_keys:
            return text
    return cleaned[0]


def _recent_aura_reply_keys(context: PersonaContext) -> set[str]:
    debug = context.debug if isinstance(context.debug, dict) else {}
    recent = debug.get("recent_aura_replies")
    if not isinstance(recent, list):
        return set()
    keys = {_dedupe_key(str(item or "")) for item in recent}
    return {key for key in keys if key}


def _world_forbidden_reply_tokens(context: PersonaContext) -> tuple[str, ...]:
    debug = context.debug if isinstance(context.debug, dict) else {}
    world = debug.get("world_snapshot") if isinstance(debug.get("world_snapshot"), dict) else {}
    policy = world.get("mention_policy") if isinstance(world.get("mention_policy"), dict) else {}
    query = debug.get("query_context") if isinstance(debug.get("query_context"), dict) else {}
    current = world.get("current") if isinstance(world.get("current"), dict) else {}
    plan = world.get("today_plan") if isinstance(world.get("today_plan"), list) else []
    allow_location = bool(policy.get("allow_location"))
    allow_activity = bool(policy.get("allow_activity"))
    allow_plan = bool(policy.get("allow_plan"))
    intent = str(query.get("intent") or policy.get("intent") or "").strip()
    subject = str(query.get("subject_entity") or policy.get("subject_entity") or "").strip()
    tokens: set[str] = set()

    if not allow_location:
        _add_forbidden_token(tokens, current.get("location_label"))
        _add_forbidden_token(tokens, current.get("location_key"))
        if intent not in {"weather", "weather_advice", "time"}:
            tokens.update({"大悦城", "商场", "店铺", "咖啡店", "便利店", "小店"})
    if not allow_activity:
        _add_forbidden_token(tokens, current.get("activity_label"))
        _add_forbidden_token(tokens, current.get("title"))
        if intent not in {"weather", "weather_advice", "time"}:
            tokens.update({"吃早饭", "吃午饭", "吃晚饭", "早饭", "午饭", "晚饭", "逛街", "逛商场"})
    if not allow_plan:
        for item in plan:
            if not isinstance(item, dict):
                continue
            _add_forbidden_token(tokens, item.get("location"))
            _add_forbidden_token(tokens, item.get("location_label"))
            _add_forbidden_token(tokens, item.get("title"))
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            _add_forbidden_token(tokens, payload.get("location_label"))
            _add_forbidden_token(tokens, payload.get("activity_label"))
    if subject == "aura" and intent in {"activity_or_location", "day_plan"}:
        if allow_location or allow_activity or allow_plan:
            return tuple(sorted(tokens, key=len, reverse=True))
    return tuple(sorted(tokens, key=len, reverse=True))


def _add_forbidden_token(tokens: set[str], value: Any) -> None:
    token = _clean_reply_piece(value)
    if len(token) < 2:
        return
    if token in {"home", "desk", "park", "mall", "manual", "idle"}:
        return
    tokens.add(token)


def _drop_sentences_with_tokens(text: str, tokens: list[str]) -> str:
    parts = [part.strip() for part in re.split(r"([^。！？!?]+[。！？!?]?)", str(text or "")) if part.strip()]
    if not parts:
        return ""
    kept = [part for part in parts if not any(token in part for token in tokens)]
    value = "".join(kept).strip()
    if value == str(text or "").strip():
        return ""
    return value


def _dedupe_repeated_spoken_sentences(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    parts = [part.strip() for part in re.split(r"([^。！？!?]+[。！？!?]?)", value) if part.strip()]
    if len(parts) <= 1:
        return value
    kept: list[str] = []
    keys: list[str] = []
    changed = False
    for part in parts:
        key = _dedupe_key(part)
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
    if not changed:
        return value
    return "".join(kept).strip()


def _clean_reply_piece(value: Any) -> str:
    return str(value or "").strip().strip("。！？!?，,；;：:")


def _context_with_local_preface(
    context: PersonaContext,
    local_preface: str,
    *,
    voice_turn: VoiceTurnResult | None = None,
) -> PersonaContext:
    preface = str(local_preface or "").strip()
    if not preface:
        return context
    decision_path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    focus = _local_preface_continuation_focus(decision_path)
    prompt = (
        context.prompt
        + "\n\n## 已经说出口的本地前缀\n"
        + f"你已经先对用户说了：{preface}\n"
        + "接下来只自然补充一句很短的增量信息，最多16个汉字；不要重复这句话，不要否定它，也不要改写成另一种状态。"
        + "不要用“嗯”“嗯咯”“好哒”“好的”“我想一下”“稍等”这类前导占位开头。"
        + focus
        + "如果没有确实可补充的新信息，就输出空字符串。"
    )
    return PersonaContext(
        prompt=prompt,
        state_summary=context.state_summary,
        debug={
            **context.debug,
            "local_preface": {
                "enabled": True,
                "chars": len(preface),
                "decision_path": decision_path,
            },
            "prompt_chars": len(prompt),
        },
    )


def _local_preface_continuation_focus(decision_path: str) -> str:
    path = str(decision_path or "").strip()
    if path == "cached_weather_advice":
        return "补充方向：只给一个实用行动建议或解释，不要再次播报温度、湿度、城市和“依据是”。"
    if path == "outing_weather_advice":
        return "补充方向：只给一句贴近日常的出门提醒，不要再次播报天气数字。"
    if path == "state_mood":
        return "补充方向：只接一句和用户关系有关的自然情绪，不要把 mood/energy/stress 数值说出来。"
    if path == "local_social":
        return "补充方向：只接一句自然寒暄或回应用户当下语境。"
    if path == "supportive_chat":
        return "补充方向：只接一句具体陪伴或轻安慰，不要讲大道理，不要转成任务建议，不要说“不说话/不聊天/不聊了”。"
    if path == "casual_chat_preface":
        return "补充方向：只补一句有内容的轻切入；可以问一个具体但不冒犯的问题。不要替用户猜熬夜、赶项目、疲惫、压力或私人处境，也不要只说“你说/我听着/从哪儿开始”。"
    if path == "status_review_entry":
        return "补充方向：只给一个开放式切入问题或一个非常短的复盘维度；不要编用户最近熬夜、赶项目、紧绷、焦虑、疲惫、效率变化或对话频率。"
    return "补充方向：只补用户会觉得有用的一小句，不要展开背景。"


def _local_preface_text_for_stream(voice_turn: VoiceTurnResult) -> str:
    text = str(voice_turn.speak_text or "").strip()
    path = str((voice_turn.debug or {}).get("decision_path") or "")
    if path == "cached_weather_advice":
        return _strip_weather_evidence_for_preface(text)
    if path == "outing_weather_advice":
        return _strip_weather_tail_for_preface(text)
    return text


def _strip_weather_evidence_for_preface(text: str) -> str:
    value = str(text or "").strip()
    if "；依据是" in value:
        value = value.split("；依据是", 1)[0].strip()
    value = value.rstrip("。")
    return value + "。" if value else ""


def _strip_weather_tail_for_preface(text: str) -> str:
    value = str(text or "").strip()
    if "，湿度" in value:
        head, _sep, tail = value.partition("，湿度")
        if "，出门记得" in tail:
            hint = "出门记得" + tail.split("，出门记得", 1)[1]
            return f"{head}，{hint}".strip()
    return value


def _model_result_with_local_preface(
    model_result: HermesLilyResult,
    local_preface: str,
    *,
    decision_path: str = "",
) -> HermesLilyResult:
    preface = str(local_preface or "").strip()
    if not preface:
        return model_result
    model_text = str(model_result.response or "").strip()
    response = (
        _join_local_preface_and_model(preface, model_text, decision_path=decision_path)
        if model_result.ok
        else preface
    )
    evidence = {
        **dict(model_result.evidence or {}),
        "local_preface": True,
        "local_preface_chars": len(preface),
    }
    if not model_result.ok:
        evidence["model_failed_after_preface"] = True
    return HermesLilyResult(
        ok=True if response else model_result.ok,
        status=model_result.status if model_result.ok else "completed_with_local_preface",
        response=response or preface,
        request_id=model_result.request_id,
        latency_ms=model_result.latency_ms,
        evidence=evidence,
    )


def _join_local_preface_and_model(local_preface: str, model_text: str, *, decision_path: str = "") -> str:
    preface = str(local_preface or "").strip()
    continuation = _model_text_after_preface(preface, model_text, decision_path=decision_path)
    if not preface:
        return continuation.strip()
    if not continuation:
        return preface
    if _needs_separator(preface, continuation):
        return f"{preface}，{continuation}"
    return f"{preface}{continuation}"


def _model_delta_after_preface(local_preface: str, model_text: str, *, decision_path: str = "") -> str:
    return _model_text_after_preface(local_preface, model_text, decision_path=decision_path).strip()


def _model_text_after_preface(local_preface: str, model_text: str, *, decision_path: str = "") -> str:
    preface = str(local_preface or "").strip()
    text = str(model_text or "").strip()
    if not preface or not text:
        return text
    path = str(decision_path or "").strip()
    normalized_preface = _dedupe_key(preface)
    normalized_text = _dedupe_key(text)
    if normalized_text == normalized_preface:
        return ""
    if normalized_preface.startswith(normalized_text):
        return ""
    if normalized_text.startswith(normalized_preface):
        continuation = _strip_model_continuation_filler(_trim_preface_by_chars(preface, text), allow_empty=True)
    else:
        continuation = _strip_model_continuation_filler(_trim_preface_overlap(preface, text), allow_empty=True)
    if path in {"status_review_entry", "supportive_chat"} and _continuation_guesses_user_state(continuation):
        return ""
    return _sanitize_model_continuation(continuation, decision_path=decision_path)


def _local_preface_stream_should_stop(local_preface: str, model_text: str, *, decision_path: str = "") -> bool:
    raw = str(model_text or "").strip()
    if not raw:
        return False
    path = str(decision_path or "").strip()
    if _looks_like_action_or_stage_direction(raw):
        return True
    if path == "supportive_chat" and _supportive_continuation_is_unsafe(raw):
        return True
    if not _strip_model_continuation_filler(raw, allow_empty=True).strip():
        return False
    if _model_text_after_preface(local_preface, raw, decision_path=decision_path).strip():
        return False
    return bool(re.search(r"[。！？!?]$", raw)) or len(raw) >= 18


def _local_preface_continuation_ready(audible_text: str, *, raw_text: str, limit: int = 18) -> bool:
    audible = str(audible_text or "").strip()
    if not audible:
        return False
    if re.search(r"[。！？!?]$", audible):
        return True
    raw = str(raw_text or "").strip()
    return len(audible) >= limit or bool(raw and re.search(r"[。！？!?]$", raw))


def _next_stable_stream_delta(audible_text: str, emitted_chars: int) -> tuple[str, int]:
    audible = str(audible_text or "")
    emitted = max(0, int(emitted_chars or 0))
    if emitted <= 0 and not _stream_audible_text_can_start(audible):
        return "", 0
    if emitted <= 0:
        return audible, len(audible)
    if len(audible) < emitted:
        return "", emitted
    return audible[emitted:], len(audible)


def _next_safe_voice_stream_delta(
    audible_text: str,
    emitted_chars: int,
    *,
    voice_turn: VoiceTurnResult | None,
) -> tuple[str, int]:
    if not _voice_stream_requires_complete_sentence(voice_turn):
        return _next_stable_stream_delta(audible_text, emitted_chars)
    audible = str(audible_text or "")
    emitted = max(0, int(emitted_chars or 0))
    if emitted > len(audible):
        return "", emitted
    safe_end = _last_complete_streaming_sentence_end(audible)
    if safe_end <= emitted:
        return "", emitted
    start = emitted
    if start <= 0:
        start = _skip_low_value_stream_openings(audible, safe_end=safe_end, voice_turn=voice_turn)
    if safe_end <= start:
        return "", emitted
    delta = audible[start:safe_end]
    if _streaming_text_ends_with_incomplete_phrase(delta):
        return "", emitted
    return delta, safe_end


def _voice_stream_requires_complete_sentence(voice_turn: VoiceTurnResult | None) -> bool:
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    return path in {"supportive_chat", "status_review_entry"}


def _last_complete_streaming_sentence_end(text: str) -> int:
    value = str(text or "")
    end = 0
    for index, ch in enumerate(value):
        if ch in "。！？!?":
            end = index + 1
    return end


def _skip_low_value_stream_openings(
    text: str,
    *,
    safe_end: int,
    voice_turn: VoiceTurnResult | None,
) -> int:
    if not _voice_stream_requires_complete_sentence(voice_turn):
        return 0
    value = str(text or "")
    cursor = 0
    while cursor < safe_end:
        sentence = _first_streaming_sentence(value[cursor:safe_end], include_partial=False)
        if not sentence:
            break
        if not _streaming_sentence_is_low_value_opening(sentence):
            break
        cursor += len(sentence)
        while cursor < len(value) and value[cursor] in " \t\r\n，,、；;：:":
            cursor += 1
    return cursor


def _streaming_sentence_is_low_value_opening(sentence: str) -> bool:
    key = _dedupe_key(sentence)
    if not key:
        return True
    if _status_reply_is_topic_echo(sentence) or _status_reply_is_weak_ack(sentence):
        return True
    low_value = {
        "嗯",
        "嗯嗯",
        "好",
        "好的",
        "好呀",
        "好哒",
        "好啦",
        "晓得",
        "知道",
        "收到了",
        "收到",
        "我在",
        "我在呢",
        "听到了",
        "听见了",
    }
    if key in low_value:
        return True
    return len(key) <= 2 and not _reply_has_specific_anchor(sentence)


def _safe_streamed_prefix_or_fallback(
    audible_text: str,
    emitted_chars: int,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None,
) -> str:
    prefix = str(audible_text or "")[: max(0, int(emitted_chars or 0))].strip()
    if prefix:
        safe_end = _last_complete_streaming_sentence_end(prefix)
        start = _skip_low_value_stream_openings(prefix, safe_end=safe_end, voice_turn=voice_turn)
        prefix = prefix[start:safe_end].strip() if safe_end > start else ""
    if prefix and not _streaming_text_ends_with_incomplete_phrase(prefix):
        return prefix
    return _quality_fallback_spoken_reply(context=context, voice_turn=voice_turn)


def _stream_audible_text_can_start(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.fullmatch(r"(?:嗯+咯?|嗯嗯|好哒|好呀|好啦|好咯|好嘛|是滴|晓得啦|嘛|呢|呀|啊|唔|呃|额)[\s,，。.!！?？、;；:：]*", value):
        return False
    key = _dedupe_key(value)
    if key.startswith(("那我就", "那我在", "我就在")) and not re.search(r"[，,。！？!?；;、]", value):
        return False
    if re.search(r"[，,。！？!?；;、]", value):
        return True
    return len(value) >= 4


def _strip_model_continuation_filler(text: str, *, allow_empty: bool = False) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    hard_filler_re = re.compile(
        r"^(?:嗯+咯?|嗯嗯|好哒|好呀|好啦|好咯|好嘛|是滴|晓得啦)"
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
            return "" if allow_empty else value
        value = stripped
    return value


def _has_substantive_spoken_text(text: str) -> bool:
    return bool(re.search(r"[\w\u3400-\u9fff]", str(text or "")))


def _sanitize_model_continuation(text: str, *, decision_path: str = "") -> str:
    raw = str(text or "").strip()
    path = str(decision_path or "").strip()
    if path in {"supportive_chat", "status_review_entry"} and _streaming_text_ends_with_incomplete_phrase(raw):
        return ""
    if path == "supportive_chat" and _supportive_continuation_is_unsafe(raw):
        return ""
    if path == "casual_chat_preface" and _casual_continuation_is_placeholder(raw):
        return ""
    if path == "casual_chat_preface" and _casual_continuation_is_unfounded_guess(raw):
        return ""
    if _looks_like_action_or_stage_direction(raw):
        return ""
    value = _compact_model_continuation(text)
    if not value:
        return ""
    if path == "supportive_chat" and _supportive_continuation_is_unsafe(value):
        return ""
    if path == "casual_chat_preface" and _casual_continuation_is_placeholder(value):
        return ""
    if path == "casual_chat_preface" and _casual_continuation_is_unfounded_guess(value):
        return ""
    if _looks_like_action_or_stage_direction(value):
        return ""
    return value


def _is_enumeration_request(user_text: str) -> bool:
    """用户点名要“推荐三个/有哪些”这类列举答案时返回 True。

    这类回合如果还按两句/80字压缩，模型先铺垫一句，真正的清单就整句被丢掉，
    用户听到的只有“那就不废话了。”——这正是“答非所问”的根因。
    """
    value = str(user_text or "")
    if not value:
        return False
    if re.search(r"[一两二三四五12345几多]\s*[个家处条种样款首部道]", value) and re.search(
        r"推荐|介绍|列|说|讲|给我|来[点几]|盘点", value
    ):
        return True
    return bool(re.search(r"有(?:哪些|什么).{0,10}(?:推荐|地方|去处|选择|好玩|好吃|好去处)", value))


def _is_detail_request(user_text: str) -> bool:
    """用户点名要“详细讲/展开/为什么/怎么做”这类内容型回答时返回 True。"""
    value = str(user_text or "")
    if not value:
        return False
    if re.search(r"详细|展开(?:讲|说|聊)?|细说|具体(?:讲|说|聊|介绍|解释|说明)|仔细(?:讲|说)|讲(?:清楚|全|透)|说清楚|完整(?:讲|说|介绍)", value):
        return True
    return bool(re.search(r"为什么|什么原理|什么区别|区别在哪|怎么(?:做|实现|操作|解决|回事)|如何(?:做|实现|操作|解决)", value))


def _voice_reply_budget(user_text: str) -> tuple[int, int]:
    """返回本轮语音压缩预算 (max_sentences, limit)。"""
    if _is_detail_request(user_text):
        return 6, 320
    if _is_enumeration_request(user_text):
        return 4, 160
    return 2, 80


def _compact_streaming_voice_model_text(text: str, *, max_sentences: int = 2, limit: int = 80) -> str:
    value = _prepare_streaming_voice_model_text(text)
    if not value:
        return ""
    value = _drop_leading_unspeakable_streaming_text(value)
    if not value:
        return ""
    if _streaming_voice_sentence_is_unsafe(_first_streaming_sentence(value, include_partial=True)):
        return ""

    cut = _streaming_voice_cut_index(value, max_sentences=max_sentences, limit=limit)
    was_truncated = cut < len(value)
    audible = value[:cut].strip()
    while audible and _streaming_voice_sentence_is_unsafe(_last_streaming_sentence(audible)):
        previous = audible[: -len(_last_streaming_sentence(audible))].strip()
        if previous == audible:
            break
        audible = previous
    if was_truncated and audible and not re.search(r"[。！？!?]$", audible):
        audible = _close_truncated_streaming_sentence(audible, limit=limit)
    return audible


def _streaming_voice_model_text_is_complete(
    audible_text: str,
    *,
    raw_text: str,
    max_sentences: int = 2,
    limit: int = 80,
) -> bool:
    audible = str(audible_text or "").strip()
    if not audible:
        return False
    if _streaming_text_ends_with_incomplete_phrase(audible):
        return False
    if len(audible) >= limit:
        return True
    if _streaming_sentence_end_count(audible) >= max_sentences:
        return True
    raw = _prepare_streaming_voice_model_text(raw_text)
    if not raw:
        return False
    compacted = _compact_streaming_voice_model_text(raw, max_sentences=max_sentences, limit=limit)
    if compacted != audible:
        return False
    hidden_tail = raw[len(raw) - max(0, len(raw) - len(audible)):].strip() if raw.startswith(audible) else ""
    if hidden_tail and _streaming_sentence_end_count(hidden_tail) > 0:
        return True
    return len(raw) >= limit + 8


def _prepare_streaming_voice_model_text(text: str, *, allow_empty_filler: bool = False) -> str:
    value = re.sub(r"[ \t\r\n]+", " ", str(text or "")).strip()
    if not value:
        return ""
    if re.fullmatch(r"(?:嗯+咯?|嗯嗯|唔|呃|啊|额|我想一下|稍等|等一下)[\s,，。.!！?？、;；:：]*", value):
        return ""
    value = _strip_model_continuation_filler(value, allow_empty=allow_empty_filler)
    value = re.sub(r"^\s*(?:Aura|Lily|Doudou|豆豆|AI|助手)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    return value.strip()


def _drop_leading_unspeakable_streaming_text(text: str) -> str:
    value = str(text or "").strip()
    for _ in range(3):
        next_value = value
        bracket = re.match(r"^[（(【\[]([^）)】\]]{1,80})[）)】\]]\s*", next_value)
        if bracket and _looks_like_action_or_stage_direction(bracket.group(0)):
            next_value = next_value[bracket.end():].lstrip()
        starred = re.match(r"^[\*＊~～]([^*＊~～]{1,80})[\*＊~～]\s*", next_value)
        if starred and _looks_like_action_or_stage_direction(starred.group(0)):
            next_value = next_value[starred.end():].lstrip()
        sentence = _first_streaming_sentence(next_value, include_partial=False)
        if sentence and _streaming_voice_sentence_is_unsafe(sentence):
            next_value = next_value[len(sentence):].lstrip()
        if next_value == value:
            break
        value = next_value
    if value and re.match(r"^[（(【\[][^）)】\]]{0,80}$", value):
        return ""
    if value and re.match(r"^[\*＊~～][^*＊~～]{0,80}$", value):
        return ""
    return value


def _streaming_voice_cut_index(text: str, *, max_sentences: int, limit: int) -> int:
    value = str(text or "")
    if not value:
        return 0
    sentence_ends = [index + 1 for index, ch in enumerate(value) if ch in "。！？!?"]
    if sentence_ends:
        selected = 0
        for end in sentence_ends[: max(1, max_sentences)]:
            if end <= limit:
                selected = end
                continue
            break
        if selected:
            # 宁可只留完整句，也不要逗号腰斩再补假句号：那就是“说一半就没了”的根因。
            return selected
    if len(value) <= limit:
        return len(value)
    cut_at = 0
    for index, ch in enumerate(value[: max(1, limit)]):
        if ch in "。！？!?，,；;、":
            cut_at = index + 1
    return cut_at or max(1, limit)


def _close_truncated_streaming_sentence(text: str, *, limit: int) -> str:
    value = str(text or "").strip().rstrip("，,；;、：: ")
    if not value:
        return ""
    if len(value) >= limit:
        value = value[: max(1, limit - 1)].rstrip("，,；;、：: ")
    return value + "。"


def _streaming_text_ends_with_incomplete_phrase(text: str) -> bool:
    value = str(text or "").strip().rstrip("，,；;、：: ")
    if not value:
        return False
    if _streaming_text_has_malformed_punctuation(value):
        return True
    if _streaming_text_has_unfinished_tail(value):
        return True
    if re.search(r"[？?]\s*是$", value):
        return True
    incomplete_suffixes = (
        "其实我",
        "其实",
        "其实我也",
        "其实你",
        "其实看你",
        "感觉你",
        "感觉",
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
        "是那种活儿干",
        "活儿干",
        "活儿",
        "那种",
        "比如",
        "像是",
        "因为",
        "所以",
        "但是",
        "不过",
        "是觉得",
        "是觉得事情",
        "是觉得工作",
        "是感觉",
        "是感觉事情",
        "是感觉工作",
        "是觉得生活节奏",
    )
    if any(value.endswith(suffix) for suffix in incomplete_suffixes):
        return True
    return False


def _streaming_text_has_malformed_punctuation(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"[，,、；;：:]\s*[。！？!?]", value):
        return True
    if re.search(r"[。！？!?]\s*[，,、；;：:]", value):
        return True
    return False


def _streaming_text_has_unfinished_tail(text: str, *, min_chars: int = 8) -> bool:
    value = str(text or "").strip().rstrip("，,；;、：: ")
    if not value or re.search(r"[。！？!?]$", value):
        return False
    if not re.search(r"[\u3400-\u9fff]", value):
        return False
    last_end = -1
    for ch in "。！？!?":
        last_end = max(last_end, value.rfind(ch))
    tail = value[last_end + 1:].strip() if last_end >= 0 else value
    return len(_dedupe_key(tail)) >= max(1, min_chars)


def _first_streaming_sentence(text: str, *, include_partial: bool) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    for index, ch in enumerate(value):
        if ch in "。！？!?":
            return value[: index + 1].strip()
    return value if include_partial else ""


def _last_streaming_sentence(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    end = len(value)
    for index in range(len(value) - 2, -1, -1):
        if value[index] in "。！？!?":
            return value[index + 1:end].strip()
    return value


def _streaming_sentence_end_count(text: str) -> int:
    return sum(1 for ch in str(text or "") if ch in "。！？!?")


def _streaming_voice_sentence_is_unsafe(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if _looks_like_action_or_stage_direction(value):
        return True
    blocked = (
        "睡吧",
        "睡觉",
        "闭上眼",
        "闭眼",
        "眯一会",
        "眯会",
        "放段",
        "放首",
        "放歌",
        "老歌",
        "音乐",
    )
    return any(token in value for token in blocked)


def _looks_like_provider_error_reply(text: str) -> bool:
    value = str(text or "").strip().lower()
    if not value:
        return False
    markers = (
        "aura direct llm http",
        "aura direct llm failed",
        "direct llm http",
        "http 400",
        "http 401",
        "http 404",
        "http 429",
        "model_invalid",
        "rate_limited",
    )
    return any(marker in value for marker in markers)


def _reply_is_too_vague_for_user_text(
    reply: str,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None = None,
) -> bool:
    response = str(reply or "").strip()
    if not response:
        return False
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if path not in {"normal_chat", "casual_chat_preface", "supportive_chat", "status_review_entry"}:
        return False
    key = _dedupe_key(response)
    if not key:
        return False
    vague_patterns = (
        "那我就听着",
        "那我就在这儿听着",
        "那我就在这听着",
        "我就听着",
        "我就在这儿听着",
        "我就在这听着",
        "就在这儿听着",
        "就在这听着",
        "你想从哪儿开始讲",
        "你想从哪开始讲",
        "你想从哪儿开始聊",
        "你想从哪开始聊",
        "你想从哪儿开始说",
        "你想从哪开始说",
        "想到哪儿说哪儿",
        "想到哪说哪",
    )
    if any(_dedupe_key(pattern) in key for pattern in vague_patterns):
        return True
    if not _user_topic_needs_specific_anchor(context):
        return False
    first_sentence = _first_streaming_sentence(response, include_partial=True)
    if _status_reply_is_topic_echo(first_sentence):
        tail = response[len(first_sentence):].strip()
        return not _reply_has_specific_anchor(tail)
    if _status_reply_is_weak_ack(first_sentence):
        tail = response[len(first_sentence):].strip()
        return not _reply_has_specific_anchor(tail)
    first_key = _dedupe_key(first_sentence)
    if not first_key:
        return False
    if path == "status_review_entry" and not _reply_has_specific_anchor(response):
        return True
    if any(token in first_key for token in ("状态", "复盘", "工作", "天气", "速度", "响应", "时间", "位置", "心情", "计划")):
        return False
    if any(token in first_key for token in ("听着", "我在", "陪你", "慢慢说", "开始讲", "开始聊", "开始说")):
        return True
    return False


def _status_reply_is_topic_echo(text: str) -> bool:
    key = _dedupe_key(text)
    if not key:
        return False
    echo_patterns = (
        "最近状态",
        "最近状态啊",
        "最近状态嘛",
        "最近状态呢",
        "状态",
        "状态啊",
        "状态嘛",
        "状态呢",
        "复盘",
        "复盘啊",
        "复盘嘛",
        "工作状态",
        "工作状态啊",
        "最近工作节奏",
        "最近工作节奏啊",
        "工作节奏",
        "工作节奏啊",
        "想聊最近状态是吧",
    )
    if any(key == _dedupe_key(pattern) for pattern in echo_patterns):
        return True
    if any(token in key for token in ("节奏", "睡眠", "情绪", "提不起劲", "太满", "最累", "卡住", "事情", "项目", "压力")):
        return False
    return False


def _streaming_final_repair_reason_for_original(text: str) -> str:
    response = str(text or "").strip()
    first_sentence = _first_streaming_sentence(response, include_partial=True)
    if first_sentence and (
        _status_reply_is_topic_echo(first_sentence) or _status_reply_is_weak_ack(first_sentence)
    ):
        tail = response[len(first_sentence):].strip()
        if tail and (
            not _last_complete_streaming_sentence_end(tail)
            or _streaming_text_ends_with_incomplete_phrase(tail)
        ):
            return "blocked_incomplete_streaming_reply"
        if not _reply_has_specific_anchor(tail):
            return "blocked_vague_reply"
    return "blocked_incomplete_streaming_reply"


def _status_reply_is_weak_ack(text: str) -> bool:
    key = _dedupe_key(text)
    if not key:
        return False
    if _reply_has_specific_anchor(text):
        return False
    weak_patterns = (
        "最近确实得理理",
        "确实得理理",
        "确实得理一下",
        "我也觉得该理理了",
        "我也觉得该理理",
        "我也觉得该理一下",
        "这个可以复盘一下",
        "这个得复盘一下",
        "是该复盘一下",
        "可以聊聊这个",
        "可以好好聊聊",
    )
    return any(key == _dedupe_key(pattern) for pattern in weak_patterns)


def _strip_low_value_status_opening(text: str) -> str:
    response = str(text or "").strip()
    if not response:
        return ""
    first_sentence = _first_streaming_sentence(response, include_partial=True)
    if not first_sentence:
        return ""
    if not (_status_reply_is_topic_echo(first_sentence) or _status_reply_is_weak_ack(first_sentence)):
        return ""
    tail = response[len(first_sentence):].lstrip(" \t\r\n，,。.!！?？、；;：:")
    if not _reply_has_specific_anchor(tail):
        return ""
    return tail


def _reply_has_specific_anchor(text: str) -> bool:
    key = _dedupe_key(text)
    if not key:
        return False
    anchors = (
        "节奏",
        "睡眠",
        "情绪",
        "提不起劲",
        "太满",
        "最累",
        "卡住",
        "事情",
        "项目",
        "压力",
        "工作",
        "先看",
        "说起",
    )
    return any(token in key for token in anchors)


def _reply_hallucinates_aura_self_state(
    reply: str,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None = None,
) -> bool:
    response = str(reply or "").strip()
    if not response:
        return False
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if path not in {"normal_chat", "casual_chat_preface", "supportive_chat", "status_review_entry"}:
        return False
    if path not in {"casual_chat_preface", "supportive_chat"} and not _user_topic_needs_specific_anchor(context):
        return False
    key = _dedupe_key(response)
    if not key:
        return False
    debug = context.debug if isinstance(context.debug, dict) else {}
    user_text = str(debug.get("focused_user_text") or debug.get("user_text") or "").strip()
    user_key = _dedupe_key(user_text)
    if _reply_uses_unsolicited_time_framing(key, user_key=user_key):
        return True
    first_person_self_state_patterns = (
        "我这几天",
        "我最近",
        "我一直",
        "我也就",
        "我也闲着",
        "正好我也闲着",
        "我也刚醒",
        "我还没睡",
        "我刚醒",
        "我的状态",
        "我状态",
        "盯着后台",
        "后台看",
    )
    for pattern in first_person_self_state_patterns:
        marker_key = _dedupe_key(pattern)
        if marker_key and marker_key in key:
            return True
    contextual_self_state_patterns = (
        "还没睡",
        "大半夜",
        "刚好清醒",
        "清醒着",
        "刚醒",
        "脑子最清醒",
        "脑子清醒",
        "最清醒",
        "忙完这一阵",
        "能喘口气",
        "没敢睡",
        "睡死",
        "一大早上",
        "一大早",
        "大早上",
        "大清早",
        "凌晨",
        "半夜",
        "今晚这时间点",
        "这时间点儿",
        "这时间点",
    )
    for pattern in contextual_self_state_patterns:
        marker_key = _dedupe_key(pattern)
        if not marker_key or marker_key not in key:
            continue
        if marker_key in user_key and not _contextual_self_marker_has_first_person_anchor(key, marker_key):
            continue
        return True
    return False


def _contextual_self_marker_has_first_person_anchor(key: str, marker_key: str) -> bool:
    if not key or not marker_key:
        return False
    start = 0
    first_person_nearby = (
        "我也",
        "我也是",
        "我这边",
        "我还",
        "我刚",
        "我正",
        "我昨",
        "我今",
        "我没",
        "我不",
        "咱",
    )
    while True:
        index = key.find(marker_key, start)
        if index < 0:
            return False
        prefix = key[max(0, index - 8):index]
        if prefix.endswith("我") or any(token in prefix for token in first_person_nearby):
            return True
        start = index + max(1, len(marker_key))


def _reply_makes_unfounded_user_state_claim(
    reply: str,
    *,
    context: PersonaContext,
    voice_turn: VoiceTurnResult | None = None,
) -> bool:
    response = str(reply or "").strip()
    if not response:
        return False
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if path not in {"normal_chat", "casual_chat_preface", "supportive_chat", "status_review_entry"}:
        return False
    if path != "supportive_chat" and not _user_topic_needs_specific_anchor(context):
        return False
    key = _dedupe_key(response)
    debug = context.debug if isinstance(context.debug, dict) else {}
    user_text = str(debug.get("focused_user_text") or debug.get("user_text") or "").strip()
    user_key = _dedupe_key(user_text)
    if key.startswith(_dedupe_key("是觉得生活节奏")) and _streaming_text_ends_with_incomplete_phrase(response):
        return False
    user_claim_markers = (
        "你这两天",
        "你这几天",
        "你这段时间",
        "这段时间挺拼",
        "你上礼拜",
        "你上周",
        "上礼拜",
        "上周",
        "连着两天",
        "连着三天",
        "连着几天",
        "你最近",
        "你整个人",
        "你一直",
        "一直在看你",
        "一直在跟我",
        "听得出来",
        "看得出来",
        "感觉你",
        "你心里",
        "看你这",
        "盯着屏幕",
        "半天没动",
        "精神头",
        "比上周",
        "那个结",
        "结还没解开",
        "没解开",
        "念叨",
        "琐碎",
        "绷得太紧",
        "隐形加班",
        "疲惫",
        "觉得累",
        "让你觉得累",
        "压力很大",
        "挺拼",
        "又在折腾",
        "大工程",
        "赶项目",
        "新项目",
        "那个新项目",
        "搞那个项目",
        "搞那个新项目",
        "活儿干不完",
        "活干不完",
        "整个人都不好",
        "抓狂",
        "搞心态",
        "没意义的消耗",
        "无意义的消耗",
        "压不过来",
        "环境让人憋屈",
        "活儿多到理不清",
        "没必要这么熬",
        "熬夜",
        "熬大夜",
    )
    if any(_dedupe_key(marker) in key for marker in user_claim_markers):
        return True
    unfounded_openers = (
        "其实最近",
        "最近看你",
        "我看你",
        "感觉你",
        "看你",
    )
    if any(key.startswith(_dedupe_key(marker)) for marker in unfounded_openers):
        return True
    inferred_state_markers = (
        "挺紧绷",
        "紧绷",
        "焦虑",
        "大脑超频",
        "压力",
        "压力大",
        "累得够呛",
        "赶进度",
        "几个项目",
        "开源项目",
        "折腾",
        "消息回得慢",
        "回消息",
        "频率",
        "比平时",
        "进度压得太死",
        "手头的活",
        "手头活儿太多",
        "活儿太多",
        "压得慌",
        "活儿卡住",
        "干不完",
        "手头的事",
        "手头的事儿",
        "手头的事儿太杂",
        "手头项目",
        "手头那几个项目",
        "最近节奏",
        "节奏乱",
        "节奏乱了",
        "节奏蛮快",
        "最近事情",
        "事情是不是",
        "整个人都在飘",
        "都在飘",
        "找点重心",
        "透透气",
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
        "生活节奏",
        "挺杂",
        "效率上不去",
        "闲不住",
        "真闲不住",
        "停不下来",
        "马拉松",
        "处理那些",
        "那些琐",
        "琐碎",
    )
    for marker in inferred_state_markers:
        marker_key = _dedupe_key(marker)
        if marker_key and marker_key in key and marker_key not in user_key:
            return True
    return False


def _continuation_guesses_user_state(text: str) -> bool:
    key = _dedupe_key(text)
    if not key:
        return False
    markers = (
        "最近看你",
        "消息回得慢",
        "回得慢",
        "进度压得太死",
        "压得太死",
        "手头的事儿太杂",
        "手头的事太杂",
        "太杂",
        "最近节奏",
        "节奏蛮快",
        "最近事情",
        "事情是不是",
        "挺杂",
        "挺紧绷",
        "紧绷",
        "焦虑",
        "疲惫",
        "压力大",
        "赶项目",
        "熬夜",
        "熬大夜",
        "效率上不去",
        "几个项目",
        "回消息",
        "比平时",
    )
    return any(_dedupe_key(marker) in key for marker in markers)


def _reply_uses_unsolicited_time_framing(key: str, *, user_key: str) -> bool:
    if not key:
        return False
    time_markers = (
        "凌晨",
        "半夜",
        "大半夜",
        "一大早",
        "大早上",
        "大清早",
        "今晚这时间点",
        "这时间点儿",
        "这时间点",
        "这么早",
        "这么晚",
    )
    return any(marker in key and marker not in user_key for marker in time_markers)


def _user_topic_needs_specific_anchor(context: PersonaContext) -> bool:
    debug = context.debug if isinstance(context.debug, dict) else {}
    query = debug.get("query_context") if isinstance(debug.get("query_context"), dict) else {}
    intent = str(query.get("intent") or "").strip()
    if intent not in {"", "chat"}:
        return False
    keywords = debug.get("user_topic_keywords") if isinstance(debug.get("user_topic_keywords"), list) else []
    return any(str(token) in {"最近状态", "状态", "复盘", "工作"} for token in keywords)


def _user_topic_is_job_change(context: PersonaContext) -> bool:
    debug = context.debug if isinstance(context.debug, dict) else {}
    query = debug.get("query_context") if isinstance(debug.get("query_context"), dict) else {}
    intent = str(query.get("intent") or "").strip()
    if intent not in {"", "chat"}:
        return False
    user_text = str(debug.get("focused_user_text") or debug.get("user_text") or "").strip()
    return any(token in user_text for token in ("换工作", "跳槽", "离职", "找工作", "新工作"))


def _supportive_continuation_is_unsafe(text: str) -> bool:
    value = str(text or "")
    blocked = (
        "睡吧",
        "睡觉",
        "闭上眼",
        "闭眼",
        "眯一会",
        "眯会",
        "放段",
        "放首",
        "放歌",
        "老歌",
        "音乐",
        "代码",
        "烦人",
        "别想那些",
        "躺下",
        "晓得哒",
        "有的没的",
        "不聊那些",
        "别聊那些",
        "先不聊",
        "不说话",
        "不聊天",
        "不聊了",
    )
    return any(token in value for token in blocked)


def _casual_continuation_is_placeholder(text: str) -> bool:
    key = _dedupe_key(text)
    if not key:
        return False
    if key in {"先", "聊先", "那就聊先", "那聊先", "就聊先"}:
        return True
    placeholders = (
        "行呀",
        "好呀",
        "那就聊嘛",
        "那就聊",
        "那咱们",
        "直奔主题",
        "你说我在听",
        "我在听",
        "我听着",
        "我就在这儿听着",
        "我就在这听着",
        "就在这儿听着",
        "就在这听着",
        "那我就在这儿陪着你",
        "那我就在这陪着你",
        "我就在这儿陪着你",
        "我就在这陪着你",
        "我在这儿陪着你",
        "我在这陪着你",
        "在这儿陪着你",
        "在这陪着你",
        "你慢慢说",
        "慢慢说",
        "你想聊什么",
        "你想说什么",
        "你想从哪",
        "想从哪",
        "聊啥",
        "想聊啥",
        "说呗",
        "那就说呗",
        "既然你想聊那就说呗",
        "想换就换",
        "小孩子过家家",
        "过家家",
        "聊点什么",
        "聊点儿什么",
        "想聊点什么",
        "想聊点儿什么",
        "从哪儿聊起",
        "从哪聊起",
        "从哪里聊起",
        "先从哪儿聊起",
        "先从哪聊起",
        "先从哪里聊起",
        "从哪儿开始",
        "从哪开始",
        "从哪里开始",
        "我在你从哪儿开始讲都行",
        "我在你从哪开始讲都行",
        "我在你从哪儿开始聊都行",
        "我在你从哪开始聊都行",
        "从哪儿开始讲都行",
        "从哪开始讲都行",
        "从哪儿开始聊都行",
        "从哪开始聊都行",
        "你从哪儿开始讲都行",
        "你从哪开始讲都行",
        "你从哪儿开始聊都行",
        "你从哪开始聊都行",
        "发生什么了",
        "怎么了",
    )
    return any(token in key for token in placeholders)


def _casual_chat_stream_should_wait_first_sentence(
    text: str,
    *,
    voice_turn: VoiceTurnResult | None,
) -> bool:
    path = str(((voice_turn.debug if voice_turn else {}) or {}).get("decision_path") or "")
    if path not in {"casual_chat_preface", "supportive_chat"}:
        return False
    value = str(text or "").strip()
    if not value:
        return False
    key = _dedupe_key(value)
    if not key:
        return False
    first_sentence = _first_streaming_sentence(value, include_partial=False)
    if not first_sentence:
        return True
    first_key = _dedupe_key(first_sentence)
    if first_key in {
        "那我就在这儿陪着你",
        "那我就在这陪着你",
        "我就在这儿陪着你",
        "我就在这陪着你",
        "我在这儿陪着你",
        "我在这陪着你",
        "在这儿陪着你",
        "在这陪着你",
        "陪着你",
        "正好我也闲着",
        "我也闲着",
    }:
        return True
    if key.startswith(first_key) and len(key) > len(first_key):
        tail_key = key[len(first_key):]
        if any(token in tail_key for token in ("你想从哪", "想从哪", "聊点什么", "想聊点什么")):
            return True
    return False


def _casual_continuation_is_unfounded_guess(text: str) -> bool:
    value = str(text or "")
    if not value.strip():
        return False
    guess_markers = ("是不是", "该不会", "不会又", "又在", "又是", "是不是又", "你最近是不是", "是觉得")
    private_assumptions = (
        "熬夜",
        "熬大夜",
        "熬",
        "赶进度",
        "项目",
        "工作",
        "加班",
        "失眠",
        "压力很大",
        "心情不好",
        "坑",
        "憋屈",
        "手痒",
        "攒够了失望",
        "攒够失望",
        "往上爬",
        "平台已经没法",
        "出事",
        "遇到麻烦",
        "钻牛角尖",
        "突然想通",
        "想通了",
    )
    if not any(marker in value for marker in guess_markers):
        return False
    if any(token in value for token in private_assumptions):
        return True
    return bool(re.search(r"(是不是|该不会|不会又).{0,12}(了|吧|啊|呀|呢|？|\\?)", value))


def _voice_turn_has_local_complete(voice_turn: VoiceTurnResult) -> bool:
    debug = voice_turn.debug or {}
    for value in debug.values():
        if isinstance(value, dict) and bool(value.get("local_complete")):
            return True
    return False


def _voice_turn_has_fallback_only(voice_turn: VoiceTurnResult) -> bool:
    debug = voice_turn.debug or {}
    for value in debug.values():
        if isinstance(value, dict) and bool(value.get("fallback_only")):
            return True
    return False


def _voice_turn_can_use_conversational_preface(voice_turn: VoiceTurnResult) -> bool:
    debug = voice_turn.debug or {}
    path = str(debug.get("decision_path") or "")
    if path == "supportive_chat":
        supportive = debug.get("supportive_chat") if isinstance(debug.get("supportive_chat"), dict) else {}
        return str(supportive.get("matched") or "") in {
            "overtime_support",
            "emotion",
            "job_change",
        }
    if path == "casual_chat_preface":
        casual = debug.get("casual_chat") if isinstance(debug.get("casual_chat"), dict) else {}
        return str(casual.get("matched") or "") == "job_change"
    return False


def _looks_like_action_or_stage_direction(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"[\*＊~～].{0,40}[\*＊~～]", value):
        return True
    if re.search(r"[（(【\[].{0,50}(笑|抱|摸|拍|叹|眨|点头|摇头|靠|凑|动作|表情|语气|心理|旁白).{0,50}[）)】\]]", value):
        return True
    return False


def _compact_model_continuation(text: str, *, limit: int = 24) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    match = re.search(r"[。！？!?]", value)
    if match:
        value = value[: match.end()].strip()
    elif len(value) > limit:
        cut_at = -1
        for punct in ("，", ",", "；", ";", "、"):
            index = value.find(punct)
            if 0 < index <= limit:
                cut_at = max(cut_at, index)
        value = value[:cut_at].strip() if cut_at > 0 else value[:limit].rstrip()
        value = value.rstrip("，,；;、：:")
        if value and not value.endswith(("。", "！", "？", ".", "!", "?")):
            value += "。"
    return value


def _trim_preface_by_chars(local_preface: str, model_text: str) -> str:
    preface_chars = _significant_chars(local_preface)
    if not preface_chars:
        return str(model_text or "").strip()
    seen = 0
    for index, ch in enumerate(str(model_text or "")):
        if _is_dedupe_punctuation(ch):
            continue
        seen += 1
        if seen >= len(preface_chars):
            return str(model_text[index + 1:]).lstrip(" ，,。.!！?？、；;：:")
    return ""


def _trim_preface_overlap(local_preface: str, model_text: str, *, min_chars: int = 4) -> str:
    preface_key = _dedupe_key(local_preface)
    model_key = _dedupe_key(model_text)
    if not preface_key or not model_key:
        return str(model_text or "").strip()
    max_overlap = min(len(preface_key), len(model_key))
    for overlap in range(max_overlap, max(1, min_chars) - 1, -1):
        if preface_key.endswith(model_key[:overlap]):
            return _trim_text_by_significant_chars(model_text, overlap)
    return str(model_text or "").strip()


def _trim_text_by_significant_chars(text: str, count: int) -> str:
    remaining = max(0, int(count or 0))
    if remaining <= 0:
        return str(text or "").strip()
    for index, ch in enumerate(str(text or "")):
        if _is_dedupe_punctuation(ch):
            continue
        remaining -= 1
        if remaining <= 0:
            return str(text[index + 1:]).lstrip(" ，,。.!！?？、；;：:")
    return ""


def _dedupe_key(text: str) -> str:
    return "".join(_significant_chars(text)).lower()


def _significant_chars(text: str) -> list[str]:
    return [ch for ch in str(text or "") if not _is_dedupe_punctuation(ch)]


def _is_dedupe_punctuation(ch: str) -> bool:
    return ch.isspace() or ch in ",，。.!！?？、~～…·:：;；\"'“”‘’（）()【】[]{}<>《》-—"


def _needs_separator(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return not left.endswith(("。", "！", "？", "，", ",", ".", "!", "?", "；", ";", "：", ":"))


def result_from_hermes(result: HermesLilyResult) -> PersonaTurnResult:
    return PersonaTurnResult(
        ok=result.ok,
        status=result.status,
        response=result.response,
        request_id=result.request_id,
        latency_ms=result.latency_ms,
        voice_turn={},
        evidence=dict(result.evidence or {}),
    )
