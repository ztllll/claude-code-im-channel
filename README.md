# claude-code-im-channel

把 **Telegram / Discord / 飞书（Lark）** 桥接到 Claude Code CLI 会话，作为
独立的 systemd 守护进程跑——**不依赖 tmux、不依赖 MCP `--channels` 插件、
不受 firstParty 闸限制**。任何能跑 `claude -p` 的环境都能用，包括
sub2api / OneAPI / Bedrock / Vertex 中转。

---

## 为什么要造这个轮子

Claude Code 官方的 Telegram / Discord 通道插件是 `claude` 二进制通过
`--channels` 机制启动的 MCP server。这套设计有两个硬伤：

1. Bot 连接寄生在 `claude` 进程里——`claude` 崩了、被杀了、退出了，bot 也
   一起消失，入向消息直接丢。
2. `--channels` 被 Anthropic 内部 `firstParty()` 闸锁定。任何走中转端点
   (sub2api / OneAPI / new-api / Bedrock / Vertex) 的部署都用不了。

常见的绕法是把 `claude --channels plugin:telegram` 塞到 tmux 里跑，靠终端
复用器保活。但这没解决 #2，而且一旦 claude 卡在工具循环里，bot 的事件
循环也会被一起拖垮。

本项目走的是相反的路线：

- Daemon 拥有 bot 连接，把 `(platform, chat_id) → claude_session_id` 持久化
  在 SQLite 里；每收到一条消息就 spawn 一个独立的
  `claude -p --output-format stream-json --resume <session_id>` 子进程。
- 单条消息的工具循环再长也只是子进程的事，bot 一直在轮询。
- systemd `Restart=always` 兜底崩溃；`config.yaml` mtime 一变就热加载。

因为根本不碰 `--channels`，firstParty 闸跟我们没关系。能跑 `claude -p` 就
能跑这个 daemon。

---

## 功能特性

- ✅ **三平台并启** —— Telegram (aiogram) / Discord (discord.py) / 飞书 (lark-oapi)
- ✅ **按 chat 维度持久化 session** —— 每个 chat（私聊或群）有自己的 claude
  session_id，daemon 重启不丢
- ✅ **流式进度卡片** —— 单条 placeholder 消息原地编辑，实时显示
  `tool_use` / `text` / `tool_result` 事件
- ✅ **会话归档与恢复** —— `/new` 把当前会话归档到 `session_history` 表（不是删除），
  `/resume <标签>` 按标签或 session_id 前缀恢复任意归档会话，切换时自动归档当前会话
- ✅ **按 chat 锁定模型** —— `/model sonnet|opus|haiku` 为当前 chat 固定模型，
  `/model clear` 恢复全局配置；短别名映射到完整 model ID
- ✅ **完整 CLI `/` 命令支持**
  - Daemon 拦截（秒回，不过 claude）：`/new` `/cancel` `/status` `/sessions`
    `/log` `/menu` `/help` `/rename` `/context` `/model` `/resume` `/compact`
  - Skill 透传（喂给 `claude -p`）：`/review` `/init` `/security-review`
    `/simplify` `/ultrareview` 以及任何用户自定义 skill
- ✅ **双向附件**
  - 入：用户在 IM 里发的图片/文件被下载到本地，路径作为 `Read` 提示透传给 claude
  - 出：claude 在回复里写 `[ATTACH:/abs/path]` 标记，桥接剥出来并把文件发回
- ✅ **自动重启、自动错误兜底** —— `Restart=always` + `ClaudeRunError` 统一封包
- ✅ **空闲 / 总超时双闸** —— 默认 15 分钟空闲 / 2 小时总时长
- ✅ **逐平台白名单** —— 每个 adapter 独立 `allowed_user_ids`；
  `group_only_when_mentioned` 控制群聊是否需要 @bot

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/ztllll/claude-code-im-channel.git
cd claude-code-im-channel
./deploy/install.sh
```

`install.sh` 会自动建 `.venv` 并安装基础包，最后打印下一步。要把三个
adapter 的依赖一次装齐：

```bash
.venv/bin/pip install -e '.[all]'
```

或者按需选装：

```bash
.venv/bin/pip install -e '.[discord]'     # 加 discord.py
.venv/bin/pip install -e '.[feishu]'      # 加 lark-oapi
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

打开要用的平台（`enabled: true`），填好 token / app_secret，把你的 IM
账号 id 加到 `allowed_user_ids`。token 也可以走环境变量：

```bash
export TELEGRAM_BOT_TOKEN="..."
export DISCORD_BOT_TOKEN="..."
export FEISHU_APP_ID="cli_..."
export FEISHU_APP_SECRET="..."
```

