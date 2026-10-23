from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .world_ledger import LEDGER_SOURCE_NOTE


# ── X3 语音便签板 ────────────────────────────────────────────
# 设备信息板有 3 格便签（firmware/esp32/main/ui/panels_info_board.c），
# 每格用 char note[64] 缓冲、两行、每行 118px（draw_note_cell 折行）；宽度算法见
# firmware/esp32/main/ui/font.c 的 font_utf8_width：ASCII 9px、其它 17px，
# 总和再减 1。一行刚好 7 个汉字（7*17-1=118），两行 14 个，所以这里按像素裁，
# 混合中英文时比"数字数"更准；折行处浪费的几个像素由固件第二行的"..."兜底。
NOTE_SLOT_LIMIT = 3
# 信息板每格两行、每行 118px（固件 draw_note_cell 折行）：两行 7 个汉字 = 14×17-1。
NOTE_LINE_WIDTH = 118
NOTE_MAX_WIDTH = 237
NOTE_MAX_BYTES = 63  # 设备 char note[64] 要留一个 '\0'
NOTE_GLYPH_NARROW = 9
NOTE_GLYPH_WIDE = 17
NOTE_ELLIPSIS = "…"
# 便签也走提醒那条 notes 通道，合计最多 3 条。
INFO_BOARD_NOTE_LIMIT = NOTE_SLOT_LIMIT
PINNED_NOTES_STATE_KEY = "pinned_notes"
NOTE_SIGNAL_FILENAME = "pinned_notes_signal.json"

PIN_SOURCE_VOICE = "voice"
PIN_SOURCE_AGENT = "agent"

PIN_CONFIRM_REPLY = "记好啦，贴在便签上了。"
CLEAR_CONFIRM_REPLY = "好，便签都擦掉了。"

# 便签目标：便签/黑板/信息板，几种口头说法都收。
_NOTE_TARGET_RE = re.compile(r"便签|便条|黑板|信息板|记事板|备忘")
# "记在便签上" / "记到黑板上" / "贴个便签" / "写个便签" / "挂个便签"
_PIN_VERB_RE = re.compile(r"记|写|贴|挂|留")
_PIN_PARTICLE_RE = re.compile(r"一?下|个|到|在|上|给|把|将")
# 内容分隔符：冒号、逗号、"是"都当"指令词说完了"处理。
_CONTENT_SPLIT_RE = re.compile(r"^[:：,，、\s]+")
# 裸指令（"帮我记一下：买牛奶"）没有目标名词，靠"记一下"认。
_PIN_BARE_RE = re.compile(r"^(?:帮我|帮忙|麻烦|请你?|给我)?\s*(?:记|写)(?:一?下|下来|着)")
# 动词也说全："删除便签"、"便签都擦掉"、"把便签删了"都算清空。
_CLEAR_VERBS = r"(?:删|删除|清除|清空|擦|抹|去掉|取消|不要了|不用了)"
_CLEAR_RE = re.compile(
    rf"(?:便签|便条|黑板|信息板|记事板|备忘)\s*(?:都|全|给我|一下)?\s*{_CLEAR_VERBS}"
    rf"|{_CLEAR_VERBS}\s*(?:一下|掉|了)?\s*(?:便签|便条|黑板|信息板|记事板|备忘)"
)
_CLEAR_TARGET_RE = re.compile(r"便签|便条|黑板|信息板|记事板|备忘")
_CLEAR_VERB_RE = re.compile(_CLEAR_VERBS)
_LEADING_STRIP_RE = re.compile(r"^(?:帮我|帮忙|麻烦|请你?|给我|把|将|在|到|上|一下|个|便签|便条|黑板|信息板|记事板|备忘|记|写|贴|挂|留|说|是|：|:|,|，|、|\s)+")


def note_display_width(text: str) -> int:
    """跟固件 font_utf8_width 同一个算法，用来在服务端就裁到放得下。"""
    width = 0
    for char in str(text or ""):
        width += NOTE_GLYPH_NARROW if ord(char) < 0x80 else NOTE_GLYPH_WIDE
    return width - 1 if width > 0 else 0


