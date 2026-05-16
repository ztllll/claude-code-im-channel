"""Discord adapter — discord.py gateway client.

Mirrors the Telegram adapter shape. discord.py is async-native so no thread
bridging is needed; both inbound dispatch and outbound API calls run on the
same asyncio loop as the daemon.

Notes:

- ``message_content`` intent must be enabled for the bot in the Discord
  developer portal AND in code via ``Intents.message_content = True``;
  otherwise ``message.content`` is always empty for non-mention messages.
- Discord caps message text at 2000 chars; the daemon's chunker uses 1900 to
  share one ceiling with Telegram (4096).
- Edit-in-place: fetch the ``Message`` by id from the channel, then ``.edit``.
  We cache recent send/edit targets to avoid one extra API call per progress
  tick — see ``_message_cache``.
- Slash commands (app commands): v0.2 doesn't register them. Users type
  ``/menu``, ``/status`` etc. as normal text; the daemon's command-parser
  picks them up. Adding app-command registration is a future enhancement.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from pathlib import Path

try:
    import discord
except ImportError as exc:  # pragma: no cover — surfaced clearly at runtime
    raise ImportError(
        "discord adapter requires discord.py. "
        "Install with: pip install 'im-claude-channel[discord]'"
    ) from exc

from .base import IncomingMessage, OnMessageHandler

log = logging.getLogger(__name__)

# Discord message edit is a separate API call; cache the most recent N
# Message objects so the progress-card edit path avoids re-fetching.
_MESSAGE_CACHE_SIZE = 64


class DiscordAdapter:
    platform = "discord"

    def __init__(self, bot_token: str, *, inbound_dir: str) -> None:
        self._token = bot_token
        self._inbound_dir = Path(inbound_dir).expanduser()
        self._inbound_dir.mkdir(parents=True, exist_ok=True)

        intents = discord.Intents.default()
        intents.message_content = True  # required to read message text
        intents.messages = True
        self._client = discord.Client(intents=intents)
        self._on_message: OnMessageHandler | None = None
        self._bot_id: int | None = None
        self._message_cache: OrderedDict[str, discord.Message] = OrderedDict()

    async def start(self, on_message: OnMessageHandler) -> None:
        self._on_message = on_message

        @self._client.event
        async def on_ready() -> None:
            self._bot_id = self._client.user.id if self._client.user else None
            log.info("discord adapter ready as %s (id=%s)",
                     self._client.user, self._bot_id)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            # Ignore our own bot's messages.
            if self._bot_id is not None and message.author.id == self._bot_id:
                return
            try:
                imsg = await self._normalise(message)
            except Exception:  # noqa: BLE001
                log.exception("discord: failed to normalise message id=%s", message.id)
                return
            if imsg is None:
                return
            if self._on_message is None:
                return
            try:
                await self._on_message(imsg)
            except Exception:  # noqa: BLE001
                log.exception("discord: on_message handler raised for id=%s", message.id)

        # discord.py reconnects on its own (handles gateway resumes). If the
        # token is bad it raises LoginFailure synchronously; let it propagate
        # so systemd surfaces it.
        await self._client.start(self._token)

    async def stop(self) -> None:
        if self._client and not self._client.is_closed():
            await self._client.close()

    async def send_message(
        self, chat_id: str, text: str, *, reply_to: str | None = None
    ) -> str:
        channel = await self._get_channel(chat_id)
        kwargs = {}
        if reply_to:
            try:
                ref = discord.MessageReference(
                    message_id=int(reply_to),
                    channel_id=int(chat_id),
                    fail_if_not_exists=False,
                )
                kwargs["reference"] = ref
                kwargs["mention_author"] = False
            except (ValueError, TypeError):
                pass
        sent = await channel.send(content=text or "(空)", **kwargs)
        self._cache_message(sent)
        return str(sent.id)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        msg = self._message_cache.get(message_id)
        if msg is None:
            channel = await self._get_channel(chat_id)
            try:
                msg = await channel.fetch_message(int(message_id))
            except (discord.NotFound, discord.HTTPException) as e:
                log.warning("discord: cannot fetch message %s for edit: %s", message_id, e)
                return
            self._cache_message(msg)
        try:
            await msg.edit(content=text or "(空)")
        except discord.HTTPException as e:
            # Discord's "Invalid Form Body" sometimes fires when content is
            # identical to existing — treat as success.
            if "INVALID_FORM_BODY" in str(e).upper() or e.status == 400:
                log.debug("discord edit: %s — ignored", e)
                return
            raise

    async def send_file(
        self, chat_id: str, file_path: str, *, caption: str = ""
    ) -> str:
        channel = await self._get_channel(chat_id)
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(file_path)
        file = discord.File(str(path), filename=path.name)
        sent = await channel.send(content=caption or None, file=file)
        return str(sent.id)

    async def set_menu(self, commands: list[tuple[str, str]]) -> None:
        # v0.2 doesn't register slash commands. Users see them as text via /help.
        log.info("discord set_menu: slash commands not registered in v0.2 (use /help)")

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    async def _get_channel(self, chat_id: str):
        try:
            cid = int(chat_id)
        except (ValueError, TypeError) as e:
            raise ValueError(f"discord chat_id must be numeric: {chat_id!r}") from e
        ch = self._client.get_channel(cid)
        if ch is None:
            ch = await self._client.fetch_channel(cid)
        return ch

    def _cache_message(self, msg: discord.Message) -> None:
        key = str(msg.id)
        self._message_cache[key] = msg
        self._message_cache.move_to_end(key)
        while len(self._message_cache) > _MESSAGE_CACHE_SIZE:
            self._message_cache.popitem(last=False)

    async def _normalise(self, msg: discord.Message) -> IncomingMessage | None:
        is_dm = isinstance(msg.channel, (discord.DMChannel, discord.GroupChannel))
        is_group = not is_dm

        text = msg.content or ""
        is_mentioned = False
        if is_dm:
            is_mentioned = True
        elif self._client.user is not None:
            # @-mention or reply-to-bot counts.
            if self._client.user in (msg.mentions or []):
                is_mentioned = True
            if msg.reference and msg.reference.resolved:
                resolved = msg.reference.resolved
                if (
                    isinstance(resolved, discord.Message)
                    and resolved.author.id == self._bot_id
                ):
                    is_mentioned = True
            mention_str = f"<@{self._client.user.id}>"
            mention_nick = f"<@!{self._client.user.id}>"
            text = text.replace(mention_str, "").replace(mention_nick, "").strip()

        attachments: list[str] = []
        for att in msg.attachments or []:
            try:
                attachments.append(await self._save_attachment(msg, att))
            except Exception:  # noqa: BLE001
                log.exception("discord: attachment save failed for %s", att.filename)

        if not text and not attachments:
            return None

        return IncomingMessage(
            platform=self.platform,
            chat_id=str(msg.channel.id),
            user_id=str(msg.author.id),
            user_name=(msg.author.display_name or msg.author.name or "")[:64],
            message_id=str(msg.id),
            text=text.strip(),
            is_group=is_group,
            is_mentioned=is_mentioned,
            attachments=attachments,
        )

    async def _save_attachment(
        self, msg: discord.Message, attachment: discord.Attachment
    ) -> str:
        save_dir = self._inbound_dir / str(msg.id)
        save_dir.mkdir(parents=True, exist_ok=True)
        # Sanitise filename — users can upload anything.
        safe_name = (attachment.filename or f"file_{attachment.id}").replace("/", "_")
        dest = save_dir / safe_name
        await attachment.save(fp=dest)
        return str(dest)
