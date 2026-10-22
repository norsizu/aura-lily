"""E1/E2：情绪集合、兼容映射、标签剥离、规则兜底、状态调制与惯性。

只测纯函数，不碰 store / HTTP：协议和账本那一半在 test_emotion_protocol.py。
"""

from __future__ import annotations

from integrations.aura_persona_gateway.emotion_engine import (
    EMOTIONS,
    EMOTION_LABELS,
    EMOTION_MOOD_DELTA,
    EMOTION_TAG_INSTRUCTION,
    apply_inertia,
    classify,
    emotion_label,
    emotion_ledger_cause,
    emotion_mood_delta,
    emotion_prompt_line,
    modulate,
    next_intensity,
    normalize_emotion,
    read_emotion_state,
    resolve_turn_emotion,
    strip_emotion_tag,
    strip_mid_emotion_tags,
    write_emotion_state,
    EmotionTagStripper,
)


# ── E1 情绪集合与兼容映射 ───────────────────────────────────
def test_emotion_set_is_exactly_the_eight_protocol_values():
    assert EMOTIONS == (
        "neutral",
        "happy",
        "excited",
        "shy",
        "sad",
        "worried",
        "pouty",
        "surprised",
    )


def test_every_emotion_has_a_chinese_label():
    assert set(EMOTION_LABELS) == set(EMOTIONS)
    assert EMOTION_LABELS["neutral"] == "平静"
    assert EMOTION_LABELS["happy"] == "高兴"
    assert EMOTION_LABELS["excited"] == "兴奋"
    assert EMOTION_LABELS["shy"] == "害羞"
    assert EMOTION_LABELS["sad"] == "难过"
    assert EMOTION_LABELS["worried"] == "担心"
    assert EMOTION_LABELS["pouty"] == "委屈"
    assert EMOTION_LABELS["surprised"] == "惊讶"


def test_normalize_emotion_keeps_valid_values():
    for value in EMOTIONS:
        assert normalize_emotion(value) == value
    assert normalize_emotion("HAPPY") == "happy"
    assert normalize_emotion(" sad ") == "sad"


def test_normalize_emotion_maps_legacy_values():
    assert normalize_emotion("warm") == "happy"
    assert normalize_emotion("firm") == "neutral"
    assert normalize_emotion("proud") == "neutral"
    assert normalize_emotion("calm") == "neutral"
    assert normalize_emotion("focused") == "neutral"
    assert normalize_emotion("attentive") == "neutral"
    assert normalize_emotion("curious") == "surprised"


def test_normalize_emotion_rejects_unknown_values():
    for value in ("", None, "angry", "生气", "sadness", 7):
        assert normalize_emotion(value) == ""


def test_emotion_mood_delta_matches_the_ledger_table():
    assert EMOTION_MOOD_DELTA == {
        "neutral": 0,
        "happy": 2,
        "excited": 3,
        "shy": 1,
        "sad": -3,
        "worried": -2,
        "pouty": -2,
        "surprised": 0,
    }
    assert emotion_mood_delta("warm") == 2
    assert emotion_mood_delta("angry") == 0
    assert emotion_label("angry") == ""


def test_prompt_instruction_lists_the_e1_values():
    for value in EMOTIONS:
        assert value in EMOTION_TAG_INSTRUCTION
    assert "<emo:X>" in EMOTION_TAG_INSTRUCTION


# ── E2.2 流式标签剥离 ───────────────────────────────────────
def test_stripper_passes_a_complete_first_line_tag_through():
    stripper = EmotionTagStripper()
    out = stripper.feed("<emo:sad>我在。")
    assert out == "我在。"
    assert stripper.emotion == "sad"
    assert stripper.raw_emotion == "sad"
    assert stripper.started is True


def test_stripper_handles_a_tag_split_across_three_chunks():
    stripper = EmotionTagStripper()
    assert stripper.feed("<em") == ""
    assert stripper.feed("o:sh") == ""
    assert stripper.feed("y>你好") == "你好"
    assert stripper.emotion == "shy"
    assert stripper.finish() == ""


def test_stripper_releases_immediately_when_reply_does_not_start_with_a_tag():
    stripper = EmotionTagStripper()
    # 第一个字符不是 "<"：一个字都不许憋着，否则首字延迟会涨。
    assert stripper.feed("你好") == "你好"
    assert stripper.emotion == ""
    assert stripper.started is True


