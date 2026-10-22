"""E3/E4：情绪后果（世界账本）与协议/声音落地。

覆盖：情绪流水写进 ledger.emotion、心情 delta、neutral 不记账、同 request_id 不重复；
dialogue payload 带 E1 情绪、姿势映射、MiniMax 请求体情绪字段；
以及端到端回归——任何发往 TTS/字幕/历史的文本都不含 ``<emo:``。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from dataclasses import replace
from typing import Any

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.emotion_engine import EMOTIONS
from integrations.aura_persona_gateway.store import LilyPersonaStore, _life_event_from_row
from integrations.aura_persona_gateway.turn import AuraPersonaGateway, _record_emotion_turn
from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig
from integrations.hermes_lily_cli import gateway as gateway_module
from integrations.hermes_lily_cli.voice_providers import (
    VoiceProviderError,
    minimax_emotion,
    volcengine_emotion_debug,
)


# ── 测试辅助 ────────────────────────────────────────────────
def _config(tmp_path: Path) -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True)
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
    state.update({"satiety": 60, "energy": 80, "mood": 60, "beans": 50, "trust": 50, "stress": 10})
    state["metadata"] = {"social_need": 50, "curiosity": 40, "privacy_sensitivity": 30, "resource_comfort": 30}
    state.update(overrides)
    store.save_state(config.scope, state)
    return state


def _all_ledger_events(db_path: str, scope) -> list[dict]:
    """全量读账本（store 的 list_recent_life_events 最多只给 20 条）。"""
    conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, event_type, event_key, title, description, location, activity,
                   visibility, intensity, delta_json, payload_json, created_at
              FROM companion_life_event
             WHERE platform=? AND chat_id=? AND user_id=? AND event_type LIKE 'ledger.%'
             ORDER BY created_at ASC, id ASC
            """,
            scope.as_tuple(),
        ).fetchall()
    finally:
        conn.close()
    return [_life_event_from_row(row) for row in rows]


def _aura_messages(store: LilyPersonaStore, scope) -> list[dict]:
    return store.list_recent_messages(scope, limit=20)


def _stream_chunks(
    tmp_path: Path,
    monkeypatch,
    chunks: tuple[str, ...],
    *,
    user_text: str = "我的猫走丢了",
    prior_emotion: dict | None = None,
):
    """假 LLM 流：把 chunks 一个一个吐出来，验证剥离器在真实链路里的行为。"""

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
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)
    if prior_emotion:
        state["metadata"]["emotion_state"] = dict(prior_emotion)
        store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

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
    events = list(
        gateway.run_direct_turn_stream(
            user_text,
            metadata={"source": "aura-lily-gateway", "audio_bytes": 1234},
        )
    )
    return config, store, events


# ── E3 情绪后果：世界账本 ───────────────────────────────────
def test_emotion_writes_a_ledger_flow_with_the_mood_delta(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    facts, updated = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-happy",
        user_text="我今天升职了！",
        reply_text="那太好了呀。",
        llm_emotion="excited",
    )

    assert facts["emotion"] == "excited"
    assert facts["source"] == "llm"
    events = _all_ledger_events(config.companion_db_path, config.scope)
    emotion_events = [event for event in events if event["event_type"] == "ledger.emotion"]
    assert len(emotion_events) == 1
    event = emotion_events[0]
    assert event["event_key"] == "emotion:req-happy"
    assert event["delta"] == {"mood": 3}
    assert event["description"] == "情绪：兴奋（用户：我今天升职了！）"
    assert event["payload"]["cause"] == "情绪：兴奋（用户：我今天升职了！）"
    assert event["payload"]["source"] == "emotion"
    assert event["title"] == "情绪：兴奋"
    assert updated["mood"] == 63


