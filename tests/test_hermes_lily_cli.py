from __future__ import annotations

import base64
import asyncio
import json
import re
from io import StringIO
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from integrations.hermes_lily_cli.bridge import (
    HermesLilyBridge,
    HermesLilyConfig,
    build_hermes_command,
    public_command,
    scrub_json_value,
)
from integrations.hermes_lily_cli.cli import main
from integrations.hermes_lily_cli.gateway import (
    DEVICE_SAMPLE_RATE,
    AsrResult,
    GatewayConfig,
    TTS_BINARY_MAGIC,
    TtsResult,
    TurnState,
    _payload_language,
    background_task_result_url,
    bridge_metadata,
    call_bridge,
    device_spoken_text,
    fetch_background_task_result,
    flush_stream_tts_segments,
    handle_text_message,
    normalize_conversation_language,
    parse_args as parse_gateway_args,
    pop_stream_tts_segment,
    refresh_runtime_weather_for_gateway,
    runtime_config_for_language,
    run_voice_turn,
    stepfun_asr_text_from_response,
    synthesize_and_stream_tts,
    synthesize_tts,
    status_update_payload,
    transcribe_with_api,
    tts_text_chunks,
    stepfun_realtime_instructions,
    voice_latency_diagnosis,
)
from integrations.hermes_lily_cli import gateway as gateway_module
from integrations.hermes_lily_cli.runtime_config import load_model_options, merge_bridge_config, provider_presets
from integrations.hermes_lily_cli.quota import _normalize, _endpoint_for, _normalize_stepfun_plan, query_quota
from integrations.hermes_lily_cli import server as lily_server
from integrations.hermes_lily_cli.smoke_test import main as smoke_main
from integrations.hermes_lily_cli.server import build_config, make_handler, parse_args
from integrations.aura_persona_gateway.runtime import (
    AuraRuntimeConfig,
    asr_provider_presets,
    load_aura_runtime_config,
    save_aura_runtime_config,
    voice_latency_path,
)


def _basic_auth(user="admin", admin_pass="unit-pass"):
    raw = base64.b64encode(f"{user}:{admin_pass}".encode("utf-8")).decode("ascii")
    return {"authorization": f"Basic {raw}"}


def test_build_hermes_command_uses_public_cli_without_overlay():
    config = HermesLilyConfig(
        command=("hermes",),
        provider="deepseek",
        model="deepseek-v4-pro",
        toolsets=("web", "skills"),
        skills=("last30days",),
    )

    command = build_hermes_command("ping", config)

    assert command[:2] == ["hermes", "-z"]
    assert "ping" in command
    assert "--provider" in command
    assert command[command.index("--provider") + 1] == "deepseek"
    assert "--model" in command
    assert command[command.index("--model") + 1] == "deepseek-v4-pro"
    assert "--toolsets" in command
    assert command[command.index("--toolsets") + 1] == "web,skills"
    assert "--skills" in command
    assert "overlay" not in " ".join(command)


def test_public_command_hides_prompt():
    assert public_command(["hermes", "-z", "secret prompt", "--provider", "deepseek"]) == [
        "hermes",
        "-z",
        "<prompt>",
        "--provider",
        "deepseek",
    ]