环境变量优先级高于 yaml 文件。

#### 访问控制

```yaml
telegram:
  allowed_user_ids: ["123456789"]   # 留空 = 对所有人开放（群里很危险）
  group_only_when_mentioned: false  # true = 群聊必须 @bot 才响应
```

`discord` 和 `feishu` 有完全相同的字段。推荐的「个人 bot」配置：
填好 `allowed_user_ids`，`group_only_when_mentioned: false`——
bot 进任何群都只响应你，完全忽略其他人。

> **Telegram 注意**：`group_only_when_mentioned: false` 时，还需要去
> `@BotFather` 关掉 Privacy Mode（`/setprivacy → Disable`）并把 bot
> 重新拉进现有群，否则 Telegram 服务端会在消息到达 daemon 之前就过滤掉。

#### 平台配置要点

| 平台 | 凭证来源 | 备注 |
|------|---------|------|
| Telegram | `@BotFather` 拿 bot token | 群里收消息要 disable privacy mode 或 @ 它 |
| Discord | Developer Portal 拿 bot token | **必须开启 `message_content` 特权 intent** |
| 飞书 | open.feishu.cn/app → 应用详情 → 凭证与基础信息 | 自建应用，长连接模式无需开 inbound 端口；勾 `im:message` + `im:resource` 权限 |

### 3. 继承已有 tmux session（可选）

如果你之前是用 `claude --channels plugin:telegram` 在 tmux 里跑的：

```bash
.venv/bin/python -m im_claude_channel import-tmux-sessions --dry-run   # 预览
.venv/bin/python -m im_claude_channel import-tmux-sessions             # 应用
```

### 4. 启动

```bash
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
journalctl --user -fu im-claude-channel.service
```

---

## 斜杠命令分类

命令分三类：

| 类型 | 例子 | 行为 |
|------|------|------|
| **Daemon 拦截**（秒回，不过 claude） | `/new` `/cancel` `/status` `/sessions` `/log` `/help` `/menu` `/rename` `/context` `/model` `/resume` `/compact` | 操作 daemon 自己的状态 |
| **Skill 透传** | `/review` `/init` `/security-review` `/simplify` `/ultrareview` `/doctor` 及自定义 skill | `/cmd` 整段作为 prompt 喂给 `claude -p` |
| **TUI 限定**（`/help` 里标 `[TUI]`） | `/clear` `/permissions` `/config` … | 只对 Claude Code 交互式 TUI 有意义 |

### 会话管理工作流

Daemon 层面完整实现，无需 TUI：

```
/rename 任务A          ← 给当前会话命名
/new                   ← 归档 任务A，开新对话（不是删除！）
/rename 任务B          ← 给新会话命名
… 在任务B里工作 …
/resume                ← 列出所有归档会话
/resume 任务A          ← 恢复 任务A（任务B 自动归档）
/sessions              ← 查看活动 session + 本 chat 所有归档
```

`/resume` 支持按标签（大小写不敏感子串匹配）或 session_id 前缀定位。
切换时当前会话自动入库，不会丢失任何东西。

### 模型切换

```
/model                 ← 查看当前模型（chat 固定 + 全局配置）
/model sonnet          ← 切到 claude-sonnet-4-6
/model opus            ← 切到 claude-opus-4-7
/model haiku           ← 切到 claude-haiku-4-5-20251001
/model claude-opus-4-7 ← 完整 ID 也支持
/model clear           ← 清除本 chat 固定，恢复全局配置
```

model 固定存在 SQLite 里，`/new` 和 daemon 重启都不会清掉，只有 `/model clear` 才清。

---

## 架构

```
                   ┌────────────────────────────────────┐
                   │  claude-code-im-channel  (daemon)  │
                   │                                    │
   ┌─Telegram──────┼─►┐                                 │
   │   (aiogram)   │  │                                 │
   │               │  ├─► 鉴权 / 去重 / 命令解析        │
   ┌─Discord───────┼─►┤   daemon 命令 ─► 拦截执行       │
   │  (discord.py) │  │                              │  │
   │               │  ├─► claude_runner.run_stream   │  │
   ┌─飞书──────────┼─►┘     │                        │  │
   │  (lark-oapi)  │        ▼                        │  │
   │               │   ┌────────────────┐            │  │
   │               │   │ claude -p      │            │  │
   │               │   │   --resume sid │            │  │
   │               │   │   stream-json  │            │  │
   │               │   └────────────────┘            │  │
   │               │        │                        │  │
   │               │        ▼ (一行一个 NDJSON 事件)  │  │
   │               │   placeholder 卡片随事件原地编辑─┘  │
   └───────────────┘                                    │
                   │  SQLite: sessions（活动）           │
                   │        + session_history（归档）    │
                   │   均以 (platform, chat_id) 为键     │
                   └────────────────────────────────────┘
```

