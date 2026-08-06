"""Exact-match fixed Aura prompts that can be played from device assets."""

from __future__ import annotations

import re
from typing import Any


LANGUAGES = ("zh", "en", "ja")

PROMPTS: dict[str, dict[str, str]] = {
    "greeting": {
        "zh": "我在。",
        "en": "I'm here.",
        "ja": "いるよ。",
    },
    "clarify": {
        "zh": "你刚才那句没说完整，再说一遍？",
        "en": "I didn't catch all of that. Could you say it again?",
        "ja": "今の言葉、全部聞き取れなかった。もう一度言ってくれる？",
    },
    "refuse": {
        "zh": "这个我不能帮你做。",
        "en": "I can't help with that.",
        "ja": "それは手伝えないよ。",
    },
    "background": {
        "zh": "好，我去查，弄完马上告诉你。",
        "en": "Okay, I'll look into it and let you know as soon as it's done.",
        "ja": "わかった。調べて、終わったらすぐ知らせるね。",
    },
    "quota": {
        "zh": "这段时间聊得很开心，不过次数用完啦。额度恢复后，我们再继续，好吗？",
        "en": "We've had a lovely chat, but this window is used up. Let's continue when it resets, okay?",
        "ja": "たくさん話せてうれしかったよ。この時間帯の回数を使い切ったから、回復したらまた話そうね。",
    },
    "language": {
        "zh": "你好，我是 Aura。以后我会用中文和你说话。",
        "en": "Hi, I'm Aura. I'll speak with you in English.",
        "ja": "こんにちは、Auraです。これから日本語で話します。",
    },
}

LOCAL_AUDIO_IDS = {
    (category, language): f"prompt_{category}_{language}"
    for category in PROMPTS
    if category != "language"
    for language in LANGUAGES
}
LOCAL_AUDIO_IDS.update({
    ("language", language): f"lang_{language}" for language in LANGUAGES
})


def normalize_language(value: Any) -> str:
    language = str(value or "").strip().lower().replace("_", "-")
    if language == "jp" or language == "ja" or language.startswith("ja-"):
        return "ja"
    if language == "en" or language.startswith("en-"):
        return "en"
    return "zh"


def spoken_key(text: Any) -> str:
    value = str(text or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def match_local_audio(text: Any, language: Any = "zh") -> tuple[str, str] | None:
    """Return localized text and a safe asset ID for an exact fixed prompt."""
    candidate = spoken_key(text)
    if not candidate:
        return None
    target_language = normalize_language(language)
    for category, variants in PROMPTS.items():
        if candidate in variants.values():
            return variants[target_language], LOCAL_AUDIO_IDS[(category, target_language)]
    return None
