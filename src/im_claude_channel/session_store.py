"""Persistent (platform, chat_id) → claude session_id mapping (SQLite).

Composite primary key so the same chat_id (e.g. a numeric Telegram user that
happens to collide with a Discord snowflake prefix) on different platforms
maps to independent claude sessions.

Also stores an optional human-readable ``label`` per chat (set via the
``/rename <label>`` daemon command) so ``/sessions`` and ``/status`` can show
"telegram会话" instead of just ``8570415194``.
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
    label TEXT,
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
            # Forward-migrate older DBs that pre-date the label column.
            # CREATE TABLE IF NOT EXISTS won't add the column to existing tables.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "label" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN label TEXT")
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
        """Drop the session_id but keep the label (renaming survives /new)."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE platform = ? AND chat_id = ?",
                (platform, chat_id),
            )
            conn.commit()

    def set_label(self, platform: str, chat_id: str, label: str | None) -> None:
        """Set/clear the human-readable label for a chat.

        Creates a row with no session_id if the chat hasn't talked yet — that
        way ``/rename`` can pre-label a chat before its first message.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            # UPSERT preserving session_id/message_count if a row exists.
            conn.execute(
                """
                INSERT INTO sessions (platform, chat_id, session_id, last_active_ts, message_count, label)
                VALUES (?, ?, '', ?, 0, ?)
                ON CONFLICT(platform, chat_id) DO UPDATE SET label = excluded.label
                """,
                (platform, chat_id, now, label),
            )
            conn.commit()

    def get_label(self, platform: str, chat_id: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT label FROM sessions WHERE platform = ? AND chat_id = ?",
                (platform, chat_id),
            ).fetchone()
        return row[0] if row else None

    def list_all(self) -> list[tuple[str, str, str, float, int, str | None]]:
        with self._lock, self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT platform, chat_id, session_id, last_active_ts, message_count, label FROM sessions"
                )
            )

    def archive_idle(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE last_active_ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