def test_emotion_mood_delta_matches_the_table(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    expected = {"happy": 2, "excited": 3, "shy": 1, "sad": -3, "worried": -2, "pouty": -2}

    for index, (emotion, delta) in enumerate(expected.items()):
        state = _seed_state(store, config, mood=60)
        facts, updated = _record_emotion_turn(
            store=store,
            scope=config.scope,
            state=state,
            request_id=f"req-{emotion}",
            user_text="随便聊聊",
            reply_text="",
            llm_emotion=emotion,
        )
        assert facts["emotion"] == emotion
        assert updated["mood"] == 60 + delta
        events = [
            event
            for event in _all_ledger_events(config.companion_db_path, config.scope)
            if event["event_key"] == f"emotion:req-{emotion}"
        ]
        assert len(events) == 1
        assert events[0]["delta"] == {"mood": delta}


def test_neutral_and_surprised_do_not_write_a_ledger_flow(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    for request_id, emotion in (("req-neutral", "neutral"), ("req-surprised", "surprised")):
        facts, updated = _record_emotion_turn(
            store=store,
            scope=config.scope,
            state=state,
            request_id=request_id,
            user_text="今天几号",
            reply_text="",
            llm_emotion=emotion,
        )
        assert facts["emotion"] == emotion
        assert "ledger" not in facts
        assert updated["mood"] == 60

    assert _all_ledger_events(config.companion_db_path, config.scope) == []


def test_same_request_id_is_not_recorded_twice(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config, mood=60)

    first_facts, first_state = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-dup",
        user_text="我的猫走丢了",
        reply_text="别难过。",
        llm_emotion="sad",
    )
    second_facts, second_state = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=first_state,
        request_id="req-dup",
        user_text="我的猫走丢了",
        reply_text="别难过。",
        llm_emotion="sad",
    )

    events = _all_ledger_events(config.companion_db_path, config.scope)
    assert len(events) == 1
    assert first_state["mood"] == 57
    # 第二次同一个 request_id：不重复记账，也不再把心情往下扣。
    assert second_state["mood"] == 57
    assert "ledger" not in second_facts
    assert "ledger" in first_facts


def test_speculative_turn_records_nothing(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    facts, updated = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-spec",
        user_text="我的猫走丢了",
        reply_text="别难过。",
        llm_emotion="sad",
        speculative=True,
    )

    assert facts["emotion"] == "sad"
    assert facts["speculative"] is True
    assert updated == state
    assert _all_ledger_events(config.companion_db_path, config.scope) == []
    # 情绪状态也不落盘，否则推测回合会污染下一轮 prompt。
    assert "emotion_state" not in (updated.get("metadata") or {})


def test_emotion_state_lands_in_metadata_for_the_next_prompt(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    _facts, updated = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-state",
        user_text="我的猫走丢了",
        reply_text="别难过。",
        llm_emotion="sad",
    )
    stored = updated["metadata"]["emotion_state"]
    assert stored["emotion"] == "sad"
    assert stored["label"] == "难过"
    assert 1 <= stored["intensity"] <= 5
    assert stored["cause"] == "用户：我的猫走丢了"