def clip_note_text(text: Any, *, max_width: int = NOTE_MAX_WIDTH, max_bytes: int = NOTE_MAX_BYTES) -> str:
    """裁到设备放得下：像素不超 max_width、字节不超 max_bytes，超了补“…”。"""
    value = _clean_note_text(text)
    if not value:
        return ""
    if note_display_width(value) <= max_width and len(value.encode("utf-8")) <= max_bytes:
        return value
    suffix = NOTE_ELLIPSIS
    kept = ""
    for char in value:
        candidate = kept + char + suffix
        if len(candidate.encode("utf-8")) > max_bytes:
            break
        if note_display_width(candidate) > max_width:
            break
        kept += char
    return f"{kept}{suffix}" if kept else ""


def _clean_note_text(text: Any) -> str:
    value = str(text if text is not None else "")
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip()


def read_pinned_notes(state: Any) -> list[dict[str, Any]]:
    """读 ``metadata.pinned_notes``，新的在前，最多 3 条。"""
    if not isinstance(state, dict):
        return []
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        return []
    stored = metadata.get(PINNED_NOTES_STATE_KEY)
    if not isinstance(stored, list):
        return []
    notes: list[dict[str, Any]] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        text = clip_note_text(item.get("text"))
        if not text:
            continue
        notes.append(
            {
                "text": text,
                "source": str(item.get("source") or PIN_SOURCE_VOICE),
                "at": float(item.get("at") or 0.0),
            }
        )
    notes.sort(key=lambda note: note.get("at") or 0.0, reverse=True)
    return notes[:NOTE_SLOT_LIMIT]


