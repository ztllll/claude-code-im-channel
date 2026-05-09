"""Adapter ABC — common surface area between Telegram and Discord.

Each adapter owns one bot connection (long-poll or WS) and exposes async
methods the daemon's worker can call from any thread via
``asyncio.run_coroutine_threadsafe(adapter.method(...), loop)``.

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

    platform: str           # "telegram" | "discord"
    chat_id: str            # canonical chat key (per-DM or per-channel)
    user_id: str            # canonical author id
    user_name: str          # display name, best-effort
    message_id: str         # platform message id (for reply-to / edit / react)
    text: str               # message text body, mentions stripped
    is_group: bool          # True for group chats / non-DM channels
    is_mentioned: bool      # bot @-mentioned (or replied-to)
    attachments: list[str] = field(default_factory=list)  # local file paths


OnMessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class Adapter(Protocol):
    """Common surface area exposed to the daemon."""

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
        ...

    async def send_file(self, chat_id: str, file_path: str, *, caption: str = "") -> str:
        ...

    async def set_menu(self, commands: list[tuple[str, str]]) -> None:
        """Register the command menu shown in the platform's chat UI.

        ``commands`` is a list of ``(name, description)`` tuples. Adapter
        translates to its native API (Telegram setMyCommands, etc).
        Best-effort: failures must not abort startup.
        """
        ...
