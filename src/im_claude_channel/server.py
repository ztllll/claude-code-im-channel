"""Daemon main: bot adapters + per-message claude workers + progress card.

Inbound flow:

    bot adapter (asyncio)
      → on_message(IncomingMessage)
      → access check
      → per-(platform, chat_id) lock (so two fast messages serialise instead
        of racing for session_id)
      → ThreadPoolExecutor.submit(_process)
            ↓ (worker thread)
        claude_runner.run_stream(prompt, resume_session_id, on_event=...)
            ↓ (each event)
        on_event(ev) → asyncio.run_coroutine_threadsafe(
                          adapter.edit_message(progress card),
                          loop)   # fire-and-forget
            ↓ (final result)
        deliver final reply via adapter.edit_message + send_message for
        chunked / overflow text and adapter.send_file for [ATTACH:] markers
        sessions.upsert(platform, chat_id, new_session_id)

The worker thread blocks on the claude subprocess; the asyncio loop stays
responsive because every UI call is scheduled back onto it via
``run_coroutine_threadsafe``. We never await blocking calls on the loop itself.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from .access import AccessControl
from .claude_runner import ClaudeRunError, run_stream as claude_run_stream
from .commands import (
    MENU as COMMAND_MENU,
    CancelRegistry,
    dispatch as dispatch_command,
    parse as parse_command,
    rewrite_for_claude,
)
from .config import Config
from .session_store import SessionStore

if TYPE_CHECKING:
    from .adapters.base import Adapter, IncomingMessage

log = logging.getLogger(__name__)


def _hydrate_claude_env(env_path: str = "~/.claude/.env") -> None:
    """Pull ANTHROPIC_* vars from ~/.claude/.env if not already set.

    The claude CLI on a typical install reads env from ~/.claude/.env via a
    bashrc shim. When we spawn it from a daemon (no bashrc), we wire those
    vars in ourselves so sub2api endpoints / API tokens are honored.
    """
    p = Path(env_path).expanduser()
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lstrip("export").strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------


# Telegram caps message text at 4096 chars; Discord at 2000. Pick the smaller
# common ceiling so a single chunker works for both.
_TEXT_MAX = 1900
_THINKING_PLACEHOLDER = "🤔 *正在思考…*"

# Edits during a tool-heavy turn would burn the bot's API quota; throttle.
_CARD_EDIT_MIN_INTERVAL = 1.5
_PROGRESS_PREVIEW_MAX = 280

_ATTACH_RE = re.compile(r"\[ATTACH:\s*([^\]\s][^\]]*)\]")
_BRIDGE_HINT_OUTBOUND_FILES = (
    "\n\n[bridge note] To send a file or image back to the user, write "
    "[ATTACH:/abs/path] anywhere in your reply. The bridge will strip the "
    "marker and send the file as a separate message; the rest of your text "
    "is delivered normally.\n"
)


def _chunk(text: str, max_chars: int = _TEXT_MAX) -> list[str]:
    """Split a long reply on paragraph boundaries first, char boundaries as fallback."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for para in text.split("\n"):
        while len(para) > max_chars:
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        added = len(para) + (1 if cur else 0)
        if cur_len + added > max_chars and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [para], len(para)
        else:
            cur.append(para)
            cur_len += added
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _extract_attachments_from_reply(text: str) -> tuple[str, list[str]]:
    paths: list[str] = []
    for m in _ATTACH_RE.finditer(text):
        p = m.group(1).strip()
        if Path(p).is_file():
            paths.append(p)
        else:
            log.warning("[ATTACH] target does not exist: %s", p)
    cleaned = _ATTACH_RE.sub("", text).strip()
    return cleaned, paths


