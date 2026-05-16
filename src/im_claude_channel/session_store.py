"""Persistent (platform, chat_id) → claude session_id mapping (SQLite).

Composite primary key so the same chat_id (e.g. a numeric Telegram user that
happens to collide with a Discord snowflake prefix) on different platforms
maps to independent claude sessions.

Also stores:
- Optional human-readable ``label`` per chat (set via ``/rename <label>``)
- Token usage stats per chat: most recent turn's input/cache/output tokens
  plus a running ``cumulative_cost_usd`` and the last model + its context
  window. Surfaced via ``/context``.
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
    last_input_tokens INTEGER NOT NULL DEFAULT 0,
    last_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    last_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    last_output_tokens INTEGER NOT NULL DEFAULT 0,
    cumulative_cost_usd REAL NOT NULL DEFAULT 0.0,
    last_model TEXT,
    context_window INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, chat_id)
);
CREATE TABLE IF NOT EXISTS session_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    label TEXT,
    archived_at REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    cumulative_cost_usd REAL NOT NULL DEFAULT 0.0,
    last_model TEXT
);
"""

# Columns added after v0.2.0. Each entry: (name, "TYPE [DEFAULT ...]") used
# only for the forward-migration of pre-existing DBs.
_LATE_COLUMNS = [
    ("label", "TEXT"),
    ("last_input_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("last_cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("last_cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("last_output_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cumulative_cost_usd", "REAL NOT NULL DEFAULT 0.0"),
    ("last_model", "TEXT"),
    ("context_window", "INTEGER NOT NULL DEFAULT 0"),
    ("model_override", "TEXT"),
]


class SessionStore:
    """Thread-safe single-file sqlite store. One row per (platform, chat_id)."""

    def __init__(self, state_dir: str) -> None:
        self._dir = Path(state_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "state.db"
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Forward-migrate older DBs that pre-date the late columns.
            # CREATE TABLE IF NOT EXISTS won't add columns to existing tables.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            for name, spec in _LATE_COLUMNS:
                if name not in cols:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {spec}")
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

    def archive_to_history(self, platform: str, chat_id: str) -> bool:
        """Copy current session row into session_history, then reset.

        Returns True if a session was archived, False if there was nothing
        to archive (no session_id set yet).
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """SELECT session_id, label, message_count, cumulative_cost_usd, last_model
                   FROM sessions WHERE platform = ? AND chat_id = ?""",
                (platform, chat_id),
            ).fetchone()
            if not row or not row[0]:
                return False
            session_id, label, msg_count, cost, model = row
            conn.execute(
                """INSERT INTO session_history
                   (platform, chat_id, session_id, label, archived_at, message_count,
                    cumulative_cost_usd, last_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, chat_id, session_id, label, time.time(),
                 msg_count, cost, model),
            )
            conn.execute(
                "DELETE FROM sessions WHERE platform = ? AND chat_id = ?",
                (platform, chat_id),
            )
            conn.commit()
        return True

    def list_history(self, platform: str, chat_id: str) -> list[dict]:
        """Return archived sessions for this chat, newest first."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT id, session_id, label, archived_at, message_count,
                          cumulative_cost_usd, last_model
                   FROM session_history
                   WHERE platform = ? AND chat_id = ?
                   ORDER BY archived_at DESC""",
                (platform, chat_id),
            ).fetchall()
        return [
            {
                "id": r[0], "session_id": r[1], "label": r[2],
                "archived_at": r[3], "message_count": r[4],
                "cumulative_cost_usd": float(r[5] or 0.0), "last_model": r[6],
            }
            for r in rows
        ]

    def restore_from_history(
        self, platform: str, chat_id: str, query: str
    ) -> dict | None:
        """Swap the current active session for one from history.

        ``query`` is matched against label (case-insensitive substring) or
        session_id prefix. The current session (if any) is auto-archived first.
        Returns the restored history row dict, or None if no match found.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT id, session_id, label, archived_at, message_count,
                          cumulative_cost_usd, last_model
                   FROM session_history
                   WHERE platform = ? AND chat_id = ?
                   ORDER BY archived_at DESC""",
                (platform, chat_id),
            ).fetchall()

        if not rows:
            return None

        q = query.strip().lower()
        match = None
        for r in rows:
            sid, label = r[1], r[2] or ""
            if q in label.lower() or sid.startswith(q) or str(r[0]) == q:
                match = r
                break
        if match is None:
            return None

        history_id, session_id, label, archived_at, msg_count, cost, model = match

        with self._lock, self._connect() as conn:
            # Archive current active session (if any) before restoring.
            cur = conn.execute(
                """SELECT session_id, label, message_count, cumulative_cost_usd, last_model
                   FROM sessions WHERE platform = ? AND chat_id = ?""",
                (platform, chat_id),
            ).fetchone()
            if cur and cur[0]:
                conn.execute(
                    """INSERT INTO session_history
                       (platform, chat_id, session_id, label, archived_at,
                        message_count, cumulative_cost_usd, last_model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (platform, chat_id, cur[0], cur[1], time.time(),
                     cur[2], cur[3], cur[4]),
                )

            # Restore the chosen session as the active one.
            conn.execute(
                """INSERT INTO sessions (platform, chat_id, session_id, last_active_ts,
                       message_count, label, cumulative_cost_usd, last_model)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(platform, chat_id) DO UPDATE SET
                       session_id = excluded.session_id,
                       last_active_ts = excluded.last_active_ts,
                       message_count = excluded.message_count,
                       label = excluded.label,
                       cumulative_cost_usd = excluded.cumulative_cost_usd,
                       last_model = excluded.last_model""",
                (platform, chat_id, session_id, time.time(),
                 msg_count, label, cost, model),
            )

            # Remove from history (it's now the active session).
            conn.execute(
                "DELETE FROM session_history WHERE id = ?", (history_id,)
            )
            conn.commit()

        return {
            "id": history_id, "session_id": session_id, "label": label,
            "archived_at": archived_at, "message_count": msg_count,
            "cumulative_cost_usd": float(cost or 0.0), "last_model": model,
        }

    def swap_session(self, platform: str, chat_id: str, new_session_id: str) -> None:
        """Replace the session_id (used by /compact) without touching label or
        cumulative_cost_usd. The last_* token counters are zeroed because they
        belong to the *previous* session — the next ``record_usage`` call will
        repopulate them for the new session.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET session_id = ?,
                    last_active_ts = ?,
                    last_input_tokens = 0,
                    last_cache_read_tokens = 0,
                    last_cache_creation_tokens = 0,
                    last_output_tokens = 0,
                    last_model = NULL,
                    context_window = 0
                WHERE platform = ? AND chat_id = ?
                """,
                (new_session_id, now, platform, chat_id),
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

    def set_model_override(self, platform: str, chat_id: str, model: str | None) -> None:
        """Pin or clear a per-chat model override (used by /model <name>)."""
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (platform, chat_id, session_id, last_active_ts, message_count, model_override)
                VALUES (?, ?, '', ?, 0, ?)
                ON CONFLICT(platform, chat_id) DO UPDATE SET model_override = excluded.model_override
                """,
                (platform, chat_id, now, model),
            )
            conn.commit()

    def get_model_override(self, platform: str, chat_id: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT model_override FROM sessions WHERE platform = ? AND chat_id = ?",
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

    def record_usage(
        self,
        platform: str,
        chat_id: str,
        *,
        model_usage: dict,
        total_cost_usd: float = 0.0,
    ) -> None:
        """Snapshot the latest turn's token usage + add to cumulative cost.

        ``model_usage`` is the ``modelUsage`` dict from a claude result event
        ({model_name: {inputTokens, outputTokens, cacheReadInputTokens,
        cacheCreationInputTokens, contextWindow, costUSD, ...}, ...}). If
        multiple models were used in the turn (rare — typically a synthesis
        model + the main model), we pick the one with the most input tokens.
        """
        if not model_usage:
            return
        best = max(model_usage.keys(), key=lambda m: model_usage[m].get("inputTokens", 0) or 0)
        md = model_usage[best] or {}
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET last_input_tokens = ?,
                    last_cache_read_tokens = ?,
                    last_cache_creation_tokens = ?,
                    last_output_tokens = ?,
                    cumulative_cost_usd = cumulative_cost_usd + ?,
                    last_model = ?,
                    context_window = ?
                WHERE platform = ? AND chat_id = ?
                """,
                (
                    md.get("inputTokens", 0) or 0,
                    md.get("cacheReadInputTokens", 0) or 0,
                    md.get("cacheCreationInputTokens", 0) or 0,
                    md.get("outputTokens", 0) or 0,
                    float(total_cost_usd or md.get("costUSD", 0.0) or 0.0),
                    best,
                    md.get("contextWindow", 0) or 0,
                    platform,
                    chat_id,
                ),
            )
            conn.commit()

    def get_usage(self, platform: str, chat_id: str) -> dict | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT last_input_tokens, last_cache_read_tokens, last_cache_creation_tokens,
                       last_output_tokens, cumulative_cost_usd, last_model, context_window,
                       message_count, label
                FROM sessions WHERE platform = ? AND chat_id = ?
                """,
                (platform, chat_id),
            ).fetchone()
        if not row:
            return None
        return {
            "last_input_tokens": row[0] or 0,
            "last_cache_read_tokens": row[1] or 0,
            "last_cache_creation_tokens": row[2] or 0,
            "last_output_tokens": row[3] or 0,
            "cumulative_cost_usd": float(row[4] or 0.0),
            "last_model": row[5],
            "context_window": row[6] or 0,
            "message_count": row[7] or 0,
            "label": row[8],
        }

    def archive_idle(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE last_active_ts < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
