from __future__ import annotations

import re
import time
from typing import Any


# ── E1 情绪集合（与协议/固件对齐）─────────────────────────────
# 只有这 8 个值允许出现在 dialogue.payload.emotion、TTS 请求和提示词里。
EMOTIONS = (
    "neutral",
    "happy",
    "excited",
    "shy",
    "sad",
    "worried",
    "pouty",
    "surprised",
)

EMOTION_LABELS = {
    "neutral": "平静",
    "happy": "高兴",
    "excited": "兴奋",
    "shy": "害羞",
    "sad": "难过",
    "worried": "担心",
    "pouty": "委屈",
    "surprised": "惊讶",
}

# TTS 语气提示（StepFun 行内语气用；X-Ray 大屏也拿它展示"她会怎么说"）。
# neutral 不加提示，按默认声线说。
EMOTION_VOICE_TONES = {
    "happy": "开心，语气轻快",
    "excited": "兴奋，语速稍快，充满活力",
    "shy": "害羞，声音轻柔，有点腼腆",
    "sad": "难过，语气低落，语速放慢",
    "worried": "担心，语气关切，略带不安",
    "pouty": "委屈，带点小抱怨",
    "surprised": "惊讶，语调上扬",
}

# 旧值兼容：voice_turn 的规则回复和历史 metadata 里还留着这些词。
# 映射不到 E1 的一律算无效（normalize_emotion 返回 ""）。
LEGACY_EMOTION_ALIASES = {
    "warm": "happy",
    "firm": "neutral",
    "proud": "neutral",
    "calm": "neutral",
    "focused": "neutral",
    "attentive": "neutral",
    "curious": "surprised",
}

# E3：每种情绪对应的心情增量。0 表示不进账本（记账只会污染时间线）。
EMOTION_MOOD_DELTA = {
    "neutral": 0,
    "happy": 2,
    "excited": 3,
    "shy": 1,
    "sad": -3,
    "worried": -2,
    "pouty": -2,
    "surprised": 0,
}

# E2.5 情绪惯性：上一轮压在这些情绪里时，本轮不许直接跳到高兴/兴奋。
INERTIA_EMOTIONS = ("sad", "worried", "pouty")
INERTIA_BLOCKED_EMOTIONS = ("happy", "excited")

# E2.4 状态调制的阈值（DESIGN.md 第 3 节）。
ENERGY_EXCITED_FLOOR = 25
MOOD_HAPPY_FLOOR = 30
STRESS_SURPRISE_CEILING = 75

# E2.5：情绪强度 1–5，每轮衰减一半。
MIN_INTENSITY = 1
MAX_INTENSITY = 5
DEFAULT_INTENSITY = 3

# E2.1：写在 persona 提示词里的自评指令。
EMOTION_TAG_INSTRUCTION = (
    "## 情绪自评\n"
    "回复的第一行必须先输出 <emo:X>，X 只能取 "
    + "/".join(EMOTIONS)
    + " 中的一个，然后换行，之后才说你要讲的话。"
    "这个标签是给系统读的，不要念出来，也不要解释它。\n"
    "如果用户这句话是在收尾、想结束这次对话（比如回头再聊、先这样、我去忙了、拜拜、晚安、不聊了），"
    "标签写成 <emo:X|end>，正常道个别就好；用户还在聊、只是顺口一句时不要加 |end。"
)

# 上一轮情绪进提示词的那一行（E3）。
EMOTION_STATE_KEY = "emotion_state"
EMOTION_HISTORY_KEY = "emotion_history"
EMOTION_HISTORY_LIMIT = 48


def normalize_emotion(value: Any) -> str:
    """任意情绪词 → E1 值；旧值按 ``LEGACY_EMOTION_ALIASES`` 映射，其余返回 ""。"""
    raw = str(value if value is not None else "").strip().lower()
    if not raw:
        return ""
    if raw in EMOTIONS:
        return raw
    return LEGACY_EMOTION_ALIASES.get(raw, "")


def emotion_label(emotion: Any) -> str:
    """E1 值的中文标签；无效值返回空串。"""
    return EMOTION_LABELS.get(normalize_emotion(emotion), "")


def emotion_mood_delta(emotion: Any) -> int:
    """E3：这种情绪对应的心情增量（0 表示不记账）。"""
    return EMOTION_MOOD_DELTA.get(normalize_emotion(emotion), 0)


def clip_text(text: Any, limit: int) -> str:
    value = str(text if text is not None else "").strip()
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def emotion_trigger_cause(user_text: Any) -> str:
    """触发这轮情绪的用户原话摘要（截断 20 字），空输入返回空串。"""
    body = clip_text(user_text, 20)
    return f"用户：{body}" if body else ""