def test_bridge_returns_json_safe_result(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="done\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    bridge = HermesLilyBridge(HermesLilyConfig(command=("hermes",), provider="deepseek", model="m"))

    result = bridge.run("do it")

    assert result.ok is True
    assert result.status == "completed"
    assert result.response == "done"
    assert result.evidence["returncode"] == 0
    assert result.evidence["command"][2] == "<prompt>"
    assert captured["command"][:3] == ["hermes", "-z", "do it"]


def test_bridge_oserror_response_does_not_expose_raw_path(monkeypatch):
    def fake_run(command, **kwargs):
        raise OSError("Permission denied: /Users/example/.hermes/config.yaml")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = HermesLilyBridge(HermesLilyConfig(command=("hermes",))).run("do it")

    assert result.ok is False
    assert result.response == "Hermes process could not be started."
    assert "/Users/example" not in result.response
    assert "/Users/example" not in json.dumps(result.to_dict())
    assert result.evidence["stop_reason"] == "process_error"
    assert result.evidence["error_type"] == "OSError"


def test_scrub_json_value_redacts_nested_secrets():
    value = scrub_json_value({
        "credential": "token: abc123",
        "nested": [{"credential": "secret: nested-value"}],
    })

    rendered = json.dumps(value)
    assert "abc123" not in rendered
    assert "nested-value" not in rendered
    assert rendered.count("<redacted>") == 2


def test_runtime_config_parses_model_options(monkeypatch):
    monkeypatch.setenv(
        "HERMES_MODEL_OPTIONS",
        "deepseek:deepseek-v4-pro|DeepSeek V4,openrouter:anthropic/claude-sonnet-4.6",
    )

    options = load_model_options()

    assert options == [
        {"provider": "deepseek", "model": "deepseek-v4-pro", "label": "DeepSeek V4"},
        {
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4.6",
            "label": "openrouter / anthropic/claude-sonnet-4.6",
        },
    ]


def test_quota_normalizes_kimi_coding_plan_windows():
    payload = {
        "limits": [{"detail": {"limit": 100, "remaining": 72, "resetTime": "2026-08-04T12:00:00Z"}}],
        "usage": {"limit": 1000, "remaining": 640, "resetTime": "2026-08-10T00:00:00Z"},
    }

    result = _normalize("kimi-for-coding", "kimi-k2", payload)

    assert result["primary"]["name"] == "five_hour"
    assert result["primary"]["remaining"] == 72
    assert result["primary"]["total"] == 100
    assert result["display"] == "5 小时 72/100 tokens"
    assert [item["name"] for item in result["windows"]] == ["five_hour", "weekly_limit"]
    assert _endpoint_for("kimi-for-coding", "https://api.kimi.com/coding/v1") == "https://api.kimi.com/coding/v1/usages"


def test_quota_normalizes_minimax_remaining_percent_and_skips_inactive_week():
    payload = {
        "model_remains": [{
            "model_name": "general",
            "current_interval_remaining_percent": 83,
            "current_weekly_remaining_percent": 100,
            "current_weekly_status": 3,
            "end_time": 1780000000000,
        }]
    }

    result = _normalize("minimax-oauth", "MiniMax-M2", payload)

    assert result["primary"]["name"] == "five_hour"
    assert result["primary"]["remaining"] == 83
    assert result["primary"]["unit"] == "%"
    assert len(result["windows"]) == 1
    assert _endpoint_for("minimax-oauth", "https://api.minimax.io/v1") == "https://api.minimax.io/v1/api/openplatform/coding_plan/remains"


def test_quota_normalizes_stepfun_plan_windows_and_does_not_use_account_balance():
    result = _normalize_stepfun_plan(
        "stepfun",
        "stepaudio-2.5-chat",
        {
            "five_hour_usage_left_rate": 0.82,
            "five_hour_usage_reset_time": 1780000000000,
            "weekly_usage_left_rate": 61,
            "weekly_usage_reset_time": 1780400000000,
            "balance": 999,
        },
    )

    assert result["plan"] == "step_plan"
    assert result["primary"]["name"] == "five_hour"
    assert result["primary"]["remaining"] == 82.0
    assert result["secondary"]["name"] == "weekly_limit"
    assert result["display"] == "5 小时 82/100 %"
    assert _endpoint_for("stepfun", "https://api.stepfun.com/step_plan/v1").endswith(
        "/QueryStepPlanRateLimit"
    )


def test_quota_normalizes_current_stepfun_credit_pool_and_topup():
    result = _normalize_stepfun_plan(
        "stepfun",
        "stepaudio-2.5-chat",
        {
            "planFamily": 1,
            "planCreditRateLimit": {
                "subscriptionCreditLeftRate": 0.72,
                "subscriptionCreditResetTime": 1780400000000,
                "creditBuckets": [
                    {
                        "type": 2,
                        "creditTotal": 400000000,
                        "creditResidual": 333000000,
                        "expireAt": 1780500000000,
                    }
                ],
            },
        },
    )

    assert result["primary"]["name"] == "plan_credit"
    assert result["primary"]["remaining"] == 72.0
    assert result["secondary"]["name"] == "topup"
    assert result["secondary"]["remaining"] == 333000000.0
    assert result["secondary"]["total"] == 400000000.0
    assert result["display"] == "Credit 72/100 %"


def test_stepfun_plan_without_platform_session_is_not_account_balance(monkeypatch):
    monkeypatch.delenv("AURA_STEPFUN_PLAN_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("STEPFUN_PLAN_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AURA_STEPFUN_PLAN_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("STEPFUN_PLAN_SESSION_COOKIE", raising=False)

    result = query_quota(
        provider="stepfun",
        model="stepaudio-2.5-chat",
        base_url="https://api.stepfun.com/step_plan/v1",
        api_key="api-key-present",
    )

    assert result["ok"] is False
    assert "API Key 只能查询账户余额" in result["error"]
    assert result["primary"] is None


def test_merge_bridge_config_only_updates_runtime_safe_fields():
    config = merge_bridge_config(
        HermesLilyConfig(command=("hermes",), provider="old", model="old-model"),
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "toolsets": "web,skills",
            "skills": ["persona-admin"],
            "timeout_seconds": 44,
            "command": "evil",
        },
    )

    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.toolsets == ("web", "skills")
    assert config.skills == ("persona-admin",)
    assert config.timeout_seconds == 44
    assert config.command == ("hermes",)


def test_cli_json_input(monkeypatch, capsys):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("sys.stdin", StringIO('{"goal":"hi","provider":"deepseek","model":"m"}'))

    code = main(["--json-input"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["response"] == "ok"


def test_lily_http_handler_roundtrip(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="server ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")
    config = build_config(parse_args(["--provider", "deepseek", "--model", "m"]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        health = json.loads(urlopen(f"{base}/health", timeout=3).read().decode("utf-8"))
        assert health["ok"] is True

        request = Request(
            f"{base}/turn",
            data=b'{"goal":"hi"}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["response"] == "server ok"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_lily_http_turn_enriches_user_geo_from_client_ip(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="geo ok", stderr="")

    def fake_geo(ip_address):
        captured["geo_ip"] = ip_address
        return {
            "city": "上海",
            "region": "上海市",
            "country": "中国",
            "latitude": 31.2304,
            "longitude": 121.4737,
            "timezone": "Asia/Shanghai",
            "source": "ip",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(lily_server, "_lookup_user_geo", fake_geo)
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'{"goal":"now"}',
            headers={"content-type": "application/json", "x-forwarded-for": "8.8.8.8"},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert payload["ok"] is True
    assert captured["geo_ip"] == "8.8.8.8"
    assert payload["evidence"]["metadata"]["user_geo"]["city"] == "上海"
    assert payload["evidence"]["metadata"]["user_geo"]["timezone"] == "Asia/Shanghai"


def test_lily_http_turn_does_not_geolocate_private_device_ip(tmp_path, monkeypatch):
    captured = {"geo_calls": []}

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="private ok", stderr="")

    def fake_geo(ip_address):
        captured["geo_calls"].append(ip_address)
        return {
            "city": "Singapore",
            "timezone": "Asia/Singapore",
            "source": "ip",
        }

    monkeypatch.delenv("AURA_USER_HOME_CITY", raising=False)
    monkeypatch.delenv("AURA_USER_TIMEZONE", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(lily_server, "_lookup_user_geo", fake_geo)
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'{"goal":"now"}',
            headers={"content-type": "application/json", "x-forwarded-for": "192.168.0.183"},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert payload["ok"] is True
    assert captured["geo_calls"] == []
    assert "user_geo" not in payload["evidence"]["metadata"]


def test_lily_http_turn_geolocates_device_public_ip(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="device public ok", stderr="")

    def fake_geo(ip_address):
        captured["geo_ip"] = ip_address
        return {
            "city": "上海",
            "timezone": "Asia/Shanghai",
            "source": "ip",
        }

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(lily_server, "_lookup_user_geo", fake_geo)
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=json.dumps({
                "goal": "now",
                "metadata": {
                    "client_ip": "192.168.0.183",
                    "device_public_ip": "8.8.8.8",
                },
            }).encode("utf-8"),
            headers={"content-type": "application/json", "x-forwarded-for": "192.168.0.183"},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert payload["ok"] is True
    assert captured["geo_ip"] == "8.8.8.8"
    assert payload["evidence"]["metadata"]["user_geo"]["city"] == "上海"


def test_metadata_with_user_geo_localizes_english_city():
    class Handler:
        headers = {}
        client_address = ("127.0.0.1", 0)

    enriched = lily_server._metadata_with_user_geo(
        {"user_geo": {"city": "Beijing", "timezone": "Asia/Shanghai"}},
        Handler(),
        None,
    )

    assert enriched["user_geo"]["city"] == "北京"
    assert enriched["user_geo"]["timezone"] == "Asia/Shanghai"


def test_lily_http_turn_prefers_manual_user_geo(tmp_path, monkeypatch):
    captured = {"geo_calls": []}

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="manual ok", stderr="")

    def fake_geo(ip_address):
        captured["geo_calls"].append(ip_address)
        return {
            "city": "Singapore",
            "timezone": "Asia/Singapore",
            "source": "ip",
        }

    monkeypatch.setenv("AURA_USER_HOME_CITY", "上海")
    monkeypatch.setenv("AURA_USER_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("AURA_USER_LATITUDE", "31.2304")
    monkeypatch.setenv("AURA_USER_LONGITUDE", "121.4737")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(lily_server, "_lookup_user_geo", fake_geo)
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=json.dumps({
                "goal": "now",
                "metadata": {"device_public_ip": "8.8.8.8"},
            }).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=3)

    assert payload["ok"] is True
    assert captured["geo_calls"] == []
    assert payload["evidence"]["metadata"]["user_geo"]["city"] == "上海"
    assert payload["evidence"]["metadata"]["user_geo"]["source"] == "manual"


def test_gateway_bridge_metadata_forwards_start_user_geo_and_public_ip():
    state = TurnState(
        turn_id=7,
        device_id="esp32-test",
        boot_id="boot-1",
        language="ja",
        audio_bytes=2048,
        client_ip="192.168.0.183",
        metadata={
            "user_geo": {
                "city": "上海",
                "timezone": "Asia/Shanghai",
                "source": "manual",
            },
            "public_ip": "8.8.8.8",
        },
    )

    payload = bridge_metadata(state, "现在几点？", streamed=True)

    assert payload["streamed"] is True
    assert payload["transcript"] == "现在几点？"
    assert payload["client_ip"] == "192.168.0.183"
    assert payload["device_public_ip"] == "8.8.8.8"
    assert payload["language"] == "ja"
    assert payload["user_geo"]["city"] == "上海"
    assert payload["user_geo"]["timezone"] == "Asia/Shanghai"


def test_gateway_conversation_language_normalization_and_payload_precedence(tmp_path):
    assert normalize_conversation_language("en-US") == "en"
    assert normalize_conversation_language("ja_JP") == "ja"
    assert normalize_conversation_language("zh-CN") == "zh"
    assert normalize_conversation_language("unsupported") == "zh"
    assert _payload_language({"language": "en-US"}, {}, "zh") == "en"
    assert _payload_language({"session": {"language": "ja-JP"}}, {}, "zh") == "ja"
    assert _payload_language({"language": "en"}, {"language": "ja"}, "zh") == "ja"

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path), asr_language="zh")
    localized = runtime_config_for_language(runtime, "ja-JP")
    assert localized.asr_language == "ja"
    assert runtime.asr_language == "zh"


def test_lily_http_admin_page_loads():
    config = build_config(parse_args([]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        html = urlopen(f"http://127.0.0.1:{server.server_port}/admin", timeout=3).read().decode("utf-8")
        assert "Aura Lily Admin" in html
        assert "adminUser" in html
        assert "adminPassword" in html
        assert "providerPreset" in html
        assert "auraProviderPreset" in html
        assert "ttsPreset" in html
        assert "asrPreset" in html
        assert "Aura 运行配置" in html
        assert "语音模型配置" in html
        assert "Aura 对话模型" in html
        assert "auraModelMaxTokens" in html
        assert "auraModelReasoningEffort" in html
        assert "apiKey" in html
        assert "baseUrl" in html
        assert "showHermesKey" in html
        assert "showAuraModelKey" in html
        assert "showTtsKey" in html
        assert "showAsrKey" in html
        assert "testHermes" in html
        assert "testAuraModel" in html
        assert "testTts" in html
        assert "ttsTimeout" in html
        assert "testAsr" in html
        assert "copyStepPlanKeyToAsr" in html
        assert "asrProfile" in html
        assert "saveAsrProfile" in html
        assert "ttsProfile" in html
        assert "saveTtsProfile" in html
        assert "clearHermesKey" in html
        assert "clearAuraModelKey" in html
        assert "clearAsrKey" in html
        assert "clearFastReplyKey" in html
        assert "clearTtsKey" in html
        assert "cachedWeatherEnabled" in html
        assert "weatherAutoRefreshEnabled" in html
        assert "refreshCachedWeather" in html
        assert "touchCachedWeather" in html
        assert "clearCachedWeather" in html
        assert "dashLocation" in html
        assert "locationBadge" in html
        assert "locationDeviceIp" in html
        assert "后台访问令牌" not in html
        assert "/admin/style.css" in html
        assert "/admin/app.js" in html
        assert "Hermes 上游" in html
        assert "世界状态" in html
        assert "worldModelEnabled" in html
        assert "refreshWorld" in html
        ids = re.findall(r'\bid="([^"]+)"', html)
        assert len(ids) == len(set(ids))

        css = urlopen(f"http://127.0.0.1:{server.server_port}/admin/style.css", timeout=3).read().decode("utf-8")
        js = urlopen(f"http://127.0.0.1:{server.server_port}/admin/app.js", timeout=3).read().decode("utf-8")
        assert ".sidebar" in css
        assert ".diagnostic-grid" in css
        assert "loadAll" in js
        assert "fillLocation" in js
        assert "locationSummaryLabel" in js
        assert "manual_missing" in js
        assert "Basic" in js
        assert "clear_api_key" in js
        assert "clear_aura_model_api_key" in js
        assert "clear_asr_api_key" in js
        assert "clear_fast_reply_api_key" in js
        assert "clear_tts_api_key" in js
        assert "cached_weather_temperature" in js
        assert "cached_weather_humidity" in js
        assert "weather_auto_refresh_enabled" in js
        assert "/admin/aura/weather/refresh" in js
        assert "touch_cached_weather" in js
        assert "clear_cached_weather" in js
        assert "/admin/test/hermes" in js
        assert "/admin/test/aura-model" in js
        assert "/admin/test/tts" in js
        assert "/admin/test/asr" in js
        assert "tts_timeout_seconds" in js
        assert "asr_profiles" in js
        assert "tts_profiles" in js
        assert "applyAsrProfile" in js
        assert "applyTtsProfile" in js
        assert "stage: ${payload.stage}" in js
        assert "preset.description" in js
        assert "实时流式" in js
        assert "voice_latency_path" in js
        assert "aura_model_max_tokens" in js
        assert "aura_model_reasoning_effort" in js
        assert "小智式 ASR/LLM/TTS 三段流式已就绪" in js
        assert "Step Plan 订阅内" in js
        assert "非 Step Plan 路由" in js
        assert "step_plan_covered" in js
        assert "step_plan_realtime_ready" in js
        assert "step_plan_realtime_configured" in js
        assert "applyStepPlanRealtime" in html
        assert "套用实验 Realtime" in html
        assert "applyStepPlanRealtimePreset" in js
        assert "applyStepPlanAsr" in html
        assert "套用 Plan ASR" in html
        assert "applyStepPlanAsrPreset" in js
        assert "copyStepPlanKeyToAsr" in js
        assert "/admin/aura/copy-stepfun-plan-key" in js
        assert "/admin/aura/apply-stepfun-open-platform" in js
        assert "applyStepfunOpenPlatform" in html
        assert "套用 Open Platform 流式" in html
        assert "applyStepfunOpenPlatformPreset" in js
        assert "非 Step Plan 路由" in js
        assert "applyXiaozhiAsr" in html
        assert "套用语义流式" in html
        assert "applyXiaozhiAsrPreset" in js
        assert "stepaudio-2.5-asr" in js
        assert "stepaudio-2.5-asr-stream" in js
        assert "已套用小智式语义流式" in js
        assert "/admin/hermes/secret/api_key" in js
        assert "/admin/aura/secret/aura_model_api_key" in js
        assert "/admin/aura/secret/asr_api_key" in js
        assert "/admin/hermes/config" in js
        assert "/admin/aura/runtime" in js
        assert "/persona/state" in js
        assert "/persona/world" in js
        assert "world_model_enabled" in js
        assert "provider_presets" not in html
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_lily_http_admin_updates_hermes_runtime_config(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="model switched", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AURA_LILY_ADMIN_PASSWORD", "unit-pass")
    monkeypatch.setenv("AURA_LILY_HERMES_CONFIG_PATH", str(tmp_path / "hermes_runtime.json"))
    monkeypatch.setenv("HERMES_MODEL_OPTIONS", "deepseek:deepseek-v4-pro|DeepSeek")
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")
    config = build_config(parse_args([
        "--provider", "old",
        "--model", "old-model",
        "--hermes-home", str(tmp_path / "hermes-home"),
    ]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            urlopen(f"{base}/admin/hermes/config", timeout=3)
        except HTTPError as exc:
            assert exc.code == 401
        else:  # pragma: no cover
            raise AssertionError("expected HTTP 401")

        request = Request(
            f"{base}/admin/hermes/config",
            data=json.dumps({
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-test-secret",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["config"]["provider"] == "deepseek"
        assert payload["config"]["model"] == "deepseek-v4-pro"
        assert payload["config"]["base_url"] == "https://api.deepseek.com"
        assert payload["config"]["api_key_configured"] is True
        preset_ids = {item["id"] for item in payload["config"]["provider_presets"]}
        assert "deepseek" in preset_ids
        assert "alibaba" in preset_ids
        assert "openai-compatible" in preset_ids
        assert payload["config"]["lily_stores_provider_keys"] is True
        assert payload["config"]["lily_returns_provider_keys"] is False
        assert "sk-test-secret" not in json.dumps(payload)
        assert "sk-test-secret" in (tmp_path / "hermes-home" / "config.yaml").read_text(encoding="utf-8")

        secret_req = Request(
            f"{base}/admin/hermes/secret/api_key",
            headers=_basic_auth(),
        )
        secret_payload = json.loads(urlopen(secret_req, timeout=3).read().decode("utf-8"))
        assert secret_payload == {"ok": True, "key": "api_key", "value": "sk-test-secret"}

        clear_request = Request(
            f"{base}/admin/hermes/config",
            data=json.dumps({"clear_api_key": True}).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        clear_payload = json.loads(urlopen(clear_request, timeout=3).read().decode("utf-8"))
        assert clear_payload["ok"] is True
        assert clear_payload["config"]["api_key_configured"] is False
        assert "sk-test-secret" not in json.dumps(clear_payload)
        assert "sk-test-secret" not in (tmp_path / "hermes-home" / "config.yaml").read_text(encoding="utf-8")

        turn = Request(
            f"{base}/turn",
            data=b'{"goal":"hi"}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        turn_payload = json.loads(urlopen(turn, timeout=3).read().decode("utf-8"))
        assert turn_payload["ok"] is True
        assert "--provider" in captured["command"]
        assert captured["command"][captured["command"].index("--provider") + 1] == "deepseek"
        assert captured["command"][captured["command"].index("--model") + 1] == "deepseek-v4-pro"

        test_req = Request(f"{base}/admin/test/hermes", headers=_basic_auth())
        test_payload = json.loads(urlopen(test_req, timeout=3).read().decode("utf-8"))
        assert test_payload["ok"] is True
        assert test_payload["kind"] == "hermes_llm"
        assert test_payload["provider"] == "deepseek"
        assert "sk-test-secret" not in json.dumps(test_payload)
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_lily_http_admin_updates_aura_runtime_config(tmp_path, monkeypatch):
    weather_urls = []
    gateway_status_path = tmp_path / "gateway_status.json"

    class FakeWeatherResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "current": {
                    "temperature_2m": 26,
                    "relative_humidity_2m": 69,
                    "weather_code": 2,
                    "time": "2026-06-04T14:00",
                }
            }).encode("utf-8")

    def fake_weather_urlopen(url, timeout):
        weather_urls.append(str(url))
        return FakeWeatherResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.weather.urlopen", fake_weather_urlopen)
    monkeypatch.setattr(
        lily_server,
        "_probe_stepfun_realtime_asr_ws",
        lambda base_url, *, model, language, api_key, timeout: {
            "ok": True,
            "stage": "stepfun_realtime_ws",
            "detail": "StepFun realtime ASR WebSocket reachable.",
            "endpoint_host": "api.stepfun.com",
        },
    )
    monkeypatch.setenv("AURA_LILY_ADMIN_PASSWORD", "unit-pass")
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    monkeypatch.setenv("AURA_COMPANION_HOME", str(tmp_path / "companion-home"))
    monkeypatch.setenv("AURA_LILY_GATEWAY_STATUS_PATH", str(gateway_status_path))
    monkeypatch.setattr(
        lily_server,
        "_lookup_user_geo",
        lambda ip_address: {
            "city": "上海",
            "timezone": "Asia/Shanghai",
            "latitude": "31.2304",
            "longitude": "121.4737",
            "source": "device_ip",
        } if ip_address == "8.8.8.8" else {},
    )
    gateway_status_path.write_text(json.dumps({
        "updated_at": time.time(),
        "source_event": "hello",
        "device_id": "esp32-unit",
        "boot_id": "boot-unit",
        "client_ip": "172.18.0.1",
        "device_public_ip": "8.8.8.8",
        "device_public_ip_configured": True,
    }), encoding="utf-8")
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            urlopen(f"{base}/admin/aura/runtime", timeout=3)
        except HTTPError as exc:
            assert exc.code == 401
        else:  # pragma: no cover
            raise AssertionError("expected HTTP 401")

        request = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "aura_model_mode": "aura_model",
                "aura_model_provider": "deepseek",
                "aura_model_model": "deepseek-chat",
                "aura_model_base_url": "https://api.deepseek.com",
                "aura_model_api_key": "aura-unit-key",
                "aura_model_max_tokens": 80,
                "aura_model_temperature": "0.3",
                "aura_model_reasoning_effort": "low",
                "fast_reply_enabled": True,
                "fast_reply_mode": "local_rule",
                "greeting_reply": "我在",
                "cached_weather_enabled": True,
                "cached_weather_city": "上海",
                "cached_weather_temperature": "23",
                "cached_weather_condition": "多云",
                "cached_weather_icon": 1,
                "cached_weather_humidity": "55",
                "cached_weather_source": "manual",
                "cached_weather_observed_at": "2026-06-04T13:00",
                "cached_weather_ttl_seconds": 3600,
                "weather_provider": "open_meteo",
                "weather_auto_refresh_enabled": True,
                "weather_refresh_interval_seconds": 1200,
                "weather_request_timeout_seconds": 4,
                "tts_enabled": True,
                "tts_provider": "openai",
                "tts_model": "gpt-4o-mini-tts",
                "tts_voice": "verse",
                "tts_api_key": "tts-secret",
                "tts_timeout_seconds": 22,
                "asr_enabled": True,
                "asr_mode": "api",
                "asr_provider": "openai",
                "asr_model": "gpt-4o-transcribe",
                "asr_base_url": "https://api.openai.com/v1",
                "asr_api_key": "asr-secret",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        payload = json.loads(urlopen(request, timeout=3).read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["config"]["aura_model_mode"] == "aura_model"
        assert payload["config"]["aura_model_provider"] == "deepseek"
        assert payload["config"]["aura_model_api_key_configured"] is True
        assert payload["config"]["aura_model_max_tokens"] == 80
        assert payload["config"]["aura_model_temperature"] == "0.3"
        assert payload["config"]["aura_model_reasoning_effort"] == "low"
        assert payload["config"]["greeting_reply"] == "我在"
        assert payload["config"]["tts_provider"] == "openai"
        assert payload["config"]["tts_api_key_configured"] is True
        assert payload["config"]["tts_timeout_seconds"] == 22
        assert payload["config"]["asr_provider"] == "openai"
        assert payload["config"]["asr_api_key_configured"] is True
        assert payload["config"]["cached_weather_fresh"] is True
        assert payload["config"]["cached_weather"]["city"] == "上海"
        assert payload["config"]["cached_weather"]["weather_icon"] == 1
        assert payload["config"]["cached_weather"]["humidity"] == "55"
        assert payload["config"]["cached_weather"]["source"] == "manual"
        assert payload["config"]["weather_auto_refresh_enabled"] is True
        assert payload["config"]["weather_refresh_interval_seconds"] == 1200
        assert payload["config"]["weather_request_timeout_seconds"] == 4
        assert any(item["kind"] == "asr" and item["model"] == "gpt-4o-transcribe" for item in payload["config"]["config_history"])
        assert any(item["id"] == "asr-stepfun-plan-realtime" for item in payload["config"]["asr_profiles"])
        assert any(item["id"] == "asr-stepfun-plan-sse" for item in payload["config"]["asr_profiles"])
        assert any(item["id"] == "asr-local-whisper-http" for item in payload["config"]["asr_profiles"])
        assert payload["config"]["tts_profiles"] == []
        assert "aura-unit-key" not in json.dumps(payload)
        assert "asr-secret" not in json.dumps(payload)
        assert "tts-secret" not in json.dumps(payload)

        refresh_req = Request(
            f"{base}/admin/aura/weather/refresh",
            data=json.dumps({"city": "上海", "force": True}).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        refresh_payload = json.loads(urlopen(refresh_req, timeout=3).read().decode("utf-8"))
        assert refresh_payload["ok"] is True
        assert refresh_payload["result"]["status"] == "refreshed"
        assert refresh_payload["config"]["cached_weather"]["display"] == "上海，26度，多云，湿度69%"
        assert refresh_payload["config"]["cached_weather"]["source"] == "open_meteo"
        assert refresh_payload["config"]["cached_weather"]["observed_at"] == "2026-06-04T14:00"
        assert weather_urls and "latitude=31.230400" in weather_urls[-1]

        aura_secret_req = Request(
            f"{base}/admin/aura/secret/aura_model_api_key",
            headers=_basic_auth(),
        )
        aura_secret = json.loads(urlopen(aura_secret_req, timeout=3).read().decode("utf-8"))
        assert aura_secret == {"ok": True, "key": "aura_model_api_key", "value": "aura-unit-key"}

        tts_secret_req = Request(
            f"{base}/admin/aura/secret/tts_api_key",
            headers=_basic_auth(),
        )
        tts_secret = json.loads(urlopen(tts_secret_req, timeout=3).read().decode("utf-8"))
        assert tts_secret == {"ok": True, "key": "tts_api_key", "value": "tts-secret"}

        stepfun_plan_req = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "aura_model_provider": "stepfun",
                "aura_model_model": "stepaudio-2.5-chat",
                "aura_model_base_url": "https://api.stepfun.com/step_plan/v1",
                "aura_model_api_key": "step-plan-secret",
                "tts_enabled": True,
                "tts_provider": "stepfun",
                "tts_model": "stepaudio-2.5-tts",
                "tts_base_url": "https://api.stepfun.com/step_plan/v1",
                "tts_api_key": "",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        stepfun_plan_payload = json.loads(urlopen(stepfun_plan_req, timeout=3).read().decode("utf-8"))
        assert stepfun_plan_payload["ok"] is True
        assert stepfun_plan_payload["config"]["aura_model_provider"] == "stepfun"
        assert "step-plan-secret" not in json.dumps(stepfun_plan_payload)

        copy_key_req = Request(
            f"{base}/admin/aura/copy-stepfun-plan-key",
            headers=_basic_auth(),
        )
        copy_key_payload = json.loads(urlopen(copy_key_req, timeout=3).read().decode("utf-8"))
        assert copy_key_payload["ok"] is True
        assert copy_key_payload["source"] == "Aura LLM"
        assert copy_key_payload["config"]["asr_provider"] == "stepfun"
        assert copy_key_payload["config"]["asr_model"] == "stepaudio-2.5-asr"
        assert copy_key_payload["config"]["asr_base_url"] == "https://api.stepfun.com/step_plan/v1"
        assert copy_key_payload["config"]["asr_api_key_configured"] is True
        assert "aura-unit-key" not in json.dumps(copy_key_payload)
        assert "step-plan-secret" not in json.dumps(copy_key_payload)
        assert "tts-secret" not in json.dumps(copy_key_payload)

        asr_secret_req = Request(
            f"{base}/admin/aura/secret/asr_api_key",
            headers=_basic_auth(),
        )
        asr_secret = json.loads(urlopen(asr_secret_req, timeout=3).read().decode("utf-8"))
        assert asr_secret == {"ok": True, "key": "asr_api_key", "value": "step-plan-secret"}

        stepfun_plan_asr_req = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "asr_enabled": True,
                "asr_mode": "api",
                "asr_provider": "stepfun",
                "asr_model": "stepaudio-2.5-asr",
                "asr_base_url": "https://api.stepfun.com/step_plan/v1",
                "clear_asr_api_key": True,
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        stepfun_plan_asr_payload = json.loads(urlopen(stepfun_plan_asr_req, timeout=3).read().decode("utf-8"))
        assert stepfun_plan_asr_payload["ok"] is True
        assert stepfun_plan_asr_payload["config"]["asr_api_key_configured"] is False
        copy_key_again_payload = json.loads(urlopen(copy_key_req, timeout=3).read().decode("utf-8"))
        assert copy_key_again_payload["ok"] is True
        assert copy_key_again_payload["config"]["asr_provider"] == "stepfun"
        assert copy_key_again_payload["config"]["asr_model"] == "stepaudio-2.5-asr"
        assert copy_key_again_payload["config"]["asr_base_url"] == "https://api.stepfun.com/step_plan/v1"
        assert copy_key_again_payload["config"]["asr_api_key_configured"] is True

        open_platform_req = Request(
            f"{base}/admin/aura/apply-stepfun-open-platform",
            headers=_basic_auth(),
        )
        open_platform_payload = json.loads(urlopen(open_platform_req, timeout=3).read().decode("utf-8"))
        assert open_platform_payload["ok"] is True
        assert open_platform_payload["source"] == "Aura LLM"
        assert open_platform_payload["billing_scope"] == "open_platform"
        assert open_platform_payload["config"]["aura_model_provider"] == "stepfun"
        assert open_platform_payload["config"]["aura_model_base_url"] == "https://api.stepfun.com/v1"
        assert open_platform_payload["config"]["aura_model_model"] == "stepaudio-2.5-chat"
        assert open_platform_payload["config"]["aura_model_reasoning_effort"] == ""
        assert open_platform_payload["config"]["tts_base_url"] == "https://api.stepfun.com/v1"
        assert open_platform_payload["config"]["asr_base_url"] == "https://api.stepfun.com/v1"
        assert open_platform_payload["config"]["asr_model"] == "stepaudio-2.5-asr-stream"
        assert open_platform_payload["config"]["voice_latency_path"]["llm_billing_scope"] == "open_platform"
        assert open_platform_payload["config"]["voice_latency_path"]["tts_billing_scope"] == "open_platform"
        assert open_platform_payload["config"]["voice_latency_path"]["asr_billing_scope"] == "open_platform"
        assert open_platform_payload["config"]["voice_latency_path"]["semantic_stream_ready"] is True
        assert open_platform_payload["config"]["voice_latency_path"]["step_plan_covered"] is False
        assert "step-plan-secret" not in json.dumps(open_platform_payload)

        xiaozhi_asr_request = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "asr_enabled": True,
                "asr_mode": "api",
                "asr_provider": "stepfun",
                "asr_model": "stepaudio-2.5-asr-stream",
                "asr_base_url": "https://api.stepfun.com/v1",
                "asr_api_key": "",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        xiaozhi_asr_payload = json.loads(urlopen(xiaozhi_asr_request, timeout=3).read().decode("utf-8"))
        assert xiaozhi_asr_payload["ok"] is True
        assert xiaozhi_asr_payload["config"]["asr_provider"] == "stepfun"
        assert xiaozhi_asr_payload["config"]["asr_model"] == "stepaudio-2.5-asr-stream"
        assert xiaozhi_asr_payload["config"]["asr_api_key_configured"] is True
        assert xiaozhi_asr_payload["config"]["voice_latency_path"]["asr_streaming"] is True
        asr_secret_after_preset = json.loads(urlopen(asr_secret_req, timeout=3).read().decode("utf-8"))
        assert asr_secret_after_preset == {"ok": True, "key": "asr_api_key", "value": "step-plan-secret"}
        assert "asr-secret" not in json.dumps(xiaozhi_asr_payload)
        assert "step-plan-secret" not in json.dumps(xiaozhi_asr_payload)

        realtime_asr_req = Request(f"{base}/admin/test/asr", headers=_basic_auth())
        realtime_asr_test = json.loads(urlopen(realtime_asr_req, timeout=3).read().decode("utf-8"))
        assert realtime_asr_test["ok"] is True
        assert realtime_asr_test["stage"] == "stepfun_realtime_ws"
        assert realtime_asr_test["endpoint_host"] == "api.stepfun.com"
        assert "asr-secret" not in json.dumps(realtime_asr_test)
        assert "step-plan-secret" not in json.dumps(realtime_asr_test)

        restore_non_secret_fields = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "aura_model_provider": "deepseek",
                "aura_model_model": "deepseek-chat",
                "aura_model_base_url": "https://api.deepseek.com",
                "tts_provider": "openai",
                "tts_model": "gpt-4o-mini-tts",
                "tts_voice": "verse",
                "asr_provider": "stepfun",
                "asr_model": "stepaudio-2.5-asr-stream",
                "asr_base_url": "https://api.stepfun.com/v1",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        restore_payload = json.loads(urlopen(restore_non_secret_fields, timeout=3).read().decode("utf-8"))
        assert restore_payload["ok"] is True

        clear_request = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "clear_tts_api_key": True,
                "clear_aura_model_api_key": True,
                "clear_asr_api_key": True,
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        clear_payload = json.loads(urlopen(clear_request, timeout=3).read().decode("utf-8"))
        assert clear_payload["ok"] is True
        assert clear_payload["config"]["aura_model_api_key_configured"] is False
        assert clear_payload["config"]["tts_api_key_configured"] is False
        assert clear_payload["config"]["asr_api_key_configured"] is False
        assert "aura-unit-key" not in json.dumps(clear_payload)
        assert "asr-secret" not in json.dumps(clear_payload)
        assert "tts-secret" not in json.dumps(clear_payload)

        summary_req = Request(
            f"{base}/admin/summary",
            headers=_basic_auth(),
        )
        summary = json.loads(urlopen(summary_req, timeout=3).read().decode("utf-8"))
        assert summary["aura_runtime"]["aura_model_provider"] == "deepseek"
        assert summary["aura_runtime"]["aura_model_api_key_configured"] is False
        assert summary["location"]["status"] == "auto_ready"
        assert summary["location"]["effective_geo"]["city"] == "上海"
        assert summary["location"]["gateway_status"]["device_public_ip"] == "8.8.8.*"
        assert summary["location"]["gateway_status"]["client_ip"] == "172.18.0.*"
        assert summary["location"]["gateway_status"]["client_ip_private"] is True

        world_req = Request(f"{base}/persona/world", headers=_basic_auth())
        world_payload = json.loads(urlopen(world_req, timeout=3).read().decode("utf-8"))
        assert world_payload["ok"] is True
        assert world_payload["world"]["enabled"] is True
        assert len(world_payload["world"]["today_plan"]) >= 6

        persona_config_req = Request(
            f"{base}/persona/config",
            data=json.dumps({"aura_home_city": "苏州", "world_model_enabled": False}).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        persona_config_payload = json.loads(urlopen(persona_config_req, timeout=3).read().decode("utf-8"))
        assert persona_config_payload["ok"] is True
        assert persona_config_payload["config"]["aura_home_city"] == "苏州"
        assert persona_config_payload["config"]["world_model_enabled"] is False
        disabled_world = json.loads(urlopen(world_req, timeout=3).read().decode("utf-8"))
        assert disabled_world["world"]["enabled"] is False
        assert disabled_world["world"]["today_plan"] == []
        assert summary["aura_runtime"]["tts_provider"] == "openai"
        assert summary["aura_runtime"]["tts_api_key_configured"] is False
        assert summary["aura_runtime"]["asr_provider"] == "stepfun"
        assert summary["aura_runtime"]["asr_model"] == "stepaudio-2.5-asr-stream"
        assert summary["aura_runtime"]["asr_api_key_configured"] is False
        assert "aura-unit-key" not in json.dumps(summary)
        assert "asr-secret" not in json.dumps(summary)
        assert "tts-secret" not in json.dumps(summary)

        local_asr = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "asr_enabled": True,
                "asr_mode": "local",
                "asr_provider": "local",
                "asr_model": "whisper-large-v3",
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        assert json.loads(urlopen(local_asr, timeout=3).read().decode("utf-8"))["ok"] is True
        asr_test = Request(f"{base}/admin/test/asr", headers=_basic_auth())
        asr_payload = json.loads(urlopen(asr_test, timeout=3).read().decode("utf-8"))
        assert asr_payload["ok"] is True
        assert asr_payload["kind"] == "asr"
        assert asr_payload["provider"] == "local"
        assert "asr-secret" not in json.dumps(asr_payload)

        profile_request = Request(
            f"{base}/admin/aura/runtime",
            data=json.dumps({
                "asr_profiles": [
                    {
                        "id": "asr-stepfun-local",
                        "label": "本机 ASR API",
                        "mode": "api",
                        "provider": "custom",
                        "model": "whisper-base-local",
                        "base_url": "http://host.docker.internal:8766/v1",
                        "api_key": "profile-asr-secret",
                    }
                ],
                "tts_profiles": [
                    {
                        "id": "tts-voxcpm-custom-aura",
                        "label": "VoxCPM self-hosted",
                        "provider": "voxcpm",
                        "model": "voxcpm2",
                        "voice": "aura",
                        "base_url": "http://tts.example.test/v1/audio/speech",
                        "api_key": "profile-tts-secret",
                    }
                ],
            }).encode("utf-8"),
            headers={"content-type": "application/json", **_basic_auth()},
            method="POST",
        )
        profile_payload = json.loads(urlopen(profile_request, timeout=3).read().decode("utf-8"))
        assert any(item["id"] == "asr-stepfun-local" for item in profile_payload["config"]["asr_profiles"])
        assert any(item["id"] == "tts-voxcpm-custom-aura" for item in profile_payload["config"]["tts_profiles"])
        assert "profile-asr-secret" not in json.dumps(profile_payload)
        assert "profile-tts-secret" not in json.dumps(profile_payload)
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_provider_presets_follow_hermes_catalog():
    presets = provider_presets()
    by_id = {item["id"]: item for item in presets}

    for provider_id in {"alibaba-coding-plan", "kimi-for-coding", "minimax-oauth"}:
        assert provider_id in by_id
        assert by_id[provider_id]["group"] == "Coding Plan"

    for provider_id in {"alibaba", "zai", "minimax", "minimax-cn", "stepfun", "stepfun-open", "tencent-tokenhub", "xiaomi", "deepseek"}:
        assert provider_id in by_id
        assert by_id[provider_id]["group"] == "国内主流"

    assert by_id["openrouter"]["group"] == "聚合器"
    assert by_id["openrouter"]["aliases"] == []
    assert by_id["alibaba"]["label"] == "Alibaba Bailian / Qwen"
    assert by_id["kimi-for-coding"]["label"] == "Kimi Coding Plan"
    assert by_id["minimax-oauth"]["label"] == "MiniMax Coding Plan / OAuth"
    assert by_id["stepfun-open"]["base_url"] == "https://api.stepfun.com/v1"
    assert by_id["stepfun-open"]["billing_scope"] == "open_platform"
    assert by_id["stepfun"]["billing_scope"] == "step_plan"
    assert by_id["zai"]["label"] == "GLM / Z.AI"
    assert by_id["qwen-oauth"]["label"] == "Alibaba Qwen OAuth"
    assert "qwen" in by_id["alibaba"]["aliases"]
    assert by_id["openai-compatible"]["requires_base_url"] is True
    assert "custom" not in by_id
    assert "kimi-coding" not in by_id
    assert "kimi-coding-cn" not in by_id


def test_tts_presets_include_self_hosted_voxcpm_and_stepfun(tmp_path):
    config = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    public = config.public_dict()
    presets = {item["id"]: item for item in public["tts_provider_presets"]}

    assert presets["stepfun-open-platform"]["provider"] == "stepfun"
    assert presets["stepfun-open-platform"]["base_url"] == "https://api.stepfun.com/v1"
    assert presets["stepfun-open-platform"]["billing_scope"] == "open_platform"
    assert presets["stepfun-open-platform"]["streaming"] is True
    assert presets["stepfun-step-plan"]["provider"] == "stepfun"
    assert presets["stepfun-step-plan"]["base_url"] == "https://api.stepfun.com/step_plan/v1"
    assert presets["stepfun-step-plan"]["billing_scope"] == "step_plan"
    assert presets["stepfun-step-plan"]["models"] == ["stepaudio-2.5-tts"]
    assert presets["stepfun-step-plan"]["route"] == "step_plan_ws_tts"
    assert presets["stepfun-step-plan"]["streaming"] is True
    assert "WebSocket" in presets["stepfun-step-plan"]["description"]
    assert "active Step Plan" in presets["stepfun-step-plan"]["description"]
    assert presets["voxcpm"]["provider"] == "voxcpm"
    assert presets["voxcpm"]["base_url"] == ""
    assert presets["voxcpm"]["voices"] == []
    assert presets["voxcpm"]["requires_base_url"] is True
    assert presets["custom-http"]["requires_base_url"] is True


def test_tts_probe_reports_health_timeout(monkeypatch):
    def fake_urlopen(_request, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_tts_endpoint(
        "http://tts.example.test/v1/audio/speech",
        provider="voxcpm",
        model="voxcpm2",
        voice="aura",
        audio_format="pcm",
        timeout=1.0,
    )

    assert payload["ok"] is False
    assert payload["stage"] == "health"
    assert payload["endpoint_host"] == "tts.example.test"
    assert "TTS health check failed" in payload["detail"]
    assert "service may be offline" in payload["detail"]


def test_asr_probe_uses_local_health_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_asr_endpoint(
        "http://host.docker.internal:8766/v1",
        provider="custom",
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "health"
    assert captured["url"] == "http://host.docker.internal:8766/health"


def test_asr_probe_accepts_transcription_endpoint_method_errors(monkeypatch):
    def fake_urlopen(_request, timeout):
        raise HTTPError(
            "https://api.openai.com/v1/audio/transcriptions",
            405,
            "method not allowed",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_asr_endpoint(
        "https://api.openai.com/v1",
        provider="openai",
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "transcriptions"
    assert payload["endpoint_host"] == "api.openai.com"


def test_stepfun_asr_presets_describe_step_plan_and_realtime_routes():
    presets = {item["id"]: item for item in asr_provider_presets()}

    assert presets["stepfun-step-plan"]["route"] == "step_plan_sse"
    assert presets["stepfun-step-plan"]["streaming"] is False
    assert presets["stepfun-step-plan"]["billing_scope"] == "step_plan"
    assert presets["stepfun-step-plan"]["recommended"] is True
    assert "/step_plan/v1" in presets["stepfun-step-plan"]["base_url"]
    assert "HTTP+SSE" in presets["stepfun-step-plan"]["description"]
    assert "订阅内" in presets["stepfun-step-plan"]["label"]
    assert presets["stepfun-stream"]["route"] == "realtime_ws"
    assert presets["stepfun-stream"]["streaming"] is True
    assert presets["stepfun-stream"]["billing_scope"] == "open_platform"
    assert presets["stepfun-stream"]["recommended"] is True
    assert presets["stepfun-stream"]["base_url"] == "https://api.stepfun.com/v1"
    assert "Aura/Lily 语义链" in presets["stepfun-stream"]["description"]
    assert presets["stepfun-step-plan-realtime"]["route"] == "step_plan_realtime_ws"
    assert presets["stepfun-step-plan-realtime"]["provider"] == "stepfun-realtime"
    assert presets["stepfun-step-plan-realtime"]["streaming"] is True
    assert presets["stepfun-step-plan-realtime"]["billing_scope"] == "step_plan"
    assert presets["stepfun-step-plan-realtime"]["recommended"] is False
    assert presets["stepfun-step-plan-realtime"]["base_url"] == "https://api.stepfun.com/step_plan/v1"
    assert presets["stepfun-step-plan-realtime"]["models"] == ["stepaudio-2.5-realtime"]
    assert "绕过 Aura/Lily" in presets["stepfun-step-plan-realtime"]["description"]


def test_admin_asr_test_refreshes_runtime_config_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "persona-home"))
    monkeypatch.setenv("AURA_COMPANION_HOME", str(tmp_path / "companion-home"))
    monkeypatch.setenv("AURA_LILY_AURA_RUNTIME_CONFIG_PATH", str(tmp_path / "aura_runtime.json"))

    runtime = lily_server.LilyRuntime(build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")])))
    assert runtime.aura_runtime_config
    assert runtime.aura_runtime_config.asr_provider != "stepfun-realtime"

    saved = save_aura_runtime_config(load_aura_runtime_config(persona_home=str(tmp_path / "persona-home")), {
        "asr_enabled": True,
        "asr_mode": "api",
        "asr_provider": "stepfun-realtime",
        "asr_model": "stepaudio-2.5-realtime",
        "asr_base_url": "https://api.stepfun.com/step_plan/v1",
        "asr_api_key": "step-plan-secret",
    })
    assert saved.asr_provider == "stepfun-realtime"

    monkeypatch.setattr(
        lily_server,
        "_probe_stepfun_step_plan_realtime_ws",
        lambda base_url, *, model, api_key, timeout: {
            "ok": True,
            "stage": "stepfun_step_plan_realtime_ws",
            "detail": "StepFun Step Plan Realtime WebSocket reachable.",
            "endpoint_host": "api.stepfun.com",
        },
    )

    payload = runtime.test_asr()

    assert payload["ok"] is True
    assert payload["provider"] == "stepfun-realtime"
    assert payload["model"] == "stepaudio-2.5-realtime"
    assert payload["stage"] == "stepfun_step_plan_realtime_ws"
    assert runtime.aura_runtime_config.asr_provider == "stepfun-realtime"
    assert "step-plan-secret" not in json.dumps(payload)


def test_voice_latency_path_distinguishes_step_plan_sse_from_realtime(tmp_path, monkeypatch):
    realtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="asr-key",
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="tts-key",
    )
    step_plan_sse = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="asr-key",
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="tts-key",
    )
    step_plan_sse_without_asr_key = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="",
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="tts-key",
    )
    step_plan_realtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="realtime-key",
    )

    realtime_path = voice_latency_path(realtime)
    assert realtime_path["xiaozhi_style_ready"] is True
    assert realtime_path["asr_streaming"] is True
    assert realtime_path["tts_streaming"] is True
    assert realtime_path["llm_streaming"] is True
    assert realtime_path["asr_billing_scope"] == "open_platform"
    assert realtime_path["tts_billing_scope"] == "step_plan"
    assert realtime_path["step_plan_covered"] is False
    assert realtime_path["asr_label"] == "StepFun 实时 WS ASR"

    step_plan_path = voice_latency_path(step_plan_sse)
    assert step_plan_path["xiaozhi_style_ready"] is False
    assert step_plan_path["asr_step_plan_sse"] is True
    assert step_plan_path["asr_streaming"] is False
    assert step_plan_path["tts_streaming"] is True
    assert step_plan_path["asr_label"] == "StepFun Step Plan SSE ASR"
    assert step_plan_path["llm_step_plan"] is True
    assert step_plan_path["tts_step_plan"] is True
    assert step_plan_path["step_plan_covered"] is True
    assert step_plan_path["asr_billing_scope"] == "step_plan"
    assert step_plan_path["llm_billing_scope"] == "step_plan"
    assert step_plan_path["tts_billing_scope"] == "step_plan"
    assert "录音结束后 SSE 转写" in step_plan_path["step_plan_summary"]

    open_platform = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_base_url="https://api.stepfun.com/v1",
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="asr-key",
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_base_url="https://api.stepfun.com/v1",
        tts_api_key="tts-key",
    )
    open_platform_path = voice_latency_path(open_platform)
    assert open_platform_path["xiaozhi_style_ready"] is True
    assert open_platform_path["semantic_stream_ready"] is True
    assert open_platform_path["step_plan_covered"] is False
    assert open_platform_path["llm_step_plan"] is False
    assert open_platform_path["tts_step_plan"] is False
    assert open_platform_path["asr_billing_scope"] == "open_platform"
    assert open_platform_path["llm_billing_scope"] == "open_platform"
    assert open_platform_path["tts_billing_scope"] == "open_platform"
    assert open_platform_path["tts_label"] == "StepFun Open Platform WS TTS"

    missing_key_path = voice_latency_path(step_plan_sse_without_asr_key)
    assert missing_key_path["asr_step_plan_sse"] is False
    assert missing_key_path["step_plan_covered"] is False
    assert "优先配置 Step Plan ASR SSE" in missing_key_path["step_plan_summary"]

    realtime_plan_path = voice_latency_path(step_plan_realtime)
    assert realtime_plan_path["xiaozhi_style_ready"] is False
    assert realtime_plan_path["step_plan_realtime_ready"] is False
    assert realtime_plan_path["step_plan_realtime_configured"] is True
    assert realtime_plan_path["step_plan_realtime_direct_enabled"] is False
    assert realtime_plan_path["asr_step_plan_realtime"] is True
    assert realtime_plan_path["asr_step_plan_realtime_direct"] is False
    assert realtime_plan_path["asr_streaming"] is False
    assert realtime_plan_path["asr_label"] == "StepFun Step Plan Realtime (实验直连未启用)"
    assert "默认不启用直连" in realtime_plan_path["step_plan_summary"]
    assert "不会绕过 Aura/Lily" in realtime_plan_path["step_plan_summary"]

    monkeypatch.setenv("AURA_STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", "1")
    direct_path = voice_latency_path(step_plan_realtime)
    assert direct_path["xiaozhi_style_ready"] is True
    assert direct_path["step_plan_realtime_ready"] is True
    assert direct_path["step_plan_realtime_direct_enabled"] is True
    assert direct_path["asr_step_plan_realtime_direct"] is True
    assert direct_path["asr_label"] == "StepFun Step Plan Realtime 直连"
    assert "实验直连" in direct_path["summary"]
    assert "绕过 Aura/Lily" in direct_path["step_plan_summary"]


def test_asr_probe_uses_stepfun_sse_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 405

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        raise HTTPError(request.full_url, 405, "method not allowed", hdrs=None, fp=None)

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_asr_endpoint(
        "https://api.stepfun.com/step_plan/v1",
        provider="stepfun",
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "stepfun_sse"
    assert payload["detail"] == "StepFun ASR SSE endpoint reachable."
    assert captured["url"] == "https://api.stepfun.com/step_plan/v1/audio/asr/sse"


def test_stepfun_realtime_asr_probe_requires_key():
    payload = lily_server._probe_stepfun_realtime_asr_ws(
        "https://api.stepfun.com/v1",
        model="stepaudio-2.5-asr-stream",
        language="zh",
        api_key="",
        timeout=3.0,
    )

    assert payload["ok"] is False
    assert payload["stage"] == "stepfun_realtime_ws"
    assert payload["endpoint_host"] == "api.stepfun.com"
    assert "API Key 未配置" in payload["detail"]


def test_stepfun_realtime_asr_probe_uses_ws_session_update(monkeypatch):
    sent = []
    captured = {}

    class FakeStepfunWs:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            return json.dumps({"type": "session.updated"})

    class FakeConnect:
        def __init__(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("additional_headers") or {}

        async def __aenter__(self):
            return FakeStepfunWs()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(lily_server, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))

    payload = lily_server._probe_stepfun_realtime_asr_ws(
        "https://api.stepfun.com/v1",
        model="stepaudio-2.5-asr-stream",
        language="zh",
        api_key="stepfun-unit-key",
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "stepfun_realtime_ws"
    assert payload["endpoint_host"] == "api.stepfun.com"
    assert "WebSocket reachable" in payload["detail"]
    assert captured["url"] == "wss://api.stepfun.com/v1/realtime/asr/stream"
    assert captured["headers"]["Authorization"] == "Bearer stepfun-unit-key"
    assert sent[0]["type"] == "session.update"
    input_config = sent[0]["session"]["audio"]["input"]
    assert input_config["format"]["rate"] == lily_server.DEVICE_SAMPLE_RATE
    assert input_config["transcription"]["model"] == "stepaudio-2.5-asr-stream"
    assert input_config["turn_detection"]["type"] == "server_vad"


def test_stepfun_step_plan_realtime_probe_uses_session_update(monkeypatch):
    sent = []
    captured = {}

    class FakeStepfunWs:
        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            return json.dumps({"type": "session.updated"})

    class FakeConnect:
        def __init__(self, url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("additional_headers") or {}

        async def __aenter__(self):
            return FakeStepfunWs()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(lily_server, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))

    payload = lily_server._probe_stepfun_step_plan_realtime_ws(
        "https://api.stepfun.com/step_plan/v1",
        model="stepaudio-2.5-realtime",
        api_key="stepfun-unit-key",
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "stepfun_step_plan_realtime_ws"
    assert payload["endpoint_host"] == "api.stepfun.com"
    assert "WebSocket reachable" in payload["detail"]
    assert captured["url"] == "wss://api.stepfun.com/step_plan/v1/realtime?model=stepaudio-2.5-realtime"
    assert captured["headers"]["Authorization"] == "Bearer stepfun-unit-key"
    assert sent[0]["type"] == "session.update"
    assert sent[0]["session"]["modalities"] == ["text", "audio"]
    assert "voice" not in sent[0]["session"]
    assert sent[0]["session"]["input_audio_format"] == "pcm16"
    assert sent[0]["session"]["output_audio_format"] == "pcm16"
    assert sent[0]["session"]["turn_detection"]["type"] == "server_vad"
    assert sent[0]["session"]["turn_detection"]["prefix_padding_ms"] == 500
    assert sent[0]["session"]["turn_detection"]["energy_awakeness_threshold"] == 2500


def test_tts_probe_uses_speech_endpoint_and_api_key(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size=-1):
            return b"audio"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_tts_endpoint(
        "https://api.example.com/v1",
        provider="openai",
        model="gpt-4o-mini-tts",
        voice="alloy",
        api_key="tts-secret",
        audio_format="mp3",
        timeout=2.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "speech"
    assert captured["url"] == "https://api.example.com/v1/audio/speech"
    assert captured["authorization"] == "Bearer tts-secret"
    assert captured["body"]["model"] == "gpt-4o-mini-tts"
    assert captured["body"]["voice"] == "alloy"
    assert captured["body"]["response_format"] == "mp3"


def test_tts_probe_builds_device_rate_wav_preview_for_voxcpm_pcm(monkeypatch):
    captured = {}
    source_rate = 24000
    source_samples = [int(index * 1000) for index in range(24)]
    source_pcm = b"".join(sample.to_bytes(2, "little", signed=True) for sample in source_samples)

    class FakeResponse:
        def __init__(self, status=200, body=b"") -> None:
            self.status = status
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size=-1):
            return self.body

    def fake_urlopen(request, timeout):
        if request.full_url == "http://tts.local/health":
            return FakeResponse(status=200)
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(status=200, body=source_pcm)

    monkeypatch.setattr(lily_server, "urlopen", fake_urlopen)

    payload = lily_server._probe_tts_endpoint(
        "http://tts.local/v1/audio/speech",
        provider="voxcpm",
        model="voxcpm2",
        voice="yan",
        audio_format="pcm",
        sample_rate=source_rate,
        timeout=3.0,
    )

    assert payload["ok"] is True
    assert payload["stage"] == "speech"
    assert captured["url"] == "http://tts.local/v1/audio/speech"
    assert captured["body"]["voice"] == "yan"
    assert captured["body"]["sample_rate"] == source_rate
    assert payload["source_sample_rate"] == source_rate
    assert payload["device_sample_rate"] == lily_server.DEVICE_SAMPLE_RATE
    assert payload["resampled_for_device"] is True
    assert payload["device_audio_bytes"] == int(len(source_pcm) / 2 * lily_server.DEVICE_SAMPLE_RATE / source_rate) * 2
    assert payload["audio_data_url"].startswith("data:audio/wav;base64,")


def test_gateway_stepfun_asr_url_uses_step_plan_endpoint():
    import integrations.hermes_lily_cli.gateway as gateway_module

    assert (
        gateway_module.asr_transcription_url("https://api.stepfun.com/step_plan/v1", provider="stepfun")
        == "https://api.stepfun.com/step_plan/v1/audio/asr/sse"
    )
    assert (
        gateway_module.asr_transcription_url("https://api.stepfun.com/step_plan/v1/audio/asr/sse", provider="stepfun")
        == "https://api.stepfun.com/step_plan/v1/audio/asr/sse"
    )


def test_gateway_stepfun_streaming_asr_url_and_availability(tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
    )
    step_plan_runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
    )

    assert (
        gateway_module.stepfun_ws_asr_url("https://api.stepfun.com/v1")
        == "wss://api.stepfun.com/v1/realtime/asr/stream"
    )
    assert gateway_module.streaming_asr_available(runtime) is True
    assert gateway_module.streaming_asr_available(step_plan_runtime) is False


def test_gateway_stepfun_step_plan_realtime_url_and_availability(tmp_path, monkeypatch):
    import integrations.hermes_lily_cli.gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
    )
    no_key = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="",
    )

    assert (
        gateway_module.stepfun_step_plan_realtime_url("https://api.stepfun.com/step_plan/v1", model="stepaudio-2.5-realtime")
        == "wss://api.stepfun.com/step_plan/v1/realtime?model=stepaudio-2.5-realtime"
    )
    monkeypatch.setattr(gateway_module, "STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", False)
    assert gateway_module.stepfun_step_plan_realtime_available(runtime) is False
    monkeypatch.setattr(gateway_module, "STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", True)
    assert gateway_module.stepfun_step_plan_realtime_available(runtime) is True
    assert gateway_module.stepfun_step_plan_realtime_available(no_key) is False


def test_gateway_stepfun_asr_sse_extracts_final_text():
    raw = (
        'event: transcript.text.delta\n'
        'data: {"type":"transcript.text.delta","data":{"delta":"测试"}}\n\n'
        'event: transcript.text.done\n'
        'data: {"type":"transcript.text.done","data":{"text":"测试一下天气"}}\n\n'
    ).encode("utf-8")

    assert stepfun_asr_text_from_response(raw) == "测试一下天气"


def test_gateway_stepfun_asr_posts_json_audio(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return 'data: {"type":"transcript.text.done","data":{"text":"你好 Lily"}}\n\n'.encode("utf-8")

    def fake_pooled_open(req, *, timeout, retry_once=True):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(gateway_module, "open_pooled_http_request", fake_pooled_open)
    monkeypatch.setattr(gateway_module, "ASR_HTTP_KEEPALIVE_ENABLED", True)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="stepfun-unit-key",
        asr_language="zh",
        asr_timeout_seconds=11,
    )

    result = transcribe_with_api(runtime, b"RIFFfake-wav")

    assert result == AsrResult(ok=True, text="你好 Lily")
    assert captured["url"] == "https://api.stepfun.com/step_plan/v1/audio/asr/sse"
    assert captured["headers"]["Authorization"] == "Bearer stepfun-unit-key"
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["body"]["audio"]["input"]["transcription"]["model"] == "stepaudio-2.5-asr"
    assert captured["body"]["audio"]["input"]["transcription"]["language"] == "zh"
    assert captured["body"]["audio"]["input"]["format"]["type"] == "wav"
    assert captured["body"]["audio"]["data"]
    assert captured["timeout"] == 11


def test_gateway_stepfun_asr_falls_back_to_urlopen_without_keepalive(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return 'data: {"type":"transcript.text.done","data":{"text":"你好"}}\n\n'.encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_module, "ASR_HTTP_KEEPALIVE_ENABLED", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="stepfun-unit-key",
        asr_language="zh",
        asr_timeout_seconds=11,
    )

    result = transcribe_with_api(runtime, b"RIFFfake-wav")

    assert result == AsrResult(ok=True, text="你好")
    assert captured["url"] == "https://api.stepfun.com/step_plan/v1/audio/asr/sse"


def test_gateway_asr_failure_reply_matches_status():
    reply_for = gateway_module.asr_failure_reply
    assert "配置" in reply_for(AsrResult(ok=False, status="asr_disabled"))
    assert "没连上" in reply_for(AsrResult(ok=False, status="asr_api_failed", detail="TimeoutError"))
    assert "没连上" in reply_for(AsrResult(ok=False, status="asr_http_error", detail="HTTP 429"))
    assert "没有收到声音" in reply_for(AsrResult(ok=False, status="empty_audio"))
    assert "再说一遍" in reply_for(AsrResult(ok=False, status="empty_transcript"))
    for status in ("asr_disabled", "asr_api_failed", "asr_http_error", "empty_audio", "empty_transcript"):
        assert "本地 ASR" not in reply_for(AsrResult(ok=False, status=status))
    assert "say it again" in reply_for(AsrResult(ok=False, status="empty_transcript"), language="en")
    assert "もう一度" in reply_for(AsrResult(ok=False, status="empty_transcript"), language="ja")


def _tap_guard_runtime(tmp_path):
    return AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )


def test_gateway_run_voice_turn_tap_guard_skips_reply_for_tiny_audio(monkeypatch, tmp_path):
    sent = []
    tts_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    def fake_transcribe(runtime_config, state):
        state.asr_pcm_ms = 240
        return AsrResult(ok=False, status="empty_transcript")

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        tts_calls.append(response)

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: _tap_guard_runtime(tmp_path))
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_transcribe)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "ASR_TAP_GUARD_MS", 500)

    state = TurnState(turn_id=7, audio_chunks=[b"pcm"])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    assert tts_calls == []
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert any(
        item.get("type") == "system" and item.get("payload", {}).get("action") == "asr_failed"
        for item in messages
    )


def test_gateway_run_voice_turn_tap_guard_keeps_reply_for_long_audio(monkeypatch, tmp_path):
    tts_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            pass

    def fake_transcribe(runtime_config, state):
        state.asr_pcm_ms = 1500
        return AsrResult(ok=False, status="empty_transcript")

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        tts_calls.append(response)

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: _tap_guard_runtime(tmp_path))
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_transcribe)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "ASR_TAP_GUARD_MS", 500)

    state = TurnState(turn_id=8, audio_chunks=[b"pcm"])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    assert tts_calls == ["我还没听清楚，再说一遍好吗？"]


def test_gateway_run_voice_turn_swallows_connection_closed(monkeypatch):
    from websockets.exceptions import ConnectionClosedError

    async def boom(websocket, config, state):
        raise ConnectionClosedError(None, None, None)

    monkeypatch.setattr(gateway_module, "_run_voice_turn", boom)

    class FakeWebsocket:
        async def send(self, payload):
            pass

    state = TurnState(turn_id=9, audio_chunks=[b"pcm"])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    asyncio.run(gateway_module.run_voice_turn(FakeWebsocket(), config, state))


def test_gateway_stepfun_ws_asr_session_sends_audio_and_reads_final(monkeypatch, tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunAsrSocket()
            self.socket.recv_queue.put_nowait(json.dumps({
                "type": "transcript.text.done",
                "data": {"text": "测试一下天气"},
            }))

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connections = []

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="stepfun-unit-key",
        asr_language="zh",
        asr_timeout_seconds=3,
    )
    state = TurnState(
        turn_id=9,
        sample_rate=DEVICE_SAMPLE_RATE,
        audio_format="pcm",
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2,
    )

    async def scenario():
        session = gateway_module.StepfunWsAsrSession(runtime, state)
        await session.start()
        await session.send_pcm(b"\x01\x00" * 320)
        result = await session.finish()
        return result, session

    result, session = asyncio.run(scenario())

    assert result == AsrResult(ok=True, text="测试一下天气", status="streaming_asr")
    assert connections[0].url == "wss://api.stepfun.com/v1/realtime/asr/stream"
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert sent_types[0] == "session.update"
    assert "input_audio_buffer.append" in sent_types
    assert "input_audio_buffer.commit" not in sent_types
    append_event = [item for item in connections[0].socket.sent if item["type"] == "input_audio_buffer.append"][-1]
    assert append_event["audio"] == append_event["data"]["audio"]
    input_config = connections[0].socket.sent[0]["session"]["audio"]["input"]
    assert input_config["transcription"]["model"] == "stepaudio-2.5-asr-stream"
    assert input_config["format"]["type"] == "pcm"
    assert input_config["format"]["codec"] == "pcm_s16le"
    assert input_config["format"]["rate"] == DEVICE_SAMPLE_RATE
    assert input_config["turn_detection"]["type"] == "server_vad"
    assert session.first_delta_ms >= 0


def test_gateway_stepfun_ws_asr_server_vad_commits_when_no_text(monkeypatch, tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.commit":
                self.recv_queue.put_nowait(json.dumps({"type": "input_audio_buffer.committed"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()
            self.socket.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connections = []

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_FINAL_WAIT_MS", 100)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="stepfun-unit-key",
        asr_timeout_seconds=3,
    )
    state = TurnState(turn_id=10, sample_rate=DEVICE_SAMPLE_RATE, audio_format="pcm")

    async def scenario():
        session = gateway_module.StepfunWsAsrSession(runtime, state)
        await session.start()
        await session.send_pcm(b"\x01\x00" * 320)
        result = await session.finish()
        return result

    result = asyncio.run(scenario())

    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert "input_audio_buffer.append" in sent_types
    assert "input_audio_buffer.commit" in sent_types
    assert result.ok is False
    assert result.status == "streaming_asr_empty"
    assert "events=" in result.detail


def test_gateway_stepfun_step_plan_realtime_session_streams_audio_to_device(monkeypatch, tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    sent_to_device = []
    pcm = b"\x01\x00" * 640

    class FakeDeviceWebsocket:
        async def send(self, payload):
            sent_to_device.append(payload)

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "response.create":
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.text.delta", "delta": "好的。"}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.done"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connections = []

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="stepfun-unit-key",
        asr_timeout_seconds=3,
        tts_voice="voice-tone-test",
        cached_weather_enabled=True,
        cached_weather_city="上海",
        cached_weather_temperature="31.2",
        cached_weather_condition="多云",
        cached_weather_updated_at=int(time.time()),
    )
    state = TurnState(turn_id=19, sample_rate=DEVICE_SAMPLE_RATE, audio_format="pcm")
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        session = gateway_module.StepfunStepPlanRealtimeSession(FakeDeviceWebsocket(), config, runtime, state)
        await session.start()
        await session.send_pcm(pcm)
        result = await session.finish()
        return result, session

    result, session = asyncio.run(scenario())

    assert result.ok is True
    assert result.audio_bytes == len(pcm)
    assert result.detail == "好的。"
    assert connections[0].url == "wss://api.stepfun.com/step_plan/v1/realtime?model=stepaudio-2.5-realtime"
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert sent_types[0] == "session.update"
    session_update = connections[0].socket.sent[0]
    assert session_update["session"]["voice"] == "voice-tone-test"
    assert session_update["session"]["turn_detection"]["prefix_padding_ms"] == 500
    assert session_update["session"]["turn_detection"]["energy_awakeness_threshold"] == 2500
    assert "input_audio_buffer.append" in sent_types
    assert "input_audio_buffer.commit" not in sent_types
    assert "response.create" in sent_types
    response_create = [item for item in connections[0].socket.sent if item["type"] == "response.create"][-1]
    assert "不是翻译" in response_create["response"]["instructions"]
    assert "上海，31.2度，多云" in response_create["response"]["instructions"]
    append_events = [item for item in connections[0].socket.sent if item["type"] == "input_audio_buffer.append"]
    assert len(append_events) == 2
    assert len(base64.b64decode(append_events[-1]["audio"])) > len(pcm)
    audio_frames = [item for item in sent_to_device if isinstance(item, bytes)]
    assert audio_frames
    assert audio_frames[0][:4] == TTS_BINARY_MAGIC
    assert audio_frames[0][16:] == pcm
    assert audio_frames[-1][12] == 1


def test_gateway_stepfun_step_plan_realtime_reads_response_done_text(monkeypatch, tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    sent_to_device = []
    pcm = b"\x02\x00" * 640

    class FakeDeviceWebsocket:
        async def send(self, payload):
            sent_to_device.append(payload)

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "response.create":
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "response.done",
                    "response": {
                        "status": "completed",
                        "output": [{
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "已经好了。"}],
                        }],
                    },
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="stepfun-unit-key",
        asr_timeout_seconds=3,
    )
    state = TurnState(turn_id=21, sample_rate=DEVICE_SAMPLE_RATE, audio_format="pcm")
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        session = gateway_module.StepfunStepPlanRealtimeSession(FakeDeviceWebsocket(), config, runtime, state)
        await session.start()
        await session.send_pcm(pcm)
        return await session.finish()

    result = asyncio.run(scenario())

    assert result.ok is True
    assert result.detail == "已经好了。"
    assert sent_to_device


def test_gateway_stepfun_step_plan_realtime_manual_turn_detection_commits(monkeypatch, tmp_path):
    import integrations.hermes_lily_cli.gateway as gateway_module

    sent_to_device = []
    pcm = b"\x01\x00" * 640

    class FakeDeviceWebsocket:
        async def send(self, payload):
            sent_to_device.append(payload)

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "response.create":
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.done"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    connections = []

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_SERVER_VAD", False)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="stepfun-unit-key",
        asr_timeout_seconds=3,
    )
    state = TurnState(turn_id=20, sample_rate=DEVICE_SAMPLE_RATE, audio_format="pcm")
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        session = gateway_module.StepfunStepPlanRealtimeSession(FakeDeviceWebsocket(), config, runtime, state)
        await session.start()
        await session.send_pcm(pcm)
        return await session.finish()

    result = asyncio.run(scenario())

    assert result.ok is True
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert "input_audio_buffer.commit" in sent_types
    assert "turn_detection" not in connections[0].socket.sent[0]["session"]
    assert sent_to_device


def test_gateway_tts_splits_long_text_and_concatenates_audio(monkeypatch, tmp_path):
    requests = []

    class FakeResponse:
        def __init__(self, audio: bytes) -> None:
            self.audio = audio

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.audio

    def fake_urlopen(req, timeout):
        body = json.loads(req.data.decode("utf-8"))
        requests.append({"url": req.full_url, "body": body, "timeout": timeout})
        return FakeResponse(f"pcm:{len(requests)};".encode("ascii"))

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="aura",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
        tts_timeout_seconds=7,
    )
    text = "第一句很短。第二句比较长，需要按照逗号拆开，不然 VoxCPM 可能会在一次长文本合成里超时。"

    result = synthesize_tts(runtime, text)

    assert result.ok is True
    assert result.audio == b"".join(f"pcm:{index};".encode("ascii") for index in range(1, len(requests) + 1))
    assert len(requests) >= 2
    assert requests[0]["url"] == "http://tts.local/v1/audio/speech"
    assert requests[0]["timeout"] == 7
    assert all(len(item["body"]["input"]) <= 42 for item in requests)
    assert all(item["body"]["sample_rate"] == DEVICE_SAMPLE_RATE for item in requests)


def test_gateway_tts_resamples_configured_sample_rate_for_device(monkeypatch, tmp_path):
    captured = {}
    source_rate = 24000
    source_samples = [int(index * 1200) for index in range(12)]
    source_pcm = b"".join(sample.to_bytes(2, "little", signed=True) for sample in source_samples)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return source_pcm

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=source_rate,
        tts_timeout_seconds=9,
    )

    result = synthesize_tts(runtime, "测试音色")

    assert result.ok is True
    assert captured["body"]["voice"] == "yan"
    assert captured["body"]["sample_rate"] == source_rate
    assert captured["timeout"] == 9
    assert len(result.audio) == int(len(source_pcm) / 2 * DEVICE_SAMPLE_RATE / source_rate) * 2
    assert result.audio != source_pcm


def test_gateway_synthesizes_and_streams_tts_chunk_by_chunk(monkeypatch, tmp_path):
    sent = []
    requests = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeResponse:
        def __init__(self, audio: bytes) -> None:
            self.parts = [audio]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            if not self.parts:
                return b""
            return self.parts.pop(0)

    def fake_urlopen(req, timeout):
        body = json.loads(req.data.decode("utf-8"))
        requests.append(body)
        return FakeResponse((f"pcm-{len(requests)};").encode("ascii"))

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )

    result = asyncio.run(synthesize_and_stream_tts(
        FakeWebsocket(),
        runtime,
        11,
        "第一句。第二句。",
        stream_id=3,
    ))

    audio_frames = [item for item in sent if isinstance(item, bytes)]
    system_frames = [json.loads(item) for item in sent if isinstance(item, str)]
    assert result.ok is True
    assert result.chunk_count == 2
    assert len(requests) == 2
    assert len(audio_frames) == 2
    assert audio_frames[0][12] == 0
    assert audio_frames[1][12] == 1
    assert system_frames[-1]["payload"]["action"] == "tts_completed"
    assert system_frames[-1]["payload"]["chunk_count"] == 2
    assert system_frames[-1]["payload"]["first_audio_ms"] >= 0
    assert system_frames[-1]["payload"]["tts_audio_chunk_gap_count"] >= 1
    assert system_frames[-1]["payload"]["tts_audio_chunk_gap_max_ms"] >= 0


def test_gateway_tts_timing_reports_audio_chunk_stalls(monkeypatch, tmp_path):
    sent = []

    class SlowFakeWebsocket:
        async def send(self, payload):
            sent.append(payload)
            if isinstance(payload, bytes) and len(payload) > 16:
                await asyncio.sleep(0.31)

    class FakeResponse:
        def __init__(self, audio: bytes) -> None:
            self.audio = audio

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            audio, self.audio = self.audio, b""
            return audio

    def fake_urlopen(req, timeout):
        return FakeResponse(b"a" * 17)

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "TTS_CHUNK_BYTES", 8)
    monkeypatch.setattr(gateway_module.request, "urlopen", fake_urlopen)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )

    result = asyncio.run(gateway_module.synthesize_and_stream_tts(
        SlowFakeWebsocket(),
        runtime,
        12,
        "测试。",
        stream_id=4,
    ))

    assert result.ok is True
    assert result.audio_chunk_gap_count >= 2
    assert result.audio_chunk_gap_max_ms >= 300
    assert result.audio_chunk_stall_count >= 1
    system_frames = [json.loads(item) for item in sent if isinstance(item, str)]
    completed = [item for item in system_frames if item.get("payload", {}).get("action") == "tts_completed"][-1]
    assert completed["payload"]["tts_audio_chunk_gap_max_ms"] >= 300
    assert completed["payload"]["tts_audio_chunk_stall_count"] >= 1


def test_gateway_default_tts_binary_chunk_matches_device_prefetch_window():
    from integrations.hermes_lily_cli import gateway as gateway_module

    assert gateway_module.TTS_CHUNK_BYTES == 2048


def test_esp32_tts_followup_queue_matches_prefetch_window():
    ws_client = Path("firmware/esp32/main/network/ws_client.c").read_text(encoding="utf-8")

    assert "#define TTS_PREFETCH_BYTES 16384" in ws_client
    assert "#define TTS_STREAM_QUEUE_BYTES 2048" in ws_client


def test_gateway_tts_send_pacing_can_be_disabled(monkeypatch):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fail_sleep(delay):
        raise AssertionError(f"unexpected pacing sleep: {delay}")

    monkeypatch.setattr(gateway_module, "TTS_CHUNK_BYTES", 1024)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_ENABLED", False)
    monkeypatch.setattr(gateway_module.asyncio, "sleep", fail_sleep)
    timing = gateway_module.AudioChunkTiming(time.monotonic())

    asyncio.run(gateway_module.send_tts_pcm_stream(
        FakeWebsocket(),
        77,
        b"a" * 4096,
        stream_id=2,
        is_final=True,
        timing=timing,
    ))

    frames = [item for item in sent if isinstance(item, bytes)]
    assert len(frames) == 4
    assert frames[-1][12] == 1
    payload = gateway_module.tts_chunk_timing_payload(TtsResult(
        ok=True,
        streamed=True,
        **gateway_module.audio_chunk_timing_summary(timing),
    ))
    assert payload["tts_audio_send_pacing_enabled"] is False
    assert payload["tts_audio_send_pacing_sleep_count"] == 0
    assert payload["tts_audio_send_bytes"] == 4096
    assert "tts_audio_buffer_lead_min_ms" in payload
    assert payload["tts_audio_buffer_lead_final_ms"] >= 0


def test_gateway_tts_send_pacing_can_be_enabled(monkeypatch):
    sent = []
    sleeps = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(gateway_module, "TTS_CHUNK_BYTES", 4096)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_ENABLED", True)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_RATE", 1.25)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_PREFILL_MS", 0)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS", 10)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_DIRECT_PACKETS", 0)
    monkeypatch.setattr(gateway_module.asyncio, "sleep", fake_sleep)
    timing = gateway_module.AudioChunkTiming(time.monotonic())

    asyncio.run(gateway_module.send_tts_pcm_stream(
        FakeWebsocket(),
        78,
        b"a" * 12288,
        stream_id=2,
        is_final=True,
        timing=timing,
    ))

    assert sleeps
    assert max(sleeps) <= 0.011
    frames = [item for item in sent if isinstance(item, bytes)]
    assert len(frames) == 3
    assert frames[-1][12] == 1
    payload = gateway_module.tts_chunk_timing_payload(TtsResult(
        ok=True,
        streamed=True,
        **gateway_module.audio_chunk_timing_summary(timing),
    ))
    assert payload["tts_audio_send_pacing_enabled"] is True
    assert payload["tts_audio_send_pacing_rate_x100"] == 125
    assert payload["tts_audio_send_pacing_sleep_count"] >= 1
    assert payload["tts_audio_send_pacing_sleep_ms"] >= 1
    assert "tts_audio_buffer_lead_min_ms" in payload


