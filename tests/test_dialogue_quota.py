from __future__ import annotations

from types import SimpleNamespace

from integrations.hermes_lily_cli.dialogue_quota import DialogueQuota
from integrations.hermes_lily_cli import quota as quota_module
from integrations.hermes_lily_cli import gateway as gateway_module
from integrations.hermes_lily_cli import server as server_module
from integrations.aura_persona_gateway.runtime import load_aura_runtime_config, save_aura_runtime_config


def test_dialogue_quota_window_starts_on_first_turn_and_resets_as_a_block(tmp_path):
    quota = DialogueQuota(tmp_path / "dialogue.sqlite3")

    initial = quota.snapshot(limit=2, window_seconds=100, now=1_000)
    assert initial["active"] is False
    assert initial["remaining"] == 2

    first = quota.consume(limit=2, window_seconds=100, now=1_000)
    assert first["allowed"] is True
    assert first["used"] == 1
    assert first["window_start"] == 1_000

    second = quota.consume(limit=2, window_seconds=100, now=1_001)
    assert second["allowed"] is True
    assert second["remaining"] == 0

    blocked = quota.consume(limit=2, window_seconds=100, now=1_050)
    assert blocked["allowed"] is False
    assert blocked["used"] == 2
    assert blocked["reset_at"] == 1_100

    # A new instance sees the same persistent window; it is not reset by a
    # process restart or by an unused portion of the old allowance.
    restarted = DialogueQuota(tmp_path / "dialogue.sqlite3")
    still_blocked = restarted.snapshot(limit=2, window_seconds=100, now=1_099)
    assert still_blocked["remaining"] == 0

    reset = restarted.snapshot(limit=2, window_seconds=100, now=1_100)
    assert reset["active"] is False
    assert reset["remaining"] == 2
    after_reset = restarted.consume(limit=2, window_seconds=100, now=1_100)
    assert after_reset["allowed"] is True
    assert after_reset["used"] == 1
    assert after_reset["window_start"] == 1_100


def test_dialogue_quota_limit_change_does_not_extend_window(tmp_path):
    quota = DialogueQuota(tmp_path / "dialogue.sqlite3")
    quota.consume(limit=3, window_seconds=100, now=2_000)
    quota.consume(limit=3, window_seconds=100, now=2_001)

    reduced = quota.snapshot(limit=1, window_seconds=100, now=2_002)
    assert reduced["used"] == 2
    assert reduced["remaining"] == 0
    denied = quota.consume(limit=1, window_seconds=100, now=2_003)
    assert denied["allowed"] is False
    assert denied["window_start"] == 2_000


def test_dialogue_quota_late_turn_does_not_slide_the_window(tmp_path):
    quota = DialogueQuota(tmp_path / "dialogue.sqlite3")

    first = quota.consume(limit=3, window_seconds=100, now=3_000)
    assert first["window_end"] == 3_100
    late = quota.consume(limit=3, window_seconds=100, now=3_099)
    assert late["window_start"] == 3_000
    assert late["window_end"] == 3_100

    # The window expires at its original end, regardless of the late turn.
    reset = quota.snapshot(limit=3, window_seconds=100, now=3_100)
    assert reset["active"] is False
    assert reset["remaining"] == 3


def test_kimi_api_key_normalizes_coding_plan_balance(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _limit):
            return b'{"limits":[{"detail":{"limit":100,"remaining":73,"resetTime":"later"}}],"usage":{"limit":1000,"remaining":640}}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(quota_module, "urlopen", fake_urlopen)
    result = quota_module.query_quota(
        provider="kimi",
        model="kimi-for-coding",
        base_url="https://api.kimi.com/coding/v1",
        api_key="kimi-secret",
    )

    assert result["ok"] is True
    assert result["primary"]["name"] == "five_hour"
    assert result["primary"]["remaining"] == 73
    assert result["primary"]["display"] == "5 小时 73/100 tokens"
    assert result["windows"][1]["name"] == "weekly_limit"
    assert captured["url"] == "https://api.kimi.com/coding/v1/usages"
    assert captured["authorization"] == "Bearer kimi-secret"


def test_dialogue_quota_limit_is_persisted_in_aura_runtime(tmp_path):
    config = load_aura_runtime_config(persona_home=str(tmp_path))
    assert config.dialogue_quota_limit == 50
    saved = save_aura_runtime_config(config, {"dialogue_quota_limit": 73})
    assert saved.dialogue_quota_limit == 73
    assert load_aura_runtime_config(persona_home=str(tmp_path)).dialogue_quota_limit == 73


