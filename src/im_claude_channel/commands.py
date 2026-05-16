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


# Telegram requires command names match ^[a-z0-9_]{1,32}$, so commands that
# claude knows by a hyphenated name are written here with underscores and
# rewritten on the way out (see ``rewrite_for_claude``).
#
# Order = display order in the menu. Daemon-handled (intercepted) ones go
# first because they actually work in this mode; then pass-through skills
# that produce useful output; then TUI-only commands marked [TUI] so the
# user knows they only get a text echo.
MENU: list[tuple[str, str]] = [
    # —— daemon 控制（拦截，绝对能用） ——
    ("new", "开新对话（清空当前 chat 的 session）"),
    ("cancel", "取消当前 chat 正在跑的 claude turn"),
    ("status", "查看 daemon 与本 chat 的会话状态"),
    ("sessions", "列出所有 chat 的活动 session"),
    ("log", "显示 daemon 最近 30 行日志"),
    ("menu", "显示分组命令菜单（/help 的同义词）"),
    ("help", "列出可用命令"),
    ("rename", "给当前 chat 起人类可读的标签（/rename 飞书会话；/rename 留空清除）"),
    ("context", "当前 chat 的真实上下文用量（模型、token、缓存命中、累计成本）"),

    # —— skill 透传（远程能用，会真的产生输出） ——
    ("review", "审查当前分支或 PR"),
    ("security_review", "安全审查（扫漏洞）"),
    ("simplify", "代码复用与质量审查"),
    ("ultrareview", "多 agent 云端审查（云端跑 + 计费）"),
    ("init", "为当前仓库生成 CLAUDE.md"),
    ("autofix_pr", "自动修复当前 PR 的 CI 问题"),
    ("recap", "一句话总结当前会话进展"),
    ("doctor", "诊断 claude 安装与凭证状态"),
    ("release_notes", "查看 claude 最新版本变更"),
    ("btw", "不打断主线，顺手问一个题外话"),
    ("branch", "从当前对话分一个旁支问题"),
    ("advisor", "让更强模型在关键节点给建议"),
    ("memory", "查看/编辑 claude 长期 memory 文件"),
    ("plan", "进入 plan 模式或查看当前 session 计划"),
    ("todo", "查看/操作当前 session 待办"),
    ("claude_api", "Anthropic SDK 编码 / 模型迁移助手"),
    ("update_config", "改 .claude/settings.json（权限/钩子/环境）"),
    ("fewer_permission_prompts", "扫历史把常用命令加进 allowlist"),
    ("keybindings_help", "自定义 ~/.claude/keybindings.json"),
    ("agent_reach", "配置 Twitter/Reddit/YouTube/GitHub 等平台访问"),

    # —— 当前 session 状态调整（TUI 限定，远程基本只能拿到文字解释） ——
    ("clear", "[TUI] 清空当前 session 上下文（-p 没意义）"),
    ("compact", "压缩当前 chat（让模型总结 → 开新 session 灌入摘要，省 token）"),
    ("focus", "[TUI] 切换 focus 视图"),
    ("color", "[TUI] 改 prompt bar 颜色"),
    ("effort", "[TUI] 切 effort 级别（low/medium/high/xhigh/max）"),
    ("fast", "[TUI] 切 fast 模式（仅 Opus 4.6）"),
    ("model", "切换当前 chat 的模型（/model sonnet|opus|haiku 或完整 ID；/model 查看；/model clear 恢复全局）"),
    ("agents", "[TUI] 管理 agent 配置"),
    ("add_dir", "[TUI] 给当前 session 加工作目录"),
    ("copy", "[TUI] 复制最近回复到剪贴板"),
    ("diff", "[TUI] 查看每轮代码差异"),

    # —— 设置面板（TUI 限定，远程只能问"这是什么"） ——
    ("config", "[TUI] 打开 config 面板"),
    ("permissions", "[TUI] 管理工具权限规则"),
    ("keybindings", "[TUI] 编辑快捷键配置"),
    ("privacy_settings", "[TUI] 隐私设置"),
    ("extra_usage", "[TUI] 配置额度耗尽处理"),
    ("hooks", "[TUI] 查看 hook 配置"),

    # —— 插件 / MCP / IDE ——
    ("mcp", "[TUI] 管理 MCP 服务"),
    ("plugin", "[TUI] 管理 plugin"),
    ("reload_plugins", "重新加载当前 session 的 plugin"),
    ("ide", "[TUI] 管理 IDE 集成"),
    ("install_github_app", "[慎用] 为仓库装 Claude GitHub Actions"),
    ("install_slack_app", "[慎用] 为账号装 Claude Slack app"),

    # —— 账户 / 设备 ——
    ("login", "[慎用] 登录 Anthropic 账号（远程操作会冲掉当前凭证）"),
    ("logout", "[慎用] 登出（会破坏 daemon 持续运行）"),
    ("passes", "邀请朋友领免费一周 Claude Code"),
    ("mobile", "[TUI] 显示 Claude mobile app 二维码"),
    ("powerup", "通过教程快速上手 Claude 功能"),
    ("radio", "[TUI] 听 Claude FM lo-fi 电台"),
    ("chrome", "[beta] Claude in Chrome 设置"),
    ("feedback", "给 Claude Code 团队提反馈"),

    # —— 会话操作 ——
    ("export", "导出当前对话到文件或剪贴板"),
    ("resume", "恢复归档会话（/resume 查看列表；/resume 标签名 切换；配合 /rename + /new 使用）"),
    ("exit", "[TUI] 退出 CLI（在 daemon 里无意义）"),

    # —— 远程控制 / 调度 ——
    ("remote_control", "启用 remote control 连接"),
    ("remote_env", "配置 remote env（teleport sessions）"),
    ("loop", "[受限] 安排周期任务（daemon 一次性 turn 模型可能不完整支持）"),
    ("schedule", "安排远程定时 agent（routine）"),

    # —— hyperframes 视频 / 动画相关 skill ——
    ("hyperframes", "HyperFrames HTML 视频合成"),
    ("hyperframes_media", "HyperFrames TTS / 转写 / 抠图预处理"),
    ("hyperframes_cli", "HyperFrames CLI 工具集"),
]

