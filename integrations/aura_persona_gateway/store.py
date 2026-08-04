from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator

from .config import PersonaScope


DEFAULT_STATE = {
    "mood": 80,
    "energy": 100,
    "satiety": 80,
    "beans": 50,
    "affinity_xp": 0,
    "coins": 50,
    "trust": 50,
    "stress": 0,
    "outfit": "default",
    "scene": "living_room",
    "metadata": {"schema_version": 1},
}


class LilyPersonaStore:
    _schema_lock = Lock()
    _schema_ready_paths: set[str] = set()
    _retryable_sqlite_errors = ("disk i/o error", "database is locked", "database is busy", "locked")

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()

    def health(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {"ok": False, "exists": False, "path": str(self.db_path)}
        def op(conn: sqlite3.Connection) -> list[str]:
            return [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            ]

        tables = self._execute_retryable(op)
        required = {"companion_state", "companion_im_message", "companion_life_event"}
        missing = sorted(required - set(tables))
        return {
            "ok": not missing,
            "exists": True,
            "path": str(self.db_path),
            "tables": tables,
            "missing_required_tables": missing,
        }

    def get_or_create_state(self, scope: PersonaScope) -> dict[str, Any]:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = self._select_state(conn, scope)
            if row is None:
                now = time.time()
                state = dict(DEFAULT_STATE)
                conn.execute(
                    """
                    INSERT INTO companion_state
                    (platform, chat_id, user_id, mood, energy, affinity_xp, coins,
                     metadata_json, created_at, updated_at, satiety, beans, trust,
                     stress, outfit, scene)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope.platform,
                        scope.chat_id,
                        scope.user_id,
                        state["mood"],
                        state["energy"],
                        state["affinity_xp"],
                        state["coins"],
                        json.dumps(state["metadata"], ensure_ascii=False),
                        now,
                        now,
                        state["satiety"],
                        state["beans"],
                        state["trust"],
                        state["stress"],
                        state["outfit"],
                        state["scene"],
                    ),
                )
                conn.commit()
                row = self._select_state(conn, scope)
            return _state_from_row(row)

        return self._execute_retryable(op)

    def save_state(self, scope: PersonaScope, state: dict[str, Any]) -> None:
        self._ensure_schema()
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        beans = int(state.get("beans") or state.get("coins") or 0)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE companion_state
                   SET mood=?, energy=?, affinity_xp=?, coins=?, metadata_json=?,
                       updated_at=?, satiety=?, beans=?, trust=?, stress=?,
                       outfit=?, scene=?
                 WHERE platform=? AND chat_id=? AND user_id=?
                """,
                (
                    int(state.get("mood") or 0),
                    int(state.get("energy") or 0),
                    int(state.get("affinity_xp") or 0),
                    beans,
                    json.dumps(metadata, ensure_ascii=False),
                    time.time(),
                    int(state.get("satiety") or 0),
                    beans,
                    int(state.get("trust") or 0),
                    int(state.get("stress") or 0),
                    str(state.get("outfit") or "default"),
                    str(state.get("scene") or "living_room"),
                    scope.platform,
                    scope.chat_id,
                    scope.user_id,
                ),
            )
            conn.commit()

        self._execute_retryable(op)

    def list_recent_messages(self, scope: PersonaScope, *, limit: int) -> list[dict[str, Any]]:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT id, direction, message_type, body, status, visible_at, created_at, metadata_json
                  FROM companion_im_message
                 WHERE platform=? AND chat_id=? AND user_id=?
                 ORDER BY id DESC
                 LIMIT ?
                """,
                (*scope.as_tuple(), max(1, int(limit or 1))),
            ).fetchall()
            return [_message_from_row(row) for row in rows]

        messages = self._execute_retryable(op)
        return list(reversed([item for item in messages if _message_is_useful(item)]))

    def save_im_message(
        self,
        scope: PersonaScope,
        *,
        direction: str,
        body: str,
        message_type: str,
        status: str = "sent",
        metadata: dict[str, Any] | None = None,
        visible_at: float | None = None,
        task_id: str | None = None,
        reply_to_id: int | None = None,
    ) -> int:
        self._ensure_schema()
        now = time.time()
        shown_at = float(visible_at if visible_at is not None else now)

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """
                INSERT INTO companion_im_message
                (direction, message_type, body, audio_path, task_id, reply_to_id,
                 platform, chat_id, user_id, status, visible_at, created_at, metadata_json)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    direction,
                    message_type,
                    body,
                    task_id,
                    reply_to_id,
                    scope.platform,
                    scope.chat_id,
                    scope.user_id,
                    status,
                    shown_at,
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

        return self._execute_retryable(op)

    def background_task_result(self, scope: PersonaScope, *, task_id: str) -> dict[str, Any] | None:
        self._ensure_schema()
        clean_task_id = str(task_id or "").strip()
        if not clean_task_id:
            return None

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT id, direction, message_type, body, status, visible_at,
                       created_at, metadata_json, task_id, reply_to_id
                  FROM companion_im_message
                 WHERE platform=? AND chat_id=? AND user_id=?
                   AND task_id=?
                   AND message_type='background_task_result'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (*scope.as_tuple(), clean_task_id),
            ).fetchone()
            return _message_from_row(row) if row else None

        return self._execute_retryable(op)

    def latest_moment(self, scope: PersonaScope) -> dict[str, Any] | None:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT id, moment_type, visibility, title, body, location_label,
                       activity_type, mood, energy, published_at, payload_json
                  FROM companion_moment_post
                 WHERE platform=? AND chat_id=? AND user_id=? AND status='published'
                 ORDER BY published_at DESC, id DESC
                 LIMIT 1
                """,
                scope.as_tuple(),
            ).fetchone()
            return _moment_from_row(row) if row else None

        return self._execute_retryable(op)

    def latest_moments(self, scope: PersonaScope, *, limit: int = 3) -> list[dict[str, Any]]:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT id, moment_type, visibility, title, body, location_label,
                       activity_type, mood, energy, published_at, payload_json
                  FROM companion_moment_post
                 WHERE platform=? AND chat_id=? AND user_id=? AND status='published'
                 ORDER BY published_at DESC, id DESC
                 LIMIT ?
                """,
                (*scope.as_tuple(), max(1, min(10, int(limit or 1)))),
            ).fetchall()
            return [_moment_from_row(row) for row in rows]

        return self._execute_retryable(op)

    def today_plan(self, scope: PersonaScope, *, day_key: str) -> list[dict[str, Any]]:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT id, plan_date, slot_key, scheduled_at, activity_type, title,
                       location, should_post, status, expected_delta_json, payload_json
                  FROM companion_day_plan
                 WHERE platform=? AND chat_id=? AND user_id=? AND plan_date=?
                 ORDER BY scheduled_at ASC, id ASC
                """,
                (*scope.as_tuple(), day_key),
            ).fetchall()
            return [_plan_from_row(row) for row in rows]

        return self._execute_retryable(op)

    def replace_day_plan(self, scope: PersonaScope, *, day_key: str, items: list[dict[str, Any]]) -> None:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                DELETE FROM companion_day_plan
                 WHERE platform=? AND chat_id=? AND user_id=? AND plan_date=?
                """,
                (*scope.as_tuple(), day_key),
            )
            for item in items:
                conn.execute(
                    """
                    INSERT INTO companion_day_plan
                    (platform, chat_id, user_id, plan_date, slot_key, scheduled_at,
                     activity_type, title, location, should_post, status,
                     expected_delta_json, payload_json, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scope.platform,
                        scope.chat_id,
                        scope.user_id,
                        str(item.get("plan_date") or day_key),
                        str(item.get("slot_key") or ""),
                        float(item.get("scheduled_at") or time.time()),
                        str(item.get("activity_type") or "life"),
                        str(item.get("title") or ""),
                        str(item.get("location") or ""),
                        1 if item.get("should_post") else 0,
                        str(item.get("status") or "pending"),
                        json.dumps(item.get("expected_delta") if isinstance(item.get("expected_delta"), dict) else {}, ensure_ascii=False),
                        json.dumps(item.get("payload") if isinstance(item.get("payload"), dict) else {}, ensure_ascii=False),
                        item.get("executed_at"),
                    ),
                )
            conn.commit()

        self._execute_retryable(op)

    def save_day_plan_statuses(self, scope: PersonaScope, *, day_key: str, items: list[dict[str, Any]]) -> None:
        self._ensure_schema()

        def op(conn: sqlite3.Connection) -> None:
            for item in items:
                conn.execute(
                    """
                    UPDATE companion_day_plan
                       SET status=?, executed_at=?
                     WHERE platform=? AND chat_id=? AND user_id=?
                       AND plan_date=? AND slot_key=?
                    """,
                    (
                        str(item.get("status") or "pending"),
                        item.get("executed_at"),
                        scope.platform,
                        scope.chat_id,
                        scope.user_id,
                        day_key,
                        str(item.get("slot_key") or ""),
                    ),
                )
            conn.commit()

        self._execute_retryable(op)

    def record_debug_event(
        self,
        scope: PersonaScope,
        *,
        title: str,
        payload: dict[str, Any],
        trace_id: str,
    ) -> int:
        self._ensure_schema()
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """
                INSERT INTO companion_life_event
                (platform, chat_id, user_id, event_type, event_key, title,
                 description, location, activity, visibility, intensity,
                 delta_json, payload_json, created_at)
                VALUES (?, ?, ?, 'lily.debug', ?, ?, '', NULL, NULL, 'debug',
                        0, '{}', ?, ?)
                """,
                (
                    scope.platform,
                    scope.chat_id,
                    scope.user_id,
                    trace_id,
                    title,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

        return self._execute_retryable(op)

    def record_life_event(
        self,
        scope: PersonaScope,
        *,
        event_type: str,
        title: str,
        description: str = "",
        visibility: str = "private",
        event_key: str = "",
        location: str = "",
        activity: str = "",
        intensity: int = 1,
        delta: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        self._ensure_schema()
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                """
                INSERT INTO companion_life_event
                (platform, chat_id, user_id, event_type, event_key, title,
                 description, location, activity, visibility, intensity,
                 delta_json, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope.platform,
                    scope.chat_id,
                    scope.user_id,
                    event_type,
                    event_key or None,
                    title,
                    description,
                    location or None,
                    activity or None,
                    visibility,
                    max(0, int(intensity or 0)),
                    json.dumps(delta or {}, ensure_ascii=False),
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

        return self._execute_retryable(op)

    def has_life_event(self, scope: PersonaScope, *, event_key: str) -> bool:
        """Idempotency check for deterministic world effects."""
        self._ensure_schema()
        key = str(event_key or "").strip()
        if not key:
            return False

        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                """SELECT 1 FROM companion_life_event
                   WHERE platform=? AND chat_id=? AND user_id=? AND event_key=?
                   LIMIT 1""",
                (*scope.as_tuple(), key),
            ).fetchone()
            return bool(row)

        return self._execute_retryable(op)

    def list_recent_life_events(
        self,
        scope: PersonaScope,
        *,
        event_prefix: str = "world.",
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        prefix = str(event_prefix or "").strip()

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            where = "event_type LIKE ?" if prefix else "visibility != 'debug'"
            params: tuple[Any, ...] = (
                (*scope.as_tuple(), f"{prefix}%", max(1, min(20, int(limit or 1))))
                if prefix
                else (*scope.as_tuple(), max(1, min(20, int(limit or 1))))
            )
            rows = conn.execute(
                f"""
                SELECT id, event_type, event_key, title, description, location,
                       activity, visibility, intensity, delta_json, payload_json,
                       created_at
                  FROM companion_life_event
                 WHERE platform=? AND chat_id=? AND user_id=? AND {where}
                 ORDER BY created_at DESC, id DESC
                 LIMIT ?
                """,
                params,
            ).fetchall()
            return [_life_event_from_row(row) for row in rows]

        return list(reversed(self._execute_retryable(op)))

    def _ensure_schema(self) -> None:
        key = str(self.db_path)
        if key in self._schema_ready_paths:
            return
        with self._schema_lock:
            if key in self._schema_ready_paths:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            def op(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companion_state (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        mood INTEGER NOT NULL DEFAULT 80,
                        energy INTEGER NOT NULL DEFAULT 65,
                        affinity_xp INTEGER NOT NULL DEFAULT 0,
                        coins INTEGER NOT NULL DEFAULT 50,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        satiety INTEGER NOT NULL DEFAULT 80,
                        beans INTEGER NOT NULL DEFAULT 50,
                        trust INTEGER NOT NULL DEFAULT 50,
                        stress INTEGER NOT NULL DEFAULT 0,
                        outfit TEXT NOT NULL DEFAULT 'default',
                        scene TEXT NOT NULL DEFAULT 'living_room',
                        UNIQUE(platform, chat_id, user_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companion_im_message (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        direction TEXT NOT NULL,
                        message_type TEXT NOT NULL,
                        body TEXT,
                        audio_path TEXT,
                        task_id TEXT,
                        reply_to_id INTEGER,
                        platform TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'sent',
                        visible_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companion_moment_post (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        moment_type TEXT NOT NULL,
                        visibility TEXT NOT NULL DEFAULT 'public',
                        title TEXT,
                        body TEXT NOT NULL,
                        location_key TEXT,
                        location_label TEXT,
                        activity_type TEXT,
                        mood INTEGER,
                        energy INTEGER,
                        source_event_id INTEGER,
                        source_plan_date TEXT,
                        source_slot_key TEXT,
                        asset_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'published',
                        published_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companion_day_plan (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        plan_date TEXT NOT NULL,
                        slot_key TEXT NOT NULL,
                        scheduled_at REAL NOT NULL,
                        activity_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        location TEXT,
                        should_post INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'pending',
                        expected_delta_json TEXT NOT NULL DEFAULT '{}',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        executed_at REAL,
                        UNIQUE(platform, chat_id, user_id, plan_date, slot_key)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS companion_life_event (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        chat_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        event_key TEXT,
                        title TEXT NOT NULL,
                        description TEXT,
                        location TEXT,
                        activity TEXT,
                        visibility TEXT NOT NULL DEFAULT 'private',
                        intensity INTEGER NOT NULL DEFAULT 1,
                        delta_json TEXT NOT NULL DEFAULT '{}',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()

            self._execute_retryable(op)
            self._schema_ready_paths.add(key)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _execute_retryable(self, operation: Any) -> Any:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                with self._connection() as conn:
                    return operation(conn)
            except sqlite3.OperationalError as exc:
                last_error = exc
                if attempt >= 2 or not self._is_retryable_sqlite_error(exc):
                    raise
                time.sleep(0.05 * (attempt + 1))
        if last_error:
            raise last_error
        raise sqlite3.OperationalError("sqlite operation failed")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        conn.row_factory = sqlite3.Row
        return conn

    def _is_retryable_sqlite_error(self, exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return any(item in message for item in self._retryable_sqlite_errors)

    @staticmethod
    def _select_state(conn: sqlite3.Connection, scope: PersonaScope) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM companion_state
             WHERE platform=? AND chat_id=? AND user_id=?
             LIMIT 1
            """,
            scope.as_tuple(),
        ).fetchone()


def _state_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    metadata = _json(data.pop("metadata_json", "{}"))
    data["metadata"] = metadata
    return data


def _message_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = _json(data.pop("metadata_json", "{}"))
    return data


def _moment_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _json(data.pop("payload_json", "{}"))
    return data


def _plan_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["expected_delta"] = _json(data.pop("expected_delta_json", "{}"))
    data["payload"] = _json(data.pop("payload_json", "{}"))
    return data


def _life_event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["delta"] = _json(data.pop("delta_json", "{}"))
    data["payload"] = _json(data.pop("payload_json", "{}"))
    return data


def _json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _message_is_useful(message: dict[str, Any]) -> bool:
    if str(message.get("status") or "") not in {"sent", "pending"}:
        return False
    body = str(message.get("body") or "").strip()
    if not body:
        return False
    if str(message.get("message_type") or "") == "task_card":
        return False
    lowered = body.lower()
    noisy = ("legacy_task_without_agent_record", "没处理成功", "没办法实时上网")
    return not any(item in lowered for item in noisy)
