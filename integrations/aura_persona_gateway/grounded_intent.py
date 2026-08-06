from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal


GroundedCurrentIntent = Literal["activity", "location"]

_PUNCT_RE = re.compile(r"[\s，。！？!?、,.~～:：；;（）()“”\"'`]+")
_ACTIVITY_PATTERNS = (
    re.compile(r"^你(?:现在|这会儿|这会)?(?:在)?(?:干嘛|干什么|做什么|忙什么)(?:呢|啊|呀|吗)?$"),
    re.compile(r"^你(?:现在|这会儿|这会)?正在(?:干嘛|干什么|做什么|忙什么)(?:呢|啊|呀|吗)?$"),
)
_LOCATION_PATTERNS = (
    re.compile(r"^你(?:现在|这会儿|这会)?在(?:哪|哪里|哪儿|什么地方)(?:呢|啊|呀)?$"),
    re.compile(r"^你人(?:在)?(?:哪|哪里|哪儿)(?:呢|啊|呀)?$"),
)
_BLOCK_TOKENS = ("顺便", "然后", "另外", "还有", "为什么", "怎么", "计划", "安排")


def classify_grounded_current_intent(text: Any) -> GroundedCurrentIntent | None:
    value = normalize_grounded_current_text(text)
    if not value or len(value) > 14:
        return None
    if any(token in value for token in _BLOCK_TOKENS):
        return None
    if any(pattern.fullmatch(value) for pattern in _LOCATION_PATTERNS):
        return "location"
    if any(pattern.fullmatch(value) for pattern in _ACTIVITY_PATTERNS):
        return "activity"
    return None


def normalize_grounded_current_text(text: Any) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    return _PUNCT_RE.sub("", value).strip().lower()