def test_server_rejects_the_next_real_turn_after_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_ENABLED", "0")
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona"))
    monkeypatch.setenv("AURA_DIALOGUE_QUOTA_DB", str(tmp_path / "quota.sqlite3"))
    runtime = server_module.LilyRuntime(
        server_module.build_config(server_module.parse_args(["--hermes-home", str(tmp_path / "hermes")])),
    )
    runtime.aura_runtime_config = save_aura_runtime_config(
        runtime.aura_runtime_config,
        {"dialogue_quota_limit": 1},
    )

    class Result:
        ok = True

        def to_dict(self):
            return {"ok": True, "response": "ok"}

    monkeypatch.setattr(runtime.bridge, "run", lambda goal, metadata: Result())
    first_status, first = runtime.run_turn("first", metadata={})
    second_status, second = runtime.run_turn("second", metadata={})

    assert first_status == 200
    assert first["dialogue_quota"]["used"] == 1
    assert second_status == 429
    assert second["status"] == "quota_exceeded"
    assert second["dialogue_quota"]["remaining"] == 0


def test_dialogue_quota_error_has_localized_voice_prompt_without_changing_protocol():
    quota = {"limit": 50, "used": 50, "remaining": 0, "reset_at": 12345}

    chinese = server_module.LilyRuntime.dialogue_quota_error(quota, language="zh")
    english = server_module.LilyRuntime.dialogue_quota_error(quota, language="en-US")
    japanese = server_module.LilyRuntime.dialogue_quota_error(quota, language="ja")

    for payload in (chinese, english, japanese):
        assert payload["ok"] is False
        assert payload["status"] == "quota_exceeded"
        assert payload["error"] == "dialogue quota exceeded"
        assert payload["voice_text"] == payload["response"]
        assert payload["dialogue_quota"] == quota
    assert "次数用完" in chinese["response"]
    assert "window is used up" in english["response"]
    assert "回数を使い切った" in japanese["response"]


def test_device_quota_rows_show_local_limit_and_kimi_balance(monkeypatch):
    monkeypatch.setattr(
        gateway_module,
        "_dialogue_quota_snapshot",
        lambda runtime_config: {"ok": True, "limit": 50, "remaining": 49, "used": 1},
    )
    payload = gateway_module._combined_device_quota_payload(
        object(),
        {"ok": True, "provider": "stepfun", "windows": []},
        {
            "ok": True,
            "provider": "kimi-for-coding",
            "windows": [{"name": "five_hour", "remaining": 73, "total": 100, "display": "5 小时 73/100 tokens"}],
            "primary": {"name": "five_hour", "remaining": 73, "total": 100, "display": "5 小时 73/100 tokens"},
        },
    )

    assert payload["headline"] == "AURA"
    assert payload["provider_display"] == "AURA"
    assert payload["primary"]["display"] == "AURA 49/50"
    assert payload["secondary"]["display"] == "5h 73%"


def test_device_quota_keeps_local_limit_when_kimi_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        gateway_module,
        "_dialogue_quota_snapshot",
        lambda runtime_config: {"ok": True, "limit": 50, "remaining": 50, "used": 0},
    )
    payload = gateway_module._combined_device_quota_payload(object(), None, None)

    assert payload["headline"] == "AURA"
    assert payload["primary"]["display"] == "AURA 50/50"
    assert payload["secondary"] == {}


def test_aura_provider_quota_is_cached_until_half_hour_and_key_change_invalidates(monkeypatch):
    calls = []
    gateway_module._MODEL_QUOTA_CACHE.clear()
    monkeypatch.setattr(gateway_module, "QUOTA_CACHE_TTL_SECONDS", 1800.0)

    def fake_query_quota(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "provider": kwargs["provider"]}

    monkeypatch.setattr(gateway_module, "query_quota", fake_query_quota)
    config = SimpleNamespace(
        aura_model_provider="kimi",
        aura_model_model="kimi-for-coding",
        aura_model_base_url="https://api.kimi.com/coding/v1",
        aura_model_api_key="key-1",
    )

    gateway_module.refresh_model_quota_cache(config)
    gateway_module.refresh_model_quota_cache(config)
    assert len(calls) == 1

    # Replacing the saved key must not wait for the old cached result to age out.
    config.aura_model_api_key = "key-2"
    gateway_module.refresh_model_quota_cache(config)
    assert len(calls) == 2
