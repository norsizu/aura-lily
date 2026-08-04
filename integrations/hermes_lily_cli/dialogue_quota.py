"""Persistent fixed-window quota for real Aura dialogue turns.

The counter is intentionally independent from upstream provider quotas.  It is
stored in a tiny SQLite database so a service restart does not reset the local
allowance and concurrent device requests are counted atomically.

The first accepted turn opens a window. Later turns consume that same window;
they never move its end time. Once the window expires, the next accepted turn
opens a new full window.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any


DEFAULT_LIMIT = 50
DEFAULT_WINDOW_SECONDS = 5 * 60 * 60


class DialogueQuota:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._fallback_path: Path | None = None

    def snapshot(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        now: int | None = None,
    ) -> dict[str, Any]:
        current = max(0, int(time.time() if now is None else now))
        limit_value = _coerce_limit(limit)
        window_value = _coerce_window(window_seconds)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT window_start, used FROM dialogue_quota_state WHERE id = 1"
            ).fetchone()
        # Status polling is read-only. The next real turn opens an expired
        # window atomically at its own timestamp.
        if row is None or _window_expired(int(row[0]), current, window_value):
            return _result(
                limit=limit_value,
                used=0,
                window_start=current,
                window_seconds=window_value,
                active=False,
            )
        return _result(
            limit=limit_value,
            used=max(0, int(row[1])),
            window_start=int(row[0]),
            window_seconds=window_value,
            active=True,
        )

    def consume(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        now: int | None = None,
    ) -> dict[str, Any]:
        current = max(0, int(time.time() if now is None else now))
        limit_value = _coerce_limit(limit)
        window_value = _coerce_window(window_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT window_start, used FROM dialogue_quota_state WHERE id = 1"
                ).fetchone()
                if row is None or _window_expired(int(row[0]), current, window_value):
                    window_start = current
                    used = 0
                    active = False
                    connection.execute(
                        "INSERT INTO dialogue_quota_state(id, window_start, used) VALUES(1, ?, 0) "
                        "ON CONFLICT(id) DO UPDATE SET window_start=excluded.window_start, used=0",
                        (window_start,),
                    )
                else:
                    window_start = int(row[0])
                    used = max(0, int(row[1]))
                    active = True

                if used < limit_value:
                    used += 1
                    connection.execute(
                        "UPDATE dialogue_quota_state SET window_start = ?, used = ? WHERE id = 1",
                        (window_start, used),
                    )
                    allowed = True
                else:
                    allowed = False
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        result = _result(
            limit=limit_value,
            used=used,
            window_start=window_start,
            window_seconds=window_value,
            active=active or used > 0,
        )
        result["allowed"] = allowed
        return result

    def _connect(self) -> sqlite3.Connection:
        path = self._fallback_path or self.path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=5.0)
        except OSError:
            # Local unit tests and read-only development hosts do not have the
            # production /data mount. Keep one per-runtime fallback database so
            # the service remains usable without hiding a quota error.
            if self._fallback_path is None:
                self._fallback_path = Path(tempfile.gettempdir()) / f"aura-dialogue-quota-{id(self)}.sqlite3"
            path = self._fallback_path
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS dialogue_quota_state ("
            "id INTEGER PRIMARY KEY CHECK(id = 1), "
            "window_start INTEGER NOT NULL, "
            "used INTEGER NOT NULL"
            ")"
        )
        connection.commit()
        return connection


def _coerce_limit(value: Any) -> int:
    try:
        return max(1, min(100_000, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _coerce_window(value: Any) -> int:
    try:
        return max(60, min(7 * 24 * 60 * 60, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_SECONDS


def _window_expired(window_start: int, now: int, window_seconds: int) -> bool:
    return window_start > now or now >= window_start + window_seconds


def _result(
    *,
    limit: int,
    used: int,
    window_start: int,
    window_seconds: int,
    active: bool,
) -> dict[str, Any]:
    used_value = max(0, int(used))
    return {
        "ok": True,
        "limit": int(limit),
        "used": used_value,
        "remaining": max(0, int(limit) - used_value),
        "window_seconds": int(window_seconds),
        "window_start": int(window_start),
        "window_end": int(window_start + window_seconds),
        "reset_at": int(window_start + window_seconds),
        "active": bool(active),
    }