def write_pinned_notes(state: Any, notes: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """写 ``metadata.pinned_notes``（原地改，返回归一化后的列表）。"""
    cleaned = read_pinned_notes({"metadata": {PINNED_NOTES_STATE_KEY: list(notes or [])}})
    if not isinstance(state, dict):
        return cleaned
    metadata = state.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        state["metadata"] = metadata
    metadata[PINNED_NOTES_STATE_KEY] = cleaned
    return cleaned


def pin_note(
    state: Any,
    text: Any,
    *,
    source: str = PIN_SOURCE_VOICE,
    at: float | None = None,
) -> dict[str, Any] | None:
    """钉一条便签：新的在最前，超过 3 条挤掉最旧的。返回钉上去的那条。"""
    note_text = clip_note_text(text)
    if not note_text:
        return None
    stamp = float(at if at is not None else time.time())
    notes = read_pinned_notes(state)
    # 内容一样的就刷新时间，不重复占一格。
    notes = [note for note in notes if note.get("text") != note_text]
    notes.insert(0, {"text": note_text, "source": str(source or PIN_SOURCE_VOICE), "at": stamp})
    write_pinned_notes(state, notes)
    return {"text": note_text, "source": str(source or PIN_SOURCE_VOICE), "at": stamp}


def clear_pinned_notes(state: Any) -> list[dict[str, Any]]:
    """清空便签，返回被擦掉的那些条。"""
    removed = read_pinned_notes(state)
    if isinstance(state, dict):
        metadata = state.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            state["metadata"] = metadata
        metadata[PINNED_NOTES_STATE_KEY] = []
    return removed


def parse_note_command(text: Any) -> dict[str, Any] | None:
    """语音便签指令 → ``{"action": "pin", "text": ...}`` / ``{"action": "clear"}`` / None。

    故意只认带"便签/黑板"或明确"记一下"的说法，免得把正常聊天当成钉便签。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    compact = re.sub(r"\s+", "", raw)
    if _CLEAR_RE.search(compact):
        return {"action": "clear"}
    content = _pin_content(compact)
    if content:
        return {"action": "pin", "text": clip_note_text(content)}
    return None


def _pin_content(compact: str) -> str:
    """从"记在便签上买牛奶"里取出"买牛奶"，取不到返回空串。"""
    if not _NOTE_TARGET_RE.search(compact) and not _PIN_BARE_RE.search(compact):
        return ""
    if not _PIN_VERB_RE.search(compact):
        return ""
    # 指令词在前："帮我记一下：买牛奶" / "记在便签上，买牛奶"
    match = _PIN_PREFIX_RE.search(compact)
    if match:
        tail = _CONTENT_SPLIT_RE.sub("", compact[match.end():])
        if tail:
            return tail
    # 指令词在后："买牛奶记在便签上"
    match = _PIN_SUFFIX_RE.search(compact)
    if match:
        head = compact[: match.start()]
        head = _CLEAR_VERB_RE.sub("", head)
        head = _LEADING_STRIP_RE.sub("", head)
        if head:
            return head
    return ""


_PIN_PREFIX_RE = re.compile(
    r"^(?:帮我|帮忙|麻烦|请你?|给我|来)?"
    r"(?:记|写|贴|挂|留)(?:一?下|个|到|在|上|给|把|将)?"
    r"(?:便签|便条|黑板|信息板|记事板|备忘)?(?:一?下|个|到|在|上)?"
)
_PIN_SUFFIX_RE = re.compile(
    r"(?:记|写|贴|挂|留)(?:一?下|个|到|在|上)?(?:便签|便条|黑板|信息板|记事板|备忘)?(?:一?下|上|里|中)?$"
)


def note_command_reply(command: dict[str, Any]) -> str:
    action = str((command or {}).get("action") or "")
    return CLEAR_CONFIRM_REPLY if action == "clear" else PIN_CONFIRM_REPLY


def merge_info_board_notes(
    reminders: Any,
    notes: Any,
    *,
    limit: int = INFO_BOARD_NOTE_LIMIT,
) -> list[str]:
    """``status_update.payload.notes``：提醒（按时间）在前，便签（新在前）在后，合计 ≤3。

    这里只做去重和限量，不再裁宽度：便签在钉上的时候就已经按两行宽度裁好了，
    提醒一向由固件 ``note_text()`` 自己裁，服务端再裁一次只会把提醒的时间前缀
    也砍掉。重复内容只留一条——3 格便签板放不下重复信息。
    """
    merged: list[str] = []
    for item in list(reminders or []) + [
        note.get("text") if isinstance(note, dict) else note for note in (notes or [])
    ]:
        text = _clean_note_text(item)
        if text and text not in merged:
            merged.append(text)
    return merged[: max(1, int(limit or 1))]


def note_ledger_cause(*, source: str, text: str, action: str = "pin") -> str:
    """账本 cause：写清楚来源和内容，X1 大屏上能直接读懂。"""
    who = "你自己说的" if source == PIN_SOURCE_VOICE else "后台任务结论"
    verb = "钉了便签" if action == "pin" else "擦掉了便签"
    return f"{who}，{verb}：{text}" if text else f"{who}，{verb}"


def note_ledger_title(*, action: str = "pin") -> str:
    return "便签：记一条" if action == "pin" else "便签：清空"


def record_note_ledger(
    store: Any,
    scope: Any,
    state: Any,
    *,
    action: str = "pin",
    source: str = PIN_SOURCE_VOICE,
    text: str = "",
    at: float | None = None,
) -> dict[str, Any] | None:
    """每次钉/清便签都留一条账本流水，X1 大屏看得到。

    返回 ``{"event_key": ..., "event_id": ...}``，调用方放进 evidence 便于对账。
    """
    if not hasattr(store, "record_life_event"):
        return None
    stamp = float(at if at is not None else time.time())
    note_text = clip_note_text(text)
    event_key = f"note:{int(stamp * 1000)}"
    before = {field: _state_number(state, field) for field in LEDGER_NOTE_FIELDS}
    after = dict(before)
    delta = {field: 0 for field in LEDGER_NOTE_FIELDS}
    event_id = store.record_life_event(
        scope,
        event_type=f"ledger.{LEDGER_SOURCE_NOTE}",
        title=note_ledger_title(action=action),
        description=note_text,
        visibility="public",
        intensity=0,
        delta=delta,
        payload={
            "before": before,
            "after": after,
            "delta": delta,
            "cause": note_ledger_cause(source=source, text=note_text, action=action),
            "source": LEDGER_SOURCE_NOTE,
            "note_source": str(source or PIN_SOURCE_VOICE),
            "note_text": note_text,
            "action": "clear" if action == "clear" else "pin",
            "at": stamp,
        },
        event_key=event_key,
    )
    return {"event_key": event_key, "event_id": event_id}


# 便签不动任何数值字段，只是为了在账本里留一行、字段保持一致。
LEDGER_NOTE_FIELDS = ("mood", "energy", "satiety", "stress")


def _state_number(state: Any, field: str) -> int:
    if not isinstance(state, dict):
        return 0
    try:
        return int(round(float(state.get(field))))
    except (TypeError, ValueError):
        return 0


def note_signal_path(config: Any) -> Path:
    """bridge 写、gateway 读的"便签变了"信号文件，放在 persona 配置目录旁边。"""
    runtime_path = getattr(config, "runtime_config_path", None)
    if runtime_path:
        return Path(runtime_path).expanduser().parent / NOTE_SIGNAL_FILENAME
    return Path(".aura/persona/config") / NOTE_SIGNAL_FILENAME


def bump_note_signal(config: Any, *, at: float | None = None) -> float:
    """便签钉上/清空后拨一下信号，让网关立刻推一次 status_update。"""
    stamp = float(at if at is not None else time.time())
    path = note_signal_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"revision": %d, "at": %.3f}\n' % (int(stamp * 1000), stamp),
            encoding="utf-8",
        )
    except OSError:
        return 0.0
    return stamp


def read_note_signal(config: Any) -> float:
    """读信号文件里的 revision；读不到返回 0（网关当作"从未变过"）。"""
    path = note_signal_path(config)
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return 0.0
    match = re.search(r'"revision"\s*:\s*(\d+)', payload or "")
    return float(match.group(1)) if match else 0.0


def agent_note_text(
    result_text: Any,
    *,
    task_kind: str = "agent_lookup",
    title: str = "",
    summarize: Callable[[str], str] | None = None,
) -> str:
    """后台任务结果 → 一条 ≤上限的结论便签。

    ``agent_lookup`` 用快模型压一句话（压不出来就取第一句）；
    ``agent_create``（文档/代码）不塞正文，只钉"《标题》已完成"。
    """
    kind = str(task_kind or "").strip().lower()
    if kind.startswith("agent_create"):
        name = _clean_note_text(title)
        return clip_note_text(f"《{name}》已完成") if name else ""
    body = _clean_note_text(result_text)
    if not body:
        return ""
    summary = ""
    if summarize is not None:
        try:
            summary = _clean_note_text(summarize(body) or "")
        except Exception:
            summary = ""
    if not summary:
        summary = _first_sentence(body)
    return clip_note_text(summary)


def _first_sentence(body: str) -> str:
    parts = re.split(r"[。！？!?；;\n]+", body)
    for part in parts:
        candidate = part.strip(" ，,、")
        if candidate:
            return candidate
    return body[:40]


def note_summary_prompt(result_text: Any, *, source_text: str = "") -> str:
    """给快模型的压缩指令：只要结论，长度按便签宽度卡。"""
    return "\n".join(
        [
            "把下面这段后台任务结果压成一条桌面小屏便签。",
            "要求：不超过 14 个汉字（数字和字母算半个），只留结论，"
            "不要标点结尾、不要来源链接、不要解释过程、不要换行。",
            f"用户的问题是：{_clean_note_text(source_text)[:120]}",
            f"任务结果是：{_clean_note_text(result_text)[:800]}",
            "直接输出便签内容本身。",
        ]
    )


__all__ = [
    "INFO_BOARD_NOTE_LIMIT",
    "NOTE_SLOT_LIMIT",
    "NOTE_MAX_WIDTH",
    "PINNED_NOTES_STATE_KEY",
    "PIN_SOURCE_AGENT",
    "PIN_SOURCE_VOICE",
    "agent_note_text",
    "bump_note_signal",
    "clear_pinned_notes",
    "clip_note_text",
    "merge_info_board_notes",
    "note_command_reply",
    "note_signal_path",
    "note_summary_prompt",
    "parse_note_command",
    "pin_note",
    "read_note_signal",
    "read_pinned_notes",
    "record_note_ledger",
    "write_pinned_notes",
]
