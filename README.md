# claude-code-im-channel

Bridge **Telegram / Discord / Feishu (Lark)** to a Claude Code CLI session,
running as an independent systemd daemon — **no tmux, no MCP `--channels`
plugin, no firstParty gate**. Works on any Claude Code install, including
sub2api / OneAPI / Bedrock / Vertex relays.

> 中文版 README → [README.zh.md](./README.zh.md)

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
- ✅ **Full CLI slash-command support** — daemon intercepts daemon-control
  commands (`/new`, `/cancel`, `/status`, `/sessions`, `/log`, `/menu`,
  `/help`); everything else (`/review`, `/init`, `/security-review`,
  `/simplify`, user-defined skills, …) passes through to the claude CLI as
  the prompt and is resolved by claude's own skill layer
- ✅ **Bi-directional attachments** — inbound images/files are downloaded
  to disk and exposed to claude as `Read`-able paths; claude can ask the
  bridge to send files back by writing `[ATTACH:/abs/path]` anywhere in its
  reply
- ✅ **Auto-restart, auto error capture** — `Restart=always` + structured
  `ClaudeRunError` → friendly Chinese error reply
- ✅ **Auto idle / total timeout** — kills a stuck turn after configurable
  silence (default 15min idle / 2h total) without losing the partial reply
- ✅ **Per-platform allowlist** — `allowed_user_ids` per adapter; group
  chats can require `@mention` to engage

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

### 3. Inherit existing tmux sessions (optional)

If you were running `claude --channels plugin:telegram` in tmux already and
want to keep the conversation context:

```bash
.venv/bin/python -m im_claude_channel import-tmux-sessions --dry-run   # preview
.venv/bin/python -m im_claude_channel import-tmux-sessions             # apply
```

It walks `~/.claude/projects/*/` looking for `<channel source="..." chat_id="...">`
tags and seeds the daemon's session store with the most recently active
session per chat. (Feishu has no equivalent because feishu was never
launched via `--channels` in the first place.)

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

Commands fall into three buckets:

| Type | Examples | Behavior |
|------|----------|----------|
| **Daemon-handled** (intercepted, never reaches claude) | `/new` `/cancel` `/status` `/sessions` `/log` `/help` `/menu` | Operate on daemon state. Always work. |
| **Skill pass-through** (forwarded as the prompt) | `/review` `/init` `/security-review` `/simplify` `/ultrareview` `/doctor` and any user-defined skill | The literal `/cmd` text is sent to `claude -p`; claude's skill layer resolves it. |
| **TUI-only** (marked `[TUI]` in `/help`) | `/clear` `/compact` `/model` `/permissions` … | These manipulate the interactive Claude Code TUI. They don't have meaning in `-p` mode; you'll get an explanation rather than an action. Daemon offers `/new` as the practical replacement for `/clear`. |

The `/menu` command renders a curated, grouped quick-reference. The flat
list (~60+ commands) is available via `/help` or — for Telegram — the
native `/` autocomplete (registered via `setMyCommands`).

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
                   │  SQLite: (platform, chat_id)       │
                   │   ↔ session_id, message_count      │
                   └────────────────────────────────────┘
```

Key files:

| File | Purpose |
|------|---------|
| `src/im_claude_channel/server.py` | Daemon main: async loop, per-chat lock, worker dispatch, [ATTACH] handling |
| `src/im_claude_channel/claude_runner.py` | `claude -p` subprocess + NDJSON stream parser + heartbeat + cancel |
| `src/im_claude_channel/session_store.py` | SQLite session map keyed by `(platform, chat_id)` |
| `src/im_claude_channel/commands.py` | Daemon-side command dispatch + curated `MENU` list |
| `src/im_claude_channel/access.py` | Per-platform allowlist + group-mention gate |
| `src/im_claude_channel/importer.py` | Seed session store from existing tmux/MCP `<channel>` tags |
| `src/im_claude_channel/adapters/base.py` | `Adapter` Protocol + `IncomingMessage` dataclass |
| `src/im_claude_channel/adapters/telegram.py` | aiogram long-polling adapter |
| `src/im_claude_channel/adapters/discord.py` | discord.py gateway adapter |
| `src/im_claude_channel/adapters/feishu.py` | lark-oapi long-conn adapter + REST client + interactive cards |

The Adapter Protocol is intentionally minimal (`send_message`,
`edit_message`, `send_file`, `set_menu`). Platform-specific concerns
(Feishu's "edit only works on cards", Discord's `message_content` intent,
Telegram's `setMyCommands` scopes) stay inside each adapter.

---

## Adding a new platform

1. Create `src/im_claude_channel/adapters/<platform>.py` and implement the
   `Adapter` Protocol from `adapters/base.py`.
2. Add a `<Platform>Config` dataclass in `config.py` and wire it into
   `Config.load`.
3. Branch in `Daemon._make_adapters` to construct it when enabled.
4. Add a section to `config.example.yaml`.

That's it. The session store, command dispatch, access control, progress
card, attachment marker, error handling — all of that is platform-neutral
and inherited for free.

---

## Roadmap (v0.3+)

- Discord application (slash) commands registration
- Optional Feishu menu card auto-pin on first use
- Wechat / DingTalk / QQ adapters (community contribution welcome)
- Permission relay: route claude permission requests to the IM channel
  instead of auto-allow
- Streaming edit on Feishu (proper progress card with multi-section
  template rather than markdown-only)

---

## License

Apache-2.0. See [LICENSE](LICENSE) once it lands — TODO.

This project is the successor to
[claude-code-feishu-channel](https://github.com/ztllll/claude-code-feishu-channel),
which has been folded in as the `feishu` adapter.
