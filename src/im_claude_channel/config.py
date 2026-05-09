"""YAML config loader with env var override.

Schema is intentionally close to the feishu-claude-channel project so the
underlying claude_runner / session_store / access modules port cleanly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    # Allowed Telegram user_ids / chat_ids. Empty list = allow everyone (NOT recommended).
    allowed_user_ids: list[str] = field(default_factory=list)
    # Group chats only invoke claude when the bot is @-mentioned.
    group_only_when_mentioned: bool = True


@dataclass
class DiscordConfig:
    enabled: bool = False
    bot_token: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)
    group_only_when_mentioned: bool = True


@dataclass
class ClaudeConfig:
    binary: str = "claude"
    work_dir: str = ""
    extra_args: list[str] = field(default_factory=list)
    timeout_seconds: int = 7200
    idle_timeout_seconds: int = 900


@dataclass
class SessionConfig:
    state_dir: str = "~/.im-claude-channel"
    archive_after_days: int = 14


@dataclass
class LoggingConfig:
    file: str = "~/.im-claude-channel/daemon.log"
    level: str = "INFO"


@dataclass
class Config:
    telegram: TelegramConfig
    discord: DiscordConfig
    claude: ClaudeConfig
    session: SessionConfig
    logging: LoggingConfig

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Config:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}

        tg = TelegramConfig(**(raw.get("telegram") or {}))
        tg.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", tg.bot_token)

        dc = DiscordConfig(**(raw.get("discord") or {}))
        dc.bot_token = os.environ.get("DISCORD_BOT_TOKEN", dc.bot_token)

        if tg.enabled and not tg.bot_token:
            raise ValueError("telegram.enabled=true but bot_token is missing")
        if dc.enabled and not dc.bot_token:
            raise ValueError("discord.enabled=true but bot_token is missing")
        if not tg.enabled and not dc.enabled:
            raise ValueError("at least one of telegram.enabled / discord.enabled must be true")

        return cls(
            telegram=tg,
            discord=dc,
            claude=ClaudeConfig(**(raw.get("claude") or {})),
            session=SessionConfig(**(raw.get("session") or {})),
            logging=LoggingConfig(**(raw.get("logging") or {})),
        )

    def expand_paths(self) -> None:
        self.claude.work_dir = (
            str(Path(self.claude.work_dir).expanduser()) if self.claude.work_dir else ""
        )
        self.session.state_dir = str(Path(self.session.state_dir).expanduser())
        self.logging.file = str(Path(self.logging.file).expanduser())
