"""E-firmware 契约检查：设备显示真实情绪脸（F1/F2/F3/F4）。

风格同 tests/test_idle_info_board.py —— 直接读 C 源码做静态契约断言，
不在宿主机上编译 ESP-IDF 工程（那由 idf.py 负责）。
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware" / "esp32" / "main"

WS_CLIENT = FIRMWARE / "network" / "ws_client.c"
MESSAGES_C = FIRMWARE / "protocol" / "messages.c"
MESSAGES_H = FIRMWARE / "protocol" / "messages.h"
PANELS_C = FIRMWARE / "ui" / "panels.c"
STATE_HELPERS_C = FIRMWARE / "ui" / "state_helpers.c"

# E1 情绪集合：顺序即 emotion_115.bin 的资源索引（任务书 F2）。
E1_EMOTIONS = (
    "neutral",
    "happy",
    "excited",
    "shy",
    "sad",
    "worried",
    "pouty",
    "surprised",
)

# F3 要求的 E1 → emoji_48 兜底索引。
E1_TO_EMOJI = {
    "neutral": 0,     # neutral
    "happy": 1,       # proud（闭眼笑）
    "excited": 7,     # excited
    "shy": 6,         # shy
    "sad": 4,         # apologetic（低头）
    "worried": 2,     # thinking
    "pouty": 5,       # assertive
    "surprised": 3,   # surprised
}

EMOTION_115_PATH = "/spiffs/emotion_115.bin"
EMOTION_115_BYTES = 8 * 115 * 115  # 105800


def _function_body(source: str, signature: str) -> str:
    """截取 signature 所在函数的函数体（到第一个单独一行的 '}' 为止）。

    跳过前向声明：只有参数列表后面紧跟 '{' 的那个出现才是定义。
    """
    start = signature.index("(")
    cursor = 0
    while True:
        found = source.index(signature, cursor)
        open_paren = found + start
        depth = 0
        for pos in range(open_paren, len(source)):
            if source[pos] == "(":
                depth += 1
            elif source[pos] == ")":
                depth -= 1
                if depth == 0:
                    break
        if source[pos + 1 :].lstrip().startswith("{"):
            break
        cursor = found + 1
    end = source.index("\n}", found)
    return source[found:end]


def test_dialogue_parses_payload_emotion():
    """F1：dialogue 分支读 payload.emotion，并随字幕一起生效。"""
    ws = WS_CLIENT.read_text(encoding="utf-8")

    dialogue = ws[ws.index('strcmp(mtype, "dialogue") == 0') :]
    dialogue = dialogue[: dialogue.index("\n    }")]
    assert 'cJSON_GetObjectItem(payload, "emotion")' in dialogue

    # 情绪写进 pending 缓冲，而不是收到消息的那一刻
    assert "s_pending_emotion" in ws
    buffered = _function_body(ws, "static void ws_buffer_pending_dialogue(")
    assert "const char *emotion" in buffered
    assert "s_pending_emotion_valid = true" in buffered

    applied = _function_body(ws, "static void ws_apply_pending_dialogue(")
    assert "g_state.current_emotion" in applied
    assert "aura_ui_set_dialogue" in applied
    # 应用点在字幕之后：先上字幕，再跟这轮的情绪
    assert applied.index("aura_ui_set_dialogue") < applied.index("g_state.current_emotion")

    cleared = _function_body(ws, "static void ws_clear_pending_dialogue(")
    assert "s_pending_emotion[0] = '\\0'" in cleared
    assert "s_pending_emotion_valid = false" in cleared


def test_dialogue_emotion_is_normalised_to_e1_and_optional():
    """F1：字段缺失保留当前情绪（旧服务端兼容），非法值当 neutral。"""
    ws = WS_CLIENT.read_text(encoding="utf-8")
    dialogue = ws[ws.index('strcmp(mtype, "dialogue") == 0') :]
    dialogue = dialogue[: dialogue.index("\n    }")]

    assert "msg_emotion_to_index(emotion_item->valuestring)" in dialogue
    assert "msg_emotion_name(" in dialogue
    # 没带字段 → 传 NULL → 不动 current_emotion
    assert "const char *emotion_name = NULL;" in dialogue


def test_e1_emotion_table_order_matches_asset_layout():
    """F2：8 个 E1 名字按序在映射表里，顺序即 emotion_115.bin 索引。"""
    header = MESSAGES_H.read_text(encoding="utf-8")
    source = MESSAGES_C.read_text(encoding="utf-8")

    assert "#define E1_EMOTION_COUNT 8" in header
    table = source[source.index("static const char *E1_EMOTION_NAMES[]") :]
    table = table[: table.index("};")]
    names = [line for line in table.splitlines() if '"' in line]
    assert len(names) == 2  # 每行 4 个
    order = [
        token.strip(' ",')
        for line in names
        for token in line.split('"')[1::2]
    ]
    assert tuple(order) == E1_EMOTIONS


def test_emotion_115_asset_path_and_length():
    """F2：/spiffs/emotion_115.bin，105800 字节，只尝试加载一次。"""
    panels = PANELS_C.read_text(encoding="utf-8")

    assert EMOTION_115_PATH in panels
    assert "#define EMOTION_FACE_SIZE 115" in panels
    assert "#define EMOTION_FACE_COUNT 8" in panels
    assert EMOTION_115_BYTES == 105800
    assert (
        "size_t total = EMOTION_FACE_COUNT * EMOTION_FACE_SIZE * EMOTION_FACE_SIZE;"
        in panels
    )

    loader = _function_body(panels, "static void load_emotion_faces(")
    assert "if (s_emotion_face_loaded || s_emotion_face_tried) return;" in loader
    assert "s_emotion_face_tried = true;" in loader
    # 长度不对就整体放弃
    assert "heap_caps_free(s_emotion_face_data); s_emotion_face_data = NULL; return;" in loader


def test_emotion_face_wins_then_emoji_fallback_then_outfit():
    """F3：情绪脸 → emoji_48 兜底 → 服装脸，三级优先级。"""
    panels = PANELS_C.read_text(encoding="utf-8")

    draw_face = _function_body(panels, "static void draw_face(")
    # 签名改成传情绪字符串，draw_face 自己决定画哪一层
    assert "int outfit_idx, const char *emotion" in draw_face
    assert "load_emotion_faces();" in draw_face
    assert "msg_emotion_to_index(emotion)" in draw_face
    # emoji 兜底只在非 neutral / 非空时触发
    assert "strcmp(emotion, \"neutral\") != 0" in draw_face
    assert "draw_emoji(buf, buf_w, buf_h, x, y, size, emotion_to_index(emotion));" in draw_face
    # 最后才是服装脸，且行为不变
    assert "s_face_data + (size_t)outfit_idx * FACE_SIZE * FACE_SIZE" in draw_face
    assert draw_face.index("s_face_data") > draw_face.index("s_emotion_face_data")

    # 调用点把当前情绪和当前穿着一起交给 draw_face
    call = panels[panels.index("draw_face(graybuf, width, height, emoji_x") :]
    call = call[: call.index(";")]
    assert "state->current_outfit, state->current_emotion" in call


def test_e1_to_emoji_fallback_mapping():
    """F3：E1 → emoji_48 索引映射表，旧词表继续能用。"""
    panels = PANELS_C.read_text(encoding="utf-8")

    table = panels[panels.index("static const int E1_TO_EMOJI[E1_EMOTION_COUNT]") :]
    table = table[: table.index("};")]
    order = [
        token.split("/*")[0].strip().rstrip(",")
        for token in table.splitlines()
        if token.strip()[:1].isdigit()
    ]
    assert len(order) == len(E1_EMOTIONS)
    assert [int(value) for value in order] == [E1_TO_EMOJI[name] for name in E1_EMOTIONS]

    mapper = _function_body(panels, "static int emotion_to_index(")
    # 先按 E1 查，查不到再按旧名表查
    assert mapper.index("msg_emotion_to_index(emotion)") < mapper.index('"proud"')
    assert '"apologetic"' in mapper and '"assertive"' in mapper and '"thinking"' in mapper


def test_neutral_emotion_falls_through_to_outfit_face():
    """F3：neutral / 空情绪走服装脸，不碰情绪脸和 emoji。"""
    panels = PANELS_C.read_text(encoding="utf-8")
    draw_face = _function_body(panels, "static void draw_face(")

    guard = draw_face[
        draw_face.index('if (emotion && emotion[0] !=') : draw_face.index("draw_emoji(buf")
    ]
    assert 'strcmp(emotion, "neutral") != 0' in guard
    # neutral 时 msg_emotion_to_index 命中，走的是情绪脸那一支而不是兜底
    assert "if (e1 >= 0)" in draw_face


def test_emotion_calms_down_after_ten_minutes_idle():
    """F4：待机 10 分钟没对话 → 情绪回 neutral，复用现有空闲计时。"""
    helpers = STATE_HELPERS_C.read_text(encoding="utf-8")

    assert "#define EMOTION_IDLE_CALM_MS (10LL * 60LL * 1000LL)" in helpers
    tick = _function_body(helpers, "bool aura_ui_display_tick(")
    assert "g_state.current_emotion" in tick
    assert "now_ms - g_state.idle_since_ms >= EMOTION_IDLE_CALM_MS" in tick
    assert 'copy_text(g_state.current_emotion, sizeof(g_state.current_emotion), "neutral");' in tick
    # 不新开任务/定时器：搭在同一个 truly_idle 分支上
    assert tick.index("const bool truly_idle") < tick.index("EMOTION_IDLE_CALM_MS")