def test_gateway_tts_send_pacing_direct_packets_match_xiaozhi_prebuffer(monkeypatch):
    sent = []
    sleeps = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(gateway_module, "TTS_CHUNK_BYTES", 1024)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_ENABLED", True)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_RATE", 1.0)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_PREFILL_MS", 0)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_PACING_MAX_SLEEP_MS", 100)
    monkeypatch.setattr(gateway_module, "TTS_AUDIO_SEND_DIRECT_PACKETS", 5)
    monkeypatch.setattr(gateway_module.asyncio, "sleep", fake_sleep)
    timing = gateway_module.AudioChunkTiming(time.monotonic())

    asyncio.run(gateway_module.send_tts_pcm_stream(
        FakeWebsocket(),
        79,
        b"a" * 6144,
        stream_id=2,
        is_final=True,
        timing=timing,
    ))

    frames = [item for item in sent if isinstance(item, bytes)]
    assert len(frames) == 6
    assert frames[-1][12] == 1
    assert len(sleeps) <= 1
    payload = gateway_module.tts_chunk_timing_payload(TtsResult(
        ok=True,
        streamed=True,
        **gateway_module.audio_chunk_timing_summary(timing),
    ))
    assert payload["tts_audio_send_pacing_enabled"] is True
    assert payload["tts_audio_send_pacing_sleep_count"] <= 1


def test_gateway_voice_latency_diagnosis_identifies_main_bottlenecks():
    state = TurnState(streaming_asr_final_ms=800)

    llm = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=2100,
        tts_first_text_ms=2200,
        tts_first_audio_since_bridge_ms=2600,
        tts_first_text_to_audio_ms=400,
    )
    assert llm["latency_bottleneck"] == "llm_first_delta"
    assert llm["latency_severity"] in {"medium", "high"}

    llm_with_stall = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=1310,
        tts_first_text_ms=1314,
        tts_first_audio_since_bridge_ms=1860,
        tts_first_text_to_audio_ms=546,
        audio_chunk_stall_count=1,
    )
    assert llm_with_stall["latency_bottleneck"] == "llm_first_delta"
    assert llm_with_stall["latency_tts_text_queue_ms"] == 4
    assert llm_with_stall["latency_audio_stall_count"] == 1

    borderline_llm_with_stall = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=1160,
        tts_first_text_ms=1162,
        tts_first_audio_since_bridge_ms=1749,
        tts_first_text_to_audio_ms=587,
        audio_chunk_stall_count=2,
    )
    assert borderline_llm_with_stall["latency_bottleneck"] == "llm_first_delta"
    assert borderline_llm_with_stall["latency_severity"] == "medium"

    tts = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=300,
        tts_first_text_ms=360,
        tts_first_audio_since_bridge_ms=2300,
        tts_first_text_to_audio_ms=1940,
        tts_provider_stream="stepfun_ws_session",
    )
    assert tts["latency_bottleneck"] == "tts_first_audio"
    assert tts["latency_tts_provider_stream"] == "stepfun_ws_session"

    send = voice_latency_diagnosis(
        state,
        bridge_first_delta_ms=240,
        tts_first_text_ms=260,
        tts_first_audio_since_bridge_ms=900,
        tts_first_text_to_audio_ms=640,
        audio_send_realtime_x100=60,
    )
    assert send["latency_bottleneck"] == "audio_send"
    assert "发送音频" in send["latency_recommendation"]


def test_gateway_ws_exception_detail_includes_status_and_scrubs_secret():
    response = Response(
        429,
        "Too Many Requests",
        Headers([("content-type", "application/json")]),
        b'{"error":"rate limit","api_key":"sk-unit-secret"}',
    )
    detail = gateway_module._ws_exception_detail("StepFun WS TTS", InvalidStatus(response))

    assert "InvalidStatus" in detail
    assert "HTTP 429" in detail
    assert "Too Many Requests" in detail
    assert "application/json" in detail
    assert "sk-unit-secret" not in detail
    assert "<redacted>" in detail


def test_gateway_stepfun_ws_tts_streams_audio_delta(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.messages = [
                json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-1"}}),
                json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-1"}}),
                json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {
                        "session_id": "sid-1",
                        "status": "finished",
                        "audio": base64.b64encode(b"pcm-ws").decode("ascii"),
                    },
                }),
                json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-1", "audio": ""},
                }),
            ]

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            return self.messages.pop(0)

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    def fake_connect(url, **kwargs):
        return FakeConnect(url, **kwargs)

    monkeypatch.setattr(gateway_module, "ws_connect", fake_connect)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )

    result = asyncio.run(synthesize_and_stream_tts(
        FakeWebsocket(),
        runtime,
        21,
        "你好呀。",
        stream_id=2,
    ))

    audio_frames = [item for item in sent if isinstance(item, bytes)]
    system_frames = [json.loads(item) for item in sent if isinstance(item, str)]
    assert result.ok is True
    assert result.streamed is True
    assert result.audio_bytes == len(b"pcm-ws")
    assert audio_frames[0][16:] == b"pcm-ws"
    assert audio_frames[0][12] == 0
    assert audio_frames[-1][12] == 1
    assert system_frames[-1]["payload"]["provider_stream"] == "stepfun_ws"
    assert connections[0].url == "wss://api.stepfun.com/step_plan/v1/realtime/audio?model=stepaudio-2.5-tts"
    assert connections[0].kwargs["additional_headers"]["Authorization"] == "Bearer unit-key"
    assert connections[0].kwargs["open_timeout"] == gateway_module.STEPFUN_WS_TTS_OPEN_TIMEOUT_SECONDS
    create_event = connections[0].socket.sent[0]
    assert create_event["type"] == "tts.create"
    assert create_event["data"]["voice_id"] == "voice-tone-test"
    assert create_event["data"]["response_format"] == "pcm"
    assert create_event["data"]["sample_rate"] == DEVICE_SAMPLE_RATE
    assert connections[0].socket.sent[1]["type"] == "tts.text.delta"
    assert connections[0].socket.sent[2]["type"] == "tts.text.flush"
    assert connections[0].socket.sent[3]["type"] == "tts.text.done"