# Daemon intercepts these — everything else is forwarded to claude.
_DAEMON_HANDLED = {"new", "cancel", "status", "sessions", "log", "help", "menu", "rename", "context", "model", "resume"}


# Curated grouped menu shown by /menu. Kept as a constant so we can iterate
# on copy without rebuilding the whole list every call.
_MENU_TEXT = """*🛠 命令菜单*

*daemon 控制（秒回）*
`/new` 开新对话（归档旧会话）· `/resume` 恢复归档 · `/cancel` 取消 turn · `/status` 状态 · `/sessions` 全部会话 · `/log` 日志尾 · `/help` 命令清单 · `/model` 切换模型

*🔍 代码审查（透传 claude）*
`/review` 审分支 · `/security-review` 安全审查 · `/simplify` 代码质量 · `/ultrareview` 多 agent 云端审

*📝 仓库 / PR*
`/init` 生成 CLAUDE.md · `/autofix-pr` 修 PR · `/branch` 旁支问题 · `/recap` 会话总结

*🔧 诊断 / 信息*
`/doctor` 诊断安装 · `/release-notes` 版本变更 · `/advisor` 强模型建议 · `/btw` 顺手问题

*📚 记忆 / 计划*
`/memory` 编辑记忆 · `/plan` 进入 plan 模式 · `/todo` 待办

*⚙ 配置 / 助手*
`/update-config` 改 settings.json · `/fewer-permission-prompts` 加 allowlist · `/keybindings-help` 改快捷键 · `/agent-reach` 配 Twitter 等 · `/claude-api` SDK 编码助手

—
`[TUI]` 系列（/clear /permissions 等 30+）远程基本只能拿到文字解释。
完整 69 项：直接打 `/` 看 Telegram 自动补全。"""

# Some commands claude knows by a hyphenated name; menu entries use
# underscores (telegram requirement) and we rewrite on the way out so
# claude's slash-command layer recognises them.
_HYPHEN_FORMS: dict[str, str] = {
    "security_review": "security-review",
    "release_notes": "release-notes",
    "autofix_pr": "autofix-pr",
    "install_github_app": "install-github-app",
    "install_slack_app": "install-slack-app",
    "remote_control": "remote-control",
    "remote_env": "remote-env",
    "privacy_settings": "privacy-settings",
    "extra_usage": "extra-usage",
    "fewer_permission_prompts": "fewer-permission-prompts",
    "update_config": "update-config",
    "keybindings_help": "keybindings-help",
    "claude_api": "claude-api",
    "agent_reach": "agent-reach",
    "add_dir": "add-dir",
    "hyperframes_media": "hyperframes-media",
    "hyperframes_cli": "hyperframes-cli",
}


