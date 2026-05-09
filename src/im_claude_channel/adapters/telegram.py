"""Telegram adapter — aiogram long-polling.

Holds one bot connection, dispatches inbound messages to the daemon's worker,
and exposes outbound send/edit/file-upload methods. aiogram's polling loop is
async-native and reconnects on its own; if the network drops we just keep
retrying.

The daemon (server.py) is responsible for access control and routing. This
adapter only normalises platform shape into IncomingMessage.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile, Message

from .base import IncomingMessage, OnMessageHandler

log = logging.getLogger(__name__)


class TelegramAdapter:
    platform = "telegram"

    def __init__(self, bot_token: str, *, inbound_dir: str) -> None:
        self._token = bot_token
        self._inbound_dir = Path(inbound_dir).expanduser()
        self._inbound_dir.mkdir(parents=True, exist_ok=True)
        self._bot: Bot | None = None
        self._dp: Dispatcher | None = None
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._stop = asyncio.Event()

    async def start(self, on_message: OnMessageHandler) -> None:
        # Bot constructed lazily so we get a fresh aiohttp session per start().
        self._bot = Bot(
            token=self._token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self._dp = Dispatcher()

        me = await self._bot.get_me()
        self._bot_id = me.id
        self._bot_username = me.username
        log.info("telegram adapter ready as @%s (id=%s)", self._bot_username, self._bot_id)

        @self._dp.message()
        async def _handle(msg: Message) -> None:
            try:
                imsg = await self._normalise(msg)
            except Exception:  # noqa: BLE001
                log.exception("telegram: failed to normalise message id=%s", msg.message_id)
                return
            if imsg is None:
                return
            try:
                await on_message(imsg)
            except Exception:  # noqa: BLE001
                log.exception(
                    "telegram: on_message handler raised for message_id=%s", msg.message_id
                )

        # Keep pending updates so messages that arrived during a daemon
        # restart get processed (telegram's offset cursor advances only when
        # we ACK, so this naturally bridges short outages without replaying
        # the full bot history).
        await self._dp.start_polling(
            self._bot,
            handle_signals=False,
            allowed_updates=["message"],
            drop_pending_updates=False,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._dp is not None:
            await self._dp.stop_polling()
        if self._bot is not None:
            await self._bot.session.close()

    async def send_message(
        self, chat_id: str, text: str, *, reply_to: str | None = None
    ) -> str:
        assert self._bot is not None
        async def _call(parse_mode_override=...):
            kwargs = dict(
                chat_id=int(chat_id),
                text=text,
                reply_to_message_id=int(reply_to) if reply_to else None,
                disable_web_page_preview=True,
            )
            if parse_mode_override is not ...:
                kwargs["parse_mode"] = parse_mode_override
            return await self._bot.send_message(**kwargs)
        try:
            sent = await self._with_retry(_call)
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e).lower():
                # Retry as plain text — claude's reply contained Markdown that
                # Telegram couldn't render. Better to deliver raw than drop it.
                sent = await self._with_retry(lambda: _call(None))
            else:
                raise
        return str(sent.message_id)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        assert self._bot is not None
        async def _call(parse_mode_override=...):
            kwargs = dict(
                text=text,
                chat_id=int(chat_id),
                message_id=int(message_id),
                disable_web_page_preview=True,
            )
            if parse_mode_override is not ...:
                kwargs["parse_mode"] = parse_mode_override
            return await self._bot.edit_message_text(**kwargs)
        try:
            await self._with_retry(_call)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                return
            if "can't parse entities" in msg:
                await self._with_retry(lambda: _call(None))
                return
            raise

    async def send_file(self, chat_id: str, file_path: str, *, caption: str = "") -> str:
        assert self._bot is not None
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(file_path)

        ext = path.suffix.lower()
        is_image = ext in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        cap = caption or None

        if is_image:
            sent = await self._with_retry(
                lambda: self._bot.send_photo(
                    chat_id=int(chat_id), photo=FSInputFile(str(path)), caption=cap
                )
            )
        else:
            sent = await self._with_retry(
                lambda: self._bot.send_document(
                    chat_id=int(chat_id), document=FSInputFile(str(path)), caption=cap
                )
            )
        return str(sent.message_id)

    async def _normalise(self, msg: Message) -> IncomingMessage | None:
        """Convert aiogram Message to platform-neutral IncomingMessage."""
        if msg.from_user is None:
            return None
        # Ignore our own bot's messages.
        if msg.from_user.id == self._bot_id:
            return None

        chat_type = msg.chat.type
        is_group = chat_type in (ChatType.GROUP, ChatType.SUPERGROUP)
        text = msg.text or msg.caption or ""

        is_mentioned = self._is_bot_mentioned(msg, text)
        text = self._strip_bot_mention(text)

        attachments: list[str] = []
        try:
            attachments = await self._download_attachments(msg)
        except Exception:  # noqa: BLE001
            log.exception("telegram: attachment download failed for msg=%s", msg.message_id)

        # Skip if nothing useful (e.g. sticker / voice we don't handle yet).
        if not text and not attachments:
            return None

        return IncomingMessage(
            platform=self.platform,
            chat_id=str(msg.chat.id),
            user_id=str(msg.from_user.id),
            user_name=(msg.from_user.username or msg.from_user.full_name or "")[:64],
            message_id=str(msg.message_id),
            text=text.strip(),
            is_group=is_group,
            is_mentioned=is_mentioned,
            attachments=attachments,
        )

    def _is_bot_mentioned(self, msg: Message, text: str) -> bool:
        if msg.chat.type == ChatType.PRIVATE:
            # In DMs every message is "for the bot" already.
            return True
        # Reply to one of the bot's messages counts as a mention.
        if msg.reply_to_message and msg.reply_to_message.from_user \
                and msg.reply_to_message.from_user.id == self._bot_id:
            return True
        if not self._bot_username:
            return False
        needle = f"@{self._bot_username}"
        return needle.lower() in (text or "").lower()

    def _strip_bot_mention(self, text: str) -> str:
        if not self._bot_username or not text:
            return text
        return text.replace(f"@{self._bot_username}", "").strip()

    async def _download_attachments(self, msg: Message) -> list[str]:
        """Pull photos / documents into local disk; return absolute paths."""
        assert self._bot is not None
        out: list[str] = []
        save_dir = self._inbound_dir / str(msg.message_id)

        async def _fetch(file_id: str, suggested_name: str) -> str | None:
            try:
                tg_file = await self._bot.get_file(file_id)
            except TelegramBadRequest as e:
                log.warning("telegram: get_file failed (%s)", e)
                return None
            save_dir.mkdir(parents=True, exist_ok=True)
            dest = save_dir / suggested_name
            await self._bot.download_file(tg_file.file_path, destination=str(dest))
            return str(dest)

        if msg.photo:
            # photo is an array of progressively larger renditions; take the largest.
            biggest = msg.photo[-1]
            name = f"photo_{biggest.file_unique_id}.jpg"
            p = await _fetch(biggest.file_id, name)
            if p:
                out.append(p)

        if msg.document:
            doc = msg.document
            name = doc.file_name or f"document_{doc.file_unique_id}"
            # Sanitise filename — telegram lets users send arbitrary filenames.
            name = os.path.basename(name).replace("/", "_") or f"document_{doc.file_unique_id}"
            p = await _fetch(doc.file_id, name)
            if p:
                out.append(p)

        return out

    async def _with_retry(self, factory):
        """Call ``factory()`` and re-issue it on TelegramRetryAfter.

        ``factory`` must return a fresh coroutine each call — we cannot await
        the same coroutine twice.
        """
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return await factory()
            except TelegramRetryAfter as e:
                last_exc = e
                wait = float(e.retry_after) + 0.5
                log.warning(
                    "telegram rate-limited; sleeping %.1fs (attempt %d)",
                    wait, attempt + 1,
                )
                await asyncio.sleep(wait)
        # Out of retries — surface the last RetryAfter so the caller can decide.
        assert last_exc is not None
        raise last_exc