def test_gateway_stepfun_ws_tts_url_preserves_billing_scope():
    assert (
        gateway_module.stepfun_ws_tts_url("https://api.stepfun.com/v1", model="stepaudio-2.5-tts")
        == "wss://api.stepfun.com/v1/realtime/audio?model=stepaudio-2.5-tts"
    )
    assert (
        gateway_module.stepfun_ws_tts_url("https://api.stepfun.com/step_plan/v1", model="stepaudio-2.5-tts")
        == "wss://api.stepfun.com/step_plan/v1/realtime/audio?model=stepaudio-2.5-tts"
    )


def test_gateway_stepfun_ws_tts_falls_back_to_http(monkeypatch, tmp_path):
    sent = []
    requests = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"http-pcm"

    from integrations.hermes_lily_cli import gateway as gateway_module

    def fake_connect(url, **kwargs):
        raise OSError("ws unavailable")

    def fake_urlopen(req, timeout):
        requests.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(gateway_module, "ws_connect", fake_connect)
    monkeypatch.setattr(gateway_module.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FALLBACK_HTTP", True)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )

    result = asyncio.run(synthesize_and_stream_tts(
        FakeWebsocket(),
        runtime,
        22,
        "你好呀。",
        stream_id=3,
    ))

    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert result.ok is True
    assert requests[0]["model"] == "stepaudio-2.5-tts"
    assert requests[0]["voice"] == "voice-tone-test"
    assert audio_frames[0][16:] == b"http-pcm"
    assert audio_frames[0][12] == 1


def test_gateway_bridge_stream_silent_drop_does_not_emit_tts(monkeypatch, tmp_path):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "ignored",
                "response": "",
                "request_id": "req-silent",
                "evidence": {"streamed": True, "silent": True, "model_skipped": True},
            },
        }

    def should_not_synthesize(runtime_config, text):
        raise AssertionError("Silent drops should not synthesize TTS")

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", should_not_synthesize)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=24, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "嗯",
    ))

    assert streamed is True
    assert not [item for item in sent if isinstance(item, bytes)]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert not [item for item in messages if item.get("type") == "dialogue"]
    silent = [item for item in messages if item.get("payload", {}).get("action") == "turn_silent_drop"][-1]
    assert silent["payload"]["status"] == "ignored"
    assert silent["payload"]["streamed_bridge"] is True


def test_gateway_bridge_stream_reuses_stepfun_ws_tts_session(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-bridge"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-bridge"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-bridge", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-bridge", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "你好呀，"}
        yield {"type": "delta", "text": "第二句。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "你好呀，第二句。",
                "request_id": "req-stream",
                "evidence": {
                    "streamed": True,
                    "persona_turn_latency_ms": 999,
                    "persona_context_build_ms": 7,
                    "aura_llm_prompt_chars": 3210,
                    "aura_llm_user_prompt_chars": 3210,
                    "aura_llm_system_prompt_chars": 180,
                    "aura_llm_max_tokens": 96,
                    "aura_llm_response_open_ms": 123,
                    "aura_llm_first_delta_ms": 420,
                    "aura_llm_response_to_first_delta_ms": 297,
                    "aura_llm_first_raw_delta_ms": 421,
                    "aura_llm_first_audible_delta_ms": 430,
                    "aura_llm_complete_ms": 980,
                    "stop_reason": "voice_compact_limit",
                },
                "debug": {
                    "aura_runtime": {
                        "aura_model_mode": "aura_model",
                        "aura_model_provider": "stepfun",
                        "aura_model_model": "stepaudio-2.5-chat",
                        "aura_model_billing_scope": "step_plan",
                        "tts_billing_scope": "step_plan",
                        "model_route": "direct_llm",
                    },
                },
            },
        }

    def fake_connect(url, **kwargs):
        return FakeConnect(url, **kwargs)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", fake_connect)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=23, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    assert len(connections) == 1
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == ["你好呀，", "第二句。"]
    assert connections[0].socket.sent[-1]["type"] == "tts.text.done"
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert len(audio_frames) >= 3
    assert any(frame[16:].startswith(b"pcm:") for frame in audio_frames)
    assert audio_frames[-1][12] == 1
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["streamed_bridge"] is True
    assert timing["payload"]["tts_provider_stream"] == "stepfun_ws_session"
    assert timing["payload"]["tts_first_text_ms"] >= 0
    assert timing["payload"]["tts_first_audio_since_bridge_ms"] >= 0
    assert timing["payload"]["tts_first_text_to_audio_ms"] >= 0
    assert timing["payload"]["bridge_first_delta_to_tts_first_text_ms"] >= 0
    assert timing["payload"]["tts_ws_text_count"] == 2
    assert timing["payload"]["tts_ws_audio_chunks"] >= 1
    assert "tts_audio_chunk_gap_p95_ms" in timing["payload"]
    assert "tts_audio_chunk_stall_count" in timing["payload"]
    assert "asr_decode_ms" in timing["payload"]
    assert "asr_backend_ms" in timing["payload"]
    assert "asr_wav_bytes" in timing["payload"]
    assert timing["payload"]["latency_bottleneck"] in {"ok", "borderline", "unknown"}
    assert "latency_recommendation" in timing["payload"]
    assert timing["payload"]["transcript_preview"] == "你好"
    assert timing["payload"]["response_preview"] == "你好呀，第二句。"
    assert timing["payload"]["model_skipped"] is False
    assert timing["payload"]["persona_turn_latency_ms"] == 999
    assert timing["payload"]["persona_context_build_ms"] == 7
    assert timing["payload"]["aura_llm_prompt_chars"] == 3210
    assert timing["payload"]["aura_llm_response_open_ms"] == 123
    assert timing["payload"]["aura_llm_first_delta_ms"] == 420
    assert timing["payload"]["aura_llm_response_to_first_delta_ms"] == 297
    assert timing["payload"]["aura_llm_first_raw_delta_ms"] == 421
    assert timing["payload"]["aura_llm_first_audible_delta_ms"] == 430
    assert timing["payload"]["aura_llm_complete_ms"] == 980
    assert timing["payload"]["aura_llm_stop_reason"] == "voice_compact_limit"
    assert timing["payload"]["aura_model_billing_scope"] == "step_plan"
    assert timing["payload"]["tts_billing_scope"] == "step_plan"


def test_gateway_bridge_stream_sends_short_unpunctuated_first_segment(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-short-first"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-short-first"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-short-first", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-short-first", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "我在这里"}
        yield {"type": "delta", "text": "陪你慢慢来"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在这里陪你慢慢来",
                "request_id": "req-short-first",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=26, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    # 无标点文本不再按字符硬切，由 final flush 整段提交，避免词中停顿。
    assert [item["data"]["text"] for item in text_events] == ["我在这里陪你慢慢来"]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:].startswith("pcm:我在这里陪你慢慢来".encode("utf-8")) for frame in audio_frames)
    assert audio_frames[-1][12] == 1


def test_gateway_bridge_stream_fallback_http_streams_segment_after_ws_failure(monkeypatch, tmp_path):
    sent = []
    requests = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeResponse:
        def __init__(self, audio: bytes) -> None:
            self.audio = audio

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.audio

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "第一小句很快。第二小句继续。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "第一小句很快。第二小句继续。",
                "request_id": "req-http-fallback",
                "evidence": {"streamed": True},
            },
        }

    def fake_connect(url, **kwargs):
        raise OSError("ws unavailable")

    def fake_urlopen(req, timeout):
        body = json.loads(req.data.decode("utf-8"))
        requests.append(body)
        return FakeResponse(f"http:{body['input']}".encode("utf-8"))

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", fake_connect)
    monkeypatch.setattr(gateway_module.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FALLBACK_HTTP", True)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=29, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    assert requests
    assert requests[0]["model"] == "stepaudio-2.5-tts"
    assert requests[0]["input"].startswith("第一小句")
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert audio_frames
    assert any(frame[16:].startswith("http:第一小句".encode("utf-8")) for frame in audio_frames)
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_provider_stream"] == ""
    assert timing["payload"]["tts_first_text_to_audio_ms"] >= 0
    assert timing["payload"]["streamed_bridge"] is True


def test_gateway_stepfun_ws_tts_rate_limit_cooldown_skips_warm_and_falls_back(monkeypatch, tmp_path):
    sent = []
    requests = []
    logs = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"http-pcm"

    from integrations.hermes_lily_cli import gateway as gateway_module

    class RateLimitedConnect:
        def __init__(self, url, **kwargs):
            pass

        async def __aenter__(self):
            raise OSError("HTTP 429 Too Many Requests; request limited concurrency reached")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_urlopen(req, timeout):
        requests.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: RateLimitedConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: logs.append((event, fields)))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FALLBACK_HTTP", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_429_COOLDOWN_SECONDS", 1.0)
    monkeypatch.setattr(gateway_module, "_STEPFUN_WS_TTS_COOLDOWN_UNTIL", 0.0)
    monkeypatch.setattr(gateway_module, "_STEPFUN_WS_TTS_COOLDOWN_DETAIL", "")

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )

    result = asyncio.run(gateway_module.synthesize_and_stream_tts(
        FakeWebsocket(),
        runtime,
        32,
        "你好呀。",
        stream_id=1,
    ))

    assert result.ok is True
    assert requests and requests[0]["input"] == "你好呀。"
    assert any(event == "stepfun_ws_tts_cooldown" for event, _fields in logs)
    assert gateway_module.stepfun_ws_tts_cooling_down() is True

    state = TurnState(turn_id=33)
    asyncio.run(gateway_module.maybe_start_stepfun_tts_warm_session(FakeWebsocket(), state, runtime))

    assert state.stepfun_tts_warm_task is None
    assert any(
        event == "stepfun_ws_tts_warm_skip" and fields.get("reason") == "cooldown"
        for event, fields in logs
    )


def test_gateway_bridge_stream_keeps_local_preface_as_whole_tts_segment(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-preface"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-preface"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-preface", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-preface", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_preface", "text": "心情还算稳，人也挺有劲。"}
        yield {"type": "delta", "text": "跟你说话会放松一点。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "心情还算稳，人也挺有劲。跟你说话会放松一点。",
                "request_id": "req-local-preface",
                "evidence": {"streamed": True, "local_preface": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=27, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == [
        "心情还算稳，人也挺有劲。",
        "跟你说话会放松一点。",
    ]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == "pcm:心情还算稳，人也挺有劲。".encode("utf-8") for frame in audio_frames)
    assert audio_frames[-1][12] == 1


def test_gateway_bridge_stream_keeps_local_voice_reply_as_whole_tts_segment(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-local-reply"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-local-reply"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-local-reply", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-local-reply", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_voice_reply", "text": "我在整理东西。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在整理东西。",
                "request_id": "req-local-reply",
                "evidence": {"streamed": True, "model_skipped": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=28, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你现在在干嘛",
    ))

    assert streamed is True
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == ["我在整理东西。"]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == "pcm:我在整理东西。".encode("utf-8") for frame in audio_frames)
    assert audio_frames[-1][12] == 1


def test_gateway_start_cancels_previous_processing_task(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    cancelled = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def stale_processing():
        try:
            await asyncio.sleep(10)
            await gateway_module.send_tts_binary(FakeWebsocket(), 1, b"stale-audio", stream_id=1, is_final=True)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def scenario():
        state = TurnState(turn_id=1)
        state.processing_task = asyncio.create_task(stale_processing())
        await asyncio.sleep(0)
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: AuraRuntimeConfig(persona_home="/tmp/persona"))
        await handle_text_message(
            FakeWebsocket(),
            config,
            state,
            json.dumps({
                "type": "start",
                "sample_rate": DEVICE_SAMPLE_RATE,
                "format": "pcm",
                "frame_duration": 40,
                "payload": {"turn_id": 2, "server_vad": False},
            }),
        )
        return state

    state = asyncio.run(scenario())

    assert cancelled.is_set()
    assert state.turn_id == 2
    assert state.processing_task is None
    assert not [item for item in sent if isinstance(item, bytes)]


def test_gateway_cancel_clears_active_turn(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    watchdog_cancelled = asyncio.Event()
    processing_cancelled = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def stale_watchdog():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            watchdog_cancelled.set()
            raise

    async def stale_processing():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            processing_cancelled.set()
            raise

    async def scenario():
        state = TurnState(turn_id=42, audio_bytes=2048, audio_packet_count=3)
        state.recording_watchdog_task = asyncio.create_task(stale_watchdog())
        state.processing_task = asyncio.create_task(stale_processing())
        await asyncio.sleep(0)
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        await gateway_module.handle_text_message(
            FakeWebsocket(),
            config,
            state,
            json.dumps({
                "type": "cancel",
                "payload": {"turn_id": 42, "reason": "upload_drain_timeout"},
            }),
        )
        await asyncio.sleep(0)
        return state

    state = asyncio.run(scenario())

    assert watchdog_cancelled.is_set()
    assert processing_cancelled.is_set()
    assert state.recording_watchdog_task is None
    assert state.processing_task is None
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "turn_cancelled" in actions


def test_gateway_start_cancels_previous_stream_tts_sender(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    synth_started = asyncio.Event()
    allow_synth = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "上一轮还在合成。"}
        await asyncio.Event().wait()

    def fake_synthesize_tts(runtime_config, text):
        synth_started.set()
        while not allow_synth.is_set():
            time.sleep(0.005)
        return TtsResult(ok=True, audio=b"stale-audio", chunk_count=1, latency_ms=1, first_chunk_ms=1)

    async def scenario():
        monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
        monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
        monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
        monkeypatch.setattr(
            gateway_module,
            "load_runtime_config_for_gateway",
            lambda: AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home")),
        )

        state = TurnState(turn_id=10, audio_chunks=[b"pcm"], asr_latency_ms=3)
        runtime = AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="voxcpm",
            tts_model="voxcpm2",
            tts_voice="yan",
            tts_base_url="http://tts.local/v1/audio/speech",
            tts_sample_rate=DEVICE_SAMPLE_RATE,
        )
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        ws = FakeWebsocket()
        state.processing_task = asyncio.create_task(gateway_module.stream_dialogue_and_tts_from_bridge(
            ws,
            config,
            runtime,
            state,
            "上一轮",
        ))
        await asyncio.wait_for(synth_started.wait(), timeout=1)
        await handle_text_message(
            ws,
            config,
            state,
            json.dumps({
                "type": "start",
                "sample_rate": DEVICE_SAMPLE_RATE,
                "format": "pcm",
                "frame_duration": 40,
                "payload": {"turn_id": 11, "server_vad": False},
            }),
        )
        allow_synth.set()
        await asyncio.sleep(0.05)
        return state

    state = asyncio.run(scenario())

    assert state.turn_id == 11
    assert state.processing_task is None
    assert state.stream_tts_sender_task is None
    assert state.stream_tts_tasks == []
    assert not [item for item in sent if isinstance(item, bytes)]


def test_gateway_stream_tts_drops_audio_if_turn_changes_before_send(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    synth_started = asyncio.Event()
    allow_synth = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "旧的一轮。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "旧的一轮。",
                "request_id": "req-stale",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synth_started.set()
        while not allow_synth.is_set():
            time.sleep(0.005)
        return TtsResult(ok=True, audio=b"stale-audio", chunk_count=1, latency_ms=1, first_chunk_ms=1)

    async def scenario():
        monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
        monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
        monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

        state = TurnState(turn_id=20, audio_chunks=[b"pcm"], asr_latency_ms=3)
        runtime = AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="voxcpm",
            tts_model="voxcpm2",
            tts_voice="yan",
            tts_base_url="http://tts.local/v1/audio/speech",
            tts_sample_rate=DEVICE_SAMPLE_RATE,
        )
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        ws = FakeWebsocket()
        task = asyncio.create_task(gateway_module.stream_dialogue_and_tts_from_bridge(
            ws,
            config,
            runtime,
            state,
            "上一轮",
        ))
        await asyncio.wait_for(synth_started.wait(), timeout=1)
        state.turn_id = 21
        allow_synth.set()
        streamed = await task
        return streamed, state

    streamed, state = asyncio.run(scenario())

    assert streamed is True
    assert state.turn_id == 21
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert not audio_frames
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["turn_id"] == 20


def test_gateway_old_stream_cleanup_does_not_clear_new_stream_resources(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    async def scenario():
        state = TurnState(turn_id=30)
        new_task = asyncio.create_task(asyncio.sleep(0))
        state.stream_tts_turn_id = 31
        state.stream_tts_sender_task = new_task
        await gateway_module.cancel_stream_tts_resources(state, reason="old_cancel", owner_turn_id=30)
        await new_task
        return state, new_task

    state, new_task = asyncio.run(scenario())

    assert state.stream_tts_turn_id == 31
    assert state.stream_tts_sender_task is new_task
    assert new_task.done()


def test_gateway_cancel_stream_closes_stepfun_ws_session(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    session_closed = asyncio.Event()
    session_ready = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSession:
        def __init__(self):
            self.started = time.monotonic()
            self.first_audio_abs_ms = 0
            self.first_text_ms = 0
            self.text_count = 0
            self.audio_chunk_count = 0
            self.first_audio_event = asyncio.Event()

        def is_healthy(self):
            return True

        def bind_turn(self, turn_id, *, stream_id=None):
            self.turn_id = turn_id
            if stream_id is not None:
                self.stream_id = stream_id

        async def send_text(self, text):
            self.text_count += 1

        async def finish(self, *, is_final=True):
            return TtsResult(ok=False, detail="should-not-finish")

        async def close(self):
            session_closed.set()

    async def fake_start_stepfun_ws_tts_session(websocket, runtime_config, turn_id, *, stream_id, started):
        session = FakeStepfunSession()
        session_ready.set()
        return session

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "第一句。"}
        await asyncio.Event().wait()

    async def scenario():
        monkeypatch.setattr(gateway_module, "start_stepfun_ws_tts_session", fake_start_stepfun_ws_tts_session)
        monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
        monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

        runtime = AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="stepfun",
            tts_model="stepaudio-2.5-tts",
            tts_voice="voice-tone-test",
            tts_base_url="https://api.stepfun.com/step_plan/v1",
            tts_api_key="unit-key",
            tts_sample_rate=DEVICE_SAMPLE_RATE,
        )
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        state = TurnState(turn_id=12, audio_chunks=[b"pcm"], asr_latency_ms=3)
        task = asyncio.create_task(gateway_module.stream_dialogue_and_tts_from_bridge(
            FakeWebsocket(),
            config,
            runtime,
            state,
            "你好",
        ))
        await asyncio.wait_for(session_ready.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return state

    state = asyncio.run(scenario())

    assert session_closed.is_set()
    assert state.stream_tts_sender_task is None
    assert state.stream_tts_stepfun_task is None
    assert state.stream_tts_stepfun_session is None
    assert not [item for item in sent if isinstance(item, bytes)]


def test_gateway_pop_stream_tts_segment_uses_fast_first_segment(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 22)

    # 没有标点时不再按字符硬切，等待后续 delta。
    segment, rest = gateway_module.pop_stream_tts_segment("我在这里陪你慢慢来", force=False)

    assert segment == ""
    assert rest == "我在这里陪你慢慢来"


def test_gateway_pop_stream_tts_segment_default_starts_after_four_chars(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 22)

    # 无标点文本不切分；有标点时仍按标点边界切。
    segment, rest = gateway_module.pop_stream_tts_segment("我在这里陪你慢慢来", force=False)

    assert segment == ""
    assert rest == "我在这里陪你慢慢来"

    segment, rest = gateway_module.pop_stream_tts_segment("我在这里，陪你慢慢来", force=False)

    assert segment == "我在这里，"
    assert rest == "陪你慢慢来"


def test_gateway_pop_stream_tts_segment_does_not_split_chinese_word_at_char_limit(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 18)

    # “目前”不能在“目”和“前”之间被拆成两个 TTS 请求。
    segment, pending = gateway_module.pop_stream_tts_segment(
        "关于发货时间，目",
        force=False,
        first_segment=True,
        followup_limit_chars=8,
    )

    assert segment == ""
    assert pending == "关于发货时间，目"


def test_gateway_pop_stream_tts_segment_splits_at_natural_colon_boundary(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 18)

    segment, pending = gateway_module.pop_stream_tts_segment(
        "关于发货时间，目前的情况如下：整体进度",
        force=False,
        first_segment=True,
        followup_limit_chars=8,
    )

    assert segment == "关于发货时间，目前的情况如下："
    assert pending == "整体进度"


def test_gateway_pop_stream_tts_segment_long_unpunctuated_delta_waits_for_final_flush(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 18)

    text = "这是一段没有任何标点但已经远远超过旧字符限制的流式文本"
    segment, pending = gateway_module.pop_stream_tts_segment(
        text,
        force=False,
        first_segment=False,
        followup_limit_chars=8,
    )

    assert segment == ""
    assert pending == text


def test_gateway_flush_stream_tts_segments_splits_final_remainder(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 22)

    # 无标点的最终剩余文本由 final flush 整体提交，不再按字符切开。
    segments = flush_stream_tts_segments("我在这里陪你慢慢来")

    assert segments == ["我在这里陪你慢慢来"]

    segments = flush_stream_tts_segments("我在这里，陪你慢慢来")

    assert segments == ["我在这里，", "陪你慢慢来"]


def test_gateway_dialogue_segments_split_chinese_english_and_japanese():
    segments = gateway_module.dialogue_segments(
        "我先整理一下。 Then we can go outside. そのあと、公園へ行こう？ 好！"
    )

    assert [item["text"] for item in segments] == [
        "我先整理一下。",
        "Then we can go outside.",
        "そのあと、公園へ行こう？",
        "好！",
    ]


def test_gateway_pcm_duration_and_world_scene_mapping():
    assert gateway_module.pcm16_mono_duration_ms(32_000) == 1000
    assert gateway_module.estimate_dialogue_duration_ms("Hello there.") == 1310
    assert gateway_module.estimate_dialogue_duration_ms("今日は公園へ行こう？") == 2270
    assert gateway_module.scene_for_world_location("outside.neighborhood") == "outside.neighborhood"
    assert gateway_module.scene_for_world_location("nearby_walk") == "outside.park"
    assert gateway_module.scene_for_world_location("mall") == "outside.mall"
    assert gateway_module.scene_for_world_location("quiet_stop") == "outside.cafe"
    assert gateway_module.scene_for_world_location("unknown") == "home.living_room"
    assert gateway_module.dialogue_scene({
        "evidence": {"world_location_key": "outside.cafe"},
    }) == "outside.cafe"


def test_gateway_flush_stream_tts_segments_drops_unclosed_stage_tail(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)

    assert flush_stream_tts_segments("（轻轻笑了一下") == []
    assert flush_stream_tts_segments("我在。（轻轻笑了一下") == ["我在。"]


def test_gateway_stream_tts_does_not_emit_incomplete_status_phrase(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 18)

    segment, rest = gateway_module.pop_stream_tts_segment("最近状态啊... 其实我", force=False)

    assert segment == ""
    assert rest == "最近状态啊... 其实我"
    assert flush_stream_tts_segments("最近状态啊... 其实我") == []
    assert flush_stream_tts_segments("最近状态确实该理理。我看") == ["最近状态确实该理理。"]
    segment, rest = gateway_module.pop_stream_tts_segment("我也觉得该理", force=False)
    assert segment == ""
    assert rest == "我也觉得该理"


def test_gateway_pop_stream_tts_segment_does_not_cut_open_parentheses(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 4)

    segment, rest = gateway_module.pop_stream_tts_segment("采样率（16k）没问题。", force=False)

    assert segment == "采样率（16k）没问题。"
    assert rest == ""


def test_gateway_stepfun_ws_tts_does_not_wait_first_audio_before_next_text(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class SlowFirstAudioStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-fast-text"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-fast-text"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta" and item["data"]["text"] == "第二句。":
                audio = base64.b64encode(b"pcm:second").decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-fast-text", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-fast-text", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = SlowFirstAudioStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "你好呀，"}
        yield {"type": "delta", "text": "第二句。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "你好呀，第二句。",
                "request_id": "req-stream",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WAIT_FIRST_AUDIO", False)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=24, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == ["你好呀，", "第二句。"]
    assert [item.get("type") for item in connections[0].socket.sent].index("tts.text.delta") < len(connections[0].socket.sent) - 1
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == b"pcm:second" for frame in audio_frames)
    assert audio_frames[-1][12] == 1


def test_stepfun_ws_tts_can_flush_first_delta_only(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    class FakeStepWs:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def run_case():
        session = gateway_module.StepfunWsTtsSession(
            websocket=object(),
            runtime_config=AuraRuntimeConfig(
                persona_home=str(tmp_path / "persona-home"),
                tts_enabled=True,
                tts_provider="stepfun",
                tts_model="stepaudio-2.5-tts",
                tts_voice="voice-tone-test",
                tts_base_url="https://api.stepfun.com/step_plan/v1",
                tts_api_key="unit-key",
            ),
            turn_id=31,
            stream_id=1,
            started=time.monotonic(),
        )
        fake_ws = FakeStepWs()
        session._step_ws = fake_ws
        session.session_id = "sid-flush"
        await session.send_text("第一段")
        await session.send_text("第二段")
        return fake_ws.sent

    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FLUSH_AFTER_DELTA", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FLUSH_EACH_DELTA", False)

    sent = asyncio.run(run_case())

    assert [item["type"] for item in sent] == [
        "tts.text.delta",
        "tts.text.flush",
        "tts.text.delta",
    ]


def test_stepfun_ws_tts_start_cancel_releases_session_slot(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    entered = asyncio.Event()
    resume = asyncio.Event()
    exits = []

    class HangingConnect:
        def __init__(self, url, **kwargs):
            pass

        async def __aenter__(self):
            entered.set()
            await resume.wait()
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            exits.append(exc_type)
            return False

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
    )

    async def scenario():
        gateway_module._STEPFUN_WS_TTS_SEMAPHORES.clear()
        gateway_module._STEPFUN_WS_TTS_SEMAPHORE_LIMITS.clear()
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_MAX_SESSIONS", 1)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ACQUIRE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ACQUIRE_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: HangingConnect(url, **kwargs))

        session = gateway_module.StepfunWsTtsSession(
            object(),
            runtime,
            turn_id=1,
            stream_id=1,
            started=time.monotonic(),
            warm=True,
        )
        task = asyncio.create_task(session.start())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        slot = await gateway_module._stepfun_ws_tts_acquire_slot(warm=True)
        assert slot is not None
        slot.release()

    asyncio.run(scenario())

    assert exits


def test_stepfun_ws_tts_warm_ignores_audio_before_first_text(tmp_path):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    session = gateway_module.StepfunWsTtsSession(
        FakeWebsocket(),
        AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="stepfun",
            tts_model="stepaudio-2.5-tts",
            tts_voice="voice-tone-test",
            tts_base_url="https://api.stepfun.com/step_plan/v1",
            tts_api_key="unit-key",
        ),
        turn_id=0,
        stream_id=1,
        started=time.monotonic(),
        warm=True,
    )
    audio = base64.b64encode(b"unexpected-pcm").decode("ascii")

    asyncio.run(session._send_audio_delta({"audio": audio}))

    assert sent == []
    assert session.audio_bytes == 0


def test_stepfun_ws_tts_flush_each_delta_defaults_off():
    from integrations.hermes_lily_cli import gateway as gateway_module

    assert gateway_module.STEPFUN_WS_TTS_FLUSH_AFTER_DELTA is True
    assert gateway_module.STEPFUN_WS_TTS_FLUSH_EACH_DELTA is False


def test_gateway_bridge_stream_reuses_warmed_stepfun_ws_tts_session(monkeypatch, tmp_path):
    sent = []
    connections = []
    log_events = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-warm"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-warm"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-warm", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-warm", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "你好呀，"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "你好呀，",
                "request_id": "req-warm",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: log_events.append((event, fields)))

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=25, audio_chunks=[b"pcm"], asr_latency_ms=3)

    async def scenario():
        websocket = FakeWebsocket()
        await gateway_module.maybe_start_stepfun_tts_warm_session(websocket, state, runtime)
        assert state.stepfun_tts_warm_task is not None
        warmed = await state.stepfun_tts_warm_task
        assert warmed.session_id == "sid-warm"
        streamed = await gateway_module.stream_dialogue_and_tts_from_bridge(
            websocket,
            config,
            runtime,
            state,
            "你好",
        )
        return streamed

    streamed = asyncio.run(scenario())

    assert streamed is True
    assert len(connections) == 1
    assert state.stepfun_tts_warm_task is None
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == ["你好呀，"]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == "pcm:你好呀，".encode("utf-8") for frame in audio_frames)
    assert audio_frames[-1][12] == 1
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_provider_stream"] == "stepfun_ws_session"
    assert timing["payload"]["tts_first_text_ms"] < 1000
    assert timing["payload"]["tts_first_audio_since_bridge_ms"] < 1000
    sent_log = [fields for event, fields in log_events if event == "bridge_stream_tts_sent"][-1]
    assert sent_log["first_audio_since_bridge_ms"] < 1000


