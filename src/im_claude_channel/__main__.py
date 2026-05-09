"""Entry point: ``python -m im_claude_channel <subcommand>``."""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="im-claude-channel",
        description="Telegram + Discord daemon bridging IM messages to claude CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Run the daemon (telegram + discord adapters).")
    p_start.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")

    p_import = sub.add_parser(
        "import-tmux-sessions",
        help="Seed session store from existing claude tmux sessions.",
    )
    p_import.add_argument("-c", "--config", default="config.yaml")
    p_import.add_argument(
        "--projects-dir",
        default="~/.claude/projects",
        help="root of claude session jsonls (default: ~/.claude/projects)",
    )
    p_import.add_argument("--platform", choices=("telegram", "discord", "both"), default="both")
    p_import.add_argument("--dry-run", action="store_true")

    p_list = sub.add_parser("list-sessions", help="Print the current session store.")
    p_list.add_argument("-c", "--config", default="config.yaml")

    args = parser.parse_args(argv)

    if args.cmd == "start":
        from .server import run

        return run(config_path=args.config)

    if args.cmd == "import-tmux-sessions":
        from .importer import main as import_main

        forwarded = ["-c", args.config, "--projects-dir", args.projects_dir,
                     "--platform", args.platform]
        if args.dry_run:
            forwarded.append("--dry-run")
        return import_main(forwarded)

    if args.cmd == "list-sessions":
        from .config import Config
        from .session_store import SessionStore

        cfg = Config.load(args.config)
        cfg.expand_paths()
        store = SessionStore(cfg.session.state_dir)
        rows = store.list_all()
        if not rows:
            print("(empty)")
            return 0
        for platform, chat_id, sid, ts, n in sorted(rows):
            print(f"{platform:8s} chat={chat_id:24s} session={sid}  msgs={n}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
