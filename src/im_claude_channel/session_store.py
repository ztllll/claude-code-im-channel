"""Persistent (platform, chat_id) → claude session_id mapping (SQLite).

Composite primary key so the same chat_id (e.g. a numeric Telegram user that
happens to collide with a Discord snowflake prefix) on different platforms
maps to independent claude sessions.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    last_active_ts REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, chat_id)
);
"""


class SessionStore:
    """Thread-safe single-file sqlite store. One row per (platform, chat_id)."""

    def __init__(self, state_dir: str) -> None:
        self._dir = Path(state_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "state.db"
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), check_same_thread=False)

    def get(self, platform: str, chat_id: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE platform = ? AND chat_id = ?",
                (platform, chat_id),
            ).fetchone()
        return row[0] if row else None

    def upsert(self, platform: str, chat_id: str, session_id: str) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (platform, chat_id, session_id, last_active_ts, message_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(platform, chat_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    last_active_ts = excluded.last_active_ts,
                    message_count = message_count + 1
                """,
                (platform, chat_id, session_id, now),
            )
            conn.commit()

    def reset(self, platform: str, chat_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE platform = ? AND chat_id = ?",
                (platform, chat_id),
            )
            conn.commit()

    def list_all(self) -> list[tuple[str, str, str, float, int]]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT platform, chat_id, session_id, last_active_ts, message_count FROM sessions"
                )
            )

    def archive_idle(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE last_active_ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