def test_rule_reply_emotion_is_normalized_into_the_e1_set(tmp_path):
    """voice_turn fastpath 的旧词表情绪也必须归一化成 E1 值。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    facts, _updated = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-fastpath",
        user_text="你好呀",
        reply_text="我在呢。",
        llm_emotion="warm",
        emotion_source="rule",
    )

    assert facts["emotion"] == "happy"
    assert facts["source"] == "rule"
    assert facts["emotion"] in EMOTIONS


# ── E4 协议 ─────────────────────────────────────────────────
def test_dialogue_emotion_only_ever_emits_e1_values():
    assert gateway_module.dialogue_emotion("") == "neutral"
    assert gateway_module.dialogue_emotion(None) == "neutral"
    assert gateway_module.dialogue_emotion("warm") == "happy"
    assert gateway_module.dialogue_emotion("curious") == "surprised"
    assert gateway_module.dialogue_emotion("firm") == "neutral"
    # 无效值不能漏进协议。
    assert gateway_module.dialogue_emotion("angry") == "neutral"
    assert gateway_module.dialogue_emotion("SAD") == "sad"
    for value in ("", None, "warm", "firm", "curious", "sad", "angry"):
        assert gateway_module.dialogue_emotion(value) in EMOTIONS


def test_emotion_to_pose_keeps_legacy_compatibility():
    assert gateway_module.emotion_to_pose("happy") == "happy"
    assert gateway_module.emotion_to_pose("excited") == "happy"
    assert gateway_module.emotion_to_pose("warm") == "happy"
    assert gateway_module.emotion_to_pose("firm") == "proud"
    assert gateway_module.emotion_to_pose("proud") == "proud"
    assert gateway_module.emotion_to_pose("sad") is None
    assert gateway_module.emotion_to_pose("neutral") is None
    assert gateway_module.emotion_to_pose(None) is None


def test_turn_pose_prefers_the_resolved_emotion_then_falls_back():
    # 本轮解析出 happy/excited → happy 姿势。
    assert gateway_module.turn_pose(None, {"emotion": "happy"}) == "happy"
    assert gateway_module.turn_pose(None, {"emotion": "excited"}) == "happy"
    # 没有本轮情绪时沿用 fastpath 旧值映射（firm → proud）。
    assert gateway_module.turn_pose({"emotion": "firm"}, None) == "proud"
    assert gateway_module.turn_pose(None, None) is None
    # E1 里没有对应姿势的情绪 → None（设备保持随机说话姿势）。
    assert gateway_module.turn_pose(None, {"emotion": "sad"}) is None


def test_strip_reply_emotion_tag_guards_the_gateway_boundary():
    assert gateway_module.strip_reply_emotion_tag("<emo:sad>我在") == ("我在", "sad")
    assert gateway_module.strip_reply_emotion_tag("我在") == ("我在", "")
    assert gateway_module.strip_reply_emotion_tag("") == ("", "")


def test_dialogue_payload_carries_the_emotion(tmp_path):
    sent: list[Any] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    state = gateway_module.TurnState(turn_id=7)
    state.supports_local_audio = False
    config = gateway_module.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        await gateway_module.send_dialogue_and_tts(
            FakeWebsocket(),
            None,
            state,
            "<emo:shy>你今天真好看呀。",
            ok=True,
            request_id="req-1",
            bridge_evidence={"emotion": "shy"},
            voice_turn={"emotion": "warm"},
        )

    import asyncio

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogues = [item for item in messages if item.get("type") == "dialogue"]
    assert len(dialogues) == 1
    payload = dialogues[0]["payload"]
    assert payload["emotion"] == "shy"
    assert payload["emotion"] in EMOTIONS
    assert payload["pose"] == "happy"
    # 兜底剥离：字幕文本里不允许残留标签。
    assert "<emo:" not in payload["text"]
    assert all("<emo:" not in segment for segment in payload["segments"])


def test_dialogue_payload_defaults_to_neutral_without_any_emotion(tmp_path):
    sent: list[Any] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    state = gateway_module.TurnState(turn_id=8)
    state.supports_local_audio = False

    async def scenario():
        await gateway_module.send_dialogue_and_tts(
            FakeWebsocket(),
            None,
            state,
            "后台任务完成了。",
            ok=True,
            request_id="task-1",
        )

    import asyncio

    asyncio.run(scenario())

    payload = [json.loads(item) for item in sent if isinstance(item, str)][0]["payload"]
    assert payload["emotion"] == "neutral"
    assert payload["emotion"] in EMOTIONS


# ── E4 TTS 情绪参数 ─────────────────────────────────────────
def test_minimax_emotion_mapping():
    assert minimax_emotion("happy") == "happy"
    assert minimax_emotion("excited") == "happy"
    assert minimax_emotion("sad") == "sad"
    assert minimax_emotion("worried") == "fearful"
    assert minimax_emotion("pouty") == "angry"
    assert minimax_emotion("surprised") == "surprised"
    # shy / neutral / 无效值都不传。
    assert minimax_emotion("shy") == ""
    assert minimax_emotion("neutral") == ""
    assert minimax_emotion("") == ""
    assert minimax_emotion("angry") == ""
    assert minimax_emotion(None) == ""


def test_volcengine_emotion_is_debug_only():
    assert volcengine_emotion_debug("sad") == {
        "emotion": "sad",
        "sent": False,
        "reason": "volcengine_emotion_field_unconfirmed",
    }
    assert volcengine_emotion_debug("") == {}
    assert volcengine_emotion_debug(None) == {}


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _minimax_runtime(tmp_path: Path):
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    return AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="minimax",
        tts_model="speech-2.6-hd",
        tts_voice="female-shaonv",
        tts_base_url="https://api.minimaxi.com/v1/t2a_v2",
        tts_api_key="minimax-unit-key",
        tts_timeout_seconds=5,
    )


def _capture_minimax_body(monkeypatch, runtime) -> dict:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(json.dumps({"data": {"audio": "0102"}}).encode("utf-8"))

    monkeypatch.setattr("integrations.hermes_lily_cli.voice_providers.request.urlopen", fake_urlopen)
    from integrations.hermes_lily_cli import voice_providers

    monkeypatch.setattr(voice_providers, "provider_options", lambda runtime_config, kind: {"group_id": "unit-group"})
    return captured


def test_minimax_request_body_carries_the_emotion(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli.voice_providers import synthesize_minimax

    runtime = _minimax_runtime(tmp_path)
    captured = _capture_minimax_body(monkeypatch, runtime)

    audio = synthesize_minimax(runtime, "别难过，我在。", sample_rate=24000, timeout=3, emotion="sad")

    assert audio == b"\x01\x02"
    assert captured["body"]["voice_setting"]["emotion"] == "sad"
    assert captured["body"]["text"] == "别难过，我在。"


def test_minimax_request_body_omits_emotion_for_shy_and_neutral(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli.voice_providers import synthesize_minimax

    runtime = _minimax_runtime(tmp_path)
    captured = _capture_minimax_body(monkeypatch, runtime)

    synthesize_minimax(runtime, "你好", sample_rate=24000, timeout=3, emotion="shy")
    assert "emotion" not in captured["body"]["voice_setting"]

    synthesize_minimax(runtime, "你好", sample_rate=24000, timeout=3)
    assert "emotion" not in captured["body"]["voice_setting"]


def test_synthesize_tts_chunk_threads_the_emotion_to_the_provider(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_provider(runtime_config, text, *, sample_rate, timeout, emotion=""):
        seen.update({"text": text, "sample_rate": sample_rate, "timeout": timeout, "emotion": emotion})
        return b"pcm"

    monkeypatch.setattr(gateway_module, "synthesize_minimax", fake_provider)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    runtime = _minimax_runtime(tmp_path)
    result = gateway_module.synthesize_tts(runtime, "我升职啦！", "excited")

    assert result.ok is True
    assert seen["emotion"] == "excited"
    assert seen["text"] == "我升职啦！"


def test_synthesize_tts_chunk_ignores_emotion_for_other_providers(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_provider(runtime_config, text, *, sample_rate, timeout, emotion=""):
        seen.update({"text": text, "emotion": emotion})
        return b"pcm"

    monkeypatch.setattr(gateway_module, "synthesize_baidu", fake_provider)
    runtime = replace(_minimax_runtime(tmp_path), tts_provider="baidu")
    result = gateway_module.synthesize_tts(runtime, "你好", "sad")

    assert result.ok is True
    assert seen["emotion"] == "sad"


def test_volcengine_request_body_never_carries_the_emotion(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli.voice_providers import synthesize_volcengine

    runtime = replace(_minimax_runtime(tmp_path), tts_provider="volcengine")
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(json.dumps({"data": "0102"}).encode("utf-8"))

    monkeypatch.setattr("integrations.hermes_lily_cli.voice_providers.request.urlopen", fake_urlopen)
    from integrations.hermes_lily_cli import voice_providers

    monkeypatch.setattr(
        voice_providers,
        "provider_options",
        lambda runtime_config, kind: {"appid": "app", "cluster": "cluster", "access_token": "token", "voice_type": "voice"},
    )
    synthesize_volcengine(runtime, "别难过。", sample_rate=24000, timeout=3, emotion="sad")

    assert "emotion" not in captured["body"]["audio"]
    assert "emotion" not in captured["body"]["request"]


def test_log_tts_emotion_only_logs_volcengine(monkeypatch, capsys):
    logged: list[tuple[str, dict]] = []

    def fake_log(message, **fields):
        logged.append((message, fields))

    monkeypatch.setattr(gateway_module, "log_gateway", fake_log)
    gateway_module.log_tts_emotion("volcengine", "sad")
    gateway_module.log_tts_emotion("minimax", "sad")
    gateway_module.log_tts_emotion("volcengine", "")

    assert len(logged) == 1
    message, fields = logged[0]
    assert message == "tts_emotion_debug_only"
    assert fields["provider"] == "volcengine"
    assert fields["sent"] is False
    assert fields["reason"] == "volcengine_emotion_field_unconfirmed"
    assert fields["emotion"] == "sad"


# ── 回归：任何路径都看不到 <emo: ─────────────────────────────
def test_stream_never_leaks_the_emotion_tag_to_subtitles_or_history(tmp_path, monkeypatch):
    """端到端：假 LLM 流返回 ``<emo:sad>我在`` + ``听。``。"""
    config, store, events = _stream_chunks(tmp_path, monkeypatch, ("<emo:sad>我在", "听。"))

    deltas = [str(event.get("text") or "") for event in events if event.get("type") == "delta"]
    assert deltas, "假流至少要吐出一个 delta"
    streamed = "".join(deltas)
    # 标签一个字都不剩；"我在" 是第一句，语音压缩可能停在这里，但绝不会多出标签。
    assert streamed.startswith("我在")
    assert "<emo:" not in streamed
    assert all("<emo:" not in delta for delta in deltas)

    emotion_events = [event for event in events if event.get("type") == "emotion"]
    assert len(emotion_events) == 1
    assert emotion_events[0]["emotion"] == "sad"
    assert emotion_events[0]["source"] == "llm"

    final = [event for event in events if event.get("type") == "final"][-1]
    payload = final["payload"]
    # 字幕/回复文本
    assert payload["response"].startswith("我在")
    assert "<emo:" not in payload["response"]
    assert payload["evidence"]["emotion"] == "sad"
    assert payload["evidence"]["emotion"] in EMOTIONS
    assert payload["debug"]["emotion"]["emotion"] == "sad"
    assert payload["debug"]["emotion"]["source"] == "llm"

    # 消息历史
    messages = _aura_messages(store, config.scope)
    aura_bodies = [str(item.get("body") or "") for item in messages if item.get("direction") == "aura"]
    assert aura_bodies
    assert all("<emo:" not in body for body in aura_bodies)
    assert aura_bodies[-1].startswith("我在")

    # 账本：难过 -3 心情，cause 带用户原话。
    ledger_events = [
        event
        for event in _all_ledger_events(config.companion_db_path, config.scope)
        if event["event_type"] == "ledger.emotion"
    ]
    assert len(ledger_events) == 1
    # 隐藏驱动可能顺手把待记账漂移补上，这里只关心心情增量。
    assert ledger_events[0]["delta"]["mood"] == -3
    assert ledger_events[0]["description"] == "情绪：难过（用户：我的猫走丢了）"
    assert ledger_events[0]["payload"]["source"] == "emotion"
    assert ledger_events[0]["event_key"] == f"emotion:{payload['request_id']}"


def test_stream_strips_a_tag_split_across_chunks(tmp_path, monkeypatch):
    config, store, events = _stream_chunks(
        tmp_path,
        monkeypatch,
        ("<em", "o:hap", "py>我今天", "升职啦！"),
    )

    deltas = [str(event.get("text") or "") for event in events if event.get("type") == "delta"]
    assert "".join(deltas).startswith("我今天升职")
    assert all("<emo:" not in delta for delta in deltas)

    final = [event for event in events if event.get("type") == "final"][-1]
    payload = final["payload"]
    assert payload["evidence"]["emotion"] == "happy"
    assert payload["debug"]["emotion"]["source"] == "llm"


def test_stream_falls_back_to_rules_when_the_llm_label_is_invalid(tmp_path, monkeypatch):
    config, store, events = _stream_chunks(
        tmp_path,
        monkeypatch,
        ("<emo:angry>怎么", "这样啊"),
        user_text="我的猫走丢了",
    )

    deltas = [str(event.get("text") or "") for event in events if event.get("type") == "delta"]
    assert "".join(deltas).startswith("怎么这样")
    assert all("<emo:" not in delta for delta in deltas)

    final = [event for event in events if event.get("type") == "final"][-1]
    emotion_facts = final["payload"]["debug"]["emotion"]
    # 非法标签剥掉后走规则兜底：用户说"我的猫走丢了" → sad。
    assert emotion_facts["source"] == "rule"
    assert emotion_facts["emotion"] == "sad"
    assert emotion_facts["raw"] == "angry"


def test_stream_keeps_working_when_the_llm_sends_no_tag_at_all(tmp_path, monkeypatch):
    config, store, events = _stream_chunks(tmp_path, monkeypatch, ("我在", "听。"))

    deltas = [str(event.get("text") or "") for event in events if event.get("type") == "delta"]
    assert "".join(deltas).startswith("我在")
    assert all("<emo:" not in delta for delta in deltas)
    assert [event for event in events if event.get("type") == "emotion"] == []

    final = [event for event in events if event.get("type") == "final"][-1]
    emotion_facts = final["payload"]["debug"]["emotion"]
    assert emotion_facts["source"] == "rule"
    assert emotion_facts["emotion"] == "sad"


def test_non_streaming_turn_never_leaks_the_emotion_tag(tmp_path, monkeypatch):
    """非流式路径：strip_emotion_tag 也要保证历史/回复干净。"""
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "<emo:worried>先别慌，我都在。"}}]},
                ensure_ascii=False,
            ).encode("utf-8")

    monkeypatch.setattr(
        "integrations.aura_persona_gateway.llm.urlopen",
        lambda req, timeout: FakeResponse(),
    )
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
        fast_reply_enabled=False,
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    result = gateway.run_turn("我明天要做手术", metadata={"source": "unit-test"})

    assert result.response == "先别慌，我都在。"
    assert "<emo:" not in result.response
    assert result.evidence["emotion"] == "worried"
    assert result.evidence["emotion"] in EMOTIONS

    messages = _aura_messages(store, config.scope)
    aura_bodies = [str(item.get("body") or "") for item in messages if item.get("direction") == "aura"]
    assert aura_bodies
    assert all("<emo:" not in body for body in aura_bodies)
    assert aura_bodies[-1] == "先别慌，我都在。"


def test_next_turn_prompt_carries_the_previous_emotion(tmp_path, monkeypatch):
    """E3：下一轮 prompt 里要有"你刚才的情绪：……"这一行。"""
    from integrations.aura_persona_gateway.context import build_persona_context
    from integrations.aura_persona_gateway.emotion_engine import emotion_prompt_line

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = _seed_state(store, config)

    _facts, updated = _record_emotion_turn(
        store=store,
        scope=config.scope,
        state=state,
        request_id="req-prompt",
        user_text="我的猫走丢了",
        reply_text="别难过。",
        llm_emotion="sad",
    )
    assert emotion_prompt_line(updated) == "你刚才的情绪：难过（因为用户：我的猫走丢了）"

    from integrations.aura_persona_gateway.assets import load_persona_assets
    from integrations.aura_persona_gateway.outlets import OutletSignals
    from integrations.aura_persona_gateway.query_context import resolve_query_context

    context = build_persona_context(
        user_text="今天几号",
        config=config,
        assets=load_persona_assets(config),
        state=updated,
        recent_messages=[],
        latest_moment=None,
        today_plan=[],
        query_context=resolve_query_context("今天几号", aura_home_city="上海"),
        outlet_signals=OutletSignals(proactive_message={}, spending={}),
    )
    assert "你刚才的情绪：难过（因为用户：我的猫走丢了）" in context.prompt
    assert "<emo:" in context.prompt  # 自评指令
    assert "previous_emotion" in context.debug["context_sections"]


# ── 回归：流式网关路径 ───────────────────────────────────────
def _gateway_module():
    from integrations.hermes_lily_cli import gateway as gateway_module

    return gateway_module


def test_bridge_stream_carries_the_emotion_into_the_dialogue_payload(tmp_path, monkeypatch):
    """流式路径：emotion 事件 + evidence 都要落到 dialogue.payload.emotion。"""
    import asyncio
    from dataclasses import replace

    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    gateway_module = _gateway_module()
    sent: list[Any] = []
    synth: list[tuple[str, str]] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "emotion", "emotion": "sad", "source": "llm"}
        yield {"type": "delta", "text": "别难过，我"}
        yield {"type": "delta", "text": "都在。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "别难过，我都在。",
                "request_id": "req-stream-1",
                "evidence": {"streamed": True, "emotion": "sad"},
                "voice_turn": {"emotion": "warm"},
            },
        }

    def fake_synthesize_tts(runtime_config, text, emotion=""):
        synth.append((text, emotion))
        from integrations.hermes_lily_cli.gateway import TtsResult

        return TtsResult(ok=True, audio=b"pcm", chunk_count=1, latency_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="minimax",
        tts_model="speech-2.6-hd",
        tts_voice="female-shaonv",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=gateway_module.DEVICE_SAMPLE_RATE,
    )
    config = gateway_module.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = gateway_module.TurnState(turn_id=31, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我的猫走丢了",
    ))

    assert streamed is True
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogues = [item for item in messages if item.get("type") == "dialogue"]
    assert len(dialogues) == 1
    payload = dialogues[0]["payload"]
    assert payload["emotion"] == "sad"
    assert payload["emotion"] in EMOTIONS
    # 姿势：E1 情绪里只有 happy/excited 有对应姿势，sad 没有；
    # 这条流的 fastpath 还带着旧值 warm，按旧映射给 happy（生产里两者同源）。
    assert payload["pose"] == "happy"
    assert "<emo:" not in payload["text"]
    # TTS 每一段都带上同一个情绪。
    assert synth, "至少要合成一段 TTS"
    assert all(emotion == "sad" for _text, emotion in synth)


def test_bridge_stream_falls_back_to_neutral_without_any_emotion(tmp_path, monkeypatch):
    import asyncio

    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    gateway_module = _gateway_module()
    sent: list[Any] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "今天二十三号。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "今天二十三号。",
                "request_id": "req-stream-2",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(
        gateway_module,
        "synthesize_tts",
        lambda runtime_config, text, emotion="": gateway_module.TtsResult(ok=True, audio=b"pcm", chunk_count=1),
    )
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="minimax",
        tts_voice="female-shaonv",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=gateway_module.DEVICE_SAMPLE_RATE,
    )
    config = gateway_module.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = gateway_module.TurnState(turn_id=32, audio_chunks=[b"pcm"], asr_latency_ms=3)

    asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "今天几号",
    ))

    dialogues = [
        json.loads(item)
        for item in sent
        if isinstance(item, str) and json.loads(item).get("type") == "dialogue"
    ]
    assert len(dialogues) == 1
    payload = dialogues[0]["payload"]
    assert payload["emotion"] == "neutral"
    assert payload["emotion"] in EMOTIONS
    assert payload["pose"] is None


def test_bridge_stream_strips_a_leaked_emotion_tag_from_deltas(tmp_path, monkeypatch):
    """上游万一漏剥标签，网关也不能让它进字幕和 TTS。"""
    import asyncio

    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    gateway_module = _gateway_module()
    sent: list[Any] = []
    synth: list[str] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        # 故意发一个没剥干净的流：标签 + 正文。
        yield {"type": "delta", "text": "<emo:pouty>你好笨"}
        yield {"type": "delta", "text": "呀。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "你好笨呀。",
                "request_id": "req-stream-3",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text, emotion=""):
        synth.append(text)
        return gateway_module.TtsResult(ok=True, audio=b"pcm", chunk_count=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="minimax",
        tts_voice="female-shaonv",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=gateway_module.DEVICE_SAMPLE_RATE,
    )
    config = gateway_module.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = gateway_module.TurnState(turn_id=33, audio_chunks=[b"pcm"], asr_latency_ms=3)

    asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好笨啊",
    ))

    assert synth, "至少要合成一段 TTS"
    assert all("<emo:" not in text for text in synth)
    dialogues = [
        json.loads(item)
        for item in sent
        if isinstance(item, str) and json.loads(item).get("type") == "dialogue"
    ]
    assert len(dialogues) == 1
    payload = dialogues[0]["payload"]
    assert "<emo:" not in payload["text"]
    assert all("<emo:" not in segment for segment in payload["segments"])


# ── 审查补丁：惯性必须落到设备脸和语气上；StepFun 行内语气 ─────────
def test_stream_early_emotion_event_already_applies_inertia(tmp_path, monkeypatch):
    """上一轮还难过，用户一句普通话，模型自评 happy：提前推给网关的也得是 neutral。"""
    _config_, _store_, events = _stream_chunks(
        tmp_path,
        monkeypatch,
        ("<emo:happy>下午", "想去公园走走。"),
        user_text="你下午打算做点什么",
        prior_emotion={"emotion": "sad", "label": "难过", "intensity": 3, "cause": "用户：我的猫走丢了", "at": time.time()},
    )
    emotion_events = [event for event in events if event.get("type") == "emotion"]
    assert [event["emotion"] for event in emotion_events] == ["neutral"]
    final = [event for event in events if event.get("type") == "final"][-1]
    assert final["payload"]["evidence"]["emotion"] == "neutral"


def test_bridge_stream_dialogue_emotion_prefers_the_final_evidence(tmp_path, monkeypatch):
    """提前事件只管 TTS 语气；设备脸以 final evidence（调制+惯性后）为准。"""
    import asyncio

    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    gateway_module = _gateway_module()
    sent: list[Any] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "emotion", "emotion": "happy", "source": "llm"}
        yield {"type": "delta", "text": "嗯，我在。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "嗯，我在。",
                "request_id": "req-stream-inertia",
                "evidence": {"streamed": True, "emotion": "neutral"},
            },
        }

    def fake_synthesize_tts(runtime_config, text, emotion=""):
        from integrations.hermes_lily_cli.gateway import TtsResult

        return TtsResult(ok=True, audio=b"pcm", chunk_count=1, latency_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="minimax",
        tts_model="speech-2.6-hd",
        tts_voice="female-shaonv",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=gateway_module.DEVICE_SAMPLE_RATE,
    )
    config = gateway_module.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = gateway_module.TurnState(turn_id=32, audio_chunks=[b"pcm"], asr_latency_ms=3)

    assert asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(FakeWebsocket(), config, runtime, state, "嗯"))
    dialogues = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogues = [item for item in dialogues if item.get("type") == "dialogue"]
    assert dialogues[-1]["payload"]["emotion"] == "neutral"


def test_stepfun_emotion_text_uses_inline_context_for_supported_models():
    from types import SimpleNamespace

    gateway_module = _gateway_module()
    stepfun = SimpleNamespace(tts_provider="stepfun", tts_model="stepaudio-2.5-tts")
    assert gateway_module.stepfun_emotion_text(stepfun, "我在听。", "sad").startswith("（难过")
    assert gateway_module.stepfun_emotion_text(stepfun, "我在听。", "sad").endswith("）我在听。")
    # neutral / 未知 / 空文本不加
    assert gateway_module.stepfun_emotion_text(stepfun, "我在听。", "neutral") == "我在听。"
    assert gateway_module.stepfun_emotion_text(stepfun, "我在听。", "") == "我在听。"
    assert gateway_module.stepfun_emotion_text(stepfun, "", "sad") == ""
    # 不支持行内语气的模型/服务商原样返回
    old_model = SimpleNamespace(tts_provider="stepfun", tts_model="step-tts-mini")
    assert gateway_module.stepfun_emotion_text(old_model, "我在听。", "sad") == "我在听。"
    minimax = SimpleNamespace(tts_provider="minimax", tts_model="stepaudio-2.5-tts")
    assert gateway_module.stepfun_emotion_text(minimax, "我在听。", "sad") == "我在听。"
    for emotion in EMOTIONS:
        hinted = gateway_module.stepfun_emotion_text(stepfun, "好", emotion)
        assert len(hinted) - 1 <= 40


def test_stepfun_ws_session_send_text_prefixes_the_emotion():
    import asyncio
    from types import SimpleNamespace

    gateway_module = _gateway_module()
    runtime = SimpleNamespace(
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_base_url="https://api.stepfun.com/v1",
        tts_api_key="unit-key",
        tts_timeout_seconds=5,
    )
    sent: list[dict] = []

    class FakeStepWs:
        async def send(self, payload):
            sent.append(json.loads(payload))

    session = gateway_module.StepfunWsTtsSession(None, runtime, 1, stream_id=1, started=0.0)
    session._step_ws = FakeStepWs()
    session.session_id = "sid"
    session.emotion = "worried"
    asyncio.run(session.send_text("明天手术加油。"))
    deltas = [item for item in sent if item["type"] == "tts.text.delta"]
    assert deltas[0]["data"]["text"].startswith("（担心")
    assert deltas[0]["data"]["text"].endswith("明天手术加油。")