def test_stripper_allows_leading_whitespace_before_the_tag():
    stripper = EmotionTagStripper()
    assert stripper.feed("\n  ") == ""
    assert stripper.feed("<emo:happy>") == ""
    assert stripper.feed("早呀") == "早呀"
    assert stripper.emotion == "happy"


def test_stripper_drops_a_tag_with_an_invalid_value():
    stripper = EmotionTagStripper()
    assert stripper.feed("<emo:angry>你好") == "你好"
    # 标签剥掉了，但 emotion 记为无效（空串），让上层走规则兜底。
    assert stripper.emotion == ""
    assert stripper.raw_emotion == "angry"


def test_stripper_releases_an_over_long_pseudo_tag_as_plain_text():
    stripper = EmotionTagStripper()
    out = stripper.feed("<emo:" + "x" * 40 + ">后面的话")
    assert "后面的话" in out
    assert "<emo:" not in out
    assert stripper.emotion == ""


def test_stripper_releases_a_buffered_pseudo_tag_once_it_exceeds_the_limit():
    stripper = EmotionTagStripper()
    assert stripper.feed("<em") == ""
    out = stripper.feed("o:" + "啊" * 30)
    # 超过 EMOTION_TAG_MAX_CHARS 还没闭合：整段当普通文本放行，一个字不丢。
    assert out.startswith("<emo:")
    assert stripper.emotion == ""
    assert stripper.started is True


def test_stripper_deletes_a_tag_repeated_in_the_middle_of_the_reply():
    stripper = EmotionTagStripper()
    out = stripper.feed("<emo:happy>你好<emo:happy>再见")
    assert out == "你好再见"
    # 中间的重复标签不影响已确定的情绪。
    assert stripper.emotion == "happy"


def test_stripper_holds_a_partial_tag_tail_across_chunks():
    stripper = EmotionTagStripper()
    assert stripper.feed("好的，我记下了<em") == "好的，我记下了"
    assert stripper.feed("o:忘了删>后面") == "后面"
    assert stripper.emotion == ""


def test_stripper_keeps_plain_angle_brackets_in_the_body():
    stripper = EmotionTagStripper()
    assert stripper.feed("1 < 2 这件事") == "1 < 2 这件事"
    assert stripper.emotion == ""


def test_stripper_never_leaks_a_tag_when_the_stream_ends_mid_tag():
    stripper = EmotionTagStripper()
    assert stripper.feed("<emo:sa") == ""
    assert stripper.finish() == ""
    assert stripper.emotion == ""


def test_stripper_returns_nothing_when_the_reply_is_only_a_tag():
    stripper = EmotionTagStripper()
    assert stripper.feed("<emo:pouty>") == ""
    assert stripper.emotion == "pouty"
    assert stripper.finish() == ""


def test_strip_mid_emotion_tags_only_touches_emo_tags():
    assert strip_mid_emotion_tags("a<emo:sad>b") == "ab"
    assert strip_mid_emotion_tags("a<b>c") == "a<b>c"
    assert strip_mid_emotion_tags("< EMO : happy >") == ""


def test_strip_emotion_tag_handles_the_non_streaming_path():
    assert strip_emotion_tag("<emo:excited>我升职啦！") == ("我升职啦！", "excited")
    assert strip_emotion_tag("没有标签") == ("没有标签", "")
    assert strip_emotion_tag("<emo:nope>没有标签") == ("没有标签", "")
    assert strip_emotion_tag("") == ("", "")


# ── E2.3 规则兜底 ───────────────────────────────────────────
def test_classify_praise_is_shy():
    assert classify("你今天真好看") == "shy"
    assert classify("你怎么这么可爱") == "shy"


def test_classify_bad_news_is_sad():
    assert classify("我的猫走丢了") == "sad"
    assert classify("我跟她分手了") == "sad"


def test_classify_worry_is_worried():
    assert classify("我明天要做手术") == "worried"
    assert classify("下周有面试，好紧张") == "worried"


def test_classify_being_scolded_is_pouty():
    assert classify("你好笨啊") == "pouty"
    assert classify("闭嘴吧你") == "pouty"


def test_classify_unexpected_good_news_is_surprised():
    assert classify("我中彩票了") == "surprised"
    assert classify("没想到居然是真的吗") == "surprised"