def emotion_ledger_cause(emotion: Any, user_text: Any = "") -> str:
    """E3 账本 cause，例如 ``情绪：害羞（用户：你今天真好看）``。"""
    label = emotion_label(emotion) or "平静"
    trigger = emotion_trigger_cause(user_text)
    return f"情绪：{label}（{trigger}）" if trigger else f"情绪：{label}"


# ── E2.2 <emo:X> 标签剥离 ────────────────────────────────────
EMOTION_TAG_PREFIX = "<emo:"
# 开头像标签时最多缓冲多少个字符；超出仍未闭合就当普通文本放行，
# 免得一个畸形的 "<" 把整段回复都憋在缓冲区里。
EMOTION_TAG_MAX_CHARS = 24

_TAG_BODY_RE = re.compile(r"^emo\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\|\s*end)?$", re.IGNORECASE)
# 模型偶尔把冒号写成 ">"：``<emo>happy>``。照样认作标签，否则 "happy>" 会进字幕被念出来。
_MALFORMED_TAG_RE = re.compile(r"^<\s*emo\s*>\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\|\s*end)?\s*>", re.IGNORECASE)
_MALFORMED_TAG_PARTIAL_RE = re.compile(r"^<\s*emo\s*>\s*[A-Za-z0-9_]*\s*(?:\|\s*[A-Za-z]*\s*)?$", re.IGNORECASE)
# 回复中间重复出现的标签（模型偶尔会写两遍）直接删掉。
_MID_TAG_RE = re.compile(
    r"<\s*emo\s*:[^<>\r\n]{0,40}>|<\s*emo\s*>\s*[A-Za-z_][A-Za-z0-9_]{0,20}\s*(?:\|\s*end\s*)?>",
    re.IGNORECASE,
)


def _tag_value(body: str) -> str | None:
    """``emo:sad`` / ``emo:sad|end`` → ``sad``；不是情绪标签 body 时返回 None。"""
    match = _TAG_BODY_RE.match(str(body or "").strip())
    if not match:
        return None
    return match.group(1).strip().lower()


def _tag_ends_dialogue(body: str) -> bool:
    match = _TAG_BODY_RE.match(str(body or "").strip())
    return bool(match and match.group(2))


def _looks_like_tag_start(text: str) -> bool:
    lowered = str(text or "").lower()
    return lowered.startswith(EMOTION_TAG_PREFIX) or EMOTION_TAG_PREFIX.startswith(lowered)


def _partial_tag_tail(text: str) -> str:
    """尾巴上还没闭合的 ``<emo:`` 残片（没有就返回空串）。"""
    index = str(text or "").rfind("<")
    if index < 0:
        return ""
    tail = text[index:]
    if ">" in tail or len(tail) > EMOTION_TAG_MAX_CHARS:
        return ""
    return tail if _looks_like_tag_start(tail) else ""


def strip_mid_emotion_tags(text: Any) -> str:
    """删掉回复中间出现的 ``<emo:...>``（不影响已确定的情绪）。"""
    return _MID_TAG_RE.sub("", str(text if text is not None else ""))


