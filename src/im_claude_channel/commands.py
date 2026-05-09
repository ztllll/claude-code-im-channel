"""Slash-command menu + daemon-side dispatch.

Two flavours of slash command coexist:

- **Daemon-handled** — intercepted before claude is even spawned. We use
  these for things that operate on daemon state (session reset, status),
  not on the conversation itself. Returning a :class:`CommandResult` from
  :func:`dispatch` short-circuits the normal worker pipeline.

- **Pass-through** — anything else starting with ``/`` is treated as a
  normal prompt and forwarded to claude. Claude's own slash-command
  layer handles ``/review``, ``/init`` etc.

The :data:`MENU` list is what we register with each adapter's command-menu
API (Telegram's ``setMyCommands``, etc) so the user gets autocomplete in
the chat UI. It's a flat list of ``(name, description)`` to keep adapters
agnostic.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .session_store import SessionStore


class CancelRegistry:
    """Per-(platform, chat_id) cancel signal.

    A worker thread polls ``is_cancelled(...)`` on each iteration and aborts
    if it returns True. ``request_cancel`` flips the bit; ``arm`` resets it
    when a new turn starts.
    """

    def __init__(self) -> None:
        self._flags: dict[tuple[str, str], threading.Event] = {}
        self._lock = threading.Lock()

    def _key(self, platform: str, chat_id: str) -> tuple[str, str]:
        return (platform, chat_id)

    def arm(self, platform: str, chat_id: str) -> threading.Event:
        with self._lock:
            ev = threading.Event()
            self._flags[self._key(platform, chat_id)] = ev
            return ev

    def disarm(self, platform: str, chat_id: str) -> None:
        with self._lock:
            self._flags.pop(self._key(platform, chat_id), None)

    def request_cancel(self, platform: str, chat_id: str) -> bool:
        with self._lock:
            ev = self._flags.get(self._key(platform, chat_id))
        if ev is None:
            return False
        ev.set()
        return True

    def is_cancelled(self, platform: str, chat_id: str) -> bool:
        with self._lock:
            ev = self._flags.get(self._key(platform, chat_id))
        return ev is not None and ev.is_set()


@dataclass
class CommandResult:
    """Reply to send back; bypasses the claude worker entirely."""

    text: str


# Order matters — this is also the display order in the Telegram menu.
MENU: list[tuple[str, str]] = [
    ("new", "开新对话（清空当前 chat 的 session）"),
    ("cancel", "取消当前 chat 正在运行的 claude turn"),
    ("status", "查看 daemon 与会话状态"),
    ("sessions", "列出所有 chat 的活动 session"),
    ("log", "显示 daemon 最近 30 行日志"),
    ("help", "列出可用命令"),
    ("review", "审查当前分支（claude /review）"),
    ("init", "为当前仓库生成 CLAUDE.md（claude /init）"),
    ("security_review", "安全审查（claude /security-review）"),
    ("simplify", "代码复用与质量审查（claude /simplify）"),
    ("ultrareview", "多 agent 云端审查（claude /ultrareview）"),
]

# Daemon intercepts these — everything else is forwarded to claude.
_DAEMON_HANDLED = {"new", "cancel", "status", "sessions", "log", "help"}


def _read_tail(path: Path, max_lines: int) -> str:
    """Read the last ``max_lines`` lines of a text file without loading it whole."""
    if not path.is_file():
        return ""
    # Simple seek-from-end loop. 8KiB chunks are enough for our line lengths.
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        chunk = 8192
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= max_lines:
            read = min(chunk, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read) + data
        text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _fmt_uptime(started_at: float) -> str:
    secs = int(time.monotonic() - started_at)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s"
    h, rem = divmod(secs, 3600)
    return f"{h}h{rem // 60}m"


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse(text: str) -> tuple[str, str] | None:
    """Strip a leading ``/cmd`` (with optional ``@botname``) from message text.

    Returns ``(name, args_remainder)`` or ``None`` if the text isn't a slash
    command. Telegram autocompletes commands as ``/cmd@botname`` in groups,
    so we tolerate the suffix.
    """
    if not text or not text.startswith("/"):
        return None
    head, _, rest = text.strip().partition(" ")
    name = head[1:]
    if "@" in name:
        name = name.split("@", 1)[0]
    # Telegram canonical command names are [a-z0-9_]; we accept the dashed
    # form too because users may type /security-review out of habit.
    name = name.replace("-", "_").lower()
    return name, rest.strip()


def dispatch(
    name: str,
    *,
    sessions: SessionStore,
    platform: str,
    chat_id: str,
    daemon_started_at: float,
    cancel_registry: CancelRegistry | None = None,
    log_file: str | None = None,
) -> CommandResult | None:
    """Handle a daemon-side command. Returns None to fall through to claude."""

    if name not in _DAEMON_HANDLED:
        return None

    if name == "new":
        prev = sessions.get(platform, chat_id)
        sessions.reset(platform, chat_id)
        if prev:
            return CommandResult(
                text=f"✅ 已开新对话。上一段会话 `{prev}` 已断开，下一条消息会启动 fresh session。"
            )
        return CommandResult(text="✅ 当前 chat 没有活动会话，下一条消息直接启动 fresh session。")

    if name == "cancel":
        if cancel_registry is None:
            return CommandResult(text="⚠️ daemon 未配置 cancel registry。")
        cancelled = cancel_registry.request_cancel(platform, chat_id)
        if cancelled:
            return CommandResult(
                text="🛑 已请求取消当前 turn。subprocess 会在下一次循环检查（≤5s）时被 kill，"
                     "你会收到一条 `cancelled by /cancel` 的报错回复。"
            )
        return CommandResult(text="ℹ️ 当前 chat 没有正在运行的 claude turn。")

    if name == "sessions":
        rows = sessions.list_all()
        if not rows:
            return CommandResult(text="(空)")
        lines = ["*🗂 全部活动 session*", ""]
        for plat, cid, sid, ts, n in sorted(rows):
            marker = " ← 你" if (plat == platform and cid == chat_id) else ""
            lines.append(f"`{plat}` / `{cid}` → `{sid[:8]}…` ({n} 条){marker}")
        lines.append("")
        lines.append(f"共 {len(rows)} 个")
        return CommandResult(text="\n".join(lines))

    if name == "log":
        if not log_file:
            return CommandResult(text="⚠️ daemon 未配置 log file path。")
        try:
            tail = _read_tail(Path(log_file).expanduser(), 30)
        except OSError as e:
            return CommandResult(text=f"⚠️ 读 log 失败：{e}")
        if not tail.strip():
            return CommandResult(text="(log 为空)")
        # Strip ANSI just in case; truncate to fit Telegram's 4096 cap.
        body = tail[-3500:]
        return CommandResult(text=f"```\n{body}\n```")

    if name == "status":
        sid = sessions.get(platform, chat_id)
        rows = sessions.list_all()
        my_row = next((r for r in rows if r[0] == platform and r[1] == chat_id), None)
        msg_count = my_row[4] if my_row else 0
        last_ts = my_row[3] if my_row else None

        lines = [
            "*📊 daemon 状态*",
            f"daemon 已运行 `{_fmt_uptime(daemon_started_at)}`",
            "",
            f"*本 chat (`{platform}` / `{chat_id}`)*",
            f"session: `{sid or '(无 — 下条消息开新会话)'}`",
            f"已发消息数: {msg_count}",
        ]
        if last_ts:
            lines.append(f"上次活动: {_fmt_ts(last_ts)}")

        lines.append("")
        lines.append(f"全局会话总数: {len(rows)}")
        return CommandResult(text="\n".join(lines))

    if name == "help":
        lines = ["*🛠 可用命令*", ""]
        for cmd_name, desc in MENU:
            display = "/" + cmd_name.replace("_", "-")
            lines.append(f"`{display}` — {desc}")
        lines.append("")
        lines.append("其他任何文本会作为 prompt 发给 claude。")
        return CommandResult(text="\n".join(lines))

    return None
