from __future__ import annotations

import re
from dataclasses import dataclass


FALLBACK_SPOKEN_REPLY = "我在。"
SPOKEN_REPLY_INSTRUCTION = (
    "这不是小说、剧本或角色扮演旁白任务；你要生成的是 Aura 准备直接说出口的话。"
    "回复会直接显示在设备屏幕并用于 TTS 朗读；只输出口语回复本身。"
    "不要写括号动作、心理旁白、舞台说明、角色名冒号、表情标签或“她/我做了什么”的叙述。"
    "情绪只能体现在措辞和语气里，不能用动作描写补充。"
)
DIRECT_LLM_SYSTEM_PROMPT = (
    "你是 Aura/Lily 的对话生成层。你必须严格遵守输出协议："
    + SPOKEN_REPLY_INSTRUCTION
)

STAGE_DIRECTION_HINTS = (
    "看到",
    "看见",
    "消息",
    "手机",
    "语音",
    "回复",
    "回了",
    "笑",
    "眨",
    "叹",
    "点头",
    "摇头",
    "抬",
    "低",
    "靠",
    "凑",
    "抱",
    "摸",
    "挥",
    "停",
    "沉默",
    "语气",
    "表情",
    "神情",
    "动作",
    "旁白",
    "心理",
    "心里",
    "开心",
    "高兴",
    "难过",
    "不高兴",
    "生气",
    "委屈",
    "害羞",
    "撒娇",
    "惊讶",
    "犹豫",
    "哭",
    "哄",
    "轻声",
    "小声",
    "认真",
    "温柔",
    "呼吸",
    "停顿",
    "语速",
    "语调",
)

SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:Aura|Lily|Doudou|豆豆|AI|助手)\s*[:：]\s*",
    flags=re.IGNORECASE,
)

# TTS 会把 emoji、markdown 记号和单位符号硬读出来（比如 °C 被念成怪音），
# 所以送去朗读前必须转成口语写法或直接删掉。
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # 表情、符号、天气图标等
    "\U00002600-\U000027BF"  # 杂项符号（☀☔✔ 等）
    "\U0001F1E6-\U0001F1FF"  # 区域旗帜
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"  # 变体选择符
    "\U0000200D"             # ZWJ
    "\U00002190-\U000021FF"  # 箭头（→ 先单独转成“到”再兜底删）
    "\U00002700-\U000027BF"
    "]+"
)
_NUMBER_COMMA_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_DOLLAR_RE = re.compile(r"[$＄]\s*(\d+(?:\.\d+)?)")
_RANGE_RE = re.compile(r"(?<=\d)\s*[~～\-—–]\s*(?=\d)")
_DEGREE_C_RE = re.compile(r"°\s*[CcＣ]|℃")
_DEGREE_F_RE = re.compile(r"°\s*[FfＦ]|℉")
_MARKDOWN_EMPHASIS_RE = re.compile(r"\*{1,3}([^*\n]+?)\*{1,3}")
_MARKDOWN_HEADING_RE = re.compile(r"(?:^|(?<=\n))\s*#{1,6}\s+")
_MARKDOWN_BULLET_RE = re.compile(r"(?:^|(?<=\n))\s*(?:[-*•]|\d+[.)])\s+")


def _normalize_symbols_for_speech(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    # markdown 记号先拆掉，免得 **32°C** 这类嵌套漏网。
    value = _MARKDOWN_EMPHASIS_RE.sub(r"\1", value)
    value = _MARKDOWN_HEADING_RE.sub("", value)
    value = _MARKDOWN_BULLET_RE.sub("", value)
    value = value.replace("`", "")
    # 数字里的千分位逗号会被断句，去掉。
    value = _NUMBER_COMMA_RE.sub("", value)
    # 单位与符号转口语。ISO 日期要先转，不然会被区间规则读成“2026到07到06”。
    value = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", r"\1年\2月\3日", value)
    value = _RANGE_RE.sub("到", value)
    value = _DEGREE_C_RE.sub("度", value)
    value = _DEGREE_F_RE.sub("华氏度", value)
    value = re.sub(r"(?<=\d)\s*km/h", "公里每小时", value)
    value = re.sub(r"(?<=\d)\s*m/s", "米每秒", value)
    value = re.sub(r"(?<=\d)\s*(?:km(?![a-zA-Z/]))", "公里", value)
    value = _PERCENT_RE.sub(lambda m: "百分之" + m.group(1), value)
    value = _DOLLAR_RE.sub(lambda m: m.group(1) + "美元", value)
    value = value.replace("≈", "大概").replace("→", "到")
    # 剩下的 emoji / 图标一律删除。
    value = _EMOJI_RE.sub(" ", value)
    return value


@dataclass(frozen=True)
class SpokenReply:
    text: str
    changed: bool
    fallback_used: bool
    raw_chars: int

    def to_debug(self, *, raw_response: str = "") -> dict[str, object]:
        debug: dict[str, object] = {
            "contract": "spoken_text_only",
            "changed": self.changed,
            "fallback_used": self.fallback_used,
            "raw_chars": self.raw_chars,
            "spoken_chars": len(self.text),
        }
        if self.changed and raw_response:
            debug["raw_response_preview"] = _clip(raw_response, 500)
        return debug


def normalize_spoken_reply(text: str) -> SpokenReply:
    raw = str(text or "")
    clean = _strip_stage_directions(raw)
    clean = SPEAKER_PREFIX_RE.sub("", clean)
    clean = _normalize_symbols_for_speech(clean)
    clean = re.sub(r"[ \t\r\n]+", " ", clean).strip()
    clean = clean.strip(" -—:：")
    fallback_used = not bool(clean)
    spoken = clean or FALLBACK_SPOKEN_REPLY
    return SpokenReply(
        text=spoken,
        changed=spoken != raw.strip(),
        fallback_used=fallback_used,
        raw_chars=len(raw),
    )


def _strip_stage_directions(text: str) -> str:
    value = _strip_bracketed_stage_directions(str(text or ""))

    def replace_starred(match: re.Match[str]) -> str:
        body = match.group(1)
        return " " if _looks_like_stage_direction(body) else match.group(0)

    value = re.sub(r"(?<!\w)\*([^*\n]{1,160})\*(?!\w)", replace_starred, value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _strip_bracketed_stage_directions(text: str) -> str:
    pairs = {"（": "）", "(": ")", "【": "】", "[": "]"}
    openers = set(pairs)
    out: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch not in openers:
            out.append(ch)
            index += 1
            continue
        closer = pairs[ch]
        close_index = text.find(closer, index + 1)
        if close_index < 0:
            out.append(ch)
            index += 1
            continue
        body = text[index + 1:close_index]
        if _looks_like_stage_direction(body):
            out.append(" ")
        else:
            out.append(text[index:close_index + 1])
        index = close_index + 1
    return "".join(out)


def _looks_like_stage_direction(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if len(body) > 180:
        return False
    return any(hint in body for hint in STAGE_DIRECTION_HINTS)


def _clip(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."
