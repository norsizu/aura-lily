from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

from .emotion_engine import emotion_label, normalize_emotion, read_emotion_state
from .world_ledger import LEDGER_EVENT_PREFIX, RECAP_FIELD_LABELS


# ── X2 问她为什么 ────────────────────────────────────────────
# 用户问 Lily 自己状态的原因时，她必须照生活账本里真实记录的原因说，不能编。
# 这里只做两件事：规则判断"这轮是不是在问她为什么"，以及把账本流水整理成
# 可以直接贴进 prompt 的一节。判断故意保守——漏判只是少一节背景，误判会把
# "为什么天是蓝的"这类问题拽到她自己身上。
SELF_WHY_WINDOW_SECONDS = 6 * 3600.0
SELF_WHY_EVENT_LIMIT = 6
# 只认这几个数值字段的变化，隐藏驱动和好感对"我为什么这样"没有解释力。
SELF_WHY_FIELDS = ("mood", "energy", "satiety", "stress")
# ``ledger.interaction`` 是每轮 ±1 的噪音（"陪你聊了一轮"），不进这一节。
SELF_WHY_EXCLUDED_EVENT_TYPES = (f"{LEDGER_EVENT_PREFIX}interaction",)
# 最近一次"问为什么"的痕迹，X1 大屏用它高亮对应的账本行。
SELF_WHY_STATE_KEY = "self_why_state"
SELF_WHY_SECTION_TITLE = (
    "## 你最近状态变化的真实原因（来自你的生活账本，回答时只能据此说，不要编造其他原因）"
)
SELF_WHY_EMPTY_LINE = "最近没有记录到明显变化。"
SELF_WHY_EMPTY_TAIL = (
    "照实说最近没什么特别的事、大概就是常态，不要编造具体原因、时间和事件。"
)
SELF_WHY_NO_BACKGROUND_TAIL = (
    "这是关于你自己的提问，直接回答；不要输出 [后台] 标记，也不要联网查资料。"
)

_PUNCT_RE = re.compile(r"[\s,，。.!！?？、~～…·:：;；\"'“”‘’（）()【】\[\]{}<>《》]+")
# "你" 也会出现在"一下"里，先排掉这种假自指（"帮我查一下为什么…"不是问她）。
_SELF_REFERENCE_RE = re.compile(r"(?<!一)你|您|lily|莉莉")
_WHY_TOKENS = ("为什么", "为啥", "怎么会", "怎么", "如何", "怎的")
_STATE_TOKENS = (
    "心情", "情绪", "状态", "感觉",
    "不开心", "高兴", "开心", "快乐", "难过", "伤心", "生气", "委屈", "害羞", "兴奋",
    "累", "疲惫", "困", "饿", "紧张", "担心", "郁闷", "烦躁", "低落", "没精神",
    "不舒服", "难受", "烦",
)
# "你怎么了" 这类裸问法没有状态词，靠结尾的 了/啦/呢 认。
_BARE_SELF_HOW_RE = re.compile(r"(?:你|您)(?:怎么|为啥|为什么)?(?:了|啦|呢|嘛)(?:$|(?=[?？]))")
# "你心情怎么样" 没有 why 词，单独放行。
_MOOD_QUESTION_RE = re.compile(r"^(?:你|您)(?:今天|现在|这会儿|刚才|最近)?心情")
# 主语其实是她以外的人："你妈妈为什么生气"不是问她。
_THIRD_PARTY_RE = re.compile(
    r"你(?:的)?(?:妈妈|父亲|母亲|爸爸|父母|朋友|同事|老板|同学|老师|学生|孩子|儿子|女儿|"
    r"老公|老婆|男朋友|女朋友|室友|猫|狗狗|狗|主人|偶像|邻居|客户)"
)


def normalize_self_why_text(text: Any) -> str:
    value = str(text if text is not None else "")
    return _PUNCT_RE.sub("", value).strip().lower()


def is_self_why_question(text: Any) -> bool:
    """这轮是不是在问 Lily 自己"为什么是这个状态"。

    正例：你为什么不开心 / 你怎么了 / 你刚才为什么生气 / 你心情怎么样。
    反例：为什么天是蓝的 / 我为什么不开心 / 你怎么看这个问题 / 你妈妈为什么生气。
    """
    value = normalize_self_why_text(text)
    if not value or len(value) > 30:
        return False
    if not _SELF_REFERENCE_RE.search(value):
        return False
    if _THIRD_PARTY_RE.search(value):
        return False
    if _BARE_SELF_HOW_RE.search(value):
        return True
    if _MOOD_QUESTION_RE.search(value):
        return True
    if not any(token in value for token in _WHY_TOKENS):
        return False
    return any(token in value for token in _STATE_TOKENS)


