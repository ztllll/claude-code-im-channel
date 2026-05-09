# claude-code-im-channel

Telegram (and later Discord) channel daemon for Claude Code.

## Why this exists

The official `plugin:telegram` / `plugin:discord` channels for Claude Code are
MCP servers spawned by `claude` itself. That coupling is the problem:

- the bot connection lives inside the `claude` process — when `claude` crashes
  or exits, the bot disappears and inbound messages are dropped
- there is no supervisor; nothing restarts a broken bot
- a long-running tool loop can starve the polling loop

This daemon decouples the two. The daemon owns the Telegram/Discord bot
connection, persists `(platform, chat_id) → claude_session_id` in SQLite, and
spawns `claude -p --output-format stream-json --resume <session_id>` per
inbound message. Live progress is shown by editing a single placeholder
message as `tool_use` / `text` / `result` events stream in.

systemd `Restart=always` covers process crashes; the per-message subprocess
model means a stuck claude turn cannot poison the bot.

## Architecture (sibling project)

This is a sibling of [claude-code-feishu-channel](https://github.com/ztllll/claude-code-feishu-channel)
and reuses its proven `claude_runner.run_stream` and SQLite session-store
patterns. The IM-specific code lives in `src/im_claude_channel/adapters/`.

## Quick start

```bash
./deploy/install.sh
cp config.example.yaml config.yaml          # set bot_token + allowed_user_ids
python -m im_claude_channel import-tmux-sessions --dry-run   # preview
python -m im_claude_channel import-tmux-sessions             # apply
mkdir -p ~/.config/systemd/user
cp systemd/im-claude-channel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now im-claude-channel.service
journalctl --user -fu im-claude-channel.service
```

## Migrating from a tmux'd `claude --channels plugin:telegram`

1. Stop the tmux'd `claude` (it must release the bot connection).
2. Run `python -m im_claude_channel import-tmux-sessions` — it scans
   `~/.claude/projects/*/` for sessions with channel markers and seeds the
   daemon's session store with the most recently active one per chat.
3. Start the systemd unit. Your next message resumes the existing claude
   session via `--resume <session_id>` — context is preserved.