def test_classify_good_news_is_happy_or_excited():
    assert classify("我升职了") == "happy"
    assert classify("项目成功了") == "happy"
    # 带感叹或"太/超级"就再往上提一档。
    assert classify("我升职了！") == "excited"
    assert classify("我太开心了") == "excited"


def test_classify_falls_back_to_neutral():
    assert classify("今天几号") == "neutral"
    assert classify("帮我定个闹钟") == "neutral"
    assert classify("", "") == "neutral"


def test_classify_prefers_the_user_text_over_the_reply_text():
    # 用户被夸 → 害羞，哪怕 Aura 自己在说难过的事。
    assert classify("你真好看", "我有点难过") == "shy"
    assert classify("", "我有点难过") == "sad"


# ── E2.4 状态调制 ───────────────────────────────────────────
def test_modulate_downgrades_excited_when_energy_is_low():
    assert modulate("excited", {"energy": 10}) == ("happy", "体力 10 不到 25，兴奋收成高兴")


def test_modulate_downgrades_happy_when_mood_is_low():
    assert modulate("happy", {"mood": 10}) == ("neutral", "心情 10 不到 30，高兴改成平静")


def test_modulate_turns_surprise_into_worry_when_stress_is_high():
    assert modulate("surprised", {"stress": 90}) == ("worried", "压力 90 超过 75，惊讶改成担心")


def test_modulate_leaves_emotion_alone_when_state_is_fine():
    assert modulate("happy", {"energy": 80, "mood": 80, "stress": 10}) == ("happy", "")
    assert modulate("excited", {"energy": 90, "mood": 90, "stress": 10}) == ("excited", "")
    assert modulate("sad", {"energy": 10, "mood": 10, "stress": 90}) == ("sad", "")


def test_modulate_ignores_missing_or_broken_state_fields():
    assert modulate("excited", {}) == ("excited", "")
    assert modulate("happy", None) == ("happy", "")
    assert modulate("excited", {"energy": "没电"}) == ("excited", "")
    assert modulate("", {"energy": 1}) == ("", "")


# ── E2.5 情绪惯性 ───────────────────────────────────────────
def test_inertia_blocks_a_jump_to_happy_right_after_sadness():
    previous = {"emotion": "sad", "intensity": 4}
    assert apply_inertia("happy", previous, user_text="今天天气不错") == "neutral"
    assert apply_inertia("excited", previous, user_text="随便聊聊") == "neutral"


def test_inertia_lets_real_content_through():
    previous = {"emotion": "worried", "intensity": 3}
    # 用户本轮说了实质内容，情绪跟着内容走，不被上一轮按住。
    assert apply_inertia("happy", previous, user_text="我升职了") == "happy"


def test_inertia_only_applies_after_negative_emotions():
    assert apply_inertia("happy", {"emotion": "happy"}, user_text="今天天气不错") == "happy"
    assert apply_inertia("happy", {"emotion": "neutral"}, user_text="今天天气不错") == "happy"
    assert apply_inertia("happy", {}, user_text="今天天气不错") == "happy"


def test_inertia_does_not_touch_negative_emotions():
    previous = {"emotion": "sad"}
    assert apply_inertia("sad", previous, user_text="今天天气不错") == "sad"
    assert apply_inertia("worried", previous, user_text="今天天气不错") == "worried"


def test_next_intensity_halves_each_round_and_stays_in_range():
    assert next_intensity({"intensity": 5}, "happy") == 2
    assert next_intensity({"intensity": 4}, "sad") == 2
    assert next_intensity({"intensity": 1}, "happy") == 1
    assert next_intensity({}, "happy") == 3
    # 同一种情绪延续时往上回一档，不至于一轮就掉到底。
    assert next_intensity({"emotion": "sad", "intensity": 4}, "sad") == 3
    for intensity in range(0, 9):
        for emotion in EMOTIONS:
            assert 1 <= next_intensity({"intensity": intensity}, emotion) <= 5


# ── 情绪状态存取 ────────────────────────────────────────────
def test_emotion_state_round_trips_through_metadata():
    state: dict = {}
    write_emotion_state(state, emotion="warm", cause="用户：你真好看", intensity=4, at=123.0)
    stored = read_emotion_state(state)
    assert stored["emotion"] == "happy"
    assert stored["label"] == "高兴"
    assert stored["intensity"] == 4
    assert stored["cause"] == "用户：你真好看"
    assert stored["at"] == 123.0
    assert emotion_prompt_line(state) == "你刚才的情绪：高兴（因为用户：你真好看）"