关键文件：

| 文件 | 作用 |
|------|------|
| `src/im_claude_channel/server.py` | Daemon 主循环、按 chat 加锁、worker 派发、`[ATTACH]` 处理 |
| `src/im_claude_channel/claude_runner.py` | `claude -p` 子进程 + NDJSON 流式解析 + heartbeat + 取消 |
| `src/im_claude_channel/session_store.py` | SQLite：`sessions`（活动）+ `session_history`（归档）两张表 |
| `src/im_claude_channel/commands.py` | Daemon 命令分发 + 完整 MENU 列表 + `/menu` 卡片文案 |
| `src/im_claude_channel/access.py` | 逐平台白名单 + 群提及策略 |
| `src/im_claude_channel/adapters/base.py` | `Adapter` Protocol + `IncomingMessage` dataclass |
| `src/im_claude_channel/adapters/telegram.py` | aiogram 长轮询适配器 |
| `src/im_claude_channel/adapters/discord.py` | discord.py 网关适配器 |
| `src/im_claude_channel/adapters/feishu.py` | lark-oapi 长连接 + REST 客户端 + interactive card |

Adapter Protocol 故意做得很薄——只要求实现 `send_message` `edit_message`
`send_file` `set_menu`。平台特有的脏活都塞在各自 adapter 里，daemon 主路径
完全平台无关。

---

## 添加新平台（比如微信 / 钉钉 / QQ）

1. 在 `src/im_claude_channel/adapters/<platform>.py` 实现 `Adapter` Protocol。
2. 在 `config.py` 加 `<Platform>Config` dataclass，加进 `Config.load`。
3. 在 `Daemon._make_adapters` 里加分支构造它。
4. 在 `config.example.yaml` 加对应的 section。

session 存储、命令分发、鉴权、进度卡片、附件标记、错误兜底、会话归档恢复——
全部平台无关，自动继承。

---

## 路线图（v0.3+）

- Discord application (slash) command 注册
- 飞书可选的菜单卡片首次置顶
- 微信 / 钉钉 / QQ adapter（欢迎社区贡献）
- Permission relay：把 claude 的权限请求转发到 IM 上人工放行

---

## 项目沿革