def _fmt_time(timestamp: Any) -> str:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return dt.datetime.fromtimestamp(value).strftime("%H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def format_self_why_event(event: dict[str, Any]) -> str:
    """一条账本事件 → 口语化一行，例如 ``14:20 去公园散步，体力 80→73``。"""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
    before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    title = str(event.get("title") or "").strip() or str(payload.get("cause") or "").strip()
    title = title or str(event.get("description") or "").strip()
    changes: list[str] = []
    for field in SELF_WHY_FIELDS:
        if field not in delta and field not in before and field not in after:
            continue
        try:
            start = int(round(float(before.get(field))))
            end = int(round(float(after.get(field))))
        except (TypeError, ValueError):
            continue
        if start == end:
            continue
        changes.append(f"{RECAP_FIELD_LABELS.get(field, field)} {start}→{end}")
    stamp = _fmt_time(event.get("created_at"))
    body = title + (f"，{'，'.join(changes)}" if changes else "")
    return " ".join(part for part in (stamp, body) if part)


def _changed_any_field(delta: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> bool:
    """mood/energy/satiety/stress 里有没有一个真的变了。"""

    def number(source: dict[str, Any], field: str) -> float | None:
        if field not in source:
            return None
        try:
            return float(source[field])
        except (TypeError, ValueError):
            return None

    for field in SELF_WHY_FIELDS:
        moved = number(delta, field)
        if moved is not None and moved != 0:
            return True
        start = number(before, field)
        end = number(after, field)
        if start is not None and end is not None and start != end:
            return True
    return False


def collect_self_why_events(
    store: Any,
    scope: Any,
    *,
    now: float | None = None,
    within_seconds: float = SELF_WHY_WINDOW_SECONDS,
    limit: int = SELF_WHY_EVENT_LIMIT,
) -> list[dict[str, Any]]:
    """最近 ``within_seconds`` 内、动过 mood/energy/satiety/stress 的账本事件。

    返回时间正序（最早→最新）：prompt 里当"最近记录"念给她听，顺着时间讲更自然。
    """
    if not hasattr(store, "list_recent_life_events"):
        return []
    current = float(now if now is not None else time.time())
    try:
        events = store.list_recent_life_events(
            scope,
            event_prefixes=(f"{LEDGER_EVENT_PREFIX}%",),
            limit=max(limit * 3, 12),
        )
    except TypeError:
        # 老 store 只认单个 event_prefix。
        events = store.list_recent_life_events(scope, event_prefix=LEDGER_EVENT_PREFIX, limit=max(limit * 3, 12))
    picked: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("event_type") or "") in SELF_WHY_EXCLUDED_EVENT_TYPES:
            continue
        try:
            created_at = float(event.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if created_at <= 0 or current - created_at > float(within_seconds):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
        after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        if not _changed_any_field(delta, before, after):
            # 只带快照、数值没动的流水（隐藏驱动 tick、每次对话的 ±1）对
            # "我为什么这样"没有解释力，塞进去只会让她念一堆废话。
            continue
        picked.append(event)
    # store 给的是时间正序，取最后 limit 条就是最近那几条，顺序保持正序。
    return picked[-max(1, int(limit or 1)) :]


def emotion_state_line(state: Any) -> str:
    """``emotion_state`` 那一行：现在的情绪 + 强度 + 一句原因。"""
    stored = read_emotion_state(state)
    emotion = normalize_emotion(stored.get("emotion"))
    if not emotion:
        return ""
    label = str(stored.get("label") or "").strip() or emotion_label(emotion)
    try:
        intensity = int(round(float(stored.get("intensity"))))
    except (TypeError, ValueError):
        intensity = 0
    cause = str(stored.get("cause") or "").strip()
    parts = [f"你现在的情绪：{label}"]
    if intensity > 0:
        parts.append(f"强度 {intensity}")
    line = "（" + "，".join(parts) + "）"
    return f"{line}——{cause}" if cause else line


def build_self_why_prompt(*, state: Any, events: list[dict[str, Any]]) -> str:
    """命中"问她为什么"时要加进 prompt 的那一节。"""
    lines = [SELF_WHY_SECTION_TITLE]
    emotion_line = emotion_state_line(state)
    if emotion_line:
        lines.append(emotion_line)
    if events:
        lines.append("最近记录：")
        lines.extend(f"- {format_self_why_event(event)}" for event in events)
        lines.append("用户问你为什么是这样时，只能依据上面这些记录解释，不要补记里没有的原因。")
    else:
        lines.append(SELF_WHY_EMPTY_LINE)
        lines.append(SELF_WHY_EMPTY_TAIL)
    lines.append(SELF_WHY_NO_BACKGROUND_TAIL)
    return "\n".join(lines)


def build_self_why_turn(
    *,
    store: Any,
    scope: Any,
    state: dict[str, Any],
    user_text: str,
    now: float | None = None,
) -> dict[str, Any]:
    """一轮的自查结果：要不要加背景、加什么、用到了哪些账本 event_key。"""
    stamp = float(now if now is not None else time.time())
    if not is_self_why_question(user_text):
        return {
            "matched": False,
            "prompt": "",
            "why_trace": [],
            "events": [],
            "debug": {"matched": False},
        }
    events = collect_self_why_events(store, scope, now=stamp)
    prompt = build_self_why_prompt(state=state, events=events)
    why_trace = [
        str(event.get("event_key") or "").strip()
        for event in events
        if str(event.get("event_key") or "").strip()
    ]
    return {
        "matched": True,
        "prompt": prompt,
        "why_trace": why_trace,
        "events": events,
        "debug": {
            "matched": True,
            "why_trace": why_trace,
            "event_count": len(events),
            "prompt_chars": len(prompt),
        },
    }


def read_self_why_state(state: Any) -> dict[str, Any]:
    """读 ``metadata.self_why_state``，没有就返回空 dict。"""
    if not isinstance(state, dict):
        return {}
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    stored = metadata.get(SELF_WHY_STATE_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def write_self_why_state(
    state: Any,
    *,
    event_keys: list[str] | tuple[str, ...] = (),
    question: str = "",
    at: float | None = None,
) -> dict[str, Any]:
    """记下这一轮"问为什么"引用的账本 event_key，供 X1 大屏高亮。"""
    if not isinstance(state, dict):
        return {}
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        state["metadata"] = metadata
    keys = [str(key or "").strip() for key in event_keys or [] if str(key or "").strip()]
    metadata[SELF_WHY_STATE_KEY] = {
        "event_keys": keys,
        "question": str(question or "")[:120],
        "at": float(at if at is not None else time.time()),
    }
    return metadata[SELF_WHY_STATE_KEY]