def test_gateway_bridge_stream_sends_local_preface_over_http_when_ws_warm_pending(monkeypatch, tmp_path):
    sent = []
    log_events = []
    ws_texts = []
    http_texts = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_preface", "text": "我在。"}
        yield {"type": "delta", "text": "先说最想说的那件事。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在。先说最想说的那件事。",
                "request_id": "req-pending-warm",
                "evidence": {"streamed": True, "local_preface": True},
            },
        }

    class FakePendingSession:
        def __init__(self):
            self.started = time.monotonic()
            self.first_text_ms = 0
            self.first_audio_abs_ms = 0
            self.text_count = 0
            self.audio_chunk_count = 0
            self.error_detail = ""

        async def send_text(self, text):
            ws_texts.append(text)
            self.text_count += 1
            if not self.first_text_ms:
                self.first_text_ms = 120

        async def finish(self, *, is_final=True):
            self.first_audio_abs_ms = 170
            return TtsResult(
                ok=True,
                audio=b"",
                audio_bytes=9,
                audio_chunk_count=1,
                chunk_count=1,
                latency_ms=180,
                first_audio_ms=50,
                first_chunk_ms=30,
                streamed=True,
            )

        async def close(self):
            return None

    async def slow_start_session(*args, **kwargs):
        await asyncio.sleep(0.05)
        return FakePendingSession()

    async def fake_synthesize_and_stream_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, **kwargs):
        http_texts.append(text)
        await gateway_module.send_tts_binary(websocket, turn_id, b"http-preface", stream_id=stream_id, is_final=False)
        return TtsResult(ok=True, audio_bytes=12, audio_chunk_count=1, chunk_count=1, latency_ms=20, first_audio_ms=20, first_chunk_ms=20, streamed=True)

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "start_stepfun_ws_tts_session", slow_start_session)
    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_synthesize_and_stream_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS", 1)
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: log_events.append((event, fields)))
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS", 0)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=26, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    assert http_texts == ["我在。"]
    assert ws_texts == ["先说最想说的那件事。"]
    assert any(event == "bridge_stream_tts_first_segment_http" for event, _fields in log_events)
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_provider_stream"] == "http_first_segment+stepfun_ws_session"
    assert timing["payload"]["tts_first_audio_since_bridge_ms"] < 100


def test_gateway_bridge_stream_sends_first_model_segment_over_http_when_ws_pending(monkeypatch, tmp_path):
    sent = []
    log_events = []
    http_texts = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "先说你最想聊的那一件。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "先说你最想聊的那一件。",
                "request_id": "req-pending-model",
                "evidence": {"streamed": True},
            },
        }

    async def slow_start_session(*args, **kwargs):
        await asyncio.sleep(0.05)
        raise AssertionError("pending WS TTS session should not block first model audio")

    async def fake_synthesize_and_stream_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, **kwargs):
        http_texts.append(text)
        await gateway_module.send_tts_binary(websocket, turn_id, b"http-first", stream_id=stream_id, is_final=False)
        return TtsResult(ok=True, audio_bytes=12, audio_chunk_count=1, chunk_count=1, latency_ms=20, first_audio_ms=20, first_chunk_ms=20, streamed=True)

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "start_stepfun_ws_tts_session", slow_start_session)
    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_synthesize_and_stream_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FIRST_SEGMENT_READY_WAIT_MS", 1)
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: log_events.append((event, fields)))

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=261, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    assert http_texts == ["先说你最想聊的那一件。"]
    assert any(event == "bridge_stream_tts_first_segment_http" for event, _fields in log_events)
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_provider_stream"] == "http_first_segment"
    assert timing["payload"]["tts_first_audio_since_bridge_ms"] < 100


def test_gateway_bridge_stream_defers_tiny_local_preface_into_first_model_segment(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-defer-preface"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-defer-preface"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(f"pcm:{item['data']['text']}".encode("utf-8")).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-defer-preface", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-defer-preface", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_preface", "text": "我在。"}
        yield {"type": "delta", "text": "先说最想说的那件事。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在。先说最想说的那件事。",
                "request_id": "req-defer-preface",
                "evidence": {"streamed": True, "local_preface": True},
            },
        }

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 12)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=28, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    text_events = [item for item in connections[0].socket.sent if item.get("type") == "tts.text.delta"]
    assert [item["data"]["text"] for item in text_events] == ["我在，先说最想说的那件事。"]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == "pcm:我在，先说最想说的那件事。".encode("utf-8") for frame in audio_frames)
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert dialogue["payload"]["text"] == "我在。先说最想说的那件事。"
    assert timing["payload"]["response_preview"] == "我在。先说最想说的那件事。"


def test_gateway_bridge_stream_can_force_local_preface_over_http(monkeypatch, tmp_path):
    sent = []
    log_events = []
    ws_texts = []
    http_texts = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "source": "local_preface", "text": "我在。"}
        yield {"type": "delta", "text": "先说最想说的那件事。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在。先说最想说的那件事。",
                "request_id": "req-force-http-preface",
                "evidence": {"streamed": True, "local_preface": True},
            },
        }

    class FakeReadySession:
        def __init__(self):
            self.started = time.monotonic()
            self.first_text_ms = 0
            self.first_audio_abs_ms = 0
            self.text_count = 0
            self.audio_chunk_count = 0
            self.error_detail = ""

        async def send_text(self, text):
            ws_texts.append(text)
            self.text_count += 1
            if not self.first_text_ms:
                self.first_text_ms = 5

        async def finish(self, *, is_final=True):
            self.first_audio_abs_ms = 80
            return TtsResult(
                ok=True,
                audio=b"",
                audio_bytes=9,
                audio_chunk_count=1,
                chunk_count=1,
                latency_ms=90,
                first_audio_ms=40,
                first_chunk_ms=30,
                streamed=True,
            )

        async def close(self):
            return None

    async def ready_start_session(*args, **kwargs):
        return FakeReadySession()

    async def fake_synthesize_and_stream_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, **kwargs):
        http_texts.append(text)
        await gateway_module.send_tts_binary(websocket, turn_id, b"http-preface", stream_id=stream_id, is_final=False)
        return TtsResult(ok=True, audio_bytes=12, audio_chunk_count=1, chunk_count=1, latency_ms=15, first_audio_ms=15, first_chunk_ms=15, streamed=True)

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "start_stepfun_ws_tts_session", ready_start_session)
    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_synthesize_and_stream_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_FIRST_SEGMENT_HTTP_POLICY", "always")
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: log_events.append((event, fields)))
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_DEFER_LOCAL_PREFACE_CHARS", 0)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=27, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    assert http_texts == ["我在。"]
    assert ws_texts == ["先说最想说的那件事。"]
    first_http_log = [fields for event, fields in log_events if event == "bridge_stream_tts_first_segment_http"][0]
    assert first_http_log["reason"] == "policy_always"
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_provider_stream"] == "http_first_segment+stepfun_ws_session"


def test_gateway_bridge_stream_rebinds_idle_warm_tts_turn_id(monkeypatch, tmp_path):
    sent = []
    connections = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": "sid-idle"}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": "sid-idle"}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(b"pcm:idle-warm").decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": "sid-idle", "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": "sid-idle", "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunSocket()
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "我在听。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在听。",
                "request_id": "req-idle",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=0, audio_chunks=[b"pcm"], asr_latency_ms=3)

    async def scenario():
        websocket = FakeWebsocket()
        await gateway_module.maybe_start_stepfun_tts_warm_session(websocket, state, runtime, reason="connect")
        warmed = await state.stepfun_tts_warm_task
        assert warmed.turn_id == 0
        state.turn_id = 601
        return await gateway_module.stream_dialogue_and_tts_from_bridge(
            websocket,
            config,
            runtime,
            state,
            "你好",
        )

    streamed = asyncio.run(scenario())

    assert streamed is True
    assert len(connections) == 1
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert audio_frames
    assert all(int.from_bytes(frame[8:12], "little") == 601 for frame in audio_frames)
    assert any(frame[16:] == b"pcm:idle-warm" for frame in audio_frames)


def test_gateway_bridge_stream_discards_unhealthy_warm_tts_session(monkeypatch, tmp_path):
    sent = []
    connections = []
    closed_sessions = []
    log_events = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class FakeStepfunSocket:
        def __init__(self, session_id, audio_payload):
            self.session_id = session_id
            self.audio_payload = audio_payload
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "tts.connection.done", "data": {"session_id": session_id}}))
            self.recv_queue.put_nowait(json.dumps({"type": "tts.response.created", "data": {"session_id": session_id}}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "tts.text.delta":
                audio = base64.b64encode(self.audio_payload).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.delta",
                    "data": {"session_id": self.session_id, "status": "unfinished", "audio": audio},
                }))
            if item.get("type") == "tts.text.done":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "tts.response.audio.done",
                    "data": {"session_id": self.session_id, "audio": ""},
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            index = len(connections)
            self.socket = FakeStepfunSocket(
                "sid-stale" if index == 0 else "sid-fresh",
                b"pcm:stale" if index == 0 else b"pcm:fresh",
            )
            connections.append(self)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            closed_sessions.append(self.socket.session_id)
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "我在听。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "我在听。",
                "request_id": "req-stale-warm",
                "evidence": {"streamed": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_WARM_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_SAMPLE_RATE", DEVICE_SAMPLE_RATE)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "log_gateway", lambda event, **fields: log_events.append((event, fields)))

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_sample_rate=24000,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=0, audio_chunks=[b"pcm"], asr_latency_ms=3)

    async def scenario():
        websocket = FakeWebsocket()
        await gateway_module.maybe_start_stepfun_tts_warm_session(websocket, state, runtime, reason="connect")
        stale = await state.stepfun_tts_warm_task
        stale.error_detail = "StepFun WS TTS timeout"
        state.turn_id = 602
        return await gateway_module.stream_dialogue_and_tts_from_bridge(
            websocket,
            config,
            runtime,
            state,
            "你好",
        )

    streamed = asyncio.run(scenario())

    assert streamed is True
    assert len(connections) == 2
    assert "sid-stale" in closed_sessions
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == b"pcm:fresh" for frame in audio_frames)
    assert not any(frame[16:] == b"pcm:stale" for frame in audio_frames)
    assert all(int.from_bytes(frame[8:12], "little") == 602 for frame in audio_frames)
    assert any(event == "stepfun_ws_tts_warm_discard" and fields.get("reason") == "adopt" for event, fields in log_events)


def test_stepfun_ws_tts_warm_session_does_not_timeout_before_first_text(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    class IdleStepWs:
        async def recv(self):
            await asyncio.sleep(0.05)
            return json.dumps({"type": "tts.response.audio.done", "data": {"session_id": "sid-idle", "audio": ""}})

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
        tts_timeout_seconds=0.01,
    )
    session = gateway_module.StepfunWsTtsSession(
        object(),
        runtime,
        turn_id=0,
        stream_id=1,
        started=time.monotonic(),
        warm=True,
    )
    session._step_ws = IdleStepWs()
    session.session_id = "sid-idle"

    async def scenario():
        task = asyncio.create_task(session._receive_loop())
        await asyncio.sleep(0.02)
        assert not task.done()
        session.first_text_ms = 1
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())

    assert session.done_seen is True
    assert session.error_detail == ""


def test_gateway_send_dialogue_and_tts_closes_unused_warm_session(monkeypatch, tmp_path):
    sent = []
    closed = asyncio.Event()

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    class WarmSession:
        async def close(self):
            closed.set()

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_synthesize_and_stream_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, **kwargs):
        await gateway_module.send_tts_binary(websocket, turn_id, b"pcm:main", stream_id=stream_id, is_final=is_final)
        return TtsResult(
            ok=True,
            chunk_count=1,
            audio_chunk_count=1,
            audio_bytes=len(b"pcm:main"),
            latency_ms=5,
            first_chunk_ms=5,
            first_audio_ms=5,
        )

    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_synthesize_and_stream_tts)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
    )
    state = TurnState(turn_id=603, asr_latency_ms=1, bridge_latency_ms=2)

    async def scenario():
        state.stepfun_tts_warm_task = asyncio.create_task(asyncio.sleep(0, result=WarmSession()))
        await state.stepfun_tts_warm_task
        await gateway_module.send_dialogue_and_tts(
            FakeWebsocket(),
            runtime,
            state,
            "我在听。",
            ok=True,
        )

    asyncio.run(scenario())

    assert closed.is_set()
    assert state.stepfun_tts_warm_task is None
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert any(frame[16:] == b"pcm:main" for frame in audio_frames)


def test_gateway_handle_connection_starts_idle_tts_warm_and_reuses_on_start(monkeypatch, tmp_path):
    sent = []
    warm_calls = []
    asr_calls = []
    close_calls = []
    status_started = asyncio.Event()
    allow_status = asyncio.Event()

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class WarmSession:
        closed = False

        async def close(self):
            self.closed = True

    class FakeStreamingAsrSession:
        async def close(self):
            pass

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
    )

    async def fake_maybe_start_stepfun_tts_warm_session(websocket, state, runtime_config, *, reason="start"):
        warm_calls.append((state.turn_id, reason, state.stepfun_tts_warm_task is not None))
        if state.stepfun_tts_warm_task is None:
            state.stepfun_tts_warm_started_at = time.monotonic()
            state.stepfun_tts_warm_task = asyncio.create_task(asyncio.sleep(0, result=WarmSession()))

    async def fake_close_stepfun_tts_warm_session(state):
        close_calls.append((state.turn_id, state.stepfun_tts_warm_task is not None))
        task = state.stepfun_tts_warm_task
        state.stepfun_tts_warm_task = None
        if task is not None:
            await task

    async def fake_maybe_start_streaming_asr_session(state, runtime_config, **kwargs):
        asr_calls.append((state.turn_id, state.stepfun_tts_warm_task is not None))
        state.streaming_asr_session = FakeStreamingAsrSession()

    async def fake_maybe_start_stepfun_step_plan_realtime_session(websocket, config, state, runtime_config):
        state.stepfun_realtime_session = None
        state.stepfun_realtime_enabled = False

    async def fake_send_status_update(websocket, runtime_config):
        status_started.set()
        await allow_status.wait()
        await gateway_module.send_json(websocket, {
            "type": "status_update",
            "payload": {"weather_city": "上海"},
        })

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "ensure_tts_preface_task", lambda runtime_config: None)
    monkeypatch.setattr(gateway_module, "send_status_update", fake_send_status_update)
    monkeypatch.setattr(gateway_module, "maybe_start_stepfun_tts_warm_session", fake_maybe_start_stepfun_tts_warm_session)
    monkeypatch.setattr(gateway_module, "close_stepfun_tts_warm_session", fake_close_stepfun_tts_warm_session)
    monkeypatch.setattr(gateway_module, "maybe_start_streaming_asr_session", fake_maybe_start_streaming_asr_session)
    monkeypatch.setattr(
        gateway_module,
        "maybe_start_stepfun_step_plan_realtime_session",
        fake_maybe_start_stepfun_step_plan_realtime_session,
    )
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", False)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_ENABLED", False)

    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 601, "server_vad": False},
    })
    ws = FakeClientWebsocket([start])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        await gateway_module.handle_connection(ws, config)
        assert status_started.is_set()
        assert warm_calls == [(0, "connect", False), (601, "start", True)]
        assert asr_calls == [(601, True)]
        allow_status.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert warm_calls == [(0, "connect", False), (601, "start", True)]
    assert asr_calls == [(601, True)]
    assert close_calls == [(601, True)]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert messages[0]["type"] == "hello"
    assert any(item.get("payload", {}).get("turn_id") == 601 for item in messages)


def test_gateway_server_vad_triggers_turn_once(monkeypatch):
    sent = []
    run_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_run_voice_turn(websocket, config, state):
        run_calls.append(state.turn_id)

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_ENABLED", True)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SPEECH_RMS", 200)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SILENCE_RMS", 100)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_SPEECH_MS", 120)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SILENCE_MS", 180)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_AUDIO_MS", 240)

    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState()
    ws = FakeWebsocket()
    start = {
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 60,
        "payload": {"turn_id": 77, "server_vad": True},
    }
    speech = b"".join((1200).to_bytes(2, "little", signed=True) for _ in range(DEVICE_SAMPLE_RATE * 60 // 1000))
    silence = b"\x00\x00" * (DEVICE_SAMPLE_RATE * 60 // 1000)

    async def scenario():
        await gateway_module.handle_text_message(ws, config, state, json.dumps(start))
        for _ in range(3):
            state.audio_chunks.append(speech)
            state.audio_bytes += len(speech)
            await gateway_module.maybe_trigger_server_vad(ws, config, state, speech)
        for _ in range(4):
            state.audio_chunks.append(silence)
            state.audio_bytes += len(silence)
            await gateway_module.maybe_trigger_server_vad(ws, config, state, silence)
        if state.processing_task:
            await state.processing_task
        await gateway_module.handle_text_message(ws, config, state, json.dumps({"type": "stop", "payload": {"turn_id": 77}}))

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "server_vad_stop" in actions
    assert run_calls == [77]
    assert state.server_vad_triggered is True


def test_gateway_server_vad_uses_shorter_streaming_asr_endpoint(monkeypatch):
    sent = []
    run_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_run_voice_turn(websocket, config, state):
        run_calls.append(state.turn_id)

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SPEECH_RMS", 200)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SILENCE_RMS", 100)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_SPEECH_MS", 120)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SILENCE_MS", 900)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_LOCAL_VAD_SILENCE_MS", 180)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_AUDIO_MS", 240)

    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(
        turn_id=78,
        server_vad_enabled=True,
        streaming_asr_session=object(),
        audio_format="pcm",
    )
    ws = FakeWebsocket()
    speech = b"".join((1200).to_bytes(2, "little", signed=True) for _ in range(DEVICE_SAMPLE_RATE * 60 // 1000))
    silence = b"\x00\x00" * (DEVICE_SAMPLE_RATE * 60 // 1000)

    async def scenario():
        for _ in range(3):
            await gateway_module.maybe_trigger_server_vad(ws, config, state, speech)
        for _ in range(4):
            await gateway_module.maybe_trigger_server_vad(ws, config, state, silence)
        if state.processing_task:
            await state.processing_task

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    payload = [item["payload"] for item in messages if item.get("payload", {}).get("action") == "server_vad_stop"][-1]
    assert payload["silence_target_ms"] == 180
    assert payload["silence_ms"] >= 180
    assert run_calls == [78]


def test_gateway_stop_while_processing_sends_deferred_notice():
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def scenario():
        state = TurnState(
            turn_id=79,
            started_at=time.monotonic(),
            audio_packet_count=3,
            audio_bytes=1200,
        )
        state.processing_task = asyncio.create_task(asyncio.sleep(10))
        try:
            await handle_text_message(
                FakeWebsocket(),
                GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn"),
                state,
                json.dumps({"type": "stop", "payload": {"turn_id": 79}}),
            )
        finally:
            state.processing_task.cancel()
            try:
                await state.processing_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    payload = [item["payload"] for item in messages if item.get("payload", {}).get("action") == "turn_stop_deferred"][-1]
    assert payload["status"] == "processing"
    assert payload["reason"] == "processing_task_active"


def test_gateway_stop_does_not_block_followup_cancel(monkeypatch):
    sent = []
    started: asyncio.Event | None = None

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    async def fake_run_voice_turn(websocket, config, state):
        assert started is not None
        started.set()
        await asyncio.sleep(10)

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)

    async def scenario():
        nonlocal started
        started = asyncio.Event()
        state = TurnState(
            turn_id=80,
            started_at=time.monotonic(),
            audio_packet_count=3,
            audio_bytes=1200,
        )
        ws = FakeWebsocket()
        config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
        await gateway_module.handle_text_message(ws, config, state, json.dumps({"type": "stop", "payload": {"turn_id": 80}}))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert state.processing_task is not None
        assert not state.processing_task.done()
        await gateway_module.handle_text_message(ws, config, state, json.dumps({"type": "cancel", "payload": {"turn_id": 80, "reason": "unit"}}))
        assert state.processing_task is None

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "audio_received" in actions
    assert "turn_cancelled" in actions


def test_gateway_run_voice_turn_prefers_streaming_asr_result(monkeypatch, tmp_path):
    sent = []
    calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    class FakeStreamingAsr:
        async def finish(self):
            return AsrResult(ok=True, text="测试一下天气", status="streaming_asr")

        async def close(self):
            pass

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run when streaming ASR has final text")

    def fake_call_bridge(config, state, transcript):
        calls.append(transcript)
        return {"ok": True, "response": "好的。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response, "timing": {"asr_ms": state.asr_latency_ms}},
        })

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)

    state = TurnState(turn_id=88, audio_chunks=[b"pcm"], streaming_asr_session=FakeStreamingAsr())
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    assert calls == ["测试一下天气"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["text"] == "测试一下天气"
    assert asr_payload["status"] == "streaming_asr"
    assert "streaming_asr_first_delta_ms" in asr_payload
    assert "streaming_asr_final_ms" in asr_payload


def test_gateway_run_voice_turn_does_not_send_asr_fragment_to_bridge(monkeypatch, tmp_path):
    sent = []
    replies = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    class FakeStreamingAsr:
        async def finish(self):
            return AsrResult(ok=True, text="这。", status="streaming_asr")

        async def close(self):
            pass

    def should_not_call_bridge(config, state, transcript):
        raise AssertionError("low-confidence ASR fragments must not reach Aura bridge")

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        replies.append(response)
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "call_bridge", should_not_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)

    state = TurnState(turn_id=89, audio_chunks=[b"pcm"], streaming_asr_session=FakeStreamingAsr())
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    assert replies == ["我在，刚才只听到一点点。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "asr_low_confidence" in actions


def test_gateway_recording_watchdog_triggers_when_stop_is_missing(monkeypatch):
    sent = []
    run_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_run_voice_turn(websocket, config, state):
        run_calls.append((state.turn_id, state.turn_trigger_reason))

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)
    monkeypatch.setattr(gateway_module, "RECORDING_NO_AUDIO_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "RECORDING_MAX_SECONDS", 0.02)

    state = TurnState(
        turn_id=90,
        started_at=time.monotonic(),
        audio_bytes=4096,
        streaming_asr_audio_bytes=4096,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        await gateway_module.monitor_recording_watchdog(FakeWebsocket(), config, state, owner_turn_id=90)
        assert state.processing_task is not None
        await state.processing_task

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "server_vad_stop" in actions
    assert run_calls == [(90, "recording_timeout")]


def test_gateway_recording_watchdog_triggers_on_audio_stall(monkeypatch):
    sent = []
    run_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_run_voice_turn(websocket, config, state):
        run_calls.append((state.turn_id, state.turn_trigger_reason))

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)
    monkeypatch.setattr(gateway_module, "RECORDING_NO_AUDIO_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(gateway_module, "RECORDING_MAX_SECONDS", 5.0)
    monkeypatch.setattr(gateway_module, "RECORDING_STALL_TIMEOUT_SECONDS", 0.05)

    state = TurnState(
        turn_id=92,
        started_at=time.monotonic() - 1.0,
        audio_bytes=1836,
        audio_last_packet_ms=100,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    async def scenario():
        started = time.monotonic()
        await gateway_module.monitor_recording_watchdog(FakeWebsocket(), config, state, owner_turn_id=92)
        # 断流应提前收尾，远早于 RECORDING_MAX_SECONDS(5s)。
        assert time.monotonic() - started < 1.0
        assert state.processing_task is not None
        await state.processing_task

    asyncio.run(scenario())

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop = [item for item in messages if item.get("payload", {}).get("action") == "server_vad_stop"][-1]
    assert stop["payload"]["reason"] == "audio_stall"
    assert run_calls == [(92, "audio_stall")]


def test_gateway_streaming_asr_low_confidence_final_does_not_stop_recording(monkeypatch):
    sent = []
    run_calls = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_run_voice_turn(websocket, config, state):
        run_calls.append(state.turn_id)

    monkeypatch.setattr(gateway_module, "run_voice_turn", fake_run_voice_turn)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)

    state = TurnState(
        turn_id=91,
        started_at=time.monotonic(),
        streaming_asr_final_ready=True,
        streaming_asr_final_text="这。",
        streaming_asr_final_reason="final",
        streaming_asr_audio_bytes=32768,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.maybe_trigger_streaming_asr_final_turn(FakeWebsocket(), config, state))

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "server_vad_stop" not in actions
    assert run_calls == []
    assert state.streaming_asr_early_turn_blocked is True
    assert state.streaming_asr_early_turn_triggered is False


def test_gateway_handle_connection_streams_asr_before_stop(monkeypatch, tmp_path):
    sent = []
    connections = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append" and len(self.sent) >= 3:
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "text": "测试一下天气",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run when streaming ASR has final text")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "好的。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 99, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 99}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["测试一下天气"]
    assert connections
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert sent_types[:2] == ["session.update", "input_audio_buffer.append"]
    assert "input_audio_buffer.commit" not in sent_types
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "streaming_asr"
    assert asr_payload["text"] == "测试一下天气"
    assert asr_payload["streaming_asr"] is True
    assert asr_payload["streaming_asr_audio_bytes"] == len(speech)
    assert asr_payload["streaming_asr_forwarded_frames"] == 1
    assert asr_payload["streaming_asr_final_ms"] >= 0


def test_gateway_handle_connection_uses_stepfun_realtime_before_stop(monkeypatch, tmp_path):
    sent = []
    connections = []
    append_seen = asyncio.Event()
    allow_stop = asyncio.Event()
    pcm = b"\x02\x00" * 640

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if item == "wait_for_append":
                await asyncio.wait_for(append_seen.wait(), timeout=2)
                allow_stop.set()
                return await self.__anext__()
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                append_seen.set()
            if item.get("type") == "response.create":
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.text.delta", "delta": "收到。"}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.done"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=True,
    )

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", True)

    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 199, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 199}})
    ws = FakeClientWebsocket([start, pcm, "wait_for_append", stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert connections
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert sent_types[:2] == ["session.update", "input_audio_buffer.append"]
    assert "response.create" in sent_types
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["provider_stream"] == "stepfun_realtime"
    assert timing["payload"]["turn_trigger_reason"] == "client_stop"
    assert "realtime_first_audio_after_response_ms" in timing["payload"]
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert audio_frames and audio_frames[0][:4] == TTS_BINARY_MAGIC


def test_gateway_stepfun_realtime_speech_stopped_triggers_before_stop(monkeypatch, tmp_path):
    sent = []
    connections = []
    append_seen = asyncio.Event()
    response_seen = asyncio.Event()
    pcm = b"\x03\x00" * 640

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if item == "wait_for_response":
                await asyncio.wait_for(response_seen.wait(), timeout=2)
                return await self.__anext__()
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                append_seen.set()
                self.recv_queue.put_nowait(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
            if item.get("type") == "response.create":
                response_seen.set()
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.text.delta", "delta": "来了。"}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.done"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
    )
    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", True)

    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 299, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 299}})
    ws = FakeClientWebsocket([start, pcm, "wait_for_response", stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert "input_audio_buffer.commit" not in sent_types
    assert "response.create" in sent_types
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop_payload = [item for item in messages if item.get("payload", {}).get("action") == "server_vad_stop"][-1]["payload"]
    assert stop_payload["reason"] == "stepfun_realtime_speech_stopped"
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["provider_stream"] == "stepfun_realtime"
    assert timing["payload"]["turn_trigger_reason"] == "stepfun_realtime_speech_stopped"
    assert "realtime_first_audio_after_response_ms" in timing["payload"]


def test_gateway_stepfun_realtime_local_vad_triggers_before_stop(monkeypatch, tmp_path):
    sent = []
    connections = []
    response_seen = asyncio.Event()
    speech = (1200).to_bytes(2, "little", signed=True) * int(DEVICE_SAMPLE_RATE * 0.36)
    silence = b"\x00\x00" * int(DEVICE_SAMPLE_RATE * 1.0)
    pcm = b"\x04\x00" * 640

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if item == "wait_for_response":
                await asyncio.wait_for(response_seen.wait(), timeout=2)
                return await self.__anext__()
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunRealtimeSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.recv_queue.put_nowait(json.dumps({"type": "session.updated"}))

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "response.create":
                response_seen.set()
                audio = base64.b64encode(pcm).decode("ascii")
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.delta", "delta": audio}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.text.delta", "delta": "本地VAD触发。"}))
                self.recv_queue.put_nowait(json.dumps({"type": "response.audio.done"}))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunRealtimeSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun-realtime",
        asr_model="stepaudio-2.5-realtime",
        asr_base_url="https://api.stepfun.com/step_plan/v1",
        asr_api_key="unit-key",
    )
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: connections.append(FakeConnect(url, **kwargs)) or connections[-1])
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "STEPFUN_REALTIME_DIRECT_REPLY_ENABLED", True)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_ENABLED", True)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_SPEECH_MS", 200)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_MIN_AUDIO_MS", 300)
    monkeypatch.setattr(gateway_module, "SERVER_VAD_SILENCE_MS", 400)
    monkeypatch.setattr(gateway_module, "REALTIME_LOCAL_VAD_SILENCE_MS", 300)

    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 300, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 300}})
    ws = FakeClientWebsocket([start, speech, silence, "wait_for_response", stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert connections
    sent_types = [item["type"] for item in connections[0].socket.sent]
    assert "response.create" in sent_types
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop_payload = [item for item in messages if item.get("payload", {}).get("action") == "server_vad_stop"][-1]["payload"]
    assert stop_payload["speech_ms"] >= 200
    assert stop_payload["silence_target_ms"] == 300
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["provider_stream"] == "stepfun_realtime"
    assert timing["payload"]["turn_trigger_reason"] == "local_server_vad_stop"
    assert timing["payload"]["turn_trigger_silence_ms"] >= 300
    assert "realtime_first_audio_after_response_ms" in timing["payload"]


def test_gateway_streaming_asr_final_triggers_turn_before_client_stop(monkeypatch, tmp_path):
    sent = []
    connections = []
    bridge_calls = []
    send_audio_seen = asyncio.Event()
    allow_next_message = asyncio.Event()

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                if not self.incoming:
                    raise StopAsyncIteration
                item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "text": "测试一下天气",
                }))
                send_audio_seen.set()

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_ws_connect(url, **kwargs):
        conn = FakeConnect(url, **kwargs)
        connections.append(conn)
        return conn

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    batch_calls = []

    def fake_batch_asr(runtime_config, state):
        batch_calls.append(state.turn_id)
        return AsrResult(ok=True, text="你那边天气怎么样", status="ok")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        allow_next_message.set()
        return {"ok": True, "response": "好的。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", fake_ws_connect)
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 100, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 100}})
    ws = FakeClientWebsocket([start, speech, ("wait", allow_next_message), stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert send_audio_seen.is_set()
    assert bridge_calls == ["测试一下天气"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    actions = [item.get("payload", {}).get("action") for item in messages]
    assert "server_vad_stop" in actions
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "streaming_asr"
    assert asr_payload["text"] == "测试一下天气"
    assert asr_payload["streaming_asr"] is True
    assert asr_payload["streaming_asr_final_ms"] >= 0
    assert any(
        item.get("payload", {}).get("reason") == "streaming_asr_final"
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
    )


def test_gateway_streaming_asr_speech_stopped_partial_waits_for_client_stop(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                if not self.incoming:
                    raise StopAsyncIteration
                item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "测试一下天气",
                }))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "input_audio_buffer.speech_stopped",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    batch_calls = []

    def fake_batch_asr(runtime_config, state):
        batch_calls.append(state.turn_id)
        return AsrResult(ok=True, text="测试一下天气", status="ok")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "好的。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)

    speech = b"\x01\x00" * 640
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 101, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 101}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert batch_calls == [101]
    assert bridge_calls == ["测试一下天气"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert not [
        item
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
        and item.get("payload", {}).get("reason") == "streaming_asr_final"
    ]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "ok"
    assert asr_payload["text"] == "测试一下天气"
    assert asr_payload["streaming_asr"] is False
    assert asr_payload["streaming_asr_final_reason"] == "speech_stopped_partial"


def test_gateway_streaming_asr_stable_partial_waits_for_client_stop_by_default(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "我今天有点累你陪我聊两句",
                }))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "input_audio_buffer.speech_stopped",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    batch_calls = []

    def fake_batch_asr(runtime_config, state):
        batch_calls.append(state.turn_id)
        return AsrResult(ok=True, text="我今天有点累你陪我聊两句", status="ok")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "我在，先慢一点。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 1200)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 106, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 106}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert batch_calls == [106]
    assert bridge_calls == ["我今天有点累你陪我聊两句"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert not [
        item
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
        and item.get("payload", {}).get("reason") == "streaming_asr_final"
    ]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "ok"
    assert asr_payload["streaming_asr"] is False
    assert asr_payload["text"] == "我今天有点累你陪我聊两句"
    assert asr_payload["streaming_asr_final_reason"] == "stable_partial"


def test_gateway_streaming_asr_stable_partial_can_be_enabled_for_early_turn(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []
    allow_next_message = asyncio.Event()

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                if not self.incoming:
                    raise StopAsyncIteration
                item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "我今天有点累你陪我聊两句",
                }))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "input_audio_buffer.speech_stopped",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run after enabled stable streaming ASR partial")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        allow_next_message.set()
        return {"ok": True, "response": "我在，先慢一点。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 1200)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 108, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 108}})
    ws = FakeClientWebsocket([start, speech, ("wait", allow_next_message), stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["我今天有点累你陪我聊两句"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop_payload = [
        item["payload"]
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
    ][-1]
    assert stop_payload["reason"] == "streaming_asr_final"
    assert stop_payload["streaming_asr_reason"] == "stable_partial"


def test_gateway_streaming_asr_quick_ack_partial_triggers_before_client_stop(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []
    allow_next_message = asyncio.Event()

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                if not self.incoming:
                    raise StopAsyncIteration
                item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "测试一下，简单回应我一句",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run after deterministic quick ack partial")

    async def fake_stream_dialogue(websocket, config, runtime_config, state, transcript, **kwargs):
        bridge_calls.append(transcript)
        allow_next_message.set()
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": "我在呢。"},
        })
        return True

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: True)
    monkeypatch.setattr(gateway_module, "stream_dialogue_and_tts_from_bridge", fake_stream_dialogue)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_LOCAL_QUALITY_MIN_AUDIO_MS", 900)
    monkeypatch.setattr(
        gateway_module,
        "STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS",
        ("weather", "time", "activity_or_location", "local_quality"),
    )

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 109, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 109}})
    ws = FakeClientWebsocket([start, speech, ("wait", allow_next_message), stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["测试一下，简单回应我一句"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop_payload = [
        item["payload"]
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
    ][-1]
    assert stop_payload["reason"] == "streaming_asr_final"
    assert stop_payload["streaming_asr_reason"] == "deterministic_partial"


def test_gateway_bridge_speculative_gate_and_reuse_decision(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
    )
    state = TurnState(
        turn_id=201,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        bridge_speculative_text="我想聊聊今天的计划",
    )
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_ENABLED", True)
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_MIN_CHARS", 8)
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_MIN_AUDIO_MS", 900)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)

    allowed, reason = gateway_module.bridge_speculative_can_start(
        state,
        "我想聊聊今天的计划",
        runtime,
    )
    assert allowed is True
    assert reason == "ok"

    hit, hit_reason = gateway_module.bridge_speculative_reuse_decision(state, "我想聊聊今天的计划")
    assert hit is True
    assert hit_reason == "exact"

    miss, miss_reason = gateway_module.bridge_speculative_reuse_decision(state, "我不是聊计划我是问天气")
    assert miss is False
    assert miss_reason == "not_prefix"

    deterministic, deterministic_reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "你现在在干嘛",
        runtime,
    )
    assert deterministic is False
    assert deterministic_reason == "deterministic"

    weather, weather_reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "今天天气怎么样适合出门吗",
        runtime,
    )
    assert weather is False
    assert weather_reason == "fast_local_intent"

    casual, casual_reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "我想聊聊最近状态",
        runtime,
    )
    assert casual is True
    assert casual_reason == "ok"

    open_model_chat, open_model_chat_reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "我想聊聊今天的计划",
        runtime,
    )
    assert open_model_chat is True
    assert open_model_chat_reason == "ok"


