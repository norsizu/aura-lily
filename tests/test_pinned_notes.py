"""X3 语音便签板：指令识别、3 格上限、设备宽度裁剪、后台结论自动钉、下发通道。

覆盖任务书 X3 的要求：
- 语音"记在便签上…/帮我记一下：…/记到黑板上…/贴个便签…"钉上，"清空便签/把便签删了"清空；
- 便签最多 3 条、新的在前、最旧的被挤掉；
- 文本按固件两行 × 118px 宽度裁（约 14 个汉字），并受 63 字节上限约束；
- 后台任务完成自动钉一条结论（快模型压缩失败时取第一句兜底），agent_create 只钉"《标题》已完成"；
- ``status_update.payload.notes`` 提醒在前、便签在后，合计 ≤3；
- 每次钉/清都留一条 ``source="note"`` 的账本流水；
- 下发给设备的便签里不出现 ``<emo:`` 之类标记。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.pinned_notes import (
    INFO_BOARD_NOTE_LIMIT,
    NOTE_MAX_BYTES,
    NOTE_MAX_WIDTH,
    NOTE_SLOT_LIMIT,
    PIN_SOURCE_AGENT,
    PIN_SOURCE_VOICE,
    agent_note_text,
    bump_note_signal,
    clear_pinned_notes,
    clip_note_text,
    merge_info_board_notes,
    note_command_reply,
    note_display_width,
    note_signal_path,
    parse_note_command,
    pin_note,
    read_note_signal,
    read_pinned_notes,
    record_note_ledger,
    write_pinned_notes,
)
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.voice_turn import BackgroundTask, VoiceTurnVerdict, execute_voice_turn


# ── 测试辅助 ────────────────────────────────────────────────
def _config(tmp_path: Path) -> PersonaGatewayConfig:
    persona_home = tmp_path / "persona-home"
    companion_home = tmp_path / "companion-home"
    (persona_home / "persona").mkdir(parents=True, exist_ok=True)
    (persona_home / "persona" / "soul.md").write_text("主 soul\n", encoding="utf-8")
    return PersonaGatewayConfig(
        enabled=True,
        persona_home=str(persona_home),
        companion_home=str(companion_home),
        hermes_home=str(tmp_path / "hermes-home"),
    )


def _state() -> dict:
    return {"mood": 60, "energy": 80, "satiety": 60, "stress": 10, "trust": 50, "beans": 50, "metadata": {}}


def _task(**kwargs) -> BackgroundTask:
    base = {
        "task_id": "bg-unit-1",
        "task_kind": "agent_lookup",
        "source_text": "帮我查一下明天北京的天气",
        "fallback_text": "这件事没处理完，我在后台留了记录。",
    }
    base.update(kwargs)
    return BackgroundTask(**base)


# ── 指令识别 ────────────────────────────────────────────────
PIN_CASES = (
    ("记在便签上，买牛奶", "买牛奶"),
    ("帮我记一下：买牛奶", "买牛奶"),
    ("记到黑板上，下午三点开会", "下午三点开会"),
    ("贴个便签，带伞", "带伞"),
    ("买牛奶记在便签上", "买牛奶"),
    ("帮我写个便签：取快递", "取快递"),
)

CLEAR_CASES = ("清空便签", "把便签删了", "便签都擦掉", "删除便签")

NOT_A_COMMAND = (
    "今天天气怎么样",
    "我记得你",
    "随便记点什么吧",
    "帮我查一下明天天气",
    "删除这条消息",
)


def test_parse_note_command_recognises_pin_phrasings():
    for text, expected in PIN_CASES:
        command = parse_note_command(text)
        assert command is not None, text
        assert command["action"] == "pin", text
        assert command["text"] == expected, (text, command)


def test_parse_note_command_recognises_clear_phrasings():
    for text in CLEAR_CASES:
        command = parse_note_command(text)
        assert command is not None, text
        assert command["action"] == "clear", text


def test_parse_note_command_ignores_ordinary_chat():
    for text in NOT_A_COMMAND:
        assert parse_note_command(text) is None, text


def test_note_command_replies_are_short_and_confident():
    assert "便签" in note_command_reply({"action": "pin"})
    assert "便签" in note_command_reply({"action": "clear"})
    assert note_command_reply({"action": "pin"}) != note_command_reply({"action": "clear"})


def test_voice_turn_pins_without_calling_the_model(tmp_path):
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    def explode(*args, **kwargs):  # pragma: no cover - 不能被调用
        raise AssertionError("便签指令是规则快答，不该碰模型")

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        fast_reply_enabled=False,
        fast_reply_mode="local_rule",
    )
    result = execute_voice_turn("记在便签上，买牛奶", runtime_config=runtime)

    assert result.verdict == VoiceTurnVerdict.SPEAK_NOW
    assert result.debug["decision_path"] == "note_pin"
    assert result.debug["pinned_note"] == {"action": "pin", "text": "买牛奶"}
    assert result.speak_text
    assert result.background_task is None
    del explode


# ── 3 格便签板 ──────────────────────────────────────────────
def test_pinned_notes_cap_at_three_and_evict_the_oldest():
    state = _state()
    pin_note(state, "第一条", at=1000.0)
    pin_note(state, "第二条", at=2000.0)
    notes = pin_note(state, "第三条", at=3000.0)
    assert notes is not None

    evicted = read_pinned_notes(state)
    assert [note["text"] for note in evicted] == ["第三条", "第二条", "第一条"]

    pin_note(state, "第四条", at=4000.0)
    final = read_pinned_notes(state)
    assert len(final) == NOTE_SLOT_LIMIT
    assert [note["text"] for note in final] == ["第四条", "第三条", "第二条"]
    assert all(note["text"] != "第一条" for note in final)


def test_pin_same_text_twice_refreshes_instead_of_duplicating():
    state = _state()
    pin_note(state, "买牛奶", at=1000.0)
    pin_note(state, "带伞", at=2000.0)
    pin_note(state, "买牛奶", at=3000.0)

    notes = read_pinned_notes(state)
    assert [note["text"] for note in notes] == ["买牛奶", "带伞"]


def test_write_pinned_notes_normalises_source_and_time():
    state = _state()
    write_pinned_notes(state, [
        {"text": "甲", "source": "agent", "at": 100.0},
        {"text": "乙", "at": 80.0},
        {"text": "丙", "at": 50.0},
        "裸字符串不算一条",
        None,
    ])

    notes = read_pinned_notes(state)
    assert [note["text"] for note in notes] == ["甲", "乙", "丙"]
    assert notes[0]["source"] == PIN_SOURCE_AGENT
    assert notes[1]["source"] == PIN_SOURCE_VOICE  # 缺 source 时按"你说的"算


def test_clear_pinned_notes_returns_what_was_wiped():
    state = _state()
    pin_note(state, "甲", at=100.0)
    pin_note(state, "乙", at=200.0)

    removed = clear_pinned_notes(state)

    assert [note["text"] for note in removed] == ["乙", "甲"]
    assert read_pinned_notes(state) == []


# ── 设备宽度 ────────────────────────────────────────────────
def test_note_width_matches_firmware_font_metrics():
    # 固件 font_utf8_width：ASCII 9px、其它 17px，总和减 1。
    assert note_display_width("abc") == 9 * 3 - 1
    assert note_display_width("你好") == 17 * 2 - 1
    assert note_display_width("") == 0


def test_clip_note_text_fits_the_device_row():
    long_hanzi = "这是一条很长很长的便签内容肯定放不下"
    clipped = clip_note_text(long_hanzi)

    assert clipped.endswith("…")
    assert note_display_width(clipped) <= NOTE_MAX_WIDTH
    assert len(clipped.encode("utf-8")) <= NOTE_MAX_BYTES
    assert len(clipped) < len(long_hanzi)
    # 固件两行各 118px：14 个汉字刚好放满，所以补"…"时只能留 13 个。
    assert note_display_width("一二三四五六七" * 2) == NOTE_MAX_WIDTH
    assert clipped == "这是一条很长很长的便签内容…"
    assert len(clipped) == 14


def test_clip_note_text_keeps_short_notes_untouched():
    assert clip_note_text("买牛奶") == "买牛奶"
    assert clip_note_text(" 买  牛奶 ") == "买 牛奶"
    assert clip_note_text("") == ""
    assert clip_note_text(None) == ""


def test_pinned_notes_are_clipped_before_they_reach_the_device():
    state = _state()
    pin_note(state, "一二三四五六七八九十一二三四五六七八九十", at=100.0)

    note = read_pinned_notes(state)[0]
    assert note_display_width(note["text"]) <= NOTE_MAX_WIDTH
    assert len(note["text"].encode("utf-8")) <= NOTE_MAX_BYTES


# ── 后台结论自动钉 ──────────────────────────────────────────
def test_agent_note_text_prefers_the_fast_model_summary():
    text = agent_note_text(
        "北京明天小雨，12 到 18 摄氏度，出门记得带伞。这是第二句。",
        task_kind="agent_lookup",
        summarize=lambda body: "明天小雨，带伞",
    )

    assert text == "明天小雨，带伞"
    assert note_display_width(text) <= NOTE_MAX_WIDTH


def test_agent_note_text_falls_back_to_the_first_sentence():
    text = agent_note_text(
        "北京明天小雨，12 到 18 摄氏度，出门记得带伞。后面还有很长很长的解释。",
        task_kind="agent_lookup",
    )

    assert text.startswith("北京明天小雨")
    assert text.endswith("…")
    assert note_display_width(text) <= NOTE_MAX_WIDTH


def test_agent_note_text_keeps_a_short_first_sentence_whole():
    text = agent_note_text("明天小雨。后面还有很长很长的解释。", task_kind="agent_lookup")

    assert text == "明天小雨"


def test_agent_note_text_falls_back_when_the_summary_explodes():
    def boom(body):  # pragma: no cover - 只在异常路径里被调用
        raise RuntimeError("快模型挂了")

    text = agent_note_text("查完了。结论在第二句。", task_kind="agent_lookup", summarize=boom)

    assert text == "查完了"


def test_agent_create_only_pins_the_title():
    text = agent_note_text(
        "文档写好了，全文三千字……",
        task_kind="agent_create",
        title="下周去上海和杭州出差的行程安排",
    )

    # 便签格只有两行，标题长了也只能裁着放——不裁就会被固件截得更难看。
    assert text.startswith("《")
    assert text.endswith("…")
    assert "下周去上海" in text
    assert note_display_width(text) <= NOTE_MAX_WIDTH


def test_agent_create_pins_a_short_title_whole():
    text = agent_note_text("文档写好了。", task_kind="agent_create", title="周报")

    assert text == "《周报》已完成"


def test_agent_note_text_returns_empty_for_empty_result():
    assert agent_note_text("", task_kind="agent_lookup") == ""
    assert agent_note_text("   ", task_kind="agent_lookup") == ""
    assert agent_note_text("有内容", task_kind="agent_create", title="") == ""


# ── 下发通道 ────────────────────────────────────────────────
def test_merge_info_board_notes_puts_reminders_first_and_caps_at_three():
    merged = merge_info_board_notes(
        ["10/23 14:20 明天上午的事项"],
        [{"text": "买牛奶", "source": "voice"}, {"text": "带伞", "source": "voice"}, {"text": "取快递"}],
    )

    assert merged == ["10/23 14:20 明天上午的事项", "买牛奶", "带伞"]
    assert len(merged) <= INFO_BOARD_NOTE_LIMIT


def test_merge_info_board_notes_dedupes_repeated_text():
    merged = merge_info_board_notes(
        ["买牛奶"],
        [{"text": "买牛奶"}, {"text": "带伞"}],
    )

    assert merged == ["买牛奶", "带伞"]


def test_note_signal_round_trip(tmp_path):
    config = _config(tmp_path)

    assert read_note_signal(config) == 0.0
    bump_note_signal(config, at=1000.0)
    first = read_note_signal(config)

    assert first > 0
    assert note_signal_path(config).exists()
    # revision 单调递增，网关据此判断"变没过"。
    bump_note_signal(config, at=2000.0)
    assert read_note_signal(config) > first


def test_note_signal_survives_a_garbage_file(tmp_path):
    config = _config(tmp_path)
    path = note_signal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all", encoding="utf-8")

    assert read_note_signal(config) == 0.0


def test_record_note_ledger_writes_a_traceable_row(tmp_path):
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())

    facts = record_note_ledger(
        store, config.scope, state, action="pin", source=PIN_SOURCE_VOICE, text="买牛奶", at=1000.0,
    )
    assert facts and facts["event_key"]

    events = store.list_recent_life_events(config.scope, event_prefixes=("ledger.note",), limit=5)
    assert [event["event_key"] for event in events] == [facts["event_key"]]
    payload = events[0]["payload"]
    assert payload["source"] == "note"
    assert payload["note_source"] == PIN_SOURCE_VOICE
    assert payload["action"] == "pin"
    assert "买牛奶" in payload["cause"]
    # 便签不动数值：before/after/delta 全 0，只为在账本里留一行。
    assert payload["delta"] == {"mood": 0, "energy": 0, "satiety": 0, "stress": 0}
    assert payload["before"] == payload["after"]


# ── 端到端：一轮语音把便签钉下去 ────────────────────────────
def _voice_gateway(tmp_path: Path, monkeypatch, *, user_text: str):
    """规则快答链路（不碰模型），但保留真实 store 和 runtime 配置。"""
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
    from integrations.aura_persona_gateway.turn import AuraPersonaGateway
    from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        fast_reply_mode="local_rule",
        # 空 mode：_run_aura_model 只会走注入的 bridge，不会真的起 hermes 子进程。
        aura_model_mode="",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)
    return config, store, gateway, user_text


def test_voice_turn_pins_a_note_end_to_end(tmp_path, monkeypatch):
    config, store, gateway, user_text = _voice_gateway(tmp_path, monkeypatch, user_text="记在便签上，买牛奶")

    result = gateway.run_turn(user_text, metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.ok is True
    assert result.evidence["pinned_note"]["action"] == "pin"
    assert result.evidence["pinned_note"]["text"] == "买牛奶"
    assert result.debug["voice_turn"]["debug"]["decision_path"] == "note_pin"

    saved = store.get_or_create_state(config.scope)
    assert [note["text"] for note in read_pinned_notes(saved)] == ["买牛奶"]

    events = store.list_recent_life_events(config.scope, event_prefixes=("ledger.note",), limit=5)
    assert events, "每次钉便签都要在账本里留一行"
    assert events[0]["payload"]["source"] == "note"
    # 立刻推 status_update 的信号也拨了。
    assert read_note_signal(config) > 0


def test_voice_turn_clears_notes_end_to_end(tmp_path, monkeypatch):
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="记在便签上，买牛奶")
    gateway.run_turn("记在便签上，买牛奶", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    result = gateway.run_turn("清空便签", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    assert result.evidence["pinned_note"]["action"] == "clear"
    saved = store.get_or_create_state(config.scope)
    assert read_pinned_notes(saved) == []
    events = store.list_recent_life_events(config.scope, event_prefixes=("ledger.note",), limit=5)
    assert any(event["payload"]["action"] == "clear" for event in events)


def test_voice_turn_keeps_the_previous_note_when_the_new_one_evicts(tmp_path, monkeypatch):
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")
    for text in ("记在便签上，甲", "记在便签上，乙", "记在便签上，丙", "记在便签上，丁"):
        gateway.run_turn(text, metadata={"source": "aura-lily-gateway", "audio_bytes": 1234})

    saved = store.get_or_create_state(config.scope)
    assert [note["text"] for note in read_pinned_notes(saved)] == ["丁", "丙", "乙"]


def test_streaming_voice_turn_pins_a_note(tmp_path, monkeypatch):
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
    from integrations.aura_persona_gateway.turn import AuraPersonaGateway
    from integrations.hermes_lily_cli.bridge import HermesLilyBridge, HermesLilyConfig

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())
    store.save_state(config.scope, state)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="base", model="base-model"))
    runtime = AuraRuntimeConfig(
        persona_home=config.persona_home,
        fast_reply_enabled=True,
        fast_reply_mode="local_rule",
        aura_model_mode="",
    )
    gateway = AuraPersonaGateway(config=config, store=store, bridge=bridge, runtime_config=runtime)

    events = list(
        gateway.run_direct_turn_stream(
            "贴个便签，带伞", metadata={"source": "aura-lily-gateway", "audio_bytes": 1234}
        )
    )
    final = events[-1]["payload"]

    assert final["evidence"]["pinned_note"]["text"] == "带伞"
    saved = store.get_or_create_state(config.scope)
    assert [note["text"] for note in read_pinned_notes(saved)] == ["带伞"]
    assert read_note_signal(config) > 0


def _fake_model_bridge(response: str):
    """假快模型：只回一句压缩好的便签文案，并记下被叫过几次。"""
    from integrations.hermes_lily_cli.bridge import HermesLilyConfig, HermesLilyResult

    class FakeBridge:
        config = HermesLilyConfig(command=("hermes",))

        def __init__(self):
            self.calls = []

        def run(self, goal, *, metadata=None):
            self.calls.append({"goal": goal, "metadata": dict(metadata or {})})
            return HermesLilyResult(
                ok=True,
                status="completed",
                response=response,
                request_id="bg-unit",
                latency_ms=9,
                evidence={"route": "hermes_agent"},
            )

    return FakeBridge()


def test_agent_result_auto_pins_a_note(tmp_path, monkeypatch):
    """后台任务跑完自动钉一条结论，用的正是快模型压出来的那句。"""
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")
    bridge = _fake_model_bridge("明天小雨，带伞")
    gateway.bridge = bridge

    facts = gateway._auto_pin_agent_note(
        task=_task(),
        result_text="后台查完了：北京明天小雨，12 到 18 摄氏度。带伞更稳。",
        state=store.get_or_create_state(config.scope),
        request_id="persona-unit",
    )

    assert facts is not None
    assert facts["source"] == PIN_SOURCE_AGENT
    assert facts["text"] == "明天小雨，带伞"
    assert note_display_width(facts["text"]) <= NOTE_MAX_WIDTH
    assert bridge.calls, "agent_lookup 应该用快模型压一句结论"

    saved = store.get_or_create_state(config.scope)
    assert [note["text"] for note in read_pinned_notes(saved)] == ["明天小雨，带伞"]
    events = store.list_recent_life_events(config.scope, event_prefixes=("ledger.note",), limit=5)
    assert events[0]["payload"]["note_source"] == PIN_SOURCE_AGENT
    assert read_note_signal(config) > 0


def test_agent_result_falls_back_to_the_first_sentence(tmp_path, monkeypatch):
    """快模型压不出东西（返回空）时，退回任务结果的第一句。"""
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")
    gateway.bridge = _fake_model_bridge("   ")

    facts = gateway._auto_pin_agent_note(
        task=_task(),
        result_text="明天小雨。后面还有很长很长的解释和免责声明。",
        state=store.get_or_create_state(config.scope),
        request_id="persona-unit",
    )

    assert facts["text"] == "明天小雨"


def test_agent_result_pins_nothing_when_the_result_is_empty(tmp_path, monkeypatch):
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")

    facts = gateway._auto_pin_agent_note(
        task=_task(),
        result_text="   ",
        state=store.get_or_create_state(config.scope),
        request_id="persona-unit",
    )

    assert facts is None
    saved = store.get_or_create_state(config.scope)
    assert read_pinned_notes(saved) == []


def test_agent_note_uses_the_fast_model_when_configured(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli.bridge import HermesLilyConfig

    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")
    gateway.bridge.config = HermesLilyConfig(command=("hermes",), provider="base", model="base-model")
    captured: dict[str, object] = {}

    class _Result:
        response = "明天小雨，带伞"

    def fake_model(prompt, *, metadata=None, timeout_seconds=None):
        captured["prompt"] = prompt
        captured["timeout"] = timeout_seconds
        return _Result()

    monkeypatch.setattr(gateway, "_run_aura_model", fake_model)

    facts = gateway._auto_pin_agent_note(
        task=_task(),
        result_text="后台查完了：北京明天小雨，12 到 18 摄氏度，出门记得带伞。",
        state=store.get_or_create_state(config.scope),
        request_id="persona-unit",
    )

    assert facts["text"] == "明天小雨，带伞"
    assert "便签" in str(captured["prompt"])
    # 压缩便签最多等 5 秒，压不出来比播得慢更重要。
    assert float(captured["timeout"]) <= 5.0


def test_agent_create_result_pins_only_the_title(tmp_path, monkeypatch):
    config, store, gateway, _ = _voice_gateway(tmp_path, monkeypatch, user_text="")

    facts = gateway._auto_pin_agent_note(
        task=_task(task_kind="agent_create", source_text="帮我做一份《下周去上海和杭州出差的行程安排》的文档"),
        result_text="文档写好了，全文三千字，第一章是……",
        state=store.get_or_create_state(config.scope),
        request_id="persona-unit",
    )

    assert facts["text"].startswith("《")
    assert facts["text"].endswith("…")
    assert note_display_width(facts["text"]) <= NOTE_MAX_WIDTH


def test_note_signal_makes_the_gateway_push_status_update_right_away(tmp_path, monkeypatch):
    """钉完便签要"立刻"下发：bridge 拨一下信号文件，网关 1 秒内推一次。"""
    import asyncio

    from integrations.hermes_lily_cli import gateway as gateway_module

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())
    store.save_state(config.scope, state)
    monkeypatch.setenv("AURA_PERSONA_HOME", config.persona_home)
    monkeypatch.setenv("AURA_COMPANION_HOME", config.companion_home)
    monkeypatch.setattr(gateway_module, "WORLD_SYNC_POLL_INTERVAL_SECONDS", 3600.0)

    sent: list[dict] = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(json.loads(payload))

    async def scenario():
        task = asyncio.create_task(gateway_module.poll_world_status_updates(FakeWebsocket(), None))
        await asyncio.sleep(0.05)
        # 这就是 bridge 钉便签时做的事：写 metadata + 拨信号。
        pinned_state = store.get_or_create_state(config.scope)
        pin_note(pinned_state, "买牛奶", at=time.time())
        store.save_state(config.scope, pinned_state)
        bump_note_signal(config)
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    pushes = [msg["payload"]["notes"] for msg in sent if msg.get("type") == "status_update"]
    assert pushes, "拨了信号就应该推一次 status_update"
    assert any("买牛奶" in note for notes in pushes for note in notes)


def test_status_update_carries_notes_for_the_device(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.hermes_lily_cli.gateway import companion_pinned_notes, status_update_payload
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())
    write_pinned_notes(state, [
        {"text": "买牛奶", "source": PIN_SOURCE_VOICE, "at": 3000.0},
        {"text": "带伞", "source": PIN_SOURCE_AGENT, "at": 2000.0},
    ])
    store.save_state(config.scope, state)

    monkeypatch.setenv("AURA_PERSONA_HOME", config.persona_home)
    monkeypatch.setenv("AURA_COMPANION_HOME", config.companion_home)

    assert [note["text"] for note in companion_pinned_notes()] == ["买牛奶", "带伞"]

    payload = status_update_payload(AuraRuntimeConfig(persona_home=config.persona_home))

    # 没有提醒时就只有便签，顺序按时间新的在前。
    assert payload["notes"] == ["买牛奶", "带伞"]
    # 设备端协议：notes 是纯字符串，且不带任何情绪标记。
    assert all(isinstance(item, str) for item in payload["notes"])
    assert not any("<emo:" in item for item in payload["notes"])
    assert len(payload["notes"]) <= 3


def test_status_update_notes_survive_a_missing_db(tmp_path, monkeypatch):
    from integrations.hermes_lily_cli.gateway import companion_pinned_notes, status_update_payload
    from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig

    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "no-such-persona"))
    monkeypatch.setenv("AURA_COMPANION_HOME", str(tmp_path / "no-such-companion"))

    assert companion_pinned_notes() == []
    payload = status_update_payload(AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home")))
    assert payload["notes"] == []


def test_note_ledger_row_is_visible_on_the_trace_screen(tmp_path):
    """X1 大屏要能看到便签流水：类型是 ledger.note，标题能读懂。"""
    from integrations.aura_persona_gateway.trace_data import collect_showcase_trace

    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update(_state())
    record_note_ledger(
        store, config.scope, state, action="pin", source=PIN_SOURCE_VOICE, text="买牛奶", at=time.time(),
    )

    rows = [row for row in collect_showcase_trace(config.companion_db_path)["ledger"] if row["event_type"] == "ledger.note"]

    assert len(rows) == 1
    assert "买牛奶" in rows[0]["cause"]
    assert rows[0]["changes"] == []