class EmotionTagStripper:
    """把回复第一行的 ``<emo:X>`` 标签从流式文本里剥掉。

    约束（TASK_E_server.md E2.2）：
      * 只在回复开头识别标签，允许前导空白；
      * 标签可能被拆到多个分片（``<em`` + ``o:sh`` + ``y>你好``）；
      * 开头不是 ``<`` 时立刻放行，不增加首字延迟；
      * 开头像标签时最多缓冲 ``EMOTION_TAG_MAX_CHARS`` 个字符，超出仍未闭合
        就当普通文本放行；
      * 非法值：标签照样剥掉，``emotion`` 记为无效（空串）；
      * 回复中间的 ``<emo:...>`` 也删掉，但不影响已确定的 ``emotion``。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._tail = ""
        self._started = False
        self._emotion = ""
        self._raw = ""
        self._end_dialogue = False

    @property
    def end_dialogue(self) -> bool:
        """模型在标签里写了 ``|end``：用户想结束这次对话，这轮说完不再续听。"""
        return self._end_dialogue

    @property
    def emotion(self) -> str:
        """已确定的 E1 情绪；没标签或标签非法时是空串。"""
        return self._emotion

    @property
    def raw_emotion(self) -> str:
        """标签里的原始值（可能是非法值），用于 debug。"""
        return self._raw

    @property
    def started(self) -> bool:
        """开头那段（可能是标签）是否已经判定完。"""
        return self._started

    def feed(self, chunk: str) -> str:
        """喂一个分片，返回可以安全输出的文本（可能为空串）。"""
        text = str(chunk if chunk is not None else "")
        if not text:
            return ""
        if self._started:
            return self._feed_body(text)
        return self._feed_start(text)

    def finish(self) -> str:
        """流结束，把还憋在缓冲区里的东西放行/丢掉。"""
        buffer, self._buffer = self._buffer, ""
        tail, self._tail = self._tail, ""
        if not self._started:
            self._started = True
            probe = buffer.lstrip()
            if not probe:
                return ""
            if _looks_like_tag_start(probe) or _MALFORMED_TAG_PARTIAL_RE.match(probe):
                # 没闭合的情绪标签：宁可丢字也不能把 "<emo:" 漏进字幕/TTS。
                return ""
            return strip_mid_emotion_tags(probe)
        if not tail:
            return ""
        if _looks_like_tag_start(tail):
            return ""
        return strip_mid_emotion_tags(tail)

    # ── 内部 ────────────────────────────────────────────────
    def _feed_start(self, text: str) -> str:
        self._buffer += text
        probe = self._buffer.lstrip()
        if not probe:
            # 全是前导空白，继续等第一个有效字符。
            return ""
        lowered = probe.lower()
        malformed = _MALFORMED_TAG_RE.match(probe)
        if malformed:
            value = malformed.group(1).strip().lower()
            self._started = True
            self._buffer = ""
            self._end_dialogue = bool(malformed.group(2))
            self._raw = value
            self._emotion = value if value in EMOTIONS else ""
            return self._feed_body(probe[malformed.end() :])
        if _MALFORMED_TAG_PARTIAL_RE.match(probe) and len(self._buffer) <= EMOTION_TAG_MAX_CHARS:
            # "<emo>hap" 还没写完第二个 ">"，继续等。
            return ""
        close = probe.find(">")
        if probe[0] == "<" and close >= 0:
            body = probe[1:close]
            value = _tag_value(body)
            self._started = True
            self._buffer = ""
            if value is None:
                # "<b>你好" 这类不是情绪标签，整段按普通文本放行。
                return self._feed_body(probe)
            self._raw = value
            self._emotion = value if value in EMOTIONS else ""
            self._end_dialogue = _tag_ends_dialogue(body)
            return self._feed_body(probe[close + 1 :])
        if probe[0] != "<" and close < 0:
            # 第一行不是标签，也不会再变成标签：立刻放行（前导空白一起丢掉）。
            self._started = True
            self._buffer = ""
            return self._feed_body(probe)
        if probe[0] == "<" and _looks_like_tag_start(lowered) and len(self._buffer) <= EMOTION_TAG_MAX_CHARS:
            # 还可能是标签（"<em" / "<emo:sh"），继续等闭合。
            return ""
        # 不是标签，或者缓冲超限：当普通文本放行。
        self._started = True
        self._buffer = ""
        return self._feed_body(probe)

    def _feed_body(self, text: str) -> str:
        value = self._tail + str(text or "")
        self._tail = ""
        if not value:
            return ""
        cleaned = strip_mid_emotion_tags(value)
        held = _partial_tag_tail(cleaned)
        if held:
            cleaned = cleaned[: len(cleaned) - len(held)]
            self._tail = held
        return cleaned


def strip_emotion_tag(text: Any) -> tuple[str, str]:
    """非流式路径：一次剥掉 ``<emo:X>``，返回 ``(干净文本, E1 情绪)``。"""
    stripper = EmotionTagStripper()
    clean = stripper.feed(text) + stripper.finish()
    return clean, stripper.emotion


# ── E2.3 规则兜底 ───────────────────────────────────────────
# 被夸 → 害羞
_PRAISE_KEYWORDS = ("好看", "可爱", "喜欢你", "真棒", "漂亮", "帅", "厉害", "么么", "亲亲")
# 坏消息、失去、告别 → 难过
_SAD_KEYWORDS = ("走丢", "去世", "离世", "分手", "失恋", "难过", "伤心", "哭了", "哭", "搬走", "搬家", "告别", "永别", "丢了", "离开")
# 担忧 → 担心
_WORRIED_KEYWORDS = ("手术", "生病", "感冒", "考试", "面试", "医院", "看病", "体检", "害怕", "担心", "紧张", "不安", "焦虑")
# 用户凶/骂 → 委屈
_POUTY_KEYWORDS = ("笨", "讨厌你", "闭嘴", "滚", "烦你", "傻", "蠢", "走开", "嫌弃", "烦死")
# 意外好事 → 惊讶
_SURPRISED_KEYWORDS = ("中奖", "彩票", "居然", "竟然", "真的吗", "真假", "没想到", "想不到", "天哪", "天啊")
# 好消息 → 高兴（带感叹或"太/超级"→ 兴奋）
_HAPPY_KEYWORDS = ("升职", "加薪", "涨薪", "录用", "录取", "通过", "成功", "考上", "赢", "好消息", "开心", "高兴", "太好了", "喜事", "庆功")
_EXCITED_HINTS = ("！", "!", "太", "超级", "超")


def _hit(value: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword and keyword in value for keyword in keywords)


def _is_excited(value: str) -> bool:
    return _hit(value, _EXCITED_HINTS)


def classify(user_text: Any, reply_text: Any = "") -> str:
    """E2.3 规则兜底：用户文本优先于回复文本，都不命中返回 ``neutral``。"""
    for text in (user_text, reply_text):
        emotion = _classify_one(text)
        if emotion:
            return emotion
    return "neutral"


def _classify_one(text: Any) -> str:
    value = str(text if text is not None else "").strip().lower()
    if not value:
        return ""
    if _hit(value, _PRAISE_KEYWORDS):
        return "shy"
    if _hit(value, _SAD_KEYWORDS):
        return "sad"
    if _hit(value, _WORRIED_KEYWORDS):
        return "worried"
    if _hit(value, _POUTY_KEYWORDS):
        return "pouty"
    if _hit(value, _SURPRISED_KEYWORDS):
        return "surprised"
    if _hit(value, _HAPPY_KEYWORDS):
        return "excited" if _is_excited(value) else "happy"
    return ""


# ── E2.4 状态调制 ───────────────────────────────────────────
def _state_number(state: Any, key: str) -> int | None:
    if not isinstance(state, dict) or key not in state:
        return None
    try:
        return int(round(float(state.get(key))))
    except (TypeError, ValueError):
        return None


def modulate(emotion: Any, state: Any) -> tuple[str, str]:
    """E2.4 状态调制，返回 ``(情绪, 原因)``；没被调制时原因是空串。"""
    value = str(emotion if emotion is not None else "").strip().lower()
    if not value:
        return "", ""
    energy = _state_number(state, "energy")
    mood = _state_number(state, "mood")
    stress = _state_number(state, "stress")
    if value == "excited" and energy is not None and energy < ENERGY_EXCITED_FLOOR:
        return "happy", f"体力 {energy} 不到 {ENERGY_EXCITED_FLOOR}，兴奋收成高兴"
    if value == "happy" and mood is not None and mood < MOOD_HAPPY_FLOOR:
        return "neutral", f"心情 {mood} 不到 {MOOD_HAPPY_FLOOR}，高兴改成平静"
    if value == "surprised" and stress is not None and stress > STRESS_SURPRISE_CEILING:
        return "worried", f"压力 {stress} 超过 {STRESS_SURPRISE_CEILING}，惊讶改成担心"
    return value, ""


# ── E2.5 情绪惯性 ───────────────────────────────────────────
def apply_inertia(emotion: Any, previous: Any, *, user_text: Any = "") -> str:
    """E2.5：上一轮还在难过/担心/委屈时，本轮不许因为一句普通话直接跳到高兴。"""
    value = str(emotion if emotion is not None else "").strip().lower()
    if not value:
        return ""
    stored = previous if isinstance(previous, dict) else {}
    previous_emotion = normalize_emotion(stored.get("emotion"))
    if value not in INERTIA_BLOCKED_EMOTIONS:
        return value
    if previous_emotion not in INERTIA_EMOTIONS:
        return value
    if classify(user_text) != "neutral":
        # 用户本轮说了实质内容（被夸、坏消息……），情绪跟着内容走。
        return value
    return "neutral"


def next_intensity(previous: Any, emotion: Any) -> int:
    """本轮情绪强度：上一轮的强度先衰减一半（取整），同情绪时不衰减到底。"""
    stored = previous if isinstance(previous, dict) else {}
    previous_emotion = normalize_emotion(stored.get("emotion"))
    try:
        previous_intensity = int(round(float(stored.get("intensity"))))
    except (TypeError, ValueError):
        previous_intensity = 0
    previous_intensity = max(0, min(MAX_INTENSITY, previous_intensity))
    if previous_intensity <= 0:
        base = DEFAULT_INTENSITY
    else:
        base = max(MIN_INTENSITY, min(MAX_INTENSITY, round(previous_intensity * 0.5)))
    if emotion and normalize_emotion(emotion) == previous_emotion:
        return min(MAX_INTENSITY, base + 1)
    return base


def read_emotion_state(state: Any) -> dict[str, Any]:
    """读 ``state["metadata"]["emotion_state"]``，没有就返回空 dict。"""
    if not isinstance(state, dict):
        return {}
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    stored = metadata.get(EMOTION_STATE_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def write_emotion_state(
    state: Any,
    *,
    emotion: Any,
    cause: Any = "",
    intensity: Any = 0,
    at: Any = None,
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把本轮情绪写进 ``state["metadata"]["emotion_state"]``（原地改，返回同一个 dict）。

    ``resolution`` 是 X1 大屏要的"这一轮情绪怎么定下来的"：LLM 自评还是规则兜底、
    被状态调制前后是什么、有没有被上一轮情绪惯性拉住。带上它，投影上就能把
    "happy 怎么变成 neutral"讲清楚，不用再猜。
    """
    if not isinstance(state, dict):
        return {}
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        state["metadata"] = metadata
    value = normalize_emotion(emotion)
    try:
        strength = int(round(float(intensity)))
    except (TypeError, ValueError):
        strength = DEFAULT_INTENSITY
    payload = {
        "emotion": value,
        "label": emotion_label(value),
        "intensity": max(MIN_INTENSITY, min(MAX_INTENSITY, strength or DEFAULT_INTENSITY)),
        "cause": str(cause or ""),
        "at": float(at if at is not None else time.time()),
    }
    if isinstance(resolution, dict) and resolution:
        payload["last_resolution"] = {
            "source": str(resolution.get("source") or ""),
            "raw": str(resolution.get("raw") or ""),
            "modulated_from": str(resolution.get("modulated_from") or ""),
            "inertia_from": str(resolution.get("inertia_from") or ""),
            "reason": str(resolution.get("reason") or ""),
        }
    metadata[EMOTION_STATE_KEY] = payload
    # 大屏点账本回看某一轮的思考过程：留最近若干轮，旧的滚掉。
    history = metadata.get(EMOTION_HISTORY_KEY)
    history = [item for item in history if isinstance(item, dict)] if isinstance(history, list) else []
    history.append(dict(payload))
    metadata[EMOTION_HISTORY_KEY] = history[-EMOTION_HISTORY_LIMIT:]
    return state