def test_gateway_bridge_speculative_blocks_low_confidence_fragments(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
    )
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_ENABLED", True)
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_MIN_CHARS", 1)
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_MIN_AUDIO_MS", 0)

    allowed, reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "这。",
        runtime,
    )

    assert allowed is False
    assert reason == "low_confidence_fragment"
    assert gateway_module.transcript_is_low_confidence_fragment("这。") is True
    assert gateway_module.transcript_is_low_confidence_fragment("天气") is False
    assert gateway_module.transcript_is_low_confidence_fragment("在吗") is False
    assert gateway_module.transcript_is_low_confidence_fragment("这个方案怎么样") is False


def test_gateway_bridge_speculative_default_audio_gate_is_700ms(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
    )
    text = "我想聊聊今天的计划"
    bytes_per_ms = DEVICE_SAMPLE_RATE * 2 / 1000
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_ENABLED", True)
    monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_MIN_CHARS", 8)

    assert gateway_module.BRIDGE_SPECULATIVE_MIN_AUDIO_MS == 700

    too_early, too_early_reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=int(bytes_per_ms * 699)),
        text,
        runtime,
    )
    assert too_early is False
    assert too_early_reason == "audio_too_short"

    allowed, reason = gateway_module.bridge_speculative_can_start(
        TurnState(streaming_asr_audio_bytes=int(bytes_per_ms * 700)),
        text,
        runtime,
    )
    assert allowed is True
    assert reason == "ok"


def test_gateway_bridge_speculative_request_is_marked_speculative(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    calls = []

    async def fake_bridge_stream_events(config, state, transcript, *, metadata_extra=None):
        calls.append((transcript, dict(metadata_extra or {})))
        yield {"type": "delta", "text": "先回答一句。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "先回答一句。",
                "evidence": {"streamed": True, "speculative": True},
            },
        }

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=202, streaming_asr_started_at=time.monotonic())

    result = asyncio.run(gateway_module.run_bridge_speculative(config, state, "我想测试一下回复速度"))

    assert result.status == "completed"
    assert result.delta_chars == len("先回答一句。")
    assert calls == [("我想测试一下回复速度", {"speculative": True, "speculative_text": "我想测试一下回复速度"})]


def test_gateway_bridge_speculative_hit_reuses_prefetched_events(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    stream_calls = []
    normal_bridge_calls = []
    state = TurnState(
        turn_id=203,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        streaming_asr_started_at=time.monotonic(),
        bridge_speculative_text="我想测试一下回复速度",
    )
    speculative = gateway_module.BridgeSpeculativeResult(
        text="我想测试一下回复速度",
        events=[
            {"type": "delta", "text": "这次我会短一点。"},
            {
                "type": "final",
                "payload": {
                    "ok": True,
                    "status": "completed",
                    "response": "这次我会短一点。",
                    "evidence": {"streamed": True, "speculative": True},
                },
            },
        ],
        status="completed",
        delta_chars=len("这次我会短一点。"),
    )

    async def done_speculative():
        return speculative

    async def fake_finalize_streaming_asr_session(state_arg, runtime_config):
        return AsrResult(ok=True, text="我想测试一下回复速度", status="streaming_asr")

    async def fake_stream_dialogue(websocket, config, runtime_config, state_arg, transcript, *, preface=None, prefetched_events=None, prefetched_event_source=None):
        stream_calls.append((transcript, list(prefetched_events or []), prefetched_event_source))
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state_arg.turn_id, "text": "这次我会短一点。"},
        })
        return True

    def fake_call_bridge(config, state_arg, transcript):
        normal_bridge_calls.append(transcript)
        return {"ok": True, "response": "不该走到这里。", "evidence": {}}

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    async def scenario():
        state.bridge_speculative_status = "completed"
        state.bridge_speculative_event_count = len(speculative.events)
        state.bridge_speculative_delta_chars = speculative.delta_chars
        state.bridge_speculative_task = asyncio.create_task(done_speculative())
        await asyncio.sleep(0)
        monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
        monkeypatch.setattr(gateway_module, "ensure_tts_preface_task", lambda runtime_config: None)
        monkeypatch.setattr(gateway_module, "finalize_streaming_asr_session", fake_finalize_streaming_asr_session)
        monkeypatch.setattr(gateway_module, "await_streaming_asr_prefetch", lambda state_arg: asyncio.sleep(0))
        monkeypatch.setattr(gateway_module, "stream_dialogue_and_tts_from_bridge", fake_stream_dialogue)
        monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
        monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
        await gateway_module.run_voice_turn(
            FakeWebsocket(),
            GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn"),
            state,
        )

    asyncio.run(scenario())

    assert normal_bridge_calls == []
    assert stream_calls == [("我想测试一下回复速度", speculative.events, None)]
    assert state.bridge_speculative_task is None
    assert state.bridge_speculative_decision == "hit"
    assert state.bridge_speculative_reason == "exact"
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["bridge_speculative_status"] == "completed"


def test_gateway_bridge_speculative_miss_cancels_and_uses_normal_stream(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    stream_calls = []
    cancelled = asyncio.Event()
    state = TurnState(
        turn_id=204,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        streaming_asr_started_at=time.monotonic(),
        bridge_speculative_text="我想测试一下回复速度",
    )

    async def pending_speculative():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_finalize_streaming_asr_session(state_arg, runtime_config):
        return AsrResult(ok=True, text="我不是测试速度我是问天气", status="streaming_asr")

    async def fake_stream_dialogue(websocket, config, runtime_config, state_arg, transcript, *, preface=None, prefetched_events=None, prefetched_event_source=None):
        stream_calls.append((transcript, prefetched_events, prefetched_event_source))
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state_arg.turn_id, "text": "我按天气来回答。"},
        })
        return True

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    async def scenario():
        state.bridge_speculative_task = asyncio.create_task(pending_speculative())
        monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
        monkeypatch.setattr(gateway_module, "ensure_tts_preface_task", lambda runtime_config: None)
        monkeypatch.setattr(gateway_module, "finalize_streaming_asr_session", fake_finalize_streaming_asr_session)
        monkeypatch.setattr(gateway_module, "await_streaming_asr_prefetch", lambda state_arg: asyncio.sleep(0))
        monkeypatch.setattr(gateway_module, "stream_dialogue_and_tts_from_bridge", fake_stream_dialogue)
        monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
        await gateway_module.run_voice_turn(
            FakeWebsocket(),
            GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn"),
            state,
        )

    asyncio.run(scenario())

    assert cancelled.is_set()
    assert stream_calls == [("我不是测试速度我是问天气", None, None)]
    assert state.bridge_speculative_task is None
    assert state.bridge_speculative_decision == "miss"
    assert state.bridge_speculative_reason == "not_prefix"


def test_gateway_bridge_speculative_adopts_reusable_live_stream(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    state = TurnState(
        turn_id=205,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        streaming_asr_started_at=time.monotonic(),
        bridge_speculative_text="我想聊聊最近状态",
    )

    async def delayed_speculative():
        await state.bridge_speculative_queue.put({"type": "delta", "text": "你说，我在听。"})
        await state.bridge_speculative_queue.put({
            "type": "final",
            "payload": {"ok": True, "status": "completed", "response": "你说，我在听。"},
        })
        await state.bridge_speculative_queue.put(None)
        await asyncio.sleep(60)
        return gateway_module.BridgeSpeculativeResult(
            text="我想聊聊最近状态",
            status="completed",
            events=[],
        )

    async def scenario():
        monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
        state.bridge_speculative_queue = asyncio.Queue()
        state.bridge_speculative_task = asyncio.create_task(delayed_speculative())
        reuse = await gateway_module.resolve_bridge_speculative_reuse(state, "我想聊聊最近状态")
        events = []
        async for item in reuse.event_source:
            events.append(item)
        state.bridge_speculative_task.cancel()
        try:
            await state.bridge_speculative_task
        except asyncio.CancelledError:
            pass
        return reuse, events

    reuse, events = asyncio.run(scenario())

    assert reuse.decision == "hit"
    assert reuse.reason == "exact"
    assert reuse.events is None
    assert events[0]["text"] == "你说，我在听。"
    assert state.bridge_speculative_adopted is True


def test_gateway_bridge_speculative_live_hit_does_not_wait_for_task_done(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    state = TurnState(
        turn_id=206,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        streaming_asr_started_at=time.monotonic(),
        bridge_speculative_text="我想聊聊最近状态",
    )
    async def slow_speculative():
        await asyncio.sleep(60)

    async def scenario():
        monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
        state.bridge_speculative_queue = asyncio.Queue()
        state.bridge_speculative_task = asyncio.create_task(slow_speculative())
        started = time.monotonic()
        reuse = await gateway_module.resolve_bridge_speculative_reuse(state, "我想聊聊最近状态")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        state.bridge_speculative_task.cancel()
        try:
            await state.bridge_speculative_task
        except asyncio.CancelledError:
            pass
        return reuse, elapsed_ms

    reuse, elapsed_ms = asyncio.run(scenario())

    assert reuse.decision == "hit"
    assert reuse.reason == "exact"
    assert reuse.event_source is not None
    assert elapsed_ms < 20


def test_gateway_bridge_speculative_live_hit_is_used_by_voice_turn(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    sent = []
    stream_calls = []
    normal_bridge_calls = []
    state = TurnState(
        turn_id=207,
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2,
        streaming_asr_started_at=time.monotonic(),
        bridge_speculative_text="我想聊聊最近状态",
    )

    async def pending_speculative():
        await asyncio.sleep(60)

    async def fake_finalize_streaming_asr_session(state_arg, runtime_config):
        return AsrResult(ok=True, text="我想聊聊最近状态", status="streaming_asr")

    async def fake_stream_dialogue(
        websocket,
        config,
        runtime_config,
        state_arg,
        transcript,
        *,
        preface=None,
        prefetched_events=None,
        prefetched_event_source=None,
    ):
        events = []
        async for item in prefetched_event_source:
            events.append(item)
        stream_calls.append((transcript, prefetched_events, events))
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state_arg.turn_id, "text": "你说，我在听。"},
        })
        return True

    def fake_call_bridge(config, state_arg, transcript):
        normal_bridge_calls.append(transcript)
        return {"ok": True, "response": "不该走到这里。", "evidence": {}}

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    async def scenario():
        state.bridge_speculative_queue = asyncio.Queue()
        state.bridge_speculative_status = "started"
        state.bridge_speculative_task = asyncio.create_task(pending_speculative())
        await state.bridge_speculative_queue.put({"type": "delta", "text": "你说，我在听。"})
        await state.bridge_speculative_queue.put({
            "type": "final",
            "payload": {"ok": True, "status": "completed", "response": "你说，我在听。"},
        })
        await state.bridge_speculative_queue.put(None)
        monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
        monkeypatch.setattr(gateway_module, "ensure_tts_preface_task", lambda runtime_config: None)
        monkeypatch.setattr(gateway_module, "finalize_streaming_asr_session", fake_finalize_streaming_asr_session)
        monkeypatch.setattr(gateway_module, "await_streaming_asr_prefetch", lambda state_arg: asyncio.sleep(0))
        monkeypatch.setattr(gateway_module, "stream_dialogue_and_tts_from_bridge", fake_stream_dialogue)
        monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
        monkeypatch.setattr(gateway_module, "BRIDGE_SPECULATIVE_REUSE_ENABLED", True)
        await gateway_module.run_voice_turn(
            FakeWebsocket(),
            GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn"),
            state,
        )
        state.bridge_speculative_task.cancel()
        try:
            await state.bridge_speculative_task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert normal_bridge_calls == []
    assert stream_calls == [(
        "我想聊聊最近状态",
        None,
        [
            {"type": "delta", "text": "你说，我在听。"},
            {"type": "final", "payload": {"ok": True, "status": "completed", "response": "你说，我在听。"}},
        ],
    )]
    assert state.bridge_speculative_decision == "hit"
    assert state.bridge_speculative_reason == "exact"
    assert state.bridge_speculative_adopted is True
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["bridge_speculative_status"] == "started"


def test_gateway_streaming_asr_stable_partial_allows_common_short_question(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS", 6)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 1200)

    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "你现在在干嘛？",
    ) == (True, "ok")

    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2),
        "来。",
    ) == (False, "too_short")


def test_gateway_streaming_asr_deterministic_partial_allows_safe_local_quality(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(persona_home=str(tmp_path / "persona-home"))
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(
        gateway_module,
        "STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS",
        ("weather", "time", "activity_or_location", "local_quality"),
    )
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_LOCAL_QUALITY_MIN_AUDIO_MS", 900)

    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 1),
        "测试一下，简单回应我一句",
        runtime,
    )

    assert allowed is True
    assert reason == "ok"
    assert plan["intent"] == "local_quality"
    assert plan["local_quality_intent"] == "quick_ack"

    too_short, _, short_reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE),
        "测试一下，简单回应我一句",
        runtime,
    )
    assert too_short is False
    assert short_reason == "audio_too_short"

    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS", ("weather", "time"))
    disabled, _, disabled_reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 1),
        "测试一下，简单回应我一句",
        runtime,
    )
    assert disabled is False
    assert disabled_reason == "intent_not_early_safe"


def test_gateway_streaming_asr_stable_partial_blocks_correction(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "等一下我不是问你那边我是问我这边今天适不适合出门",
                }))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "input_audio_buffer.speech_stopped",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    batch_calls = []

    def fake_batch_asr(runtime_config, state):
        batch_calls.append(state.turn_id)
        return AsrResult(
            ok=True,
            text="等一下我不是问你那边我是问我这边今天适不适合出门",
            status="ok",
        )

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "我按你这边来判断。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 1200)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_FINAL_WAIT_MS", 100)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 107, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 107}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert batch_calls == [107]
    assert bridge_calls == ["等一下我不是问你那边我是问我这边今天适不适合出门"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert not [
        item
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
        and item.get("payload", {}).get("reason") == "streaming_asr_final"
    ]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "ok"
    assert asr_payload["streaming_asr"] is False
    assert asr_payload["streaming_asr_final_reason"] == "speech_stopped_partial"


def test_gateway_streaming_asr_short_final_waits_for_client_stop(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "text": "来。",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("short streaming ASR final should remain usable at client stop")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "好的。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS", 1200)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_FINAL_WAIT_MS", 100)

    speech = b"\x01\x00" * 640
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 102, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 102}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["来。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    assert not [
        item
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
        and item.get("payload", {}).get("reason") == "streaming_asr_final"
    ]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["text"] == "来。"
    assert asr_payload["status"] == "streaming_asr"


def test_gateway_streaming_asr_prefetch_plan_weather_and_time(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="北京",
        user_location_mode="manual",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)

    aura_weather = gateway_module.streaming_asr_prefetch_plan("你那边天气怎么样", runtime)
    user_weather = gateway_module.streaming_asr_prefetch_plan("今天天气怎么样", runtime)
    user_weather_advice = gateway_module.streaming_asr_prefetch_plan("我需要带伞吗", runtime)
    provided_geo_weather = gateway_module.streaming_asr_prefetch_plan(
        "今天天气怎么样",
        runtime,
        user_geo={"city": "上海", "timezone": "Asia/Shanghai"},
    )
    time_plan = gateway_module.streaming_asr_prefetch_plan("今天是几月几号，现在几点", runtime)
    chat_plan = gateway_module.streaming_asr_prefetch_plan("测试一下，你好吗", runtime)

    assert aura_weather["intent"] == "weather"
    assert aura_weather["subject"] == "aura"
    assert aura_weather["city"] == "上海"
    assert user_weather["intent"] == "weather"
    assert user_weather["subject"] == "user"
    assert user_weather["city"] == "北京"
    assert user_weather_advice["intent"] == "weather_advice"
    assert user_weather_advice["subject"] == "user"
    assert user_weather_advice["city"] == "北京"
    assert provided_geo_weather["subject"] == "user"
    assert provided_geo_weather["city"] == "上海"
    assert time_plan["intent"] == "time"
    assert chat_plan == {}


def test_gateway_streaming_asr_prefetch_skips_grounded_current_intent(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)

    assert gateway_module.streaming_asr_prefetch_plan("你现在在干嘛", runtime) == {}
    plan = gateway_module.streaming_asr_deterministic_partial_plan("你现在在干嘛", runtime)
    assert plan["intent"] == "activity_or_location"
    assert plan["subject"] == "aura"
    assert plan["grounded_current_intent"] == "activity"


def test_gateway_streaming_asr_prefetch_plan_local_quality_intents(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)

    mood = gateway_module.streaming_asr_prefetch_plan("你今天心情怎么样", runtime)
    quick_ack = gateway_module.streaming_asr_deterministic_partial_plan("测试一下，简单回应我一句", runtime)
    supportive = gateway_module.streaming_asr_deterministic_partial_plan("我今天有点累你陪我聊两句", runtime)
    latency = gateway_module.streaming_asr_deterministic_partial_plan("我想测试一下回复速度语音链路可能慢在哪里", runtime)
    outing = gateway_module.streaming_asr_prefetch_plan("我今天下午打算出门", runtime)
    correction = gateway_module.streaming_asr_prefetch_plan("等一下我不是问你心情怎么样", runtime)
    correction_partial = gateway_module.streaming_asr_deterministic_partial_plan("等一下我不是测试一下", runtime)
    correction_weather = gateway_module.streaming_asr_prefetch_plan("等一下我不是测试一下，我是问今天天气怎么样", runtime)
    correction_weather_partial = gateway_module.streaming_asr_deterministic_partial_plan(
        "等一下我不是测试一下，我是问今天天气怎么样",
        runtime,
    )
    reasoning = gateway_module.streaming_asr_prefetch_plan("你为什么建议我出门", runtime)

    assert mood == {
        "intent": "state_mood",
        "subject": "aura",
        "location": "",
        "local_quality": True,
    }
    assert quick_ack["intent"] == "local_quality"
    assert quick_ack["local_quality_intent"] == "quick_ack"
    assert supportive["intent"] == "local_quality"
    assert supportive["local_quality_intent"] == "supportive_chat"
    assert latency["intent"] == "local_quality"
    assert latency["local_quality_intent"] == "voice_latency_diagnostic"
    assert outing["intent"] == "outing_weather_advice"
    assert outing["subject"] == "user"
    assert outing["local_quality"] is True
    assert outing["user_weather"] is False
    assert correction == {}
    assert correction_partial == {}
    assert correction_weather == {}
    assert correction_weather_partial == {}
    assert reasoning == {}


def test_gateway_streaming_asr_prefetch_skips_weather_advice(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="北京",
        user_location_mode="manual",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)

    assert gateway_module.streaming_asr_prefetch_plan("你为什么建议我带伞？", runtime) == {}
    assert gateway_module.streaming_asr_prefetch_plan("今天要不要带伞", runtime) == {}
    assert gateway_module.streaming_asr_deterministic_partial_plan("今天穿什么合适？", runtime) == {}


def test_gateway_streaming_asr_prefetch_skips_unknown_user_weather(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="",
        user_location_mode="device_ip",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)

    assert gateway_module.streaming_asr_prefetch_plan("今天天气怎么样", runtime) == {}
    assert gateway_module.streaming_asr_prefetch_plan("北京今天天气怎么样", runtime)["city"] == "北京"


def test_gateway_streaming_asr_prefetch_starts_once_for_weather(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    async def fake_prefetch(state, runtime_config, plan):
        state.streaming_asr_prefetch_status = "done"
        state.streaming_asr_prefetch_done_ms = 12

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )
    state = TurnState(turn_id=55, streaming_asr_started_at=time.monotonic())
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "run_streaming_asr_prefetch", fake_prefetch)

    async def run_case():
        gateway_module.maybe_start_streaming_asr_prefetch(state, "你那边天气怎么样", runtime)
        first_task = state.streaming_asr_prefetch_task
        gateway_module.maybe_start_streaming_asr_prefetch(state, "你那边天气怎么样", runtime)
        assert state.streaming_asr_prefetch_task is first_task
        await first_task

    asyncio.run(run_case())

    assert state.streaming_asr_prefetch_intent == "weather"
    assert state.streaming_asr_prefetch_subject == "aura"
    assert state.streaming_asr_prefetch_location == "上海"
    assert state.streaming_asr_prefetch_status == "done"


def test_gateway_streaming_asr_prefetch_outing_refreshes_user_weather(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    calls = []
    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="上海",
        user_location_mode="manual",
    )
    state = TurnState(turn_id=61, streaming_asr_started_at=time.monotonic())
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)

    def fake_refresh(runtime_config, *, city="", latitude="", longitude=""):
        calls.append((city, latitude, longitude))
        return runtime_config, {
            "status": "fresh",
            "city": city,
            "temperature": "28",
            "condition": "多云",
        }

    monkeypatch.setattr(gateway_module, "refresh_user_weather_if_needed", fake_refresh)

    async def run_case():
        gateway_module.maybe_start_streaming_asr_prefetch(state, "我今天下午打算出门", runtime)
        await state.streaming_asr_prefetch_task

    asyncio.run(run_case())

    assert calls == [("上海", "", "")]
    assert state.streaming_asr_prefetch_status == "done"
    assert state.streaming_asr_prefetch_intent == "outing_weather_advice"
    assert state.streaming_asr_prefetch_subject == "user"
    assert state.streaming_asr_prefetch_location == "上海"


def test_gateway_streaming_asr_prefetch_does_not_start_for_chat(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )
    state = TurnState(turn_id=56, streaming_asr_started_at=time.monotonic())
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)

    gateway_module.maybe_start_streaming_asr_prefetch(state, "测试一下，你好吗", runtime)

    assert state.streaming_asr_prefetch_task is None
    assert state.streaming_asr_prefetch_status == ""


def test_gateway_streaming_asr_stable_partial_guardrails(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS", 8)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS", 1200)
    state = TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2)

    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        state,
        "我今天有点累你陪我聊两句",
    ) == (True, "ok")
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        state,
        "等一下我不是问你那边我是问我这边",
    ) == (False, "correction_marker")
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        state,
        "你好吗",
    ) == (False, "too_short")
    short_audio_state = TurnState(streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2)
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        short_audio_state,
        "我今天有点累你陪我聊两句",
    ) == (False, "audio_too_short")


def test_gateway_streaming_asr_stable_partial_default_audio_gate_is_700ms(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_STABLE_PARTIAL_MIN_CHARS", 8)
    bytes_per_ms = DEVICE_SAMPLE_RATE * 2 / 1000

    assert gateway_module.STREAMING_ASR_STABLE_PARTIAL_MIN_AUDIO_MS == 700
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        TurnState(streaming_asr_audio_bytes=int(bytes_per_ms * 699)),
        "我今天有点累你陪我聊两句",
    ) == (False, "audio_too_short")
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        TurnState(streaming_asr_audio_bytes=int(bytes_per_ms * 700)),
        "我今天有点累你陪我聊两句",
    ) == (True, "ok")
    assert gateway_module.streaming_asr_can_trigger_stable_partial(
        TurnState(streaming_asr_audio_bytes=int(bytes_per_ms * 1200)),
        "等一下我不是问这个",
    ) == (False, "correction_marker")


def test_gateway_stepfun_ws_asr_early_turn_default_audio_gate_is_700ms():
    from integrations.hermes_lily_cli import gateway as gateway_module

    assert gateway_module.STEPFUN_WS_ASR_EARLY_TURN_MIN_AUDIO_MS == 700