本仓库是 [claude-code-feishu-channel](https://github.com/ztllll/claude-code-feishu-channel)
的合并升级版——飞书相关代码已并入 `adapters/feishu.py`，老仓库进入只读
维护状态。两者部署位置不同步骤一致，老仓库部署机器可用以下方式平滑切换：

```bash
# 1) 停老服务
systemctl --user stop feishu-claude-channel.service

# 2) 拉新仓
git clone https://github.com/ztllll/claude-code-im-channel.git ~/claude-code-im-channel

# 3) 把老 config.yaml 的飞书字段搬到新 config.yaml 的 feishu: 段下
cd ~/claude-code-im-channel
cp config.example.yaml config.yaml
$EDITOR config.yaml

# 4) 用新 systemd unit 起来
./deploy/install.sh
.venv/bin/pip install -e '.[feishu]'
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
```

---

## 许可证

Apache-2.0。

---

---

# claude-code-im-channel (English)

Bridge **Telegram / Discord / Feishu (Lark)** to a Claude Code CLI session,
running as an independent systemd daemon — **no tmux, no MCP `--channels`
plugin, no firstParty gate**. Works on any Claude Code install, including
sub2api / OneAPI / Bedrock / Vertex relays.

---

## Why this exists

The official Telegram / Discord channel plugins for Claude Code are MCP
servers that `claude` itself spawns through the `--channels` feature. Two
problems with that design:

1. The bot connection lives inside the `claude` process — if `claude`
   crashes, gets killed, or exits, the bot disappears and inbound messages
   are dropped.
2. The `--channels` feature is gated by Anthropic's internal `firstParty()`
   check. Anyone running `claude` against a relay (sub2api / OneAPI /
   new-api / Bedrock / Vertex) cannot use it.

A common workaround is to run `claude --channels plugin:telegram` inside
`tmux` and trust the terminal multiplexer to keep things alive. That still
doesn't fix problem #2, and a stuck claude turn can starve the bot anyway.

This daemon takes the opposite approach:

- The daemon owns the bot connection, persists
  `(platform, chat_id) → claude_session_id` in SQLite, and spawns
  `claude -p --output-format stream-json --resume <session_id>` per inbound
  message.
- A long-running tool turn cannot poison the bot — it's a separate
  subprocess, and the bot keeps polling.
- systemd `Restart=always` handles crashes; the daemon hot-reloads
  `config.yaml` on file change.

Because we never touch `--channels`, the firstParty gate is irrelevant.
Anything that can run `claude -p` can run this daemon.

---

## Features

- ✅ **Tri-platform** — Telegram (aiogram), Discord (discord.py), Feishu / Lark (lark-oapi)
- ✅ **Per-chat sessions** — every chat (DM or group) has its own claude
  session_id, persisted across restarts
- ✅ **Live progress card** — single placeholder message edited in place as
  `tool_use` / `text` / `tool_result` events stream in
- ✅ **Session archive & restore** — `/new` archives the current session
  instead of discarding it; `/resume <label>` restores any archived session
  by label or session-id prefix, auto-archiving the current one first
- ✅ **Per-chat model override** — `/model sonnet|opus|haiku` pins a model
  for the current chat; `/model clear` reverts to the global config; alias
  table maps short names to full model IDs
- ✅ **Rich daemon commands** — `/new` `/cancel` `/status` `/sessions`
  `/log` `/menu` `/help` `/rename` `/context` `/model` `/resume` and
  `/compact` are all intercepted and handled by the daemon (never reach
  claude), giving instant responses and full session control from IM
- ✅ **Skill pass-through** — `/review`, `/init`, `/security-review`,
  `/simplify`, user-defined skills and more are forwarded verbatim to
  `claude -p` and resolved by claude's own skill layer
- ✅ **Bi-directional attachments** — inbound images/files are downloaded
  to disk and exposed to claude as `Read`-able paths; claude can ask the
  bridge to send files back by writing `[ATTACH:/abs/path]` anywhere in its
  reply
- ✅ **Auto-restart, auto error capture** — `Restart=always` + structured
  `ClaudeRunError` → friendly error reply
- ✅ **Auto idle / total timeout** — kills a stuck turn after configurable
  silence (default 15 min idle / 2 h total) without losing the partial reply
- ✅ **Per-platform allowlist** — `allowed_user_ids` per adapter; group
  chats can optionally require `@mention` (`group_only_when_mentioned`)

---

## Quick start

### 1. Install

```bash
git clone https://github.com/ztllll/claude-code-im-channel.git
cd claude-code-im-channel
./deploy/install.sh
```

`install.sh` creates a `.venv`, installs the base package, and prints the
next steps. To install with all three adapter drivers:

```bash
.venv/bin/pip install -e '.[all]'
```

Or pick exactly what you need:

```bash
.venv/bin/pip install -e '.[discord]'     # +discord.py
.venv/bin/pip install -e '.[feishu]'      # +lark-oapi
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Enable the platforms you want, fill in tokens / app secrets, and put your
IM user_ids into `allowed_user_ids`. Bot tokens can also come from env:

```bash
export TELEGRAM_BOT_TOKEN="..."
export DISCORD_BOT_TOKEN="..."
export FEISHU_APP_ID="cli_..."
export FEISHU_APP_SECRET="..."
```

#### Access model

```yaml
telegram:
  allowed_user_ids: ["123456789"]   # empty = allow everyone (unsafe in groups)
  group_only_when_mentioned: false  # true = require @bot in group chats
```

The same fields exist for `discord` and `feishu`. With a populated
`allowed_user_ids` and `group_only_when_mentioned: false` the bot responds
to your messages in any group it is added to, silently ignoring everyone
else — the recommended "personal bot" setup.

> **Telegram note**: with `group_only_when_mentioned: false` you must also
> disable the bot's Privacy Mode in `@BotFather` (`/setprivacy → Disable`)
> and re-invite the bot to existing groups, otherwise Telegram's server
> filters non-@-mention messages before they reach the daemon.

| Platform | Credentials | Notes |
|----------|-------------|-------|
| Telegram | Bot token from `@BotFather` | Groups need Privacy Mode disabled or @mention |
| Discord | Bot token from Developer Portal | **Must enable `message_content` privileged intent** |
| Feishu | open.feishu.cn/app → App Details → Credentials | Long-conn mode, no inbound port needed; grant `im:message` + `im:resource` |

### 3. Inherit existing tmux sessions (optional)

```bash
.venv/bin/python -m im_claude_channel import-tmux-sessions --dry-run   # preview
.venv/bin/python -m im_claude_channel import-tmux-sessions             # apply
```

### 4. Run

```bash
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
journalctl --user -fu im-claude-channel.service
```

---

## Slash commands

| Type | Examples | Behavior |
|------|----------|----------|
| **Daemon-handled** | `/new` `/cancel` `/status` `/sessions` `/log` `/help` `/menu` `/rename` `/context` `/model` `/resume` `/compact` | Operate on daemon state. Never reach claude. |
| **Skill pass-through** | `/review` `/init` `/security-review` `/simplify` `/ultrareview` `/doctor` and any user-defined skill | Forwarded verbatim to `claude -p`. |
| **TUI-only** (marked `[TUI]` in `/help`) | `/clear` `/permissions` `/config` … | No meaningful action in `-p` mode. |

### Session management workflow

```
/rename TaskA          — name the current session
/new                   — archive TaskA, start a fresh session
/rename TaskB          — name the new session
… work on TaskB …
/resume                — list all archived sessions
/resume TaskA          — restore TaskA (TaskB is auto-archived)
/sessions              — show active + archived sessions for this chat
```

### Model switching

```
/model                 — show current model (override + global config default)
/model sonnet          — switch to claude-sonnet-4-6
/model opus            — switch to claude-opus-4-7
/model haiku           — switch to claude-haiku-4-5-20251001
/model claude-opus-4-7 — full model ID also works
/model clear           — remove per-chat pin, revert to global config
```

---

## Architecture

```
                   ┌────────────────────────────────────┐
                   │  claude-code-im-channel  (daemon)  │
                   │                                    │
   ┌─Telegram──────┼─►┐                                 │
   │   (aiogram)   │  │                                 │
   │               │  ├─► access check, dedupe, parse   │
   ┌─Discord───────┼─►┤   slash commands ──► daemon dispatch
   │  (discord.py) │  │                              │  │
   │               │  ├─► claude_runner.run_stream   │  │
   ┌─Feishu────────┼─►┘     │                        │  │
   │  (lark-oapi)  │        ▼                        │  │
   │               │   ┌────────────────┐            │  │
   │               │   │ claude -p      │            │  │
   │               │   │   --resume sid │            │  │
   │               │   │   stream-json  │            │  │
   │               │   └────────────────┘            │  │
   │               │        │                        │  │
   │               │        ▼ (NDJSON per line)      │  │
   │               │   placeholder card edited       │  │
   └───────────────┘   per tool_use / text event ────┘  │
                   │                                    │
                   │  SQLite: sessions + session_history│
                   │   keyed by (platform, chat_id)     │
                   └────────────────────────────────────┘
```

Key files:

| File | Purpose |
|------|---------|
| `src/im_claude_channel/server.py` | Daemon main: async loop, per-chat lock, worker dispatch, [ATTACH] handling |
| `src/im_claude_channel/claude_runner.py` | `claude -p` subprocess + NDJSON stream parser + heartbeat + cancel |
| `src/im_claude_channel/session_store.py` | SQLite: `sessions` (active) + `session_history` (archived) |
| `src/im_claude_channel/commands.py` | Daemon-side command dispatch + curated MENU list |
| `src/im_claude_channel/access.py` | Per-platform allowlist + group-mention gate |
| `src/im_claude_channel/adapters/base.py` | `Adapter` Protocol + `IncomingMessage` dataclass |
| `src/im_claude_channel/adapters/telegram.py` | aiogram long-polling adapter |
| `src/im_claude_channel/adapters/discord.py` | discord.py gateway adapter |
| `src/im_claude_channel/adapters/feishu.py` | lark-oapi long-conn adapter + REST client + interactive cards |

---

## Adding a new platform

1. Create `src/im_claude_channel/adapters/<platform>.py` and implement the `Adapter` Protocol.
2. Add a `<Platform>Config` dataclass in `config.py` and wire it into `Config.load`.
3. Branch in `Daemon._make_adapters` to construct it when enabled.
4. Add a section to `config.example.yaml`.

Session store, command dispatch, access control, progress card, attachment
marker, error handling, session archive/restore — all platform-neutral,
inherited for free.

---

## Roadmap (v0.3+)

- Discord application (slash) commands registration
- Optional Feishu menu card auto-pin on first use
- WeChat / DingTalk / QQ adapters (community contribution welcome)
- Permission relay: route claude permission requests to the IM channel instead of auto-allow

---

## License

Apache-2.0. See [LICENSE](LICENSE).

This project is the successor to
[claude-code-feishu-channel](https://github.com/ztllll/claude-code-feishu-channel),
which has been folded in as the `feishu` adapter.
