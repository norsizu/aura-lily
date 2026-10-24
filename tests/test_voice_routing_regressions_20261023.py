"""2026-10-23 实测回归：聊身体不跳天气、后台查询不续听、语音改休眠走设备回执。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from integrations.aura_persona_gateway.query_context import resolve_query_context
from integrations.aura_persona_gateway.voice_turn import parse_sleep_timeout_request
from integrations.hermes_lily_cli import gateway as gateway_module


def test_weather_words_in_small_talk_go_to_the_model():
    for text in ("今天天气有点冷，我好像感冒了。", "外面温度好低啊", "今天天气真好", "今晚有点冷，我好像感冒了"):
        assert resolve_query_context(text, aura_home_city="上海", user_home_city="上海").intent == "chat", text
    for text in ("今天天气怎么样", "北京天气", "帮我查一下明天北京的天气", "上海明天下雨吗？", "周六天气好不好", "几度了"):
        assert resolve_query_context(text, aura_home_city="上海", user_home_city="上海").intent.startswith("weather"), text


class FakeWebsocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, payload):
        if isinstance(payload, str):
            self.sent.append(json.loads(payload))

    def dialogues(self) -> list[dict[str, Any]]:
        return [item["payload"] for item in self.sent if item.get("type") == "dialogue"]


def test_deferred_reply_does_not_reopen_the_microphone():
    ws = FakeWebsocket()
    state = gateway_module.TurnState(turn_id=11)
    state.metadata = {"continuous_dialogue": True}

    async def scenario():
        await gateway_module.send_dialogue_and_tts(ws, None, state, "我去查一下。", ok=True, deferred=True)
        await gateway_module.send_dialogue_and_tts(ws, None, state, "好的。", ok=True)

    asyncio.run(scenario())
    deferred_payload, normal_payload = ws.dialogues()
    assert deferred_payload["continue_listening"] is False
    assert normal_payload["continue_listening"] is True


def test_pending_background_task_blocks_continuous_listening():
    state = gateway_module.TurnState(turn_id=12)
    state.metadata = {"continuous_dialogue": True}

    async def scenario():
        task = asyncio.create_task(asyncio.sleep(10))
        state.background_result_tasks.add(task)
        busy = gateway_module.continue_listening_for_device(state)
        task.cancel()
        return busy

    assert asyncio.run(scenario()) is False


def test_sleep_timeout_phrases():
    assert parse_sleep_timeout_request("呃，休眠时间调到 1 分钟") == 1
    assert parse_sleep_timeout_request("把休眠时间改成十五分钟") == 15
    assert parse_sleep_timeout_request("休眠时间改长一点") is None
    assert parse_sleep_timeout_request("如果把休眠改成5分钟会怎样") is None


def test_sleep_timeout_waits_for_device_receipt():
    ws = FakeWebsocket()
    state = gateway_module.TurnState(turn_id=13, boot_id="b1")
    state.supports_sleep_timeout = True

    async def device_replies():
        while not any(item.get("type") == "action" for item in ws.sent):
            await asyncio.sleep(0.01)
        action = next(item for item in ws.sent if item.get("type") == "action")
        assert action["action"] == "set_sleep_timeout"
        assert action["payload"]["minutes"] == 1
        gateway_module.receive_device_action_result(state, {
            "action_id": action["action_id"], "action": "set_sleep_timeout", "status": "ok", "code": "sleep_timeout_set",
        })

    async def scenario():
        replier = asyncio.create_task(device_replies())
        handled = await gateway_module.maybe_handle_sleep_timeout_request(ws, None, state, "休眠时间调到1分钟")
        await replier
        return handled

    assert asyncio.run(scenario()) is True
    assert ws.dialogues()[-1]["text"].startswith("休眠时间已改为1分钟")


def test_sleep_timeout_without_firmware_support_is_honest():
    ws = FakeWebsocket()
    state = gateway_module.TurnState(turn_id=14)

    assert asyncio.run(gateway_module.maybe_handle_sleep_timeout_request(ws, None, state, "休眠时间调到1分钟")) is True
    assert not any(item.get("type") == "action" for item in ws.sent)
    assert "不能说已经完成" in ws.dialogues()[-1]["text"]
    assert asyncio.run(gateway_module.maybe_handle_sleep_timeout_request(ws, None, state, "今天天气怎么样")) is False