def test_gateway_streaming_asr_deterministic_partial_plan(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="北京",
        user_location_mode="manual",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)

    weather = gateway_module.streaming_asr_deterministic_partial_plan("你那边天气怎么样", runtime)
    user_time = gateway_module.streaming_asr_deterministic_partial_plan("今天是几月几号现在几点", runtime)
    current_activity = gateway_module.streaming_asr_deterministic_partial_plan("你现在在干嘛", runtime)
    current_location = gateway_module.streaming_asr_deterministic_partial_plan("你在哪", runtime)
    current_location_suffix = gateway_module.streaming_asr_deterministic_partial_plan("你在哪买的这个", runtime)
    chat = gateway_module.streaming_asr_deterministic_partial_plan("测试一下你好吗", runtime)

    assert weather["intent"] == "weather"
    assert weather["subject"] == "aura"
    assert user_time["intent"] == "time"
    assert current_activity["intent"] == "activity_or_location"
    assert current_activity["grounded_current_intent"] == "activity"
    assert current_location["intent"] == "activity_or_location"
    assert current_location["grounded_current_intent"] == "location"
    mood = gateway_module.streaming_asr_deterministic_partial_plan("你今天心情怎么样", runtime)
    assert mood["intent"] == "local_quality"
    assert mood["local_quality_intent"] == "state_mood"
    assert gateway_module.streaming_asr_deterministic_partial_plan("我今天下午打算出门", runtime) == {}
    assert gateway_module.streaming_asr_deterministic_partial_plan("等一下我不是问你心情怎么样", runtime) == {}
    assert current_location_suffix == {}
    assert chat == {}


def test_gateway_streaming_asr_deterministic_partial_requires_audio_and_location(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(persona_home=str(tmp_path / "persona-home"))
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
        user_home_city="",
        user_location_mode="device_ip",
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS", 1200)

    short_audio = TurnState(turn_id=57, streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2)
    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        short_audio,
        "你那边天气怎么样",
        runtime,
    )
    assert allowed is False
    assert plan["intent"] == "weather"
    assert reason == "audio_too_short"

    enough_audio = TurnState(turn_id=58, streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2 * 2)
    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        enough_audio,
        "今天天气怎么样",
        runtime,
    )
    assert allowed is False
    assert plan == {}
    assert reason == "not_deterministic"

    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        enough_audio,
        "你那边天气怎么样",
        runtime,
    )
    assert allowed is True
    assert plan["subject"] == "aura"
    assert reason == "ok"

    monkeypatch.setattr(gateway_module, "STREAMING_ASR_GROUNDED_CURRENT_MIN_AUDIO_MS", 900)
    grounded_short_audio = TurnState(turn_id=59, streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE)
    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        grounded_short_audio,
        "你在哪",
        runtime,
    )
    assert allowed is False
    assert plan["intent"] == "activity_or_location"
    assert reason == "audio_too_short"

    grounded_enough_audio = TurnState(turn_id=60, streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2)
    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        grounded_enough_audio,
        "你在哪",
        runtime,
    )
    assert allowed is True
    assert plan["grounded_current_intent"] == "location"
    assert reason == "ok"

    monkeypatch.setattr(
        gateway_module,
        "STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS",
        ("weather", "time"),
    )
    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        grounded_enough_audio,
        "你在哪",
        runtime,
    )
    assert allowed is False
    assert plan["grounded_current_intent"] == "location"
    assert reason == "intent_not_early_safe"


def test_gateway_streaming_asr_deterministic_partial_triggers_turn_before_final(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []
    allow_next_message = asyncio.Event()

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            if isinstance(item, tuple) and item[0] == "wait":
                await item[1].wait()
                if not self.incoming:
                    raise StopAsyncIteration
                item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "你那边天气怎么样",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run after deterministic streaming ASR partial")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        allow_next_message.set()
        return {"ok": True, "response": "上海现在二十四度。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS", 1200)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 104, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 104}})
    ws = FakeClientWebsocket([start, speech, ("wait", allow_next_message), stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["你那边天气怎么样"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    stop_payload = [
        item["payload"]
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
    ][-1]
    assert stop_payload["reason"] == "streaming_asr_final"
    assert stop_payload["streaming_asr_reason"] == "deterministic_partial"
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["text"] == "你那边天气怎么样"
    assert asr_payload["streaming_asr_final_reason"] == "deterministic_partial"


def test_gateway_streaming_asr_grounded_current_partial_waits_for_final_by_default(monkeypatch, tmp_path):
    sent = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            item = self.incoming.pop(0)
            return item

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()
            self.append_count = 0

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.append_count += 1
                if self.append_count == 1:
                    self.recv_queue.put_nowait(json.dumps({
                        "type": "conversation.item.input_audio_transcription.delta",
                        "text": "你现在在干嘛",
                    }))
                else:
                    self.recv_queue.put_nowait(json.dumps({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "text": "你现在在干嘛",
                    }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )

    def should_not_call_batch_asr(runtime_config, state):
        raise AssertionError("batch ASR should not run after grounded current streaming ASR final")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "我在这边听你说话。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", should_not_call_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ALLOW_PARTIAL", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS", 1200)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_EARLY_INTENTS", ("weather", "time"))

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 106, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 106}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert bridge_calls == ["你现在在干嘛"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    early_stop_payloads = [
        item["payload"]
        for item in messages
        if item.get("payload", {}).get("action") == "server_vad_stop"
    ]
    assert early_stop_payloads == []
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["text"] == "你现在在干嘛"
    assert asr_payload["streaming_asr_final_reason"] == "final"
    assert asr_payload["turn_trigger_reason"] == "client_stop"


def test_gateway_stepfun_ws_asr_deterministic_partial_finish_does_not_wait_for_final(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "你那边天气怎么样",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        asr_timeout_seconds=3,
    )
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )
    state = TurnState(
        turn_id=105,
        sample_rate=DEVICE_SAMPLE_RATE,
        audio_format="pcm",
    )
    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_FINAL_WAIT_MS", 2500)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_MIN_AUDIO_MS", 100)

    async def scenario():
        session = gateway_module.StepfunWsAsrSession(runtime, state)
        await session.start()
        state.streaming_asr_audio_bytes = DEVICE_SAMPLE_RATE * 2
        started = time.monotonic()
        await session.send_pcm(b"\x01\x00" * 320)
        result = await session.finish()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return result, elapsed_ms, session

    result, elapsed_ms, session = asyncio.run(scenario())

    assert result.ok is False
    assert result.status == "streaming_asr_partial_only"
    assert result.text == ""
    assert state.streaming_asr_final_reason == "deterministic_partial"
    assert session.confirmed_final is False
    assert elapsed_ms < 1000


def test_gateway_stepfun_ws_asr_finish_queue_put_times_out(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        asr_timeout_seconds=3,
    )
    state = TurnState(
        turn_id=106,
        sample_rate=DEVICE_SAMPLE_RATE,
        audio_format="pcm",
    )
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_FINISH_QUEUE_TIMEOUT_MS", 20)

    async def scenario():
        session = gateway_module.StepfunWsAsrSession(runtime, state)
        session._sender_task = asyncio.create_task(asyncio.sleep(10))
        for _ in range(gateway_module.STEPFUN_WS_ASR_QUEUE_FRAMES):
            session._queue.put_nowait(b"\x01\x00")
        started = time.monotonic()
        result = await session.finish()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return result, elapsed_ms

    result, elapsed_ms = asyncio.run(scenario())

    assert result.ok is False
    assert result.status == "streaming_asr_empty"
    assert state.streaming_asr_finish_queue_timeout is True
    assert state.streaming_asr_finish_queue_ms < 500
    assert state.streaming_asr_sender_drain_ms < 500
    assert elapsed_ms < 1000


def test_gateway_streaming_asr_deterministic_partial_disabled_by_default(monkeypatch, tmp_path):
    from integrations.hermes_lily_cli import gateway as gateway_module

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
    )
    state = TurnState(
        turn_id=107,
        sample_rate=DEVICE_SAMPLE_RATE,
        audio_format="pcm",
        streaming_asr_audio_bytes=DEVICE_SAMPLE_RATE * 2,
    )
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_DETERMINISTIC_PARTIAL_TURN_ENABLED", False)

    allowed, plan, reason = gateway_module.streaming_asr_can_trigger_deterministic_partial(
        state,
        "你那边天气怎么样",
        runtime,
    )

    assert allowed is False
    assert plan == {}
    assert reason == "not_deterministic"


def test_gateway_streaming_asr_prefetch_in_streaming_turn(monkeypatch, tmp_path):
    sent = []
    refresh_calls = []
    bridge_calls = []

    class FakeClientWebsocket:
        def __init__(self, incoming):
            self.incoming = incoming
            self.remote_address = ("127.0.0.1", 54321)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.incoming:
                raise StopAsyncIteration
            return self.incoming.pop(0)

        async def send(self, payload):
            sent.append(payload)

        async def close(self, code=1000, reason=""):
            pass

    class FakeStepfunAsrSocket:
        def __init__(self):
            self.sent = []
            self.recv_queue = asyncio.Queue()

        async def send(self, payload):
            item = json.loads(payload)
            self.sent.append(item)
            if item.get("type") == "input_audio_buffer.append":
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.delta",
                    "text": "你那边天气怎么样",
                }))
                self.recv_queue.put_nowait(json.dumps({
                    "type": "conversation.item.input_audio_transcription.completed",
                    "text": "你那边天气怎么样？",
                }))

        async def recv(self):
            return await self.recv_queue.get()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.socket = FakeStepfunAsrSocket()

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, exc_type, exc, tb):
            return False

    from integrations.hermes_lily_cli import gateway as gateway_module
    from integrations.aura_persona_gateway.config import PersonaGatewayConfig

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
        aura_model_mode="aura_model",
        tts_enabled=False,
    )
    persona = PersonaGatewayConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_home_city="上海",
    )

    def fake_refresh(config, *, city="", force=False):
        refresh_calls.append(city)
        updated = AuraRuntimeConfig(
            persona_home=config.persona_home,
            cached_weather_city=city,
            cached_weather_temperature="24",
            cached_weather_condition="晴",
        )
        return updated, {"ok": True, "status": "refreshed", "weather": {"status": "fresh", "city": city}}

    batch_calls = []

    def fake_batch_asr(runtime_config, state):
        batch_calls.append(state.turn_id)
        return AsrResult(ok=True, text="你那边天气怎么样", status="ok")

    def fake_call_bridge(config, state, transcript):
        bridge_calls.append(transcript)
        return {"ok": True, "response": "上海现在二十四度。", "evidence": {}}

    async def fake_tts(websocket, runtime_config, state, response, **kwargs):
        await gateway_module.send_json(websocket, {
            "type": "dialogue",
            "payload": {"turn_id": state.turn_id, "text": response},
        })

    monkeypatch.setattr(gateway_module, "ws_connect", lambda url, **kwargs: FakeConnect(url, **kwargs))
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: runtime)
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    monkeypatch.setattr(gateway_module, "refresh_cached_weather_if_needed", fake_refresh)
    monkeypatch.setattr(gateway_module, "should_stream_bridge", lambda runtime_config: False)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_batch_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_call_bridge)
    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_ASR_EARLY_TURN_ENABLED", False)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_PREFETCH_ENABLED", True)
    monkeypatch.setattr(gateway_module, "STREAMING_ASR_PREFETCH_WAIT_MS", 500)

    speech = b"\x01\x00" * (DEVICE_SAMPLE_RATE * 2)
    start = json.dumps({
        "type": "start",
        "sample_rate": DEVICE_SAMPLE_RATE,
        "format": "pcm",
        "frame_duration": 40,
        "payload": {"turn_id": 103, "server_vad": False},
    })
    stop = json.dumps({"type": "stop", "payload": {"turn_id": 103}})
    ws = FakeClientWebsocket([start, speech, stop])
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    asyncio.run(gateway_module.handle_connection(ws, config))

    assert batch_calls == []
    assert refresh_calls[-1] == "上海"
    assert refresh_calls.count("上海") >= 1
    assert bridge_calls == ["你那边天气怎么样？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    asr_payload = [item for item in messages if item.get("type") == "asr_result"][-1]["payload"]
    assert asr_payload["status"] == "streaming_asr"
    assert asr_payload["streaming_asr"] is True
    assert asr_payload["streaming_asr_prefetch_status"] == "done"
    assert asr_payload["streaming_asr_prefetch_intent"] == "weather"
    assert asr_payload["streaming_asr_prefetch_subject"] == "aura"
    assert asr_payload["streaming_asr_prefetch_location"] == "上海"
    assert asr_payload["streaming_asr_final_reason"] == "final"


def test_gateway_bridge_stream_keeps_reading_while_tts_synthesizes(monkeypatch, tmp_path):
    sent = []
    order = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        order.append("delta1")
        yield {"type": "delta", "text": "你好呀，"}
        await asyncio.sleep(0)
        order.append("delta2")
        yield {"type": "delta", "text": "第二句。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "你好呀，第二句。",
                "request_id": "req-stream",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        order.append(f"tts_start:{text}")
        if "你好" in text:
            time.sleep(0.02)
        order.append(f"tts_done:{text}")
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "TTS_PREFETCH_CONCURRENCY", 2)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=12, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    assert order.index("delta2") < order.index("tts_done:你好呀，")
    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert len(audio_frames) >= 2
    assert audio_frames[-1][12] == 1
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["streamed_bridge"] is True
    assert timing["payload"]["bridge_first_delta_ms"] >= 0
    assert timing["payload"]["asr_to_bridge_first_delta_ms"] >= 0
    assert timing["payload"]["bridge_to_tts_first_audio_ms"] >= 0
    assert timing["payload"]["asr_to_tts_first_audio_ms"] >= 0
    assert "turn_to_tts_first_audio_ms" in timing["payload"]
    assert "streaming_asr_first_delta_ms" in timing["payload"]
    assert "streaming_asr_final_ms" in timing["payload"]
    assert "tts_audio_chunk_gap_p95_ms" in timing["payload"]
    assert "tts_audio_chunk_stall_count" in timing["payload"]


def test_gateway_bridge_stream_skips_stage_direction_segments_for_tts(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "（轻轻笑了一下）"}
        yield {"type": "delta", "text": "我在。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "（轻轻笑了一下）我在。",
                "request_id": "req-stream",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 2)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=13, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "你好",
    ))

    assert streamed is True
    assert synthesized == ["我在。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "我在。"


def test_gateway_bridge_stream_falls_back_when_stream_ends_incomplete(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态啊... 其实我"}

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=14, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天想聊聊最近状态，你自然回应一句。",
    ))

    assert streamed is True
    assert synthesized == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_gateway_bridge_stream_falls_back_for_short_unfounded_status_opening(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "其实你"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "其实你",
                "request_id": "req-short-bad",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=15, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
    ))

    assert streamed is True
    assert synthesized == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_gateway_bridge_stream_blocks_status_metaphor_before_tts(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态嘛，感觉是‘电量还剩一半，但不知道往哪儿充’？"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "最近状态嘛，感觉是‘电量还剩一半，但不知道往哪儿充’？",
                "request_id": "req-status-metaphor",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 10)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=153, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天想聊聊最近状态，你自然回应一句。",
    ))

    assert streamed is True
    assert synthesized == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_gateway_stream_guard_blocks_incomplete_life_rhythm_status_axis():
    assert gateway_module.stream_bridge_response_needs_guard(
        "是觉得生活节奏",
        transcript="我今天想聊聊最近状态，你自然回应一句。",
    ) is True


def test_gateway_stream_guard_blocks_incomplete_work_rhythm_status_opening():
    assert gateway_module.stream_bridge_response_needs_guard(
        "我也觉得该理",
        transcript="我想聊聊最近的工作节奏，你自然回应一句。",
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "我也觉得该理一理，",
        transcript="我想聊聊最近的工作节奏，你自然回应一句。",
    ) is True


def test_gateway_stream_guard_blocks_cutoff_work_rhythm_reply():
    transcript = "我想聊聊最近的工作节奏，你自然回应一句。"

    assert gateway_module.stream_bridge_response_needs_guard(
        "我也觉得这种节奏有点磨人。",
        transcript=transcript,
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "我也觉得这种节奏有点磨人。是事情堆得太满了让你觉得累，还是单纯提不起劲儿搞那些",
        transcript=transcript,
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "周六凌晨三点。",
        transcript=transcript,
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "最近是事情太满，？",
        transcript=transcript,
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "从工作节奏说起：是事情太满，还是提不起劲？",
        transcript=transcript,
    ) is False


def test_gateway_stream_waits_for_complete_first_sentence_on_work_rhythm():
    transcript = "我想聊聊最近的工作节奏，你自然回应一句。"

    assert gateway_module.stream_bridge_requires_complete_first_sentence(
        transcript=transcript,
        first_segment=True,
    ) is True
    assert gateway_module.stream_bridge_should_wait_first_sentence(
        "其实我也觉得最",
        transcript=transcript,
        first_segment=True,
    ) is True


def test_gateway_stream_waits_for_complete_first_sentence_on_supportive_chat():
    transcript = "我最近加班有点烦，想聊聊。"

    assert gateway_module.stream_bridge_requires_complete_first_sentence(
        transcript=transcript,
        first_segment=True,
    ) is True
    assert gateway_module.stream_bridge_should_wait_first_sentence(
        "这种时候确实",
        transcript=transcript,
        first_segment=True,
    ) is True
    assert gateway_module.stream_bridge_should_wait_first_sentence(
        "这种时候确实不想听大道理。",
        transcript=transcript,
        first_segment=True,
    ) is False


def test_gateway_stream_waits_for_complete_first_sentence_on_job_change():
    transcript = "最近想换工作，能聊聊吗？"

    assert gateway_module.stream_bridge_requires_complete_first_sentence(
        transcript=transcript,
        first_segment=True,
    ) is True
    assert gateway_module.stream_bridge_should_wait_first_sentence(
        "可以聊。先看动因",
        transcript=transcript,
        first_segment=True,
    ) is False


def test_gateway_stream_guard_allows_status_terms_user_already_said():
    assert gateway_module.stream_bridge_response_needs_guard(
        "生活节奏有点乱的话，先看睡眠还是工作？",
        transcript="我最近状态生活节奏有点乱，想充电。",
    ) is False
    assert gateway_module.stream_bridge_response_needs_guard(
        "那怎么办",
        transcript="我想聊聊。",
    ) is False
    assert gateway_module.stream_bridge_response_needs_guard(
        "但是没关系",
        transcript="我想聊聊。",
    ) is False


def test_gateway_stream_guard_blocks_open_chat_wakeup_state_and_incomplete_fragment():
    assert gateway_module.stream_bridge_response_needs_guard(
        "反正我也刚醒，这会儿脑子最清醒。",
        transcript="我想聊聊。",
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "不过，",
        transcript="我想聊聊。",
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "不过这一大早的，你是突然想通了，还是又钻牛角尖里去了？",
        transcript="我想聊聊。",
    ) is True
    assert gateway_module.stream_bridge_response_needs_guard(
        "不过先说好，今晚这时间点儿，你是想聊点正经的，还是单纯想找人说说话？",
        transcript="我想聊聊。",
    ) is True


def test_gateway_bridge_stream_skips_status_topic_echo_and_uses_next_sentence(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态啊？"}
        yield {"type": "delta", "text": "先看睡眠还是工作？"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "最近状态啊？先看睡眠还是工作？",
                "request_id": "req-status-echo-then-good",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=151, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天想聊聊最近状态，你自然回应一句。",
    ))

    assert streamed is True
    assert synthesized == ["先看睡眠还是工作？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "先看睡眠还是工作？"


def test_gateway_bridge_stream_falls_back_for_status_topic_echo_only(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态啊？"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "最近状态啊？",
                "request_id": "req-status-echo-only",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=152, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天想聊聊最近状态，你自然回应一句。",
    ))

    assert streamed is True
    assert synthesized == ["从工作节奏说起：是事情太满，还是提不起劲？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "从工作节奏说起：是事情太满，还是提不起劲？"


def test_gateway_bridge_stream_waits_full_first_status_sentence(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态"}
        yield {"type": "delta", "text": "可以先从工作节奏复盘。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "最近状态可以先从工作节奏复盘。",
                "request_id": "req-status-first",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=16, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天想聊聊最近状态，你自然回应一句。",
    ))

    assert streamed is True
    assert synthesized == ["最近状态可以先从工作节奏复盘。"]


def test_gateway_bridge_stream_skips_empty_opening_and_uses_next_answer(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "行呀，那咱们就直奔主题。"}
        yield {"type": "delta", "text": "先看工作节奏：是事情太满，还是提不起劲？"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "行呀，那咱们就直奔主题。先看工作节奏：是事情太满，还是提不起劲？",
                "request_id": "req-empty-opening",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 16)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=161, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
    ))

    assert streamed is True
    assert synthesized == ["先看工作节奏：是事情太满，还是提不起劲？"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "先看工作节奏：是事情太满，还是提不起劲？"


def test_gateway_bridge_stream_blocks_open_chat_placeholder_to_tts(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "聊啥？"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "聊啥？",
                "request_id": "req-open-placeholder",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=162, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    assert synthesized == ["先说你最想聊的那一件。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "先说你最想聊的那一件。"


def test_gateway_bridge_stream_blocks_open_chat_companion_placeholder_tail_to_tts(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "那我就在这儿陪着你。"}
        yield {"type": "delta", "text": "你想从哪儿"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "那我就在这儿陪着你。你想从哪儿",
                "request_id": "req-open-companion-placeholder",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=163, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我想聊聊。",
    ))

    assert streamed is True
    assert synthesized == ["先说你最想聊的那一件。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "先说你最想聊的那一件。"


def test_gateway_bridge_stream_keeps_spoken_safe_sentence_when_final_tail_bad(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "最近状态可以先从工作节奏复盘。"}
        yield {"type": "delta", "text": "我看"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "最近状态可以先从工作节奏复盘。我看",
                "request_id": "req-status-tail",
                "evidence": {"streamed": True},
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=17, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我今天有点想复盘一下工作状态，你结合我们最近聊的内容自然说一句。",
    ))

    assert streamed is True
    assert synthesized == ["最近状态可以先从工作节奏复盘。"]
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "最近状态可以先从工作节奏复盘。"


def test_gateway_bridge_stream_speaks_quality_fallback_after_partial_guard(monkeypatch, tmp_path):
    sent = []
    synthesized = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    from integrations.hermes_lily_cli import gateway as gateway_module

    async def fake_bridge_stream_events(config, state, transcript):
        yield {"type": "delta", "text": "这种时候确实不想听大道理。"}
        yield {
            "type": "final",
            "payload": {
                "ok": True,
                "status": "completed",
                "response": "加班这件事先别自己憋着，是累还是烦哪一点更多？",
                "request_id": "req-supportive-tail",
                "evidence": {
                    "streamed": True,
                    "stop_reason": "voice_quality_guard_after_partial",
                    "quality_guard": {
                        "reason": "blocked_unfounded_user_state_claim",
                        "fallback_used": True,
                    },
                },
            },
        }

    def fake_synthesize_tts(runtime_config, text):
        synthesized.append(text)
        return TtsResult(ok=True, audio=f"pcm:{text}".encode("utf-8"), chunk_count=1, latency_ms=1, first_chunk_ms=1)

    monkeypatch.setattr(gateway_module, "bridge_stream_events", fake_bridge_stream_events)
    monkeypatch.setattr(gateway_module, "synthesize_tts", fake_synthesize_tts)
    monkeypatch.setattr(gateway_module, "STEPFUN_WS_TTS_ENABLED", False)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 6)

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="voxcpm",
        tts_model="voxcpm2",
        tts_voice="yan",
        tts_base_url="http://tts.local/v1/audio/speech",
        tts_sample_rate=DEVICE_SAMPLE_RATE,
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=18, audio_chunks=[b"pcm"], asr_latency_ms=3)

    streamed = asyncio.run(gateway_module.stream_dialogue_and_tts_from_bridge(
        FakeWebsocket(),
        config,
        runtime,
        state,
        "我最近加班有点烦，想聊聊。",
    ))

    assert streamed is True
    fallback = "加班这件事先别自己憋着，是累还是烦哪一点更多？"
    assert fallback in synthesized
    assert "".join(item for item in synthesized if item != fallback) == "这种时候确实不想听大道理。"
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    dialogue = [item for item in messages if item.get("type") == "dialogue"][-1]
    assert dialogue["payload"]["text"] == "加班这件事先别自己憋着，是累还是烦哪一点更多？"


def test_gateway_run_voice_turn_reports_end_to_end_audio_timing(monkeypatch, tmp_path):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    def fake_runtime():
        return AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=False,
            asr_enabled=True,
        )

    def fake_asr(runtime_config, state):
        time.sleep(0.001)
        return AsrResult(ok=True, text="你好")

    def fake_bridge(config, state, transcript=""):
        time.sleep(0.001)
        return {"ok": True, "response": "嗯，我在。", "request_id": "req-1", "evidence": {}}

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", fake_runtime)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_bridge)
    monkeypatch.setattr(
        gateway_module,
        "synthesize_and_stream_tts",
        lambda websocket, runtime_config, turn_id, text, *, stream_id, **kwargs: asyncio.sleep(
            0,
            result=gateway_module.TtsResult(ok=True, chunk_count=1, latency_ms=12, first_chunk_ms=7, first_audio_ms=7),
        ),
    )
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=42, audio_chunks=[b"pcm"])

    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["turn_id"] == 42
    assert timing["payload"]["asr_ms"] >= 0
    assert "streaming_asr_first_delta_ms" in timing["payload"]
    assert "streaming_asr_final_ms" in timing["payload"]
    assert timing["payload"]["bridge_ms"] >= 0
    assert timing["payload"]["asr_to_bridge_first_delta_ms"] >= 0
    assert timing["payload"]["bridge_to_tts_first_audio_ms"] >= 7
    assert timing["payload"]["asr_to_tts_first_audio_ms"] >= 7
    assert timing["payload"]["turn_to_tts_first_audio_ms"] >= timing["payload"]["bridge_to_tts_first_audio_ms"]
    assert timing["payload"]["trigger_to_tts_first_audio_ms"] >= timing["payload"]["bridge_to_tts_first_audio_ms"]
    assert timing["payload"]["turn_trigger_reason"] == "client_stop"
    assert "streaming_asr_final_reason" in timing["payload"]
    assert "tts_first_audio_since_bridge_ms" in timing["payload"]
    assert timing["payload"]["tts_first_audio_ms"] == 7
    assert timing["payload"]["tts_total_ms"] == 12
    assert "tts_audio_chunk_gap_p95_ms" in timing["payload"]
    assert "tts_audio_chunk_stall_count" in timing["payload"]


def test_gateway_run_voice_turn_sends_cached_preface_before_slow_bridge(monkeypatch, tmp_path):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    def fake_runtime():
        return AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="voxcpm",
            tts_model="voxcpm2",
            tts_voice="yan",
            tts_base_url="http://tts.local/v1/audio/speech",
            tts_sample_rate=DEVICE_SAMPLE_RATE,
            asr_enabled=True,
        )

    def fake_asr(runtime_config, state):
        return AsrResult(ok=True, text="你好")

    def fake_bridge(config, state, transcript=""):
        time.sleep(0.02)
        return {"ok": True, "response": "我在这里。", "request_id": "req-1", "evidence": {}}

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "TTS_PREFACE_ENABLED", True)
    monkeypatch.setattr(gateway_module, "TTS_PREFACE_DELAY_MS", 1)
    monkeypatch.setattr(gateway_module, "TTS_PREFACE_MAX_WAIT_MS", 100)
    monkeypatch.setattr(gateway_module, "TTS_PREFACE_TEXT", "嗯，我想一下。")
    gateway_module._TTS_PREFACE_CACHE.clear()
    gateway_module._TTS_PREFACE_TASKS.clear()
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", fake_runtime)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_bridge)
    monkeypatch.setattr(
        gateway_module,
        "synthesize_tts_preface",
        lambda runtime_config, key=None: TtsResult(ok=True, audio=b"preface-pcm", chunk_count=1, audio_bytes=11),
    )

    async def fake_main_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, preface=False):
        await gateway_module.send_tts_binary(websocket, turn_id, b"answer-pcm", stream_id=stream_id, is_final=is_final)
        return TtsResult(ok=True, chunk_count=1, audio_chunk_count=1, audio_bytes=10, latency_ms=8, first_chunk_ms=8, first_audio_ms=8)

    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_main_tts)

    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=43, audio_chunks=[b"pcm"])

    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert len(audio_frames) == 2
    assert audio_frames[0][12] == 0
    assert audio_frames[0][16:] == b"preface-pcm"
    assert audio_frames[1][12] == 1
    assert audio_frames[1][16:] == b"answer-pcm"
    messages = [json.loads(item) for item in sent if isinstance(item, str)]
    timing = [item for item in messages if item.get("payload", {}).get("action") == "turn_audio_timing"][-1]
    assert timing["payload"]["tts_preface_ms"] >= 0