class _SeenEventCache:
    """Tiny thread-safe LRU keyed by (platform, message_id).

    Telegram redelivers updates if our long-poll loop crashes mid-handle
    without ACKing; Discord shouldn't but bugs happen. Either way, dedupe.
    """

    def __init__(self, maxsize: int = 1024) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def check_and_add(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return True
            self._d[key] = None
            if len(self._d) > self._max:
                self._d.popitem(last=False)
            return False


def _summarise_tool(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return (inp.get("command") or "")[:80]
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return inp.get("file_path") or inp.get("notebook_path") or ""
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} 项 todo"
    if name in ("Task", "Agent"):
        return (inp.get("description") or "")[:80]
    return ""


# ---------------------------------------------------------------------------
# Worker (sync, runs in ThreadPoolExecutor)
# ---------------------------------------------------------------------------


def _process_message(
    *,
    msg,                   # IncomingMessage
    cfg: Config,
    sessions: SessionStore,
    adapter,               # Adapter
    loop: asyncio.AbstractEventLoop,
    cancels: CancelRegistry,
) -> None:
    """Worker body — runs in a thread. Pushes UI updates back to the asyncio loop."""

    chat_id = msg.chat_id
    platform = msg.platform

    # Helper to schedule an async adapter call from this thread.
    def _run_async(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # Step 1: post the placeholder progress card.
    progress_msg_id: str | None = None
    try:
        fut = _run_async(
            adapter.send_message(chat_id, _THINKING_PLACEHOLDER, reply_to=msg.message_id)
        )
        progress_msg_id = fut.result(timeout=15)
    except Exception:  # noqa: BLE001
        log.warning("worker: placeholder send failed (continuing without progress card)",
                    exc_info=True)

    progress = {
        "action": "🚀 启动中...",
        "tool_calls": 0,
        "preview": "",
        "last_edit_ts": 0.0,
    }

    def _render_card(elapsed: float, *, final: bool = False) -> str:
        head = "✅ 完成" if final else progress["action"]
        lines = [f"*{head}*"]
        if progress["tool_calls"]:
            lines.append(f"🔧 已调用工具 {progress['tool_calls']} 次")
        if progress["preview"]:
            lines.append("")
            lines.append(progress["preview"])
        lines.append("")
        lines.append(f"⏱ 已运行 {int(elapsed)}s")
        return "\n".join(lines)

    def _maybe_edit(elapsed: float, *, force: bool = False) -> None:
        if not progress_msg_id:
            return
        now = time.monotonic()
        if not force and now - progress["last_edit_ts"] < _CARD_EDIT_MIN_INTERVAL:
            return
        progress["last_edit_ts"] = now
        # Fire-and-forget; UI failures must not poison the run.
        text = _render_card(elapsed)
        future = _run_async(adapter.edit_message(chat_id, progress_msg_id, text))

        def _done(f):
            exc = f.exception()
            if exc is not None:
                log.warning("progress edit failed (non-fatal): %s", exc)

        future.add_done_callback(_done)

    def on_event(ev: dict) -> None:
        elapsed = ev.get("_elapsed", 0)
        ev_type = ev.get("type")
        sub = ev.get("subtype")

        if ev_type == "system" and sub == "init":
            progress["action"] = "🤔 思考中..."
            log.info("stream: init session=%s", ev.get("session_id"))
            _maybe_edit(elapsed, force=True)
            return

        if ev_type == "heartbeat":
            if int(elapsed) % 30 == 0:
                log.info("stream: heartbeat elapsed=%ss", int(elapsed))
            _maybe_edit(elapsed)
            return

        if ev_type == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    progress["tool_calls"] += 1
                    name = block.get("name", "?")
                    summary = _summarise_tool(name, block.get("input") or {})
                    log.info(
                        "stream: tool_use #%d %s %s elapsed=%ss",
                        progress["tool_calls"], name, summary[:120], int(elapsed),
                    )
                    label = f"🔧 [{name}] {summary}" if summary else f"🔧 [{name}]"
                    progress["action"] = label[:_PROGRESS_PREVIEW_MAX]
                    _maybe_edit(elapsed)
                elif btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        preview_lines = text.split("\n")[-3:]
                        progress["preview"] = ("\n".join(preview_lines))[:_PROGRESS_PREVIEW_MAX]
                        progress["action"] = "💭 思考中..."
                        log.info(
                            "stream: assistant text len=%d elapsed=%ss",
                            len(text), int(elapsed),
                        )
                        _maybe_edit(elapsed)
                elif btype == "thinking":
                    progress["action"] = "💭 思考中..."
                    _maybe_edit(elapsed)
            return

        if ev_type == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    progress["action"] = "✅ 工具结果已返回，继续..."
                    log.info("stream: tool_result elapsed=%ss", int(elapsed))
                    _maybe_edit(elapsed)
                    break
            return

        if ev_type == "result":
            log.info(
                "stream: result elapsed=%ss is_error=%s len=%d",
                int(elapsed), ev.get("is_error"), len((ev.get("result") or "")),
            )
            # Don't force-edit here. Step 3 (worker post-stream) is about to
            # replace this message with the full reply; firing an edit now
            # races with that and can land *after* the final edit on
            # Telegram's side, leaving the user looking at the progress card
            # forever. Let step 3 own the final state.
            return

    # Step 2: build prompt and run claude.
    # Rewrite menu underscore commands back to claude's hyphenated form
    # (e.g. /security_review → /security-review) so claude's slash-command
    # / skill layer recognises them.
    prompt = rewrite_for_claude(msg.text)
    if msg.attachments:
        attach_lines = "\n".join(f"- file: {p}" for p in msg.attachments)
        prompt = (
            (prompt + "\n\n" if prompt else "")
            + "[user attached the following file(s); use Read tool to inspect them if useful]\n"
            + attach_lines
        )
    full_prompt = prompt + _BRIDGE_HINT_OUTBOUND_FILES

    cancel_event = cancels.arm(platform, chat_id)
    try:
        resume = sessions.get(platform, chat_id)
        # Apply per-chat model override if set (strips any existing --model flag
        # from extra_args to avoid duplicates, then appends the override).
        runtime_claude_cfg = cfg.claude
        model_override = sessions.get_model_override(platform, chat_id)
        if model_override:
            import dataclasses
            stripped = [
                a for i, a in enumerate(cfg.claude.extra_args)
                if a != "--model" and (i == 0 or cfg.claude.extra_args[i - 1] != "--model")
            ]
            runtime_claude_cfg = dataclasses.replace(
                cfg.claude, extra_args=stripped + ["--model", model_override]
            )
        log.info(
            "worker: claude run platform=%s chat=%s prompt-len=%d resume=%s model=%s",
            platform, chat_id, len(prompt), resume, model_override or "(global)",
        )
        result = claude_run_stream(
            full_prompt,
            runtime_claude_cfg,
            resume_session_id=resume,
            on_event=on_event,
            cancel_check=cancel_event.is_set,
        )
        if result.session_id:
            sessions.upsert(platform, chat_id, result.session_id)
        # Snapshot per-turn token usage + accumulate cost so /context can
        # surface real numbers (claude's own /context only sees a fresh
        # session, not our resumed long-running one).
        raw = result.raw or {}
        sessions.record_usage(
            platform,
            chat_id,
            model_usage=raw.get("modelUsage") or {},
            total_cost_usd=float(raw.get("total_cost_usd") or 0.0),
        )
        reply = result.text
        if result.is_error:
            reply = f"⚠️ Claude 报错\n\n{reply}"
        log.info(
            "worker: done session=%s reply-len=%d tool_calls=%d is_error=%s",
            result.session_id, len(reply), progress["tool_calls"], result.is_error,
        )
    except ClaudeRunError as e:
        reply = f"⚠️ 桥接出错：{e}"
        log.exception("worker: claude_runner failed")
    except Exception as e:  # noqa: BLE001
        reply = f"⚠️ 桥接出错：{type(e).__name__}: {e}"
        log.exception("worker: unexpected error")
    finally:
        cancels.disarm(platform, chat_id)

    # Step 3: deliver text part (with [ATTACH:] markers stripped) + files.
    text_part, attachments = _extract_attachments_from_reply(reply)
    if attachments:
        log.info("worker: claude requested %d attachment(s)", len(attachments))

    chunks = _chunk(text_part) if text_part else [""]

    delivered = 0
    for i, chunk in enumerate(chunks, start=1):
        body = chunk if len(chunks) == 1 else f"（{i}/{len(chunks)}）\n\n{chunk}"
        body = body or "（无文字内容）"
        try:
            if i == 1 and progress_msg_id:
                # Replace the placeholder with the first chunk.
                _run_async(adapter.edit_message(chat_id, progress_msg_id, body)).result(timeout=30)
            else:
                _run_async(
                    adapter.send_message(chat_id, body, reply_to=msg.message_id)
                ).result(timeout=30)
            delivered += 1
        except Exception as e:  # noqa: BLE001
            log.warning("worker: edit/send chunk %d/%d failed (%s); fallback to send",
                        i, len(chunks), e)
            try:
                _run_async(
                    adapter.send_message(chat_id, body, reply_to=msg.message_id)
                ).result(timeout=30)
                delivered += 1
            except Exception:  # noqa: BLE001
                log.exception("worker: fallback send chunk %d/%d failed",
                              i, len(chunks))

    for path in attachments:
        try:
            _run_async(adapter.send_file(chat_id, path)).result(timeout=120)
        except Exception:  # noqa: BLE001
            log.exception("worker: attach %s failed", path)

    log.info(
        "worker: delivered %d/%d chunks + %d attachments total-chars=%d",
        delivered, len(chunks), len(attachments), len(reply),
    )


# ---------------------------------------------------------------------------
# /compact worker — summarise current session then bootstrap a new one with
# the summary as its first message. Two blocking claude calls, so runs in the
# same thread pool as _process_message.
# ---------------------------------------------------------------------------


_COMPACT_SUMMARY_PROMPT_TEMPLATE = (
    "<<system_compaction>>\n"
    "You are about to be compacted. Summarise the entire conversation above "
    "as concisely as possible while preserving everything needed to continue "
    "working in a fresh session. Include:\n"
    "  - The user's overarching goal(s) and current focus area\n"
    "  - Key technical decisions made, with brief reasoning\n"
    "  - Open questions, blockers, and pending TODOs\n"
    "  - Important file paths, function names, identifiers, URLs the user has been working with\n"
    "  - Any commitments the assistant made (\"I will…\", \"next I'll…\")\n"
    "  - The most recent meaningful state of the work\n"
    "{focus_section}"
    "\nDo NOT include greetings, meta-commentary, or 'here is the summary'. "
    "Return ONLY the summary content, in whatever format (prose, bullets, "
    "code blocks) best preserves the information."
)


def _build_summary_prompt(focus_hint: str) -> str:
    focus_section = (
        f"\nUser-supplied focus for this compact: {focus_hint}\n"
        if focus_hint
        else ""
    )
    return _COMPACT_SUMMARY_PROMPT_TEMPLATE.format(focus_section=focus_section)


def _bootstrap_prompt(summary_text: str) -> str:
    return (
        "[Previous conversation context — compacted summary]\n\n"
        f"{summary_text}\n\n"
        "[End of summary]\n\n"
        "Please acknowledge this context briefly (one sentence) so I know "
        "you've loaded it; then wait for the user's next message."
    )


def _process_compact(
    *,
    msg,                   # IncomingMessage
    cfg: Config,
    sessions: SessionStore,
    adapter,               # Adapter
    loop: asyncio.AbstractEventLoop,
    cancels: CancelRegistry,
    focus_hint: str,
) -> None:
    """Two-step compact: claude summarises → fresh claude session seeded with summary."""

    chat_id = msg.chat_id
    platform = msg.platform

    def _run_async(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop)

    # Step 0: post placeholder.
    progress_msg_id: str | None = None
    try:
        fut = _run_async(
            adapter.send_message(chat_id, "🔧 准备 compact…", reply_to=msg.message_id)
        )
        progress_msg_id = fut.result(timeout=15)
    except Exception:  # noqa: BLE001
        log.warning("compact: placeholder send failed", exc_info=True)

    def _edit(text: str) -> None:
        if not progress_msg_id:
            return
        try:
            _run_async(adapter.edit_message(chat_id, progress_msg_id, text)).result(timeout=30)
        except Exception:  # noqa: BLE001
            log.warning("compact: edit failed (non-fatal)", exc_info=True)

    old_sid = sessions.get(platform, chat_id)
    if not old_sid:
        _edit("ℹ️ 本 chat 没有 session，无需 compact。直接发消息开始即可。")
        return

    u_before = sessions.get_usage(platform, chat_id) or {}
    old_ctx = (
        u_before.get("last_input_tokens", 0)
        + u_before.get("last_cache_read_tokens", 0)
        + u_before.get("last_cache_creation_tokens", 0)
    )

    # Step 1: summarise the existing session.
    pretty_old = f"{old_ctx:,}" if old_ctx else "(未知 — 升级前没有 usage 记录)"
    _edit(f"🤔 [1/2] 让模型总结现有上下文 ({pretty_old} tokens)…")
    log.info(
        "compact: starting summary turn platform=%s chat=%s old_sid=%s old_ctx=%d focus=%r",
        platform, chat_id, old_sid, old_ctx, focus_hint,
    )

    try:
        summary_result = claude_run_stream(
            _build_summary_prompt(focus_hint),
            cfg.claude,
            resume_session_id=old_sid,
        )
    except ClaudeRunError as e:
        log.exception("compact: summary turn failed")
        _edit(f"⚠️ compact 第 1 步失败：{e}\n\n旧 session 不动，可继续对话。")
        return
    if summary_result.is_error or not summary_result.text:
        _edit("⚠️ compact 第 1 步：模型返回空或错误。旧 session 不动，可继续对话。")
        return

    summary_text = summary_result.text
    raw1 = summary_result.raw or {}
    cost1 = float(raw1.get("total_cost_usd") or 0.0)

    # Step 2: spawn a new session with the summary as the bootstrap prompt.
    _edit(f"🔧 [2/2] 开新 session，灌入 {len(summary_text):,} 字摘要…")
    log.info(
        "compact: starting bootstrap turn summary-len=%d cost1=$%.4f",
        len(summary_text), cost1,
    )

    try:
        bootstrap_result = claude_run_stream(
            _bootstrap_prompt(summary_text),
            cfg.claude,
            resume_session_id=None,
        )
    except ClaudeRunError as e:
        log.exception("compact: bootstrap turn failed")
        _edit(
            f"⚠️ compact 第 2 步失败：{e}\n\n"
            f"摘要拿到了但没能落到新 session。旧 session 不动，可继续对话。"
        )
        return

    new_sid = bootstrap_result.session_id
    if not new_sid:
        _edit("⚠️ compact 第 2 步：拿不到新 session_id。旧 session 不动。")
        return

    raw2 = bootstrap_result.raw or {}
    cost2 = float(raw2.get("total_cost_usd") or 0.0)

    # Atomic swap and record the bootstrap turn's usage as the new baseline.
    sessions.swap_session(platform, chat_id, new_sid)
    sessions.record_usage(
        platform, chat_id,
        model_usage=raw2.get("modelUsage") or {},
        total_cost_usd=cost2,
    )

    u_after = sessions.get_usage(platform, chat_id) or {}
    new_ctx = (
        u_after.get("last_input_tokens", 0)
        + u_after.get("last_cache_read_tokens", 0)
        + u_after.get("last_cache_creation_tokens", 0)
    )

    cost_total = cost1 + cost2
    saved = old_ctx - new_ctx
    if old_ctx > 0 and saved > 0:
        saved_line = f"节省 `{saved:,}` tokens (`{saved / old_ctx * 100:.1f}%`)"
    elif old_ctx > 0:
        saved_line = f"新上下文反而大了 `{-saved:,}` tokens（摘要太冗长？）"
    else:
        saved_line = "(旧上下文未知，无法计算节省)"

    log.info(
        "compact: done old_sid=%s → new_sid=%s old_ctx=%d new_ctx=%d cost=$%.4f",
        old_sid, new_sid, old_ctx, new_ctx, cost_total,
    )

    _edit(
        f"✅ Compact 完成\n\n"
        f"旧 session: `{old_sid[:8]}…`\n"
        f"新 session: `{new_sid[:8]}…`\n\n"
        f"上下文: `{old_ctx:,}` → `{new_ctx:,}` tokens\n"
        f"{saved_line}\n"
        f"摘要长度: `{len(summary_text):,}` 字\n"
        f"本次 compact 花费: `${cost_total:.4f}` "
        f"(总结 `${cost1:.4f}` + bootstrap `${cost2:.4f}`)\n\n"
        f"下条消息会在新 session 里继续。"
    )


# ---------------------------------------------------------------------------
# Daemon entry
# ---------------------------------------------------------------------------


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.sessions = SessionStore(cfg.session.state_dir)
        self.seen = _SeenEventCache()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="claude-worker")
        # One lock per (platform, chat_id), so two fast messages from the same
        # chat serialise (otherwise both resume the *previous* session_id and
        # only one resulting session is recorded).
        self._chat_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._access: dict[str, AccessControl] = {}
        self._adapters: list = []
        self._started_at = time.monotonic()
        self.cancels = CancelRegistry()

    def _chat_lock(self, platform: str, chat_id: str) -> asyncio.Lock:
        key = (platform, chat_id)
        lock = self._chat_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[key] = lock
        return lock

    async def _make_adapters(self) -> list:
        adapters = []
        inbound_dir = os.path.join(self.cfg.session.state_dir, "inbound")
        if self.cfg.telegram.enabled:
            from .adapters.telegram import TelegramAdapter
            tg = TelegramAdapter(
                self.cfg.telegram.bot_token,
                inbound_dir=inbound_dir,
                menu=COMMAND_MENU,
            )
            self._access["telegram"] = AccessControl(
                self.cfg.telegram.allowed_user_ids,
                self.cfg.telegram.group_only_when_mentioned,
            )
            adapters.append(tg)
        if self.cfg.discord.enabled:
            try:
                from .adapters.discord import DiscordAdapter
            except ImportError as e:
                log.error("discord enabled but adapter import failed: %s", e)
            else:
                dc = DiscordAdapter(self.cfg.discord.bot_token, inbound_dir=inbound_dir)
                self._access["discord"] = AccessControl(
                    self.cfg.discord.allowed_user_ids,
                    self.cfg.discord.group_only_when_mentioned,
                )
                adapters.append(dc)
        if self.cfg.feishu.enabled:
            try:
                from .adapters.feishu import FeishuAdapter
            except ImportError as e:
                log.error("feishu enabled but adapter import failed: %s", e)
            else:
                fs = FeishuAdapter(
                    self.cfg.feishu.app_id,
                    self.cfg.feishu.app_secret,
                    inbound_dir=inbound_dir,
                    log_level=self.cfg.feishu.log_level,
                )
                self._access["feishu"] = AccessControl(
                    self.cfg.feishu.allowed_user_ids,
                    self.cfg.feishu.group_only_when_mentioned,
                )
                adapters.append(fs)
        return adapters

    async def _on_message(self, msg) -> None:
        # 1) dedupe
        seen_key = f"{msg.platform}:{msg.message_id}"
        if self.seen.check_and_add(seen_key):
            log.info("dedupe: skipping repeated message %s", seen_key)
            return

        # 2) access
        access = self._access.get(msg.platform)
        if access is None:
            log.warning("no access control configured for platform %s; rejecting",
                        msg.platform)
            return
        if not access.allow(msg.user_id):
            log.warning("access denied platform=%s user=%s", msg.platform, msg.user_id)
            return
        if msg.is_group and access.group_only_when_mentioned() and not msg.is_mentioned:
            return

        log.info(
            "incoming: platform=%s chat=%s user=%s msg_id=%s text-len=%d attachments=%d",
            msg.platform, msg.chat_id, msg.user_id, msg.message_id,
            len(msg.text), len(msg.attachments),
        )

        # 3) slash-command interception — daemon-handled commands skip claude.
        adapter = next((a for a in self._adapters if a.platform == msg.platform), None)
        if adapter is None:
            log.error("no adapter for platform %s", msg.platform)
            return

        parsed = parse_command(msg.text)
        if parsed is not None:
            name, args = parsed

            # /compact needs the worker pool (two blocking claude calls), so
            # it's intercepted BEFORE dispatch_command (which is synchronous
            # and returns CommandResult).
            if name == "compact":
                log.info("command: %s/%s ran /compact (worker, focus=%r)",
                         msg.platform, msg.chat_id, args)
                loop = asyncio.get_running_loop()
                lock = self._chat_lock(msg.platform, msg.chat_id)

                async def _compact_runner():
                    async with lock:
                        fut = loop.run_in_executor(
                            self.executor,
                            functools.partial(
                                _process_compact,
                                msg=msg,
                                cfg=self.cfg,
                                sessions=self.sessions,
                                adapter=adapter,
                                loop=loop,
                                cancels=self.cancels,
                                focus_hint=args,
                            ),
                        )
                        try:
                            await fut
                        except Exception:  # noqa: BLE001
                            log.exception("compact worker raised unhandled error")

                task = asyncio.create_task(_compact_runner())
                task.add_done_callback(lambda _t: None)
                return

            extra = self.cfg.claude.extra_args
            try:
                global_model: str | None = extra[extra.index("--model") + 1]
            except (ValueError, IndexError):
                global_model = None
            result = dispatch_command(
                name,
                sessions=self.sessions,
                platform=msg.platform,
                chat_id=msg.chat_id,
                daemon_started_at=self._started_at,
                cancel_registry=self.cancels,
                log_file=self.cfg.logging.file,
                args=args,
                global_model=global_model,
            )
            if result is not None:
                log.info("command: %s/%s ran /%s (daemon-handled)",
                         msg.platform, msg.chat_id, name)
                try:
                    await adapter.send_message(
                        msg.chat_id, result.text, reply_to=msg.message_id
                    )
                except Exception:  # noqa: BLE001
                    log.exception("command: failed to deliver reply for /%s", name)
                return
            # Not daemon-handled → fall through; claude will see the slash
            # command as the prompt and route it through its own skill layer.
            log.info("command: /%s passed through to claude", name)

        # 4) per-chat serialisation + offload to worker
        loop = asyncio.get_running_loop()
        lock = self._chat_lock(msg.platform, msg.chat_id)

        async def _runner():
            async with lock:
                # Run the blocking worker in a thread pool. We can't just
                # asyncio.to_thread because we need a futures.Future to await
                # without exceptions surfacing as crashes — so wrap in shield.
                fut = loop.run_in_executor(
                    self.executor,
                    functools.partial(
                        _process_message,
                        msg=msg,
                        cfg=self.cfg,
                        sessions=self.sessions,
                        adapter=adapter,
                        loop=loop,
                        cancels=self.cancels,
                    ),
                )
                try:
                    await fut
                except Exception:  # noqa: BLE001
                    log.exception("worker raised unhandled error")

        # We don't await this — the bot adapter's handler must return
        # promptly so polling continues. Track the task so it isn't GC'd.
        task = asyncio.create_task(_runner())
        task.add_done_callback(lambda _t: None)

    async def run(self) -> None:
        self._adapters = await self._make_adapters()
        if not self._adapters:
            raise RuntimeError("no adapters enabled — set telegram.enabled or discord.enabled")

        async def _run_adapter(a):
            log.info("starting adapter: %s", a.platform)
            try:
                await a.start(self._on_message)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("adapter %s crashed; will restart in 5s", a.platform)
                await asyncio.sleep(5)
                raise

        # If any adapter crashes, all crash → systemd restarts the whole
        # daemon. Simpler than per-adapter resilience and safer for state.
        await asyncio.gather(*(_run_adapter(a) for a in self._adapters))


def run(config_path: str = "config.yaml") -> int:
    _hydrate_claude_env()
    cfg = Config.load(config_path)
    cfg.expand_paths()

    Path(cfg.session.state_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(cfg.logging.file)],
    )
    log.info("starting im-claude-channel daemon")

    daemon = Daemon(cfg)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    return 0
