"""YAML config loader with env var override.

Schema is intentionally close to the feishu-claude-channel project so the
underlying claude_runner / session_store / access modules port cleanly across
all three platforms (telegram / discord / feishu).
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
    # Allowed Telegram numeric user_ids. Empty = allow everyone (only safe if
    # the bot is invite-only at the platform level).
    allowed_user_ids: list[str] = field(default_factory=list)
    # Group chats only invoke claude when the bot is @-mentioned (or replied-to).
    group_only_when_mentioned: bool = True


@dataclass
class DiscordConfig:
    enabled: bool = False
    bot_token: str = ""
    # Allowed Discord user_ids (snowflakes as strings). Empty = allow everyone.
    allowed_user_ids: list[str] = field(default_factory=list)
    group_only_when_mentioned: bool = True


@dataclass
class FeishuConfig:
    """飞书 (Lark) self-built app credentials. Long-conn mode — no inbound port needed.

    Get app_id / app_secret from https://open.feishu.cn/app → 应用详情 → 凭证与基础信息.
    """

    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""
    # Allowed Feishu open_ids (ou_xxx). Empty = allow everyone in the bot's reach.
    allowed_user_ids: list[str] = field(default_factory=list)
    # Group chats only invoke claude when the bot is @-mentioned.
    group_only_when_mentioned: bool = True
    # SDK log level: DEBUG / INFO / WARN / ERROR. Default INFO — DEBUG floods
    # the daemon log with lark ping/pong every 30s.
    log_level: str = "INFO"


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
    feishu: FeishuConfig
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

        fs = FeishuConfig(**(raw.get("feishu") or {}))
        fs.app_id = os.environ.get("FEISHU_APP_ID", fs.app_id)
        fs.app_secret = os.environ.get("FEISHU_APP_SECRET", fs.app_secret)

        if tg.enabled and not tg.bot_token:
            raise ValueError("telegram.enabled=true but bot_token is missing")
        if dc.enabled and not dc.bot_token:
            raise ValueError("discord.enabled=true but bot_token is missing")
        if fs.enabled and not (fs.app_id and fs.app_secret):
            raise ValueError("feishu.enabled=true but app_id/app_secret missing")
        if not (tg.enabled or dc.enabled or fs.enabled):
            raise ValueError(
                "at least one of telegram.enabled / discord.enabled / feishu.enabled must be true"
            )

        return cls(
            telegram=tg,
            discord=dc,
            feishu=fs,
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