def test_gateway_run_voice_turn_can_preface_during_slow_asr(monkeypatch, tmp_path):
    sent = []

    class FakeWebsocket:
        async def send(self, payload):
            sent.append(payload)

    def fake_runtime():
        return AuraRuntimeConfig(
            persona_home=str(tmp_path / "persona-home"),
            tts_enabled=True,
            tts_provider="voxcpm",
            tts_model="voxcpm2",
            tts_voice="yan",
            tts_base_url="http://tts.local/v1/audio/speech",
            tts_sample_rate=DEVICE_SAMPLE_RATE,
            asr_enabled=True,
        )

    def fake_asr(runtime_config, state):
        time.sleep(0.02)
        return AsrResult(ok=True, text="你好")

    def fake_bridge(config, state, transcript=""):
        return {"ok": True, "response": "我在。", "request_id": "req-1", "evidence": {}}

    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "TTS_PREFACE_ENABLED", True)
    monkeypatch.setattr(gateway_module, "TTS_PREFACE_DELAY_MS", 1)
    monkeypatch.setattr(gateway_module, "TTS_PREFACE_MAX_WAIT_MS", 100)
    gateway_module._TTS_PREFACE_CACHE.clear()
    gateway_module._TTS_PREFACE_TASKS.clear()
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", fake_runtime)
    monkeypatch.setattr(gateway_module, "transcribe_turn_audio", fake_asr)
    monkeypatch.setattr(gateway_module, "call_bridge", fake_bridge)
    monkeypatch.setattr(
        gateway_module,
        "synthesize_tts_preface",
        lambda runtime_config, key=None: TtsResult(ok=True, audio=b"preface-pcm", chunk_count=1, audio_bytes=11),
    )

    async def fake_main_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, preface=False):
        await gateway_module.send_tts_binary(websocket, turn_id, b"answer-pcm", stream_id=stream_id, is_final=is_final)
        return TtsResult(ok=True, chunk_count=1, audio_chunk_count=1, audio_bytes=10, latency_ms=8, first_chunk_ms=8, first_audio_ms=8)

    monkeypatch.setattr(gateway_module, "synthesize_and_stream_tts", fake_main_tts)

    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")
    state = TurnState(turn_id=44, audio_chunks=[b"pcm"])

    asyncio.run(run_voice_turn(FakeWebsocket(), config, state))

    audio_frames = [item for item in sent if isinstance(item, bytes)]
    assert len(audio_frames) == 2
    assert audio_frames[0][12] == 0
    assert audio_frames[1][12] == 1


def test_gateway_tts_text_chunks_preserve_sentence_boundaries():
    chunks = tts_text_chunks("你好。这个句子比较长，需要按照逗号拆开，保证每段都比较短。", max_chars=18)

    assert chunks[0] == "你好。"
    assert len(chunks) >= 3
    assert all(len(chunk) <= 18 for chunk in chunks)


def test_gateway_tts_text_chunks_make_first_chunk_short_for_fast_start():
    chunks = tts_text_chunks("不晓得嘞，我现在在商场里面，看不见天。不过下午那会儿出门的时候，上海这边是多云。")

    assert chunks[0] == "不晓得嘞，"
    assert len(chunks[0]) <= 8
    assert "我现在在商场里面" in chunks[1]
    assert "看不见天" in chunks[1]


def test_gateway_stream_tts_waits_if_cleaned_first_segment_is_too_short():
    segment, pending = pop_stream_tts_segment("好哒我就在这儿听着。", force=False, first_segment=True)

    assert device_spoken_text(segment, allow_fallback=False) != "我"
    assert device_spoken_text(segment, allow_fallback=False) != "在"
    assert device_spoken_text(segment, allow_fallback=False).startswith("我就")
    assert pending == ""


def test_gateway_stream_tts_first_segment_waits_for_nearby_comma(monkeypatch):
    from integrations.hermes_lily_cli import gateway as gateway_module

    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_MIN_CHARS", 4)
    monkeypatch.setattr(gateway_module, "BRIDGE_STREAM_FIRST_SEGMENT_CHARS", 10)
    monkeypatch.setattr(gateway_module, "TTS_TEXT_CHUNK_CHARS", 18)

    segment, pending = pop_stream_tts_segment("是想复盘最近的工作节奏，还是单纯想找人吐吐槽？", force=False, first_segment=True)

    assert segment == "是想复盘最近的工作节奏，"
    assert pending == "还是单纯想找人吐吐槽？"


def test_gateway_stream_tts_followup_can_use_ws_eager_limit():
    text = "后面这一段应该更早送进语音队列继续生成。"

    default_segment, default_pending = pop_stream_tts_segment(text, force=False, first_segment=False)
    eager_segment, eager_pending = pop_stream_tts_segment(
        text,
        force=False,
        first_segment=False,
        followup_limit_chars=8,
    )

    # followup_limit_chars 仅保留签名兼容，不再按字符硬切；
    # 两种调用都必须等到强标点边界，不得把“更早”拆成两段。
    assert eager_segment == default_segment == "后面这一段应该更早送进语音队列继续生成。"
    assert eager_pending == default_pending == ""


def test_gateway_device_spoken_text_strips_obvious_roleplay_markup():
    assert device_spoken_text("（看到消息，把手机举到唇边，笑着回了一条语音）嗯，我听到啦。") == "嗯，我听到啦。"
    assert device_spoken_text("Lily: *轻声笑了笑* 我在。") == "我在。"


def test_gateway_device_spoken_text_preserves_normal_parentheses():
    assert device_spoken_text("采样率（16k）没问题。") == "采样率（16k）没问题。"
    assert device_spoken_text("我看到你的消息了，等下回你。") == "我看到你的消息了，等下回你。"


def test_gateway_background_work_beans_scales_with_duration():
    from integrations.hermes_lily_cli.gateway import background_work_beans

    assert background_work_beans(0) == 1
    assert background_work_beans(4.9) == 1
    assert background_work_beans(5) == 2
    assert background_work_beans(30) == 7
    assert background_work_beans(600) == 20  # 封顶
    assert background_work_beans(-3) == 1
    assert background_work_beans(None) == 1


def test_gateway_background_work_progress_climbs_but_never_finishes_early():
    from integrations.hermes_lily_cli.gateway import background_work_progress

    assert background_work_progress(0, 90) == 10
    mid = background_work_progress(45, 90)
    assert 10 < mid < 95
    assert background_work_progress(90, 90) == 95
    assert background_work_progress(300, 90) == 95  # 超时也不到 100
    assert background_work_progress(10, 0) >= 10  # 除零防御


def test_gateway_device_spoken_text_converts_symbols_for_tts():
    # 后台天气结果那种 markdown+emoji 文本，TTS 不该念符号（用户听到的“四低四低”）。
    spoken = device_spoken_text("🌩️ 上海明天 **雷阵雨**，气温 26~32°C，湿度 78%，风速 22km/h。")
    assert "🌩" not in spoken
    assert "*" not in spoken
    assert "°C" not in spoken and "%" not in spoken and "km/h" not in spoken
    assert "26到32度" in spoken
    assert "百分之78" in spoken
    assert "22公里每小时" in spoken
    assert device_spoken_text("比特币现在 $63,265，≈ 45万人民币。") == "比特币现在 63265美元，大概 45万人民币。"


def test_gateway_device_spoken_text_dedupes_repeated_realtime_text():
    assert device_spoken_text("今天上海多云，31度。今天上海多云，31度。") == "今天上海多云，31度。"
    assert device_spoken_text("心情还挺亮的，跟你说话会放松一点。跟你说话会放松一点。") == "心情还挺亮的，跟你说话会放松一点。"
    assert device_spoken_text("采样率正常。采样率正常！") == "采样率正常。"
    assert device_spoken_text("好，好，我听着。") == "好，好，我听着。"


def test_gateway_stream_bridge_fallback_response_anchors_job_change():
    assert gateway_module.stream_bridge_fallback_response("最近想换工作，能聊聊吗？") == (
        "换工作这件事先拆开看：是现在耗着难受，还是新机会更吸引你？"
    )
    assert gateway_module.stream_bridge_fallback_response("我想聊聊") == "先说你最想聊的那一件。"


def test_gateway_device_spoken_text_strips_low_value_voice_openers():
    assert device_spoken_text("好哒我就在这儿听着。") == "我就在这儿听着。"
    assert device_spoken_text("好哒咱们就从今天说起。") == "咱们就从今天说起。"
    assert device_spoken_text("是滴，先看工作节奏。") == "先看工作节奏。"
    assert device_spoken_text("那就聊嘛。不过先说正事。") == "不过先说正事。"
    assert device_spoken_text("嘛。先说正事。") == "先说正事。"


def test_gateway_device_spoken_text_keeps_pure_filler_nonempty():
    assert device_spoken_text("好哒。") == "好哒。"
    assert device_spoken_text("是滴，") == "是滴，"


def test_gateway_device_spoken_text_drops_pure_filler_for_stream_tts():
    assert device_spoken_text("好哒。", allow_fallback=False) == ""
    assert device_spoken_text("那就聊嘛。", allow_fallback=False) == ""
    assert device_spoken_text("嘛。", allow_fallback=False) == ""


def test_gateway_stepfun_realtime_instructions_include_weather_context(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        cached_weather_enabled=True,
        cached_weather_city="上海",
        cached_weather_temperature="31.2",
        cached_weather_condition="多云",
        cached_weather_humidity="59",
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )

    instructions = stepfun_realtime_instructions(runtime)

    assert "不是翻译" in instructions
    assert "绝对不要说“帮你翻译成英文”" in instructions
    assert "不要说“我来查/这就帮你查”" in instructions
    assert "上海，31.2度，多云，湿度59%" in instructions


def test_gateway_stepfun_realtime_instructions_follow_session_language(tmp_path):
    english = AuraRuntimeConfig(persona_home=str(tmp_path), asr_language="en")
    japanese = AuraRuntimeConfig(persona_home=str(tmp_path), asr_language="ja")

    assert "Reply only in natural English" in stepfun_realtime_instructions(english)
    assert "自然な日本語だけ" in stepfun_realtime_instructions(japanese)


def test_lily_http_handler_limits_concurrency(monkeypatch):
    def fake_run(command, **kwargs):
        time.sleep(0.2)
        return subprocess.CompletedProcess(command, 0, stdout="slow ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")
    config = build_config(parse_args(["--max-concurrency", "1", "--queue-timeout", "0.01"]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first_done = threading.Event()

    def first_request():
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'{"goal":"first"}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        urlopen(request, timeout=3).read()
        first_done.set()

    worker = threading.Thread(target=first_request, daemon=True)
    worker.start()
    time.sleep(0.05)
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'{"goal":"second"}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 429
            assert payload["error"] == "server is busy; retry later"
        else:  # pragma: no cover - defensive
            raise AssertionError("expected HTTP 429")
    finally:
        first_done.wait(timeout=3)
        server.shutdown()
        thread.join(timeout=3)


def test_lily_http_handler_rejects_invalid_json(monkeypatch):
    config = build_config(parse_args([]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'["not-an-object"]',
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 400
            assert payload["ok"] is False
        else:  # pragma: no cover - defensive
            raise AssertionError("expected HTTP 400")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_lily_http_handler_rejects_empty_goal(monkeypatch):
    config = build_config(parse_args([]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b'{"goal":""}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 400
            assert payload["error"] == "goal is required"
        else:  # pragma: no cover - defensive
            raise AssertionError("expected HTTP 400")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_gateway_parse_args_defaults():
    args = parse_gateway_args([])

    assert args.host == "0.0.0.0"
    assert args.port == 8787
    assert args.bridge_url == "http://127.0.0.1:8765/turn"


def test_gateway_call_bridge_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true,"response":"gateway ok"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["forwarded_for"] = req.get_header("X-forwarded-for")
        return FakeResponse()

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    config = GatewayConfig(
        host="127.0.0.1",
        port=8787,
        bridge_url="http://bridge/turn",
        bridge_timeout_seconds=12,
    )
    state = TurnState(turn_id=7, device_id="dev", boot_id="boot", audio_bytes=123, client_ip="203.0.113.8")

    result = call_bridge(config, state)

    assert result["response"] == "gateway ok"
    assert captured["url"] == "http://bridge/turn"
    assert captured["timeout"] == 12
    assert captured["body"]["metadata"]["turn_id"] == 7
    assert captured["body"]["metadata"]["audio_bytes"] == 123
    assert captured["body"]["metadata"]["client_ip"] == "203.0.113.8"
    assert captured["forwarded_for"] == "203.0.113.8"


def test_gateway_status_update_payload_uses_cached_weather(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        cached_weather_city="上海",
        cached_weather_temperature="22℃",
        cached_weather_condition="小雨",
        cached_weather_icon=2,
        cached_weather_humidity="80",
        cached_weather_source="open_meteo",
        cached_weather_observed_at="2026-06-04T14:00",
        cached_weather_updated_at=int(time.time()),
        cached_weather_ttl_seconds=3600,
    )

    payload = status_update_payload(runtime)

    assert payload["weather_temperature"] == 22.0
    assert payload["weather_icon"] == 2
    assert payload["weather_city"] == "上海"
    assert payload["weather_condition"] == "小雨"
    assert payload["weather_humidity"] == "80"
    assert payload["weather_source"] == "open_meteo"
    assert payload["weather_observed_at"] == "2026-06-04T14:00"


def test_gateway_status_update_payload_prioritizes_local_dialogue_quota(tmp_path):
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
    )
    key = gateway_module._model_quota_cache_key(runtime)
    gateway_module._MODEL_QUOTA_CACHE[key] = (
        time.monotonic(),
        _normalize_stepfun_plan(
            "stepfun",
            "stepaudio-2.5-chat",
            {"five_hour_usage_left_rate": 0.82, "weekly_usage_left_rate": 0.61},
        ),
    )

    payload = status_update_payload(runtime)

    assert payload["quota"]["headline"] == "AURA"
    assert payload["quota"]["summary_display"] == "AURA 50/50"
    assert payload["quota"]["primary"]["label"] == "AURA"
    assert payload["quota"]["primary"]["display"] == "AURA 50/50"
    assert payload["quota"]["secondary"] == {}
    gateway_module._MODEL_QUOTA_CACHE.pop(key, None)


def test_gateway_world_status_drives_scene_and_sleepwear_from_plan(tmp_path, monkeypatch):
    import datetime as dt
    from zoneinfo import ZoneInfo

    from integrations.aura_persona_gateway.config import PersonaGatewayConfig
    from integrations.aura_persona_gateway.store import LilyPersonaStore

    persona = PersonaGatewayConfig(
        enabled=True,
        persona_home=str(tmp_path / "persona-home"),
        companion_home=str(tmp_path / "companion-home"),
        aura_home_city="上海",
        world_model_enabled=True,
    )
    monkeypatch.setattr(gateway_module, "load_persona_config", lambda: persona)
    runtime = AuraRuntimeConfig(persona_home=persona.persona_home)
    midnight = dt.datetime(2026, 7, 1, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    sleeping = gateway_module.companion_world_status_fields(runtime, now=midnight)

    assert sleeping["scene"] == "home.bedroom"
    assert sleeping["world_slot"] == "sleep"
    assert sleeping["outfit_mode"] == "sleepwear"

    store = LilyPersonaStore(persona.companion_db_path)
    plan = store.today_plan(persona.scope, day_key="2026-07-01")
    dinner = next(item for item in plan if item["slot_key"] == "dinner")
    night = next(item for item in plan if item["slot_key"] == "night_settle")

    dinner_status = gateway_module.companion_world_status_fields(
        runtime,
        now=float(dinner["scheduled_at"]) + 1,
    )
    night_status = gateway_module.companion_world_status_fields(
        runtime,
        now=float(night["scheduled_at"]) + 1,
    )

    assert dinner_status["scene"] == "home.kitchen"
    assert dinner_status["outfit_mode"] == "daywear"
    assert night_status["scene"] == "home.bedroom"
    assert night_status["outfit_mode"] == "sleepwear"


def test_gateway_refreshes_weather_before_status_payload(tmp_path, monkeypatch):
    def fake_refresh(config, *, city="", force=False):
        updated = AuraRuntimeConfig(
            persona_home=config.persona_home,
            cached_weather_city=city or "上海",
            cached_weather_temperature="27",
            cached_weather_condition="晴",
            cached_weather_icon=0,
            cached_weather_humidity="66",
            cached_weather_source="open_meteo",
            cached_weather_observed_at="2026-06-04T14:30",
            cached_weather_updated_at=int(time.time()),
            cached_weather_ttl_seconds=3600,
        )
        return updated, {"ok": True, "status": "refreshed"}

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.refresh_cached_weather_if_needed", fake_refresh)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        cached_weather_city="上海",
        cached_weather_temperature="",
        cached_weather_condition="",
    )

    refreshed = refresh_runtime_weather_for_gateway(runtime)
    payload = status_update_payload(refreshed)

    assert refreshed.cached_weather_temperature == "27"
    assert payload["weather_temperature"] == 27.0
    assert payload["weather_city"] == "上海"
    assert payload["weather_humidity"] == "66"


def test_gateway_background_task_result_url():
    assert (
        background_task_result_url("http://bridge:8765/turn", "task 1")
        == "http://bridge:8765/persona/background-task/task%201"
    )


def test_gateway_fetch_background_task_result(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true,"status":"sent","task_id":"task-1","body":"done"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("integrations.hermes_lily_cli.gateway.request.urlopen", fake_urlopen)
    config = GatewayConfig(host="127.0.0.1", port=8787, bridge_url="http://bridge/turn")

    result = fetch_background_task_result(config, "task-1")

    assert result["body"] == "done"
    assert captured["url"] == "http://bridge/persona/background-task/task-1"
    assert captured["timeout"] == 5.0


def test_lily_http_handler_rejects_large_body(monkeypatch):
    config = build_config(parse_args([]))
    handler = make_handler(config)

    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/turn",
            data=b"x" * (64 * 1024 + 1),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            urlopen(request, timeout=3)
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            assert exc.code == 400
            assert "too large" in payload["error"]
        else:  # pragma: no cover - defensive
            raise AssertionError("expected HTTP 400")
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_compose_keeps_stepfun_tts_warm_enabled_by_default():
    root = Path(__file__).resolve().parents[1]
    compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8")
    env_example_text = (root / ".env.example").read_text(encoding="utf-8")

    assert "AURA_TTS_STEPFUN_WS_WARM_ENABLED: ${AURA_TTS_STEPFUN_WS_WARM_ENABLED:-1}" in compose_text
    assert "AURA_TTS_STEPFUN_WS_WARM_ENABLED=1" in env_example_text


def test_voice_latency_matrix_summarizes_profile_metrics():
    from tools.voice_latency_matrix import PROFILES, summarize_profile

    runs = [
        {
            "ok": True,
            "first_audio_sent_ms": 640,
            "timing": {
                "tts_first_text_to_audio_ms": 420,
                "tts_audio_chunk_gap_max_ms": 180,
                "tts_audio_chunk_stall_count": 0,
            },
        },
        {
            "ok": True,
            "first_audio_sent_ms": 720,
            "timing": {
                "tts_first_text_to_audio_ms": 510,
                "tts_audio_chunk_gap_max_ms": 320,
                "tts_audio_chunk_stall_count": 1,
            },
        },
    ]

    summary = summarize_profile(PROFILES["baseline"], runs)

    assert summary["ok"] == 2
    assert summary["metrics"]["first_audio_sent_ms"]["p50"] == 640
    assert summary["metrics"]["first_audio_sent_ms"]["p95"] == 720
    assert summary["metrics"]["tts_first_text_to_audio_ms"]["p95"] == 510
    assert summary["metrics"]["tts_audio_chunk_stall_count"]["max"] == 1


def test_voice_latency_benchmark_speech_stop_sim_waits_for_server_vad(monkeypatch, tmp_path):
    import tools.voice_latency_benchmark as benchmark

    captured = {}

    class FakeWs:
        pass

    async def fake_handle_connection(ws, config):
        captured["ws"] = ws
        await ws.send(json.dumps({
            "type": "system",
            "payload": {"action": "server_vad_stop", "turn_id": 1},
        }))
        await ws.send(json.dumps({
            "type": "system",
            "payload": {"action": "turn_audio_timing", "turn_id": 1},
        }))

    monkeypatch.setattr(benchmark, "handle_connection", fake_handle_connection)
    monkeypatch.setattr(benchmark.gateway_module, "ws_connect", lambda url, **kwargs: FakeWs())

    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        asr_enabled=True,
        asr_mode="api",
        asr_provider="stepfun",
        asr_model="stepaudio-2.5-asr-stream",
        asr_base_url="https://api.stepfun.com/v1",
        asr_api_key="unit-key",
    )

    result = asyncio.run(benchmark.run_voice_sim_once(
        runtime,
        "测试一下",
        bridge_url="http://bridge/turn",
        timeout=5,
        audio_ms=100,
        fake_streaming_asr=False,
        fake_streaming_asr_early_final=False,
        fake_streaming_asr_speech_stop=True,
    ))

    assert result["fake_streaming_asr_speech_stop"] is True
    assert result["ok"] is False
    assert result["audio_bytes"] == 0
    assert captured["ws"].stop_after_server_vad is True
    assert captured["ws"].wait_after_stop is True
    assert captured["ws"].stop_timeout <= 0.25


def test_voice_latency_benchmark_persona_llm_splits_soul_llm_timing(monkeypatch, tmp_path):
    import tools.voice_latency_benchmark as benchmark

    persona_home = tmp_path / "persona-home"
    (persona_home / "persona").mkdir(parents=True)
    (persona_home / "persona" / "soul.md").write_text("测试 soul\n保持自然短答。", encoding="utf-8")

    class FakeStreamResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield 'data: {"choices":[{"delta":{"content":"从工作节奏说起："}}]}\n\n'.encode("utf-8")
            yield 'data: {"choices":[{"delta":{"content":"是事情太满，还是提不起劲？"}}]}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(req, timeout):
        return FakeStreamResponse()

    monkeypatch.setattr("integrations.aura_persona_gateway.llm.urlopen", fake_urlopen)
    runtime = AuraRuntimeConfig(
        persona_home=str(persona_home),
        aura_model_mode="aura_model",
        aura_model_provider="stepfun",
        aura_model_model="stepaudio-2.5-chat",
        aura_model_base_url="https://api.stepfun.com/step_plan/v1",
        aura_model_api_key="unit-key",
        aura_model_reasoning_effort="none",
    )

    result = asyncio.run(benchmark.run_persona_llm_once(
        runtime,
        "我想复盘最近工作状态。",
        persona_home=str(persona_home),
    ))

    assert result["mode"] == "persona-llm"
    assert result["ok"] is True
    assert result["model_skipped"] is False
    assert result["response"] == "从工作节奏说起：是事情太满，还是提不起劲？"
    assert result["first_delta_ms"] >= 0
    assert result["first_model_delta_ms"] >= 0
    assert result["first_tts_text_ms"] >= result["first_delta_ms"]
    assert result["aura_llm_first_delta_ms"] >= 0
    assert result["aura_llm_response_open_ms"] >= 0
    assert result["aura_llm_response_to_first_delta_ms"] >= 0
    assert result["aura_llm_first_audible_delta_ms"] >= 0
    assert result["aura_llm_complete_ms"] >= 0
    assert result["persona_context_build_ms"] >= 0
    assert result["persona_prompt_chars"] > len("测试 soul")
    assert result["llm_billing_scope"] == "step_plan"
    assert result["tts_segment_count"] >= 1
    assert result["tts_segments"][0]["text"].startswith("从工作")


def test_voice_latency_benchmark_tts_only_reports_first_audio(monkeypatch, tmp_path):
    import tools.voice_latency_benchmark as benchmark

    async def fake_synthesize_and_stream_tts(websocket, runtime_config, turn_id, text, *, stream_id, is_final=True, **kwargs):
        await gateway_module.send_tts_binary(websocket, turn_id, b"pcm-first", stream_id=stream_id, is_final=False)
        await gateway_module.send_tts_binary(websocket, turn_id, b"", stream_id=stream_id, is_final=True)
        return TtsResult(
            ok=True,
            audio_bytes=len(b"pcm-first"),
            latency_ms=17,
            first_chunk_ms=9,
            first_audio_ms=11,
            audio_chunk_count=1,
            streamed=True,
        )

    monkeypatch.setattr(benchmark, "synthesize_and_stream_tts", fake_synthesize_and_stream_tts)
    runtime = AuraRuntimeConfig(
        persona_home=str(tmp_path / "persona-home"),
        tts_enabled=True,
        tts_provider="stepfun",
        tts_model="stepaudio-2.5-tts",
        tts_voice="voice-tone-test",
        tts_base_url="https://api.stepfun.com/step_plan/v1",
        tts_api_key="unit-key",
    )

    result = asyncio.run(benchmark.run_tts_only_once(runtime, "从工作节奏说起：是事情太满，还是提不起劲？"))

    assert result["mode"] == "tts-only"
    assert result["ok"] is True
    assert result["tts_billing_scope"] == "step_plan"
    assert result["first_audio_sent_ms"] >= 0
    assert result["tts_first_chunk_ms"] == 9
    assert result["tts_first_audio_ms"] == 11
    assert result["tts_latency_ms"] == 17
    assert result["audio_bytes"] == len(b"pcm-first")
    assert result["streamed"] is True


def test_voice_latency_benchmark_summarizes_provider_http_error():
    import tools.voice_latency_benchmark as benchmark

    summary = benchmark._provider_error_summary({
        "detail": json.dumps({
            "error": {
                "message": "you have no active step plan subscription",
                "type": "request_params_invalid",
            }
        })
    })

    assert summary == "request_params_invalid: you have no active step plan subscription"


def test_smoke_test_runs_cli_and_http(monkeypatch, capsys):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="smoke ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")

    code = smoke_main(["--provider", "deepseek", "--model", "m", "--timeout", "5"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"]["cli"]["response"] == "smoke ok"
    assert payload["checks"]["http"]["health"]["ok"] is True
    assert payload["checks"]["http"]["turn"]["response"] == "smoke ok"


def test_mdns_advertise_disabled_by_default(monkeypatch):
    from integrations.hermes_lily_cli.mdns_advertise import maybe_start_mdns_advertise

    monkeypatch.delenv("AURA_MDNS_ADVERTISE_ENABLED", raising=False)

    assert maybe_start_mdns_advertise(8787) is None


def test_mdns_advertise_skips_without_zeroconf(monkeypatch, capsys):
    import builtins

    from integrations.hermes_lily_cli.mdns_advertise import maybe_start_mdns_advertise

    monkeypatch.setenv("AURA_MDNS_ADVERTISE_ENABLED", "1")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "zeroconf":
            raise ImportError("no zeroconf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert maybe_start_mdns_advertise(8787) is None
    assert "zeroconf not installed" in capsys.readouterr().err


def test_extract_reminder_payload_prefers_evidence_then_voice_turn():
    evidence = {"reminder": {"reminder_id": "rem-a", "fire_at_epoch": 1}}
    voice_turn = {"debug": {"reminder": {"reminder_id": "rem-b", "fire_at_epoch": 2}}}

    assert gateway_module.extract_reminder_payload(evidence, voice_turn)["reminder_id"] == "rem-a"
    assert gateway_module.extract_reminder_payload({}, voice_turn)["reminder_id"] == "rem-b"
    assert gateway_module.extract_reminder_payload({}, {}) is None
    assert gateway_module.extract_reminder_payload(None, None) is None


def test_voice_reminder_fires_via_active_connection(monkeypatch):
    import time as time_module

    spoken: list[str] = []

    async def fake_send_dialogue_and_tts(websocket, runtime_config, state, response, **kwargs):
        spoken.append(response)

    monkeypatch.setattr(gateway_module, "send_dialogue_and_tts", fake_send_dialogue_and_tts)
    monkeypatch.setattr(gateway_module, "load_runtime_config_for_gateway", lambda: None)
    monkeypatch.setattr(gateway_module, "REMINDER_RETRY_INTERVAL_SECONDS", 0.05)

    async def scenario():
        websocket = object()
        state = TurnState(turn_id=99)
        gateway_module.register_active_device_connection(websocket, state)
        try:
            gateway_module.maybe_schedule_voice_reminder(
                {
                    "reminder": {
                        "reminder_id": "rem-unit-1",
                        "kind": "reminder",
                        "fire_at_epoch": time_module.time() + 0.2,
                        "fire_at_iso": "test",
                        "announce_text": "叮，到点了：带小狗洗澡。",
                    }
                },
                None,
            )
            assert "rem-unit-1" in gateway_module._SCHEDULED_REMINDERS
            # 同一个 reminder_id 不会重复排。
            gateway_module.maybe_schedule_voice_reminder(
                {
                    "reminder": {
                        "reminder_id": "rem-unit-1",
                        "fire_at_epoch": time_module.time() + 0.2,
                        "announce_text": "重复",
                    }
                },
                None,
            )
            assert len(gateway_module._SCHEDULED_REMINDERS) == 1
            await asyncio.wait_for(
                gateway_module._SCHEDULED_REMINDERS["rem-unit-1"],
                timeout=5,
            )
        finally:
            gateway_module.unregister_active_device_connection(websocket)

    asyncio.run(scenario())

    assert spoken == ["叮，到点了：带小狗洗澡。"]
    assert "rem-unit-1" not in gateway_module._SCHEDULED_REMINDERS


def test_voice_reminder_cancel_all(monkeypatch):
    import time as time_module

    async def scenario():
        gateway_module.maybe_schedule_voice_reminder(
            {
                "reminder": {
                    "reminder_id": "rem-unit-2",
                    "fire_at_epoch": time_module.time() + 60,
                    "announce_text": "不该播出来",
                }
            },
            None,
        )
        assert "rem-unit-2" in gateway_module._SCHEDULED_REMINDERS
        gateway_module.maybe_schedule_voice_reminder({"reminder": {"cancel_all": True}}, None)
        assert not gateway_module._SCHEDULED_REMINDERS
        await asyncio.sleep(0)

    asyncio.run(scenario())
