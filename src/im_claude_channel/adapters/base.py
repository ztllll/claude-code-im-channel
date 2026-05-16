"""Adapter ABC — common surface area for Telegram / Discord / Feishu.

Each adapter owns one bot connection (long-poll, gateway WebSocket, or
long-conn) and exposes async methods the daemon's worker can call from any
thread via ``asyncio.run_coroutine_threadsafe(adapter.method(...), loop)``.

The daemon hands the adapter an ``on_message`` async callback during start();
the adapter invokes it once per inbound user message that should reach claude.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class IncomingMessage:
    """Platform-neutral inbound message.

    Adapters translate platform-specific event objects into this shape before
    calling on_message. The daemon never touches platform SDK types directly.
    """

    platform: str           # "telegram" | "discord" | "feishu"
    chat_id: str            # canonical chat key (per-DM or per-channel/group)
    user_id: str            # canonical author id (numeric for tg/discord, ou_xxx for feishu)
    user_name: str          # display name, best-effort
    message_id: str         # platform message id (for reply-to / edit / react)
    text: str               # message text body, mentions stripped
    is_group: bool          # True for group chats / non-DM channels
    is_mentioned: bool      # bot @-mentioned (or replied-to)
    attachments: list[str] = field(default_factory=list)  # local file paths


OnMessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class Adapter(Protocol):
    """Common surface area exposed to the daemon.

    All adapters speak in plain text — platform-specific rendering (Feishu
    cards, Telegram MarkdownV2, Discord embeds) is the adapter's problem.
    """

    platform: str

    async def start(self, on_message: OnMessageHandler) -> None:
        """Connect bot and begin dispatching messages. Returns when shut down."""
        ...

    async def stop(self) -> None:
        ...

    async def send_message(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str:
        """Post a message; returns the platform message_id of the sent message."""
        ...

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        """Replace the text of a previously-sent bot message.

        Feishu can only edit ``interactive`` cards, so the FeishuAdapter wraps
        outbound text in a minimal card transparently. Telegram/Discord edit
        the raw text directly.
        """
        ...

    async def send_file(self, chat_id: str, file_path: str, *, caption: str = "") -> str:
        ...

    async def set_menu(self, commands: list[tuple[str, str]]) -> None:
        """Register the command menu shown in the platform's chat UI.

        ``commands`` is a list of ``(name, description)`` tuples. Adapter
        translates to its native API:

        - Telegram: ``setMyCommands`` across default/private/group scopes.
        - Discord: application slash commands (no-op for v0.2 — handled by
          inbound text parsing instead).
        - Feishu: no platform-level command menu API; expose ``/menu`` for
          the user instead. This call is a no-op.

        Best-effort: failures must not abort startup.
        """
        ...
