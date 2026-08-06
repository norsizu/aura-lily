import asyncio
import json

from integrations.aura_persona_gateway.runtime import AuraRuntimeConfig
from integrations.hermes_lily_cli import gateway
from integrations.hermes_lily_cli.local_audio import match_local_audio


def test_local_prompt_mapping_is_multilingual_and_exact():
    assert match_local_audio("我在。", "en") == ("I'm here.", "prompt_greeting_en")
    assert match_local_audio("I'm here.", "ja") == ("いるよ。", "prompt_greeting_ja")
    assert match_local_audio(
        "We've had a lovely chat, but this window is used up. Let's continue when it resets, okay?",
        "zh",
    ) == (
        "这段时间聊得很开心，不过次数用完啦。额度恢复后，我们再继续，好吗？",
        "prompt_quota_zh",
    )
    assert match_local_audio("普通的模型回答。", "en") is None


def test_non_stream_fixed_prompt_skips_tts_and_localizes(monkeypatch):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fail_tts(*args, **kwargs):
        raise AssertionError("fixed local prompt must not call TTS")

    monkeypatch.setattr(gateway, "synthesize_and_stream_tts", fail_tts)
    state = gateway.TurnState(
        turn_id=7,
        language="en",
        fw_version="0.16.9",
        supports_local_audio=True,
    )
    asyncio.run(gateway.send_dialogue_and_tts(
        FakeWebsocket(),
        AuraRuntimeConfig(),
        state,
        "我在。",
        ok=True,
    ))

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = next(item for item in messages if item.get("type") == "dialogue")
    assert dialogue["payload"]["text"] == "I'm here."
    assert dialogue["payload"]["local_audio_id"] == "prompt_greeting_en"
    assert not any(isinstance(item, bytes) for item in sent)


def test_stream_fixed_prompt_skips_tts(monkeypatch):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_voice_reply", "text": "我在。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在。",
                "request_id": "req-local-audio",
                "evidence": {"streamed": True, "model_skipped": True},
            },
        }

    def fail_tts(*args, **kwargs):
        raise AssertionError("fixed local prompt must not call TTS")

    monkeypatch.setattr(gateway, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway, "synthesize_tts", fail_tts)
    runtime = AuraRuntimeConfig(
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_base_url="http://tts.local/v1/audio/speech",
    )
    state = gateway.TurnState(
        turn_id=8,
        language="ja",
        fw_version="0.16.9",
        supports_local_audio=True,
        audio_chunks=[b"pcm"],
    )

    streamed = asyncio.run(gateway.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        gateway.GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn"),
        runtime,
        state,
        "テスト",
    ))

    assert streamed is True
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = next(item for item in messages if item.get("type") == "dialogue")
    assert dialogue["payload"]["text"] == "いるよ。"
    assert dialogue["payload"]["local_audio_id"] == "prompt_greeting_ja"
    assert not any(isinstance(item, bytes) for item in sent)
