"""X2 问她为什么：意图识别、prompt 背景节、可追溯的 why_trace。

覆盖任务书 X2 的要求：
- 意图正例/反例各 ≥6，特别是"为什么天是蓝的"不能判进来；
- 命中时 prompt 里出现账本原因（emotion_state 一行 + 口语化流水），
  evidence/debug 里 why_trace 跟账本 event_key 对得上；
- 未命中时那一节不出现；
- 没有账本记录时的照实文案；
- 流式和非流式都不会被规则快答/后台查资料截走。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.turn import AuraPersonaGateway
from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig


# ── 测试辅助 ────────────────────────────────────────────────
def _config(tmp_path: Path) -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True, exist_ok=True)
    (persona_home / "persona" / "soul.md").write_text("主 soul\n不要客服腔。", encoding="utf-8")
    return PersonaGatewayConfig(
        enabled=True,
        persona_home=str(persona_home),
        companion_home=str(companion_home),
        hermes_home=str(tmp_path / "hermes-home"),
        include_debug_context=True,
    )


def _store(tmp_path: Path) -> LilyPersonaStore:
    config = _config(tmp_path)
    return LilyPersonaStore(config.companion_db_path)


def _seed_state(store: LilyPersonaStore, config: PersonaGatewayConfig, **overrides) -> dict:
    state = store.get_or_create_state(config.scope)
    state.update({"mood": 60, "energy": 80, "satiety": 60, "stress": 10, "trust": 50, "beans": 50})
    state["metadata"] = {
        "social_need": 50,
        "curiosity": 40,
        "privacy_sensitivity": 30,
        "resource_comfort": 30,
        "emotion_state": {
            "emotion": "sad",
            "label": "难过",
            "intensity": 4,
            "cause": "用户的猫走丢了",
            "at": time.time(),
        },
    }
    state.update(overrides)
    store.save_state(config.scope, state)
    return state


def _seed_ledger_event(
    store: LilyPersonaStore,
    config: PersonaGatewayConfig,
    *,
    event_key: str,
    cause: str = "去公园散步",
    field: str = "energy",
    before: int = 80,
    after: int = 73,
    age_seconds: float = 600.0,
) -> str:
    """造一条最近 6 小时内的账本流水，返回 event_key。"""
    payload = {
        "source": "world_clock",
        "cause": cause,
        "before": {"mood": 60, "energy": 80, "satiety": 60, "stress": 10},
        "after": {"mood": 60, "energy": 73, "satiety": 60, "stress": 10},
    }
    payload["before"][field] = before
    payload["after"][field] = after
    store.record_life_event(
        config.scope,
        event_type="ledger.world_clock",
        title=f"傍晚：{cause}",
        visibility="public",
        intensity=0,
        delta={field: after - before},
        payload=payload,
        event_key=event_key,
    )
    if age_seconds:
        _force_created_at(config, event_key, time.time() - age_seconds)
    return event_key


def _force_created_at(config: PersonaGatewayConfig, event_key: str, created_at: float) -> None:
    import sqlite3

    conn = sqlite3.connect(str(config.companion_db_path))
    try:
        conn.execute(
            "UPDATE companion_life_event SET created_at=? WHERE event_key=?",
            (created_at, event_key),
        )
        conn.commit()
    finally:
        conn.close()


def _gateway(tmp_path: Path, monkeypatch, *, chunks: tuple[str, ...], user_text: str):
    """假 LLM 流：把 chunks 一个一个吐出来，走完整流式链路。"""

    class FakeStreamResponse:
        def __enter__(self):
            events = []
            for chunk in chunks:
                payload = json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
                events.append(f"data: {payload}\n\n".encode("utf-8"))
            events.append(b"data: [DONE]\n\n")
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeStreamResponse(),
    )
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
    from integrations.aura_persona_gateway.turn import AuraPersonaGateway
    from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    _seed_state(store, config)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_reasoning_effort="none",
        fast_reply_enabled=True,
        fast_reply_mode="hermes_main",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return config, store, gateway, user_text


# ── 意图识别 ────────────────────────────────────────────────
POSITIVE_CASES = (
    "你为什么不开心",
    "你怎么了",
    "你刚才为什么生气",
    "你心情怎么样",
    "你最近怎么这么累",
    "莉莉你为啥不开心",
    "你怎么这么饿",
    "你为什么会兴奋",
    "你心情为什么这么差",
    "您怎么了",
)

NEGATIVE_CASES = (
    "为什么天是蓝的",
    "我为什么不开心",
    "你怎么看这个问题",
    "你妈妈为什么生气",
    "帮我查一下为什么股市跌了",
    "明天天气怎么样",
    "今天几号",
    "你会做什么",
    "你老板为什么骂你",
    "我们出去走走吧",
)


def test_is_self_why_question_accepts_questions_about_lily_herself():
    from integrations.aura_persona_gateway.self_why import is_self_why_question

    for text in POSITIVE_CASES:
        assert is_self_why_question(text) is True, text


def test_is_self_why_question_rejects_everything_else():
    from integrations.aura_persona_gateway.self_why import is_self_why_question

    for text in NEGATIVE_CASES:
        assert is_self_why_question(text) is False, text


def test_is_self_why_question_is_punctuation_and_case_insensitive():
    from integrations.aura_persona_gateway.self_why import is_self_why_question

    assert is_self_why_question("你为什么不开心？！") is True
    assert is_self_why_question("LILY 你怎么了") is True
    assert is_self_why_question("  你怎么了  ") is True


def test_is_self_why_question_rejects_empty_and_oversized_text():
    from integrations.aura_persona_gateway.self_why import is_self_why_question

    assert is_self_why_question("") is False
    assert is_self_why_question(None) is False
    assert is_self_why_question("你" * 60 + "为什么不开心") is False


# ── prompt 背景节 ───────────────────────────────────────────
def test_prompt_section_carries_emotion_state_and_ledger_reasons(tmp_path):
    from integrations.aura_persona_gateway.self_why import (
        SELF_WHY_SECTION_TITLE,
        build_self_why_prompt,
        collect_self_why_events,
    )

    store = _store(tmp_path)
    config = _config(tmp_path)
    state = _seed_state(store, config)
    _seed_ledger_event(
        store,
        config,
        event_key="2026-10-23:walk",
        cause="去公园散步",
        field="energy",
        before=80,
        after=73,
    )
    _seed_ledger_event(
        store,
        config,
        event_key="2026-10-23:rain",
        cause="下雨了",
        field="mood",
        before=60,
        after=48,
    )

    events = collect_self_why_events(store, config.scope)
    prompt = build_self_why_prompt(state=state, events=events)

    assert prompt.startswith(SELF_WHY_SECTION_TITLE)
    # emotion_state 那一行：中文标签 + 强度 + 一句原因。
    assert "难过" in prompt
    assert "强度 4" in prompt
    assert "用户的猫走丢了" in prompt
    # 账本流水：时间 + 原因 + before→after，全中文口径。
    assert "去公园散步" in prompt
    assert "体力 80→73" in prompt
    assert "下雨了" in prompt
    assert "心情 60→48" in prompt
    assert "只能依据上面这些记录解释" in prompt


def test_prompt_section_says_so_honestly_when_nothing_is_recorded(tmp_path):
    from integrations.aura_persona_gateway.self_why import (
        SELF_WHY_EMPTY_LINE,
        build_self_why_prompt,
        collect_self_why_events,
    )

    store = _store(tmp_path)
    config = _config(tmp_path)
    state = _seed_state(store, config)

    assert collect_self_why_events(store, config.scope) == []
    prompt = build_self_why_prompt(state=state, events=[])

    assert SELF_WHY_EMPTY_LINE in prompt
    assert "不要编造具体原因" in prompt


def test_self_why_events_skip_old_and_unrelated_flows(tmp_path):
    from integrations.aura_persona_gateway.self_why import collect_self_why_events

    store = _store(tmp_path)
    config = _config(tmp_path)
    _seed_state(store, config)
    # 6 小时之外的不要。
    _seed_ledger_event(store, config, event_key="old:walk", cause="很久以前散步", age_seconds=7 * 3600)
    # 只动了 trust/beans 的不要（对"我为什么这样"没有解释力）。
    _seed_ledger_event(store, config, event_key="new:beans", cause="买了零食", field="mood", age_seconds=60)

    keys = [event["event_key"] for event in collect_self_why_events(store, config.scope)]
    assert "old:walk" not in keys


def test_self_why_events_cap_at_six_and_run_oldest_first(tmp_path):
    from integrations.aura_persona_gateway.self_why import collect_self_why_events

    store = _store(tmp_path)
    config = _config(tmp_path)
    _seed_state(store, config)
    for index in range(9):
        _seed_ledger_event(
            store,
            config,
            event_key=f"flow:{index}",
            cause=f"事件{index}",
            age_seconds=600 - index * 30,
        )

    events = collect_self_why_events(store, config.scope)

    assert len(events) == 6
    keys = [event["event_key"] for event in events]
    assert keys == sorted(keys, key=lambda key: int(key.split(":")[1])), "要按时间正序给模型读"


def test_format_self_why_event_is_colloquial_chinese():
    from integrations.aura_persona_gateway.self_why import format_self_why_event

    line = format_self_why_event(
        {
            "created_at": time.time(),
            "title": "傍晚：去公园散步",
            "delta": {"energy": -7},
            "payload": {
                "cause": "去公园散步",
                "before": {"energy": 80},
                "after": {"energy": 73},
            },
        }
    )

    assert line.endswith("去公园散步，体力 80→73")
    assert line[:5].count(":") == 1  # HH:MM 前缀


def test_build_self_why_turn_skips_when_not_asked(tmp_path):
    from integrations.aura_persona_gateway.self_why import build_self_why_turn

    store = _store(tmp_path)
    config = _config(tmp_path)
    state = _seed_state(store, config)
    _seed_ledger_event(store, config, event_key="2026-10-23:walk")

    turn = build_self_why_turn(store=store, scope=config.scope, state=state, user_text="今天天气不错")

    assert turn["matched"] is False
    assert turn["prompt"] == ""
    assert turn["why_trace"] == []


def test_build_self_why_turn_reports_why_trace(tmp_path):
    from integrations.aura_persona_gateway.self_why import build_self_why_turn

    store = _store(tmp_path)
    config = _config(tmp_path)
    state = _seed_state(store, config)
    _seed_ledger_event(store, config, event_key="2026-10-23:walk", cause="去公园散步")
    _seed_ledger_event(store, config, event_key="2026-10-23:rain", cause="下雨了", field="mood", before=60, after=48)

    turn = build_self_why_turn(
        store=store,
        scope=config.scope,
        state=state,
        user_text="你为什么不开心",
    )

    assert turn["matched"] is True
    assert set(turn["why_trace"]) == {"2026-10-23:walk", "2026-10-23:rain"}
    assert turn["debug"]["event_count"] == 2
    assert turn["debug"]["prompt_chars"] > 0


# ── metadata.self_why_state ─────────────────────────────────
def test_self_why_state_round_trip(tmp_path):
    from integrations.aura_persona_gateway.self_why import read_self_why_state, write_self_why_state

    store = _store(tmp_path)
    config = _config(tmp_path)
    state = _seed_state(store, config)

    assert read_self_why_state(state) == {}
    write_self_why_state(state, event_keys=["a", "", "b"], question="你为什么不开心", at=123.0)
    store.save_state(config.scope, state)
    reloaded = store.get_or_create_state(config.scope)

    stored = read_self_why_state(reloaded)
    assert stored["event_keys"] == ["a", "b"]
    assert stored["question"] == "你为什么不开心"
    assert stored["at"] == 123.0


# ── 链路：不被快答/后台截走 ─────────────────────────────────
def test_voice_turn_lets_self_why_questions_through_to_the_model(tmp_path):
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
    from integrations.aura_persona_gateway.voice_turn import VoiceTurnVerdict, execute_voice_turn

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="hermes_main",
    )
    result = execute_voice_turn(
        "你心情怎么样？",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
        state_summary={"mood": 86, "energy": 42, "stress": 8, "trust": 72},
    )

    # 空 speak_text → 路由层不会走本地回复，转模型（只有模型看得到账本背景节）。
    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.speak_text == ""
    assert result.debug["decision_path"] == "self_why"
    assert result.background_task is None


def test_voice_turn_does_not_enqueue_a_background_lookup_for_self_why(tmp_path):
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
    from integrations.aura_persona_gateway.voice_turn import VoiceTurnVerdict, execute_voice_turn

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=True,
        fast_reply_mode="local_rule",
        ack_and_enqueue_enabled=True,
    )
    # "确认" 命中 AGENT_LOOKUP_RE，正常会排后台查资料；问她自己状态时不该排。
    result = execute_voice_turn(
        "你怎么确认自己不开心的",
        fastpath={"intent": "chat"},
        runtime_config=runtime,
    )

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.background_task is None
    assert result.debug["decision_path"] == "self_why"


def _voice_gateway(tmp_path: Path, monkeypatch, *, chunks: tuple[str, ...], user_text: str):
    """假 LLM 流，走完整流式链路；顺手把真正发给模型的 prompt 抓下来。"""

    class FakeStreamResponse:
        def __enter__(self):
            events = []
            for chunk in chunks:
                payload = json.dumps({"choices": [{"delta": {"content": chunk}}]}, ensure_ascii=False)
                events.append(f"data: {payload}\n\n".encode("utf-8"))
            events.append(b"data: [DONE]\n\n")
            return iter(events)

        def __exit__(self, exc_type, exc, tb):
            return False

    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout):
        try:
            captured["body"] = json.loads(req.data.decode("utf-8"))
        except Exception:  # pragma: no cover - 只是探针，抓不到不影响主流程
            pass
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    _seed_state(store, config)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
        aura_model_max_tokens=64,
        aura_model_reasoning_effort="none",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return config, store, gateway, user_text, captured


def _sent_prompt(captured: dict[str, Any]) -> str:
    """从请求体里取真正承载 persona 背景的那条消息（user 角色）。"""
    body = captured.get("body") or {}
    messages = body.get("messages") or []
    for message in messages:
        if str(message.get("role") or "") == "user":
            return str(message.get("content") or "")
    for message in messages:
        return str(message.get("content") or "")
    return ""


def test_streaming_turn_answers_self_why_from_the_ledger(tmp_path, monkeypatch):
    from integrations.aura_persona_gateway.self_why import SELF_WHY_SECTION_TITLE

    config, store, gateway, user_text, captured = _voice_gateway(
        tmp_path,
        monkeypatch,
        chunks=("因为刚才下雨了。",),
        user_text="你为什么不开心",
    )
    _seed_ledger_event(store, config, event_key="2026-10-23:rain", cause="下雨了", field="mood", before=60, after=48)

    events = list(
        gateway.run_direct_turn_stream(
            user_text, metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}
        )
    )
    final = events[-1]["payload"]

    prompt = _sent_prompt(captured)
    assert SELF_WHY_SECTION_TITLE in prompt
    assert "下雨了" in prompt
    assert "心情 60→48" in prompt
    # 世界时钟可能在本轮顺手写一条真实的时段事件，只要求雨这条被引用。
    assert "2026-10-23:rain" in final["evidence"]["why_trace"]
    assert "2026-10-23:rain" in final["debug"]["setup"]["self_why"]["why_trace"]
    assert "<emo:" not in final["response"]


def test_non_streaming_turn_answers_self_why_from_the_ledger(tmp_path, monkeypatch):
    from integrations.aura_persona_gateway.self_why import SELF_WHY_SECTION_TITLE

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    _seed_state(store, config)
    _seed_ledger_event(store, config, event_key="2026-10-23:rain", cause="下雨了", field="mood", before=60, after=48)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="step-3.5-flash",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="stepfun-unit-key",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    prompts: list[str] = []
    original = gateway._run_aura_model

    def spy(prompt, *, metadata=None):
        prompts.append(str(prompt))
        return original(prompt, metadata=metadata)

    def fake_urlopen(req, timeout):
        payload = json.dumps(
            {"choices": [{"message": {"content": "因为刚才下雨了。"}}]}, ensure_ascii=False
        )

        class _Response:
            def read(self):
                return payload.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Response()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    gateway._run_aura_model = spy

    result = gateway.run_turn("你刚才为什么难过", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert prompts, "非流式路径也应该把背景节发给模型"
    assert SELF_WHY_SECTION_TITLE in prompts[-1]
    assert "下雨了" in prompts[-1]
    assert "心情 60→48" in prompts[-1]
    # 世界时钟可能在本轮顺手写一条真实的时段事件，只要求雨这条被引用。
    assert "2026-10-23:rain" in result.evidence["why_trace"]
    assert "2026-10-23:rain" in result.debug["setup"]["self_why"]["why_trace"]


def test_ordinary_turn_has_no_self_why_section(tmp_path):
    from integrations.aura_persona_gateway.assets import load_persona_assets
    from integrations.aura_persona_gateway.context import build_persona_context
    from integrations.aura_persona_gateway.outlets import OutletSignals
    from integrations.aura_persona_gateway.query_context import resolve_query_context
    from integrations.aura_persona_gateway.self_why import SELF_WHY_SECTION_TITLE

    config = _config(tmp_path)
    assets = load_persona_assets(config)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    _seed_ledger_event(store, config, event_key="2026-10-23:rain", cause="下雨了")

    context = build_persona_context(
        user_text="明天会下雨吗",
        config=config,
        assets=assets,
        state=state,
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=resolve_query_context("明天会下雨吗", aura_home_city="上海"),
        outlet_signals=OutletSignals(proactive_message={"enabled": False}, spending={"enabled": False}),
    )

    assert SELF_WHY_SECTION_TITLE not in context.prompt
    assert "self_why" not in context.debug["context_sections"]
    assert context.debug.get("self_why_chars", 0) == 0


def test_self_why_section_appears_in_prompt_when_matched(tmp_path):
    from integrations.aura_persona_gateway.assets import load_persona_assets
    from integrations.aura_persona_gateway.context import build_persona_context
    from integrations.aura_persona_gateway.emotion_engine import EMOTION_TAG_INSTRUCTION
    from integrations.aura_persona_gateway.outlets import OutletSignals
    from integrations.aura_persona_gateway.query_context import resolve_query_context
    from integrations.aura_persona_gateway.self_why import SELF_WHY_SECTION_TITLE, build_self_why_turn

    config = _config(tmp_path)
    assets = load_persona_assets(config)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    _seed_ledger_event(store, config, event_key="2026-10-23:rain", cause="下雨了", field="mood", before=60, after=48)
    turn = build_self_why_turn(
        store=store, scope=config.scope, state=state, user_text="你为什么不开心"
    )

    context = build_persona_context(
        user_text="你为什么不开心",
        config=config,
        assets=assets,
        state=state,
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=resolve_query_context("你为什么不开心", aura_home_city="上海"),
        outlet_signals=OutletSignals(proactive_message={"enabled": False}, spending={"enabled": False}),
        self_why_section=str(turn["prompt"]),
    )

    assert SELF_WHY_SECTION_TITLE in context.prompt
    assert "self_why" in context.debug["context_sections"]
    assert context.debug["self_why_chars"] > 0
    # 情绪标签指令和"延续上一轮"那行照常保留，不能被这一节挤掉。
    assert EMOTION_TAG_INSTRUCTION in context.prompt
    assert "## 延续上一轮" in context.prompt