def emotion_prompt_line(state: Any) -> str:
    """E3：给下一轮提示词用的那一行，例如 ``你刚才的情绪：难过（因为用户：我的猫走丢了）``。"""
    stored = read_emotion_state(state)
    emotion = normalize_emotion(stored.get("emotion"))
    if not emotion:
        return ""
    label = str(stored.get("label") or "").strip() or emotion_label(emotion)
    cause = str(stored.get("cause") or "").strip()
    return f"你刚才的情绪：{label}（因为{cause}）" if cause else f"你刚才的情绪：{label}"


# ── E2.6 合成入口 ───────────────────────────────────────────
def resolve_turn_emotion(
    *,
    llm_emotion: Any = "",
    user_text: Any = "",
    reply_text: Any = "",
    state: Any = None,
) -> dict[str, Any]:
    """E2.6：LLM 自评优先，非法/缺失走规则，再叠加状态调制和情绪惯性。"""
    raw = str(llm_emotion if llm_emotion is not None else "").strip().lower()
    llm_value = normalize_emotion(raw)
    if llm_value:
        emotion = llm_value
        source = "llm"
        reason = "LLM 自评"
    else:
        emotion = classify(user_text, reply_text)
        source = "rule"
        reason = f"规则兜底：LLM 标签无效（{raw or '缺失'}）" if raw else "规则兜底：LLM 没给标签"
    modulated, modulated_reason = modulate(emotion, state)
    if modulated_reason:
        reason = f"{reason}；{modulated_reason}"
    previous = read_emotion_state(state)
    final = apply_inertia(modulated, previous, user_text=user_text)
    if final != modulated:
        previous_label = emotion_label(previous.get("emotion")) or "上一轮情绪"
        reason = f"{reason}；{previous_label}还没过去，先不跳太高"
    return {
        "emotion": final,
        "source": source,
        "raw": raw,
        "modulated_from": emotion,
        "inertia_from": normalize_emotion(previous.get("emotion")),
        "intensity": next_intensity(previous, final),
        "cause": emotion_trigger_cause(user_text),
        "reason": reason,
    }