def rewrite_for_claude(text: str) -> str:
    """Rewrite a leading ``/snake_case`` command to the form claude expects.

    The menu must use underscores (telegram constraint) but several built-in
    claude commands and skills are registered under their hyphenated form.
    We rewrite the leading token only — everything after the first space
    (typed args) is preserved verbatim.
    """
    if not text or not text.startswith("/"):
        return text
    head, sep, rest = text.partition(" ")
    name = head[1:]
    bot_at = ""
    if "@" in name:
        name, _, bot_at = name.partition("@")
        bot_at = "@" + bot_at
    canonical = name.lower()
    hyphen = _HYPHEN_FORMS.get(canonical)
    if hyphen is None:
        return text
    return "/" + hyphen + bot_at + (sep + rest if sep else "")


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
    args: str = "",
    global_model: str | None = None,
) -> CommandResult | None:
    """Handle a daemon-side command. Returns None to fall through to claude.

    ``args`` is the remainder of the message after the leading ``/cmd`` token
    (already split by ``parse()``). Currently only ``/rename`` uses it.
    """

    if name not in _DAEMON_HANDLED:
        return None

    if name == "new":
        label = sessions.get_label(platform, chat_id)
        archived = sessions.archive_to_history(platform, chat_id)
        if archived:
            label_part = f" `{label}`" if label else ""
            return CommandResult(
                text=f"✅ 已开新对话。旧会话{label_part}已归档，用 `/resume {label or '标签/ID前缀'}` 可随时恢复。\n"
                     "下一条消息会启动 fresh session。"
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
        history = sessions.list_history(platform, chat_id)
        lines = ["*🗂 全部活动 session*", ""]
        if not rows:
            lines.append("(暂无活动会话)")
        else:
            for plat, cid, sid, ts, n, label in sorted(rows):
                marker = " ← 你" if (plat == platform and cid == chat_id) else ""
                label_part = f" [{label}]" if label else ""
                sid_part = f"`{sid[:8]}…`" if sid else "`(无 session)`"
                lines.append(f"`{plat}` / `{cid}`{label_part} → {sid_part} ({n} 条){marker}")
            lines.append(f"\n共 {len(rows)} 个活动")
        if history:
            lines += ["", "*📦 本 chat 历史归档（可 /resume 恢复）*", ""]
            for h in history:
                import datetime
                dt = datetime.datetime.fromtimestamp(h["archived_at"]).strftime("%m-%d %H:%M")
                lbl = f"`{h['label']}`" if h["label"] else "(无标签)"
                lines.append(
                    f"{lbl} — `{h['session_id'][:8]}…` {h['message_count']} 条  归档于 {dt}"
                )
            lines.append(f"\n共 {len(history)} 条归档")
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
        label = my_row[5] if my_row else None
        label_part = f" [{label}]" if label else ""

        lines = [
            "*📊 daemon 状态*",
            f"daemon 已运行 `{_fmt_uptime(daemon_started_at)}`",
            "",
            f"*本 chat (`{platform}` / `{chat_id}`){label_part}*",
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

    if name == "menu":
        # Curated, grouped quick reference — friendlier than the flat /help
        # dump. The full 69-entry list is always available via Telegram's
        # native `/` autocomplete (we wrote it via setMyCommands).
        return CommandResult(text=_MENU_TEXT)

    if name == "context":
        u = sessions.get_usage(platform, chat_id)
        if not u or not u.get("last_model"):
            return CommandResult(
                text="ℹ️ 当前 chat 还没有完整的 claude turn，无法统计上下文。"
                     "先发一条普通消息触发一次即可。"
            )
        label_part = f" [{u['label']}]" if u.get("label") else ""
        total_ctx = (
            u["last_input_tokens"]
            + u["last_cache_read_tokens"]
            + u["last_cache_creation_tokens"]
        )
        cw = u["context_window"] or 0
        pct = (total_ctx / cw * 100) if cw else 0.0
        cache_hit_pct = (
            (u["last_cache_read_tokens"] / total_ctx * 100) if total_ctx else 0.0
        )
        cw_disp = f"{cw:,}" if cw else "(未知)"
        return CommandResult(
            text=(
                f"*📊 上下文使用{label_part}*\n"
                f"模型: `{u['last_model']}`\n"
                f"上下文窗口: `{cw_disp}`\n\n"
                f"*最近一轮*\n"
                f"输入 (billed): `{u['last_input_tokens']:,}` tokens\n"
                f"缓存命中: `{u['last_cache_read_tokens']:,}` tokens "
                f"(`{cache_hit_pct:.0f}%`)\n"
                f"缓存写入: `{u['last_cache_creation_tokens']:,}` tokens\n"
                f"输出: `{u['last_output_tokens']:,}` tokens\n"
                f"已用上下文: `{total_ctx:,}` / `{cw_disp}`"
                + (f" (`{pct:.1f}%`)" if cw else "")
                + "\n\n"
                f"*累计 (本 chat {u['message_count']} 轮)*\n"
                f"总成本: `${u['cumulative_cost_usd']:.4f}`"
            )
        )

    if name == "resume":
        history = sessions.list_history(platform, chat_id)
        if not args.strip():
            # No arg: show history list with hint
            if not history:
                return CommandResult(
                    text="ℹ️ 本 chat 没有归档会话。\n"
                         "先用 `/rename 任务名` 命名当前会话，再用 `/new` 开新对话，"
                         "之后就可以用 `/resume 任务名` 回来。"
                )
            import datetime
            lines = ["*📦 可恢复的归档会话*", "", "用 `/resume 标签名` 或 `/resume ID前缀` 恢复：", ""]
            for h in history:
                dt = datetime.datetime.fromtimestamp(h["archived_at"]).strftime("%m-%d %H:%M")
                lbl = f"`{h['label']}`" if h["label"] else f"`{h['session_id'][:8]}…`"
                lines.append(
                    f"{lbl}  {h['message_count']} 条  归档于 {dt}  "
                    f"${h['cumulative_cost_usd']:.4f}"
                )
            return CommandResult(text="\n".join(lines))

        restored = sessions.restore_from_history(platform, chat_id, args.strip())
        if restored is None:
            return CommandResult(
                text=f"⚠️ 找不到匹配 `{args.strip()}` 的归档会话。\n"
                     "发 `/resume` 查看所有可恢复的会话。"
            )
        label_part = f"`{restored['label']}`" if restored["label"] else f"`{restored['session_id'][:8]}…`"
        return CommandResult(
            text=f"✅ 已恢复会话 {label_part}（{restored['message_count']} 条记录）。\n"
                 "下一条消息会 resume 该 session 继续对话。"
        )

    if name == "model":
        _ALIASES = {
            "opus":   "claude-opus-4-7",
            "sonnet": "claude-sonnet-4-6",
            "haiku":  "claude-haiku-4-5-20251001",
        }
        _KNOWN = [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]
        arg = args.strip().lower()
        current_override = sessions.get_model_override(platform, chat_id)
        effective = current_override or global_model or "(claude CLI 默认)"

        if not arg or arg == "list":
            lines = [
                "*🤖 模型设置*",
                f"当前生效: `{effective}`",
            ]
            if current_override:
                lines.append(f"本 chat 固定: `{current_override}`")
            if global_model:
                lines.append(f"全局配置: `{global_model}`")
            lines += [
                "",
                "*常用模型*",
                "`opus`   → `claude-opus-4-7`（最强，贵）",
                "`sonnet` → `claude-sonnet-4-6`（默认推荐）",
                "`haiku`  → `claude-haiku-4-5-20251001`（最快最省）",
                "",
                "用法: `/model sonnet` 或 `/model claude-opus-4-7`",
                "恢复: `/model clear`",
            ]
            return CommandResult(text="\n".join(lines))

        if arg in ("clear", "default", "none", "reset"):
            sessions.set_model_override(platform, chat_id, None)
            fallback = global_model or "(claude CLI 默认)"
            return CommandResult(
                text=f"✅ 已清除本 chat 模型固定，恢复使用 `{fallback}`。"
            )

        model_id = _ALIASES.get(arg, args.strip())
        sessions.set_model_override(platform, chat_id, model_id)
        note = "（下一个 turn 生效）" if sessions.get(platform, chat_id) else ""
        return CommandResult(
            text=f"✅ 本 chat 模型已固定为 `{model_id}`{note}。\n"
                 f"发 `/model clear` 恢复全局设置。"
        )

    if name == "rename":
        # /rename <label>   set the label for THIS chat
        # /rename           clear it
        # The label is stored daemon-side in SQLite (sessions.label), independent
        # of claude's own session_id, so it survives /new and claude restarts.
        # Intentionally intercepted: claude's TUI /rename doesn't work in -p
        # mode (CLI returns a synthetic "isn't available" refusal), and the
        # daemon-side label is what most users actually want anyway.
        label = args.strip()
        if not label:
            prev = sessions.get_label(platform, chat_id)
            sessions.set_label(platform, chat_id, None)
            if prev:
                return CommandResult(text=f"✅ 已清除标签（原标签：`{prev}`）。")
            return CommandResult(text="ℹ️ 当前 chat 没有标签。用 `/rename 你的标签` 设置一个。")
        if len(label) > 64:
            return CommandResult(text="⚠️ 标签太长（最多 64 字符）。")
        sessions.set_label(platform, chat_id, label)
        return CommandResult(
            text=f"✅ 本 chat 已命名为 `{label}`。/status 和 /sessions 都会显示这个标签。"
        )

    return None
