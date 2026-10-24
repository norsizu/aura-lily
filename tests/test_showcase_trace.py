"""X1 X-Ray 大屏：``GET /trace`` 与 ``GET /trace/data``。

覆盖任务书 X1 的要求：
- 未设 ``AURA_TRACE_TOKEN`` → 两个路由都 404；
- 令牌不匹配 → 403，且响应里不回显令牌本身；
- 令牌正确 → ``/trace`` 返回自包含 HTML（无任何外链）、``/trace/data`` 返回
  state/emotion/ledger/plan/notes/why/server_time 全部字段；
- 情绪那一节带 ``last_resolution`` 评估链；
- 数据库只读打开（mode=ro），读不到库时给的是错误而不是 500。
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from integrations.aura_persona_gateway.config import PersonaGatewayConfig
from integrations.aura_persona_gateway.store import LilyPersonaStore
from integrations.aura_persona_gateway.trace_data import (
    SHOWCASE_LEDGER_LIMIT,
    collect_showcase_trace,
)
from integrations.aura_persona_gateway.world import build_world_snapshot
from integrations.hermes_lily_cli import server as lily_server
from integrations.hermes_lily_cli.server import TRACE_DATA_PATH, TRACE_PAGE_PATH, build_config, make_handler, parse_args

CITY = "上海"
NOW = time.time()


# ── 测试辅助 ────────────────────────────────────────────────
def _persona_home(tmp_path: Path) -> Path:
    persona_home = tmp_path / "persona-home"
    (persona_home / "persona").mkdir(parents=True, exist_ok=True)
    (persona_home / "persona" / "soul.md").write_text("主 soul\n", encoding="utf-8")
    return persona_home


def _config(tmp_path: Path) -> PersonaGatewayConfig:
    return PersonaGatewayConfig(
        enabled=True,
        persona_home=str(_persona_home(tmp_path)),
        companion_home=str(tmp_path / "companion-home"),
        hermes_home=str(tmp_path / "hermes-home"),
        aura_home_city=CITY,
    )


def _seed_db(tmp_path: Path) -> tuple[PersonaGatewayConfig, LilyPersonaStore]:
    """造一份有状态、有日程、有账本流水、有情绪评估链的 companion.db。"""
    config = _config(tmp_path)
    store = LilyPersonaStore(config.companion_db_path)
    state = store.get_or_create_state(config.scope)
    state.update({"mood": 28, "energy": 61, "satiety": 44, "stress": 66, "trust": 50, "beans": 50})
    state["metadata"] = {
        "social_need": 70,
        "curiosity": 40,
        "privacy_sensitivity": 30,
        "resource_comfort": 30,
        "emotion_state": {
            "emotion": "neutral",
            "label": "平静",
            "intensity": 2,
            "cause": "下午下雨，没出门",
            "at": NOW - 120,
            "last_resolution": {
                "source": "llm",
                "raw": "happy",
                "modulated_from": "心情 28<30",
                "inertia_from": "sad",
                "reason": "LLM 自评 happy → 状态调制：心情 28<30 → neutral",
            },
        },
        # X3 便签 + X2 的 why 痕迹，大屏两个面板都要有东西可画。
        "pinned_notes": [
            {"text": "买牛奶", "source": "voice", "at": NOW - 60},
            {"text": "带伞", "source": "agent", "at": NOW - 300},
        ],
        "self_why_state": {
            "event_keys": ["ledger-why-key"],
            "question": "你为什么不开心",
            "at": NOW - 30,
        },
    }
    store.save_state(config.scope, state)

    snapshot = build_world_snapshot(
        config=config,
        store=store,
        state=store.get_or_create_state(config.scope),
        query_context={"intent": "world_tick"},
        weather_snapshot={
            "status": "fresh",
            "city": CITY,
            "condition": "小雨",
            "temperature": 21.0,
            "humidity": 88.0,
            "source": "test",
            "observed_at": "2026-08-04T10:00:00+08:00",
        },
        now=NOW,
    )
    assert snapshot["today_plan"], "没有日程，大屏底部面板就没东西测了"
    return config, store


def _serve(tmp_path: Path, monkeypatch, *, token: str = "unit-trace-token"):
    monkeypatch.setenv("AURA_PERSONA_HOME", str(_persona_home(tmp_path)))
    monkeypatch.setenv("AURA_COMPANION_HOME", str(tmp_path / "companion-home"))
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")
    if token:
        monkeypatch.setenv("AURA_TRACE_TOKEN", token)
    else:
        monkeypatch.delenv("AURA_TRACE_TOKEN", raising=False)
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(server, path: str):
    try:
        response = urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5)
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    return response.status, response.read().decode("utf-8")


def _stop(server, thread) -> None:
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


# ── 鉴权 ────────────────────────────────────────────────────
def test_trace_routes_are_absent_without_token(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch, token="")
    try:
        page_status, _ = _get(server, TRACE_PAGE_PATH)
        data_status, body = _get(server, f"{TRACE_DATA_PATH}?token=anything")
    finally:
        _stop(server, thread)

    assert page_status == 404
    assert data_status == 404
    assert json.loads(body)["ok"] is False


def test_trace_routes_reject_a_wrong_token(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        page_status, _ = _get(server, f"{TRACE_PAGE_PATH}?token=nope")
        data_status, data_body = _get(server, f"{TRACE_DATA_PATH}?token=nope")
        missing_status, _ = _get(server, TRACE_DATA_PATH)
    finally:
        _stop(server, thread)

    assert page_status == 403
    assert data_status == 403
    assert missing_status == 403
    # 403 的响应体里不能把真令牌漏出来。
    assert "unit-trace-token" not in data_body


def test_trace_routes_accept_the_configured_token(tmp_path, monkeypatch):
    _seed_db(tmp_path)
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        page_status, page_body = _get(server, f"{TRACE_PAGE_PATH}?token=unit-trace-token")
        data_status, data_body = _get(server, f"{TRACE_DATA_PATH}?token=unit-trace-token")
    finally:
        _stop(server, thread)

    assert page_status == 200
    assert data_status == 200
    assert "<html" in page_body.lower()
    assert json.loads(data_body)["ok"] is True


# ── 公开只读大屏 /trace/live ─────────────────────────────────
def test_public_live_routes_are_absent_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AURA_TRACE_PUBLIC", raising=False)
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        page_status, _ = _get(server, "/trace/live")
        data_status, _ = _get(server, "/trace/live/data")
    finally:
        _stop(server, thread)

    assert page_status == 404
    assert data_status == 404


def test_public_live_routes_need_no_token_and_hide_internals(tmp_path, monkeypatch):
    _seed_db(tmp_path)
    monkeypatch.setenv("AURA_TRACE_PUBLIC", "1")
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        page_status, page_body = _get(server, "/trace/live")
        data_status, data_body = _get(server, "/trace/live/data")
        # 令牌路由不受公开开关影响，仍然要令牌。
        locked_status, _ = _get(server, TRACE_DATA_PATH)
    finally:
        _stop(server, thread)

    assert page_status == 200
    assert "<html" in page_body.lower()
    assert data_status == 200
    payload = json.loads(data_body)
    assert payload["ok"] is True
    assert "db" not in payload
    assert "scope" not in payload
    assert "state" in payload and "ledger" in payload
    assert str(tmp_path) not in data_body
    assert "unit-trace-token" not in page_body + data_body
    assert locked_status == 403


def test_trace_page_is_self_contained(tmp_path, monkeypatch):
    server, thread = _serve(tmp_path, monkeypatch)
    try:
        _status, page_body = _get(server, f"{TRACE_PAGE_PATH}?token=unit-trace-token")
    finally:
        _stop(server, thread)

    # 路演现场可能没有外网：页面不能引任何 CDN/字体/脚本。情绪脸是内联的
    # data URI，先把它们剥掉再查，免得 base64 里碰巧拼出关键字。
    faces = re.findall(r"data:image/png;base64,[A-Za-z0-9+/=]+", page_body)
    assert len(faces) == 8, "八种情绪脸都要内联"
    lowered = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", page_body).lower()
    for marker in ("http://", "https://", "//cdn", "src=", "@import", "url("):
        assert marker not in lowered, marker
    # 令牌从页面自己的 URL 里取，然后拿去轮询 /trace/data。
    assert "/trace/data" in page_body
    assert "location.search" in page_body
    assert "2000" in page_body  # 2 秒轮询


# ── 数据内容 ────────────────────────────────────────────────
def test_trace_data_payload_carries_every_panel(tmp_path):
    config, store = _seed_db(tmp_path)

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    for key in ("db", "scope", "day_key", "server_time", "server_time_text", "state", "emotion", "ledger", "plan", "notes", "why"):
        assert key in payload, key
    assert payload["scope"] == {
        "platform": config.scope.platform,
        "chat_id": config.scope.chat_id,
        "user_id": config.scope.user_id,
    }
    assert payload["server_time"] == NOW
    assert payload["server_time_text"]

    # 世界模型跑过一遍会自己改数值（下雨心情 -1），所以跟库里最终值对。
    final_state = store.get_or_create_state(config.scope)
    tracked = payload["state"]["tracked"]
    assert tracked["mood"] == int(final_state["mood"])
    assert tracked["energy"] == int(final_state["energy"])
    assert tracked["stress"] == int(final_state["stress"])

    emotion = payload["emotion"]
    assert emotion["label"] == "平静"
    assert emotion["intensity"] == 2
    assert emotion["cause"] == "下午下雨，没出门"
    # 评估链：LLM 自评 → 状态调制 → 最终情绪。
    assert emotion["last_resolution"]["source"] == "llm"
    assert emotion["last_resolution"]["raw"] == "happy"
    assert "心情 28<30" in emotion["last_resolution"]["chain"]

    assert payload["plan"], "今天的日程要有"
    assert any(slot.get("current") for slot in payload["plan"])
    assert all("time" in slot and "title" in slot for slot in payload["plan"])

    assert payload["ledger"]
    assert len(payload["ledger"]) <= SHOWCASE_LEDGER_LIMIT
    assert all(row.get("time") for row in payload["ledger"])
    # 每一行都要能回答"谁在什么时候因为什么变了多少"。
    row = payload["ledger"][0]
    for key in ("time", "source", "title", "cause", "delta", "changes", "event_key"):
        assert key in row, key
    assert row["source"]

    # 便签只上用户让她记的；后台查询顺手钉的（agent）是一次性结果，不上墙。
    notes = payload["notes"]
    assert [note["text"] for note in notes] == ["买牛奶"]
    assert [note["source"] for note in notes] == ["voice"]

    assert payload["why"]["event_keys"] == ["ledger-why-key"]
    assert payload["why"]["question"] == "你为什么不开心"


def test_trace_data_payload_explains_where_the_emotion_landed(tmp_path):
    config, store = _seed_db(tmp_path)
    store.save_im_message(config.scope, direction="user", body="今天带你去看海", message_type="user_text")
    store.save_im_message(config.scope, direction="aura", body="好呀！<emo:happy>", message_type="aura_text")

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    # 大屏"她听到 / 她说"：她的回复要去掉 <emo:...> 标签再上屏。
    assert payload["exchange"]["heard"] == "今天带你去看海"
    assert payload["exchange"]["said"] == "好呀！"
    # 世界模型跑过一次，就知道她在哪、在做什么、什么天气。
    assert payload["place"]["location"]
    assert payload["place"]["weather"] == "小雨"
    # 情绪落地：语气 + 设备脸；页面用的中文标签也一起下发。
    effects = payload["emotion"]["effects"]
    assert effects["face"] == "neutral"
    assert effects["voice_tone"] == "平常的语气"
    assert payload["emotion_labels"]["happy"] == "高兴"
    assert payload["field_labels"]["mood"] == "心情"
    # 账本每行的变化拆成结构化条目，页面据此画涨跌色块。
    for row in payload["ledger"]:
        items = row["change_items"]
        assert [f"{item['label']} {item['before']}→{item['after']}" for item in items] == row["changes"]


def test_trace_data_replays_each_round_with_its_own_emotion(tmp_path):
    from integrations.aura_persona_gateway.emotion_engine import write_emotion_state

    config, store = _seed_db(tmp_path)
    # 第一轮在情绪历史上线前：只有对话，没有评估链，回看时要留空而不是编一个。
    store.save_im_message(config.scope, direction="user", body="早上好", message_type="user_text")
    store.save_im_message(config.scope, direction="aura", body="早呀", message_type="aura_text")
    rounds = [("没有一个开源", "这确实让人头大<emo:sad>", "sad", "规则兜底：用户沮丧"), ("晚上看电影吧", "好呀！", "happy", "LLM 自评")]
    for heard, said, emotion, reason in rounds:
        time.sleep(0.01)
        store.save_im_message(config.scope, direction="user", body=heard, message_type="user_text")
        store.save_im_message(config.scope, direction="aura", body=said, message_type="aura_text")
        state = store.get_or_create_state(config.scope)
        write_emotion_state(state, emotion=emotion, intensity=3, at=time.time(), resolution={"source": "llm", "raw": emotion, "reason": reason})
        store.save_state(config.scope, state)

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    exchanges = payload["exchanges"]
    assert [x["heard"] for x in exchanges] == ["早上好", "没有一个开源", "晚上看电影吧"]
    assert exchanges[0]["emotion"] == {}
    assert exchanges[1]["said"] == "这确实让人头大"
    assert exchanges[1]["emotion"]["emotion"] == "sad"
    assert exchanges[1]["emotion"]["last_resolution"]["reason"] == "规则兜底：用户沮丧"
    assert exchanges[2]["emotion"]["emotion"] == "happy"
    # 当前这一轮照旧在 emotion 里，历史滚动保留，不会越攒越多。
    assert payload["emotion"]["emotion"] == "happy"


def test_background_result_replaces_the_ack_in_its_round(tmp_path):
    config, store = _seed_db(tmp_path)
    store.save_im_message(config.scope, direction="user", body="查一下明天北京的天气", message_type="user_text")
    store.save_im_message(config.scope, direction="aura", body="好，我去查，弄完马上告诉你。", message_type="aura_text")
    time.sleep(0.01)
    store.save_im_message(config.scope, direction="aura", body="明天北京阴天，8到17度。", message_type="background_task_result")

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    for exchange in (payload["exchange"], payload["exchanges"][-1]):
        assert exchange["said"] == "明天北京阴天，8到17度。"
        assert exchange["ack"] == "好，我去查，弄完马上告诉你。"
        assert exchange["background"] is True


def test_emotion_history_rolls_over():
    from integrations.aura_persona_gateway.emotion_engine import EMOTION_HISTORY_KEY, EMOTION_HISTORY_LIMIT, write_emotion_state

    state: dict = {}
    for index in range(EMOTION_HISTORY_LIMIT + 5):
        write_emotion_state(state, emotion="happy", intensity=2, at=1000.0 + index)
    history = state["metadata"][EMOTION_HISTORY_KEY]
    assert len(history) == EMOTION_HISTORY_LIMIT
    assert history[-1]["at"] == 1000.0 + EMOTION_HISTORY_LIMIT + 4


def test_trace_data_exchange_is_empty_before_any_dialogue(tmp_path):
    config, _store = _seed_db(tmp_path)

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    assert payload["exchange"] == {}


def test_trace_data_payload_marks_nothing_current_when_plan_is_empty(tmp_path):
    config = _config(tmp_path)
    LilyPersonaStore(config.companion_db_path).get_or_create_state(config.scope)

    payload = collect_showcase_trace(config.companion_db_path, now=NOW)

    assert payload["plan"] == []
    assert payload["ledger"] == []
    assert payload["notes"] == []
    assert payload["why"]["event_keys"] == []


def test_trace_data_payload_reports_a_missing_db(tmp_path):
    config = _config(tmp_path)

    try:
        collect_showcase_trace(config.companion_db_path, now=NOW)
    except FileNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("库不存在时应该抛 FileNotFoundError")


def test_trace_data_opens_the_db_read_only(tmp_path, monkeypatch):
    config, _store = _seed_db(tmp_path)
    seen: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(target, *args, **kwargs):
        seen.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    collect_showcase_trace(config.companion_db_path, now=NOW)

    uris = [item for item in seen if "companion.db" in item]
    assert uris, "应该用 sqlite3 连接打开 companion.db"
    assert all("mode=ro" in item for item in uris), uris
    # 只读连接写不进去：大屏再怎么轮询也不会改库。
    conn = real_connect(f"file:{config.companion_db_path}?mode=ro", uri=True)
    try:
        try:
            conn.execute("UPDATE companion_state SET mood=1")
        except sqlite3.OperationalError:
            pass
        else:  # pragma: no cover
            raise AssertionError("mode=ro 连接不应该能写")
    finally:
        conn.close()


def test_trace_data_serves_over_http_without_persona_config(tmp_path, monkeypatch):
    """没有 persona 配置时 /trace/data 给的是错误 JSON，不是 500。"""
    monkeypatch.setenv("AURA_PERSONA_HOME", str(tmp_path / "no-such-persona"))
    monkeypatch.setenv("AURA_COMPANION_HOME", str(tmp_path / "no-such-companion"))
    monkeypatch.setenv("AURA_USER_GEO_PROVIDER", "disabled")
    monkeypatch.setenv("AURA_TRACE_TOKEN", "unit-trace-token")
    config = build_config(parse_args(["--hermes-home", str(tmp_path / "hermes-home")]))
    handler = make_handler(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _get(server, f"{TRACE_DATA_PATH}?token=unit-trace-token")
    finally:
        _stop(server, thread)

    assert status == 200
    assert json.loads(body)["ok"] is False