def test_emotion_prompt_line_is_empty_without_previous_emotion():
    assert emotion_prompt_line({}) == ""
    assert emotion_prompt_line({"metadata": {"emotion_state": {"emotion": "生气"}}}) == ""
    assert emotion_prompt_line(None) == ""


def test_read_emotion_state_tolerates_broken_state():
    assert read_emotion_state(None) == {}
    assert read_emotion_state({"metadata": None}) == {}
    assert read_emotion_state({"metadata": {"emotion_state": "难过"}}) == {}


def test_write_emotion_state_clamps_intensity():
    state: dict = {}
    write_emotion_state(state, emotion="sad", intensity=99)
    assert read_emotion_state(state)["intensity"] == 5
    write_emotion_state(state, emotion="sad", intensity=-4)
    assert read_emotion_state(state)["intensity"] == 1


# ── E3 cause 文案 ───────────────────────────────────────────
def test_ledger_cause_uses_the_chinese_label_and_clips_the_user_text():
    assert emotion_ledger_cause("shy", "你今天真好看") == "情绪：害羞（用户：你今天真好看）"
    long_text = "我今天真的特别难过因为我的小猫好像走丢了你能安慰我一下吗"
    cause = emotion_ledger_cause("sad", long_text)
    assert cause.startswith("情绪：难过（用户：")
    assert cause.endswith("）")
    # 用户原话截断 20 字，加一个省略号。
    quoted = cause[len("情绪：难过（用户："):-1]
    assert quoted.endswith("…")
    assert len(quoted) <= 21


def test_ledger_cause_works_without_user_text():
    assert emotion_ledger_cause("happy", "") == "情绪：高兴"
    assert emotion_ledger_cause("neutral", "") == "情绪：平静"


# ── E2.6 合成入口 ───────────────────────────────────────────
def test_resolve_prefers_a_valid_llm_label():
    facts = resolve_turn_emotion(llm_emotion="shy", user_text="今天几号", reply_text="", state={})
    assert facts["emotion"] == "shy"
    assert facts["source"] == "llm"
    assert facts["raw"] == "shy"
    assert facts["modulated_from"] == "shy"
    assert facts["intensity"] >= 1


def test_resolve_falls_back_to_rules_when_the_label_is_missing():
    facts = resolve_turn_emotion(llm_emotion="", user_text="我的猫走丢了", reply_text="", state={})
    assert facts["emotion"] == "sad"
    assert facts["source"] == "rule"
    assert facts["raw"] == ""
    assert facts["reason"] == "规则兜底：LLM 没给标签"


def test_resolve_falls_back_to_rules_when_the_label_is_invalid():
    facts = resolve_turn_emotion(llm_emotion="angry", user_text="我的猫走丢了", reply_text="", state={})
    assert facts["emotion"] == "sad"
    assert facts["source"] == "rule"
    assert facts["raw"] == "angry"
    assert "angry" in facts["reason"]


def test_resolve_normalizes_a_legacy_llm_label():
    facts = resolve_turn_emotion(llm_emotion="curious", user_text="今天几号", reply_text="", state={})
    assert facts["emotion"] == "surprised"
    assert facts["source"] == "llm"


def test_resolve_applies_state_modulation_and_records_where_it_came_from():
    facts = resolve_turn_emotion(
        llm_emotion="excited",
        user_text="我升职了",
        reply_text="",
        state={"energy": 5, "mood": 90, "stress": 0},
    )
    assert facts["emotion"] == "happy"
    assert facts["modulated_from"] == "excited"
    assert "体力" in facts["reason"]


def test_resolve_applies_inertia_and_records_it():
    state: dict = {}
    write_emotion_state(state, emotion="sad", cause="用户：我的猫走丢了", intensity=4, at=1.0)
    facts = resolve_turn_emotion(
        llm_emotion="",
        user_text="今天天气不错",
        reply_text="挺开心的",
        state=state,
    )
    assert facts["emotion"] == "neutral"
    assert facts["inertia_from"] == "sad"
    assert "还没过去" in facts["reason"]


def test_resolve_cause_carries_the_clipped_user_text():
    facts = resolve_turn_emotion(llm_emotion="happy", user_text="  升职啦  ", reply_text="", state={})
    assert facts["cause"] == "用户：升职啦"
