"""飞书 (Lark) adapter — lark-oapi long-conn for inbound, REST for outbound.

Architecture notes:

- Inbound: ``lark.ws.Client`` is sync and runs its own thread pool internally.
  The event handler we register is invoked from a lark worker thread, not the
  asyncio loop. We translate the SDK event into ``IncomingMessage`` *in that
  thread* (including downloading any attachments, sync) and then bounce the
  ``on_message`` coroutine onto the daemon's loop via
  ``asyncio.run_coroutine_threadsafe``.

- Outbound: lark's REST endpoints are hit directly with ``requests``. The
  adapter's async methods wrap the blocking calls with ``asyncio.to_thread``
  so we don't block the loop.

- Edit-in-place caveat: Feishu's ``PATCH /im/v1/messages/{id}`` ONLY accepts
  card-type messages (``msg_type=interactive``). Editing a ``text`` message
  returns ``230001 This message is NOT a card``. So every outbound message
  from this adapter is wrapped in a minimal interactive card with one
  markdown element — the daemon never has to care.

- Lifecycle: ``lark.ws.Client.start()`` blocks until the connection closes.
  We run it via ``asyncio.to_thread`` so the daemon's main loop stays alive.
  systemd ``Restart=always`` covers crash recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import requests

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )
except ImportError as exc:  # pragma: no cover — surfaced clearly at runtime
    raise ImportError(
        "feishu adapter requires lark-oapi. Install with: pip install 'im-claude-channel[feishu]'"
    ) from exc

from .base import IncomingMessage, OnMessageHandler

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Lark REST endpoints. We hit these directly with requests rather than
# threading through the SDK builder pattern — fewer moving parts, clearer
# errors.
# -----------------------------------------------------------------------------

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
_REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
_PATCH_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
_RESOURCE_URL = (
    "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
)
_UPLOAD_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
_UPLOAD_FILE_URL = "https://open.feishu.cn/open-apis/im/v1/files"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Feishu inserts `@_user_<n>` placeholders for @-mentions in group messages.
# Strip them so the prompt looks clean to claude.
_MENTION_RE = re.compile(r"@_user_\d+\s*")


class LarkAPIError(RuntimeError):
    """Wraps a non-zero ``code`` in a feishu open-api response."""


# -----------------------------------------------------------------------------
# Thin REST client. Token cache + JSON envelope unwrapping.
# -----------------------------------------------------------------------------


class _LarkREST:
    """Feishu open-api client used for outbound message sending + uploads."""

    def __init__(self, app_id: str, app_secret: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            r = requests.post(
                _TOKEN_URL,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise LarkAPIError(f"tenant_access_token: {d}")
            self._token = d["tenant_access_token"]
            self._token_exp = time.time() + int(d.get("expire", 7200))
            return self._token

    def _post(self, url: str, payload: dict, params: dict | None = None) -> dict:
        r = requests.post(
            url,
            params=params or {},
            json=payload,
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0:
            raise LarkAPIError(f"{url}: {d}")
        return d

    def _patch(self, url: str, payload: dict) -> dict:
        r = requests.patch(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0:
            raise LarkAPIError(f"{url}: {d}")
        return d

    @staticmethod
    def _msg_id(resp: dict) -> str:
        return (resp.get("data") or {}).get("message_id") or ""

    @staticmethod
    def text_card(markdown_text: str) -> dict:
        """Wrap markdown text in a minimal interactive card (so it can be edited)."""
        return {
            "config": {"wide_screen_mode": True, "update_multi": True},
            "elements": [
                {"tag": "markdown", "content": markdown_text or "（空）"},
            ],
        }

    def send_card(self, receive_id: str, card: dict) -> str:
        resp = self._post(
            _SEND_URL,
            params={"receive_id_type": "chat_id"},
            payload={
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        return self._msg_id(resp)

    def reply_card(self, root_message_id: str, card: dict) -> str:
        resp = self._post(
            _REPLY_URL.format(message_id=root_message_id),
            payload={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )
        return self._msg_id(resp)

    def edit_card(self, message_id: str, card: dict) -> None:
        self._patch(
            _PATCH_URL.format(message_id=message_id),
            payload={
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
        )

    def download_resource(
        self, message_id: str, file_key: str, kind: str, save_dir: str
    ) -> str:
        """Download an attachment from a message; return absolute local path. kind='image'|'file'."""
        url = _RESOURCE_URL.format(message_id=message_id, file_key=file_key)
        r = requests.get(
            url,
            params={"type": kind},
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=30,
            stream=True,
        )
        if r.status_code != 200:
            raise LarkAPIError(
                f"download_resource {kind}: HTTP {r.status_code}: {r.text[:200]}"
            )
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        # Prefer Content-Disposition filename; fall back to file_key + ext from mime.
        disp = r.headers.get("content-disposition", "")
        name = None
        for token in disp.split(";"):
            token = token.strip()
            if token.lower().startswith("filename="):
                name = token.split("=", 1)[1].strip().strip('"')
                break
        if not name:
            ext = (
                mimetypes.guess_extension(r.headers.get("content-type", "").split(";")[0])
                or ""
            )
            name = f"{file_key}{ext}"
        path = os.path.join(save_dir, name)
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
        return path

    def upload_image(self, local_path: str) -> str:
        with open(local_path, "rb") as f:
            r = requests.post(
                _UPLOAD_IMAGE_URL,
                headers={"Authorization": f"Bearer {self.token()}"},
                data={"image_type": "message"},
                files={"image": (os.path.basename(local_path), f)},
                timeout=60,
            )
        d = r.json()
        if d.get("code") != 0:
            raise LarkAPIError(f"upload_image: {d}")
        return d["data"]["image_key"]

    def upload_file(self, local_path: str) -> str:
        ext = Path(local_path).suffix.lower().lstrip(".")
        type_map = {
            "mp4": "mp4", "opus": "opus", "pdf": "pdf",
            "doc": "doc", "docx": "doc", "xls": "xls", "xlsx": "xls",
            "ppt": "ppt", "pptx": "ppt",
        }
        file_type = type_map.get(ext, "stream")
        with open(local_path, "rb") as f:
            r = requests.post(
                _UPLOAD_FILE_URL,
                headers={"Authorization": f"Bearer {self.token()}"},
                data={"file_type": file_type, "file_name": os.path.basename(local_path)},
                files={"file": (os.path.basename(local_path), f)},
                timeout=120,
            )
        d = r.json()
        if d.get("code") != 0:
            raise LarkAPIError(f"upload_file: {d}")
        return d["data"]["file_key"]

    def reply_image(self, root_message_id: str, image_key: str) -> str:
        resp = self._post(
            _REPLY_URL.format(message_id=root_message_id),
            payload={
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
            },
        )
        return self._msg_id(resp)

    def reply_file(self, root_message_id: str, file_key: str) -> str:
        resp = self._post(
            _REPLY_URL.format(message_id=root_message_id),
            payload={
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}, ensure_ascii=False),
            },
        )
        return self._msg_id(resp)


# -----------------------------------------------------------------------------
# Adapter
# -----------------------------------------------------------------------------


def _extract_text(content_json: str) -> str:
    """Feishu wraps text msgs as ``{"text":"..."}``."""
    try:
        return (json.loads(content_json or "{}").get("text") or "").strip()
    except (TypeError, ValueError):
        return ""


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def _extract_post_text(content_json: str) -> str:
    """Feishu rich-post messages: paragraphs of typed elements; concat text fragments."""
    try:
        pc = json.loads(content_json or "{}")
    except (TypeError, ValueError):
        return ""
    fragments: list[str] = []
    for paragraph in pc.get("content", []):
        if isinstance(paragraph, list):
            for el in paragraph:
                if isinstance(el, dict) and el.get("tag") == "text":
                    fragments.append(el.get("text", ""))
    return " ".join(fragments).strip()


class FeishuAdapter:
    """Adapter implementation for Feishu / Lark.

    Outbound text is wrapped in interactive cards so it can be edited
    (Feishu's PATCH only works on cards). Inbound attachments (image / file /
    rich post) are downloaded to ``inbound_dir`` and exposed as
    ``IncomingMessage.attachments``.

    The card-action handler is registered too, so the curated ``/menu`` card
    in :mod:`im_claude_channel.commands` can wire button clicks to commands.
    But since the daemon's text-path already handles every command, the
    button handler simply synthesises a fake inbound ``/cmd`` text message —
    no separate dispatch path to maintain.
    """

    platform = "feishu"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        inbound_dir: str,
        log_level: str = "INFO",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._inbound_dir = Path(inbound_dir).expanduser()
        self._inbound_dir.mkdir(parents=True, exist_ok=True)
        self._rest = _LarkREST(app_id, app_secret)
        self._cli: lark.ws.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_message: OnMessageHandler | None = None
        self._sdk_log_level = {
            "DEBUG": lark.LogLevel.DEBUG,
            "INFO": lark.LogLevel.INFO,
            "WARN": lark.LogLevel.WARNING,
            "WARNING": lark.LogLevel.WARNING,
            "ERROR": lark.LogLevel.ERROR,
            "CRITICAL": lark.LogLevel.CRITICAL,
        }.get(log_level.upper(), lark.LogLevel.INFO)

    # -------------------------------------------------------------------
    # Adapter Protocol — lifecycle
    # -------------------------------------------------------------------

    async def start(self, on_message: OnMessageHandler) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_message = on_message

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_lark_message)
            .register_p2_card_action_trigger(self._on_lark_card_action)
            .build()
        )

        self._cli = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=self._sdk_log_level,
        )
        log.info("feishu adapter connecting (long-conn) ...")
        # lark-oapi's WsClient.start() runs every coroutine on a *module-level*
        # ``loop`` captured at import time via ``asyncio.get_event_loop()``.
        # When the adapter is imported inside our running daemon, that grab
        # returns the main daemon loop — and ``loop.run_until_complete()`` from
        # a worker thread then fails with "loop already running". Fix: create a
        # fresh loop in the worker thread and force-rebind lark's module global
        # before letting it call start().
        import lark_oapi.ws.client as _lark_ws_client  # noqa: WPS433
        def _run_with_fresh_loop() -> None:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            _lark_ws_client.loop = new_loop
            self._cli.start()
        await asyncio.to_thread(_run_with_fresh_loop)
        log.info("feishu adapter stopped")

    async def stop(self) -> None:
        # lark-oapi ws.Client doesn't expose a graceful stop; rely on
        # systemd-level supervision. Marker only.
        log.info("feishu adapter stop requested (no graceful stop available)")

    # -------------------------------------------------------------------
    # Adapter Protocol — outbound
    # -------------------------------------------------------------------

    async def send_message(
        self, chat_id: str, text: str, *, reply_to: str | None = None
    ) -> str:
        """Send a message. If ``reply_to`` is given, thread under that message."""
        card = self._rest.text_card(text)
        if reply_to:
            return await asyncio.to_thread(self._rest.reply_card, reply_to, card)
        return await asyncio.to_thread(self._rest.send_card, chat_id, card)

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> None:
        card = self._rest.text_card(text)
        await asyncio.to_thread(self._rest.edit_card, message_id, card)

    async def send_file(
        self, chat_id: str, file_path: str, *, caption: str = ""
    ) -> str:
        """Upload + reply (or send) a local file. Picks image vs file by extension."""
        # Feishu requires a root message to reply to for image/file replies in
        # most contexts. Worker passes the user's message_id implicitly via the
        # caller chain. If we ever hit this without a context, prefer send-as-
        # new under the chat_id by uploading and then sending an image msg
        # type. For simplicity & symmetry with telegram, just thread under the
        # last user message — daemon passes chat_id = chat_id, but Feishu
        # 'reply' wants a message_id. We can't get that here without changing
        # the API. So we fall back to sending as a fresh chat message.
        ext = Path(file_path).suffix.lower()
        if ext in _IMAGE_EXTS:
            key = await asyncio.to_thread(self._rest.upload_image, file_path)
            resp = await asyncio.to_thread(
                self._rest._post,
                _SEND_URL,
                {
                    "receive_id": chat_id,
                    "msg_type": "image",
                    "content": json.dumps({"image_key": key}, ensure_ascii=False),
                },
                {"receive_id_type": "chat_id"},
            )
        else:
            key = await asyncio.to_thread(self._rest.upload_file, file_path)
            resp = await asyncio.to_thread(
                self._rest._post,
                _SEND_URL,
                {
                    "receive_id": chat_id,
                    "msg_type": "file",
                    "content": json.dumps({"file_key": key}, ensure_ascii=False),
                },
                {"receive_id_type": "chat_id"},
            )
        return (resp.get("data") or {}).get("message_id") or ""

    async def set_menu(self, commands: list[tuple[str, str]]) -> None:
        # Feishu has no setMyCommands equivalent for self-built apps; the
        # `/menu` skill command in the daemon prints a grouped list. No-op.
        log.info("feishu set_menu: no platform API; users can run /menu to see commands")

    # -------------------------------------------------------------------
    # Inbound handlers — run in lark worker threads, NOT the asyncio loop
    # -------------------------------------------------------------------

    def _on_lark_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Translate lark inbound event to IncomingMessage and push to the loop."""
        try:
            imsg = self._normalise(data)
        except Exception:  # noqa: BLE001
            log.exception("feishu: failed to normalise inbound event")
            return
        if imsg is None:
            return
        self._dispatch(imsg)

    def _on_lark_card_action(
        self, data: "P2CardActionTrigger"
    ) -> "P2CardActionTriggerResponse":
        """Button-click handler — synthesise a /cmd inbound message.

        The card's button value carries ``{"command": "<name>"}``; we wrap it
        as a normal inbound text message ``"/<name>"`` so it goes through the
        same daemon path as a typed slash command. That way we have one
        dispatch chain to maintain, and access control / cancellation /
        history all behave identically.
        """
        try:
            ev = data.event
            action = ev.action if ev else None
            ctx = ev.context if ev else None
            operator = ev.operator if ev else None

            cmd = ((action.value if action else None) or {}).get("command", "")
            chat_id = ctx.open_chat_id if ctx else ""
            msg_id = ctx.open_message_id if ctx else ""
            open_id = operator.open_id if operator else ""

            if not cmd or not chat_id:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": "无效按钮"}}
                )

            imsg = IncomingMessage(
                platform=self.platform,
                chat_id=chat_id,
                user_id=open_id,
                user_name=open_id,
                message_id=msg_id or f"button-{int(time.time() * 1000)}",
                text="/" + cmd,
                is_group=False,
                is_mentioned=True,
                attachments=[],
            )
            self._dispatch(imsg)
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": f"/{cmd} 已派发"}}
            )
        except Exception:  # noqa: BLE001
            log.exception("feishu: card action handler failed")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "处理失败"}}
            )

    def _normalise(self, data: "P2ImMessageReceiveV1") -> IncomingMessage | None:
        ev = data.event
        msg = ev.message
        sender = ev.sender

        chat_id = msg.chat_id
        message_id = msg.message_id
        msg_type = msg.message_type
        open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
        is_group = (msg.chat_type == "group")
        is_mentioned = bool(msg.mentions)

        if msg_type not in ("text", "image", "file", "post"):
            log.info("feishu: skip unhandled msg_type=%s", msg_type)
            return None

        text_body = ""
        if msg_type == "text":
            text_body = _strip_mentions(_extract_text(msg.content))
        elif msg_type == "post":
            text_body = _strip_mentions(_extract_post_text(msg.content))

        attachments = self._download_inbound_attachments(msg)

        if not text_body and not attachments:
            return None

        # DM (p2p) implicitly addresses the bot; otherwise rely on mention bit.
        if not is_group:
            is_mentioned = True

        return IncomingMessage(
            platform=self.platform,
            chat_id=chat_id,
            user_id=open_id,
            user_name=open_id,
            message_id=message_id,
            text=text_body,
            is_group=is_group,
            is_mentioned=is_mentioned,
            attachments=attachments,
        )

    def _download_inbound_attachments(self, msg) -> list[str]:
        """Pull image / file resources out of a Feishu message into local disk."""
        out: list[str] = []
        msg_type = msg.message_type
        content = msg.content or "{}"
        try:
            c = json.loads(content)
        except (TypeError, ValueError):
            return out

        save_subdir = os.path.join(str(self._inbound_dir), msg.message_id)

        if msg_type == "image":
            key = c.get("image_key")
            if key:
                try:
                    p = self._rest.download_resource(msg.message_id, key, "image", save_subdir)
                    out.append(p)
                except LarkAPIError as e:
                    log.error("feishu: inbound image download failed: %s", e)
        elif msg_type == "file":
            key = c.get("file_key")
            if key:
                try:
                    p = self._rest.download_resource(msg.message_id, key, "file", save_subdir)
                    out.append(p)
                except LarkAPIError as e:
                    log.error("feishu: inbound file download failed: %s", e)
        elif msg_type == "post":
            # Rich post can embed images via {tag: 'img', image_key: '...'}.
            for paragraph in c.get("content", []):
                if isinstance(paragraph, list):
                    for el in paragraph:
                        if isinstance(el, dict) and el.get("tag") == "img":
                            key = el.get("image_key")
                            if key:
                                try:
                                    p = self._rest.download_resource(
                                        msg.message_id, key, "image", save_subdir
                                    )
                                    out.append(p)
                                except LarkAPIError as e:
                                    log.error("feishu: post-image download failed: %s", e)
        return out

    def _dispatch(self, imsg: IncomingMessage) -> None:
        """Schedule on_message on the asyncio loop from a lark worker thread."""
        loop = self._loop
        on_message = self._on_message
        if loop is None or on_message is None:
            log.warning("feishu: dispatch before start()")
            return
        try:
            asyncio.run_coroutine_threadsafe(on_message(imsg), loop)
        except RuntimeError:
            log.exception("feishu: failed to schedule on_message")
