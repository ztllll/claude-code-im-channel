"""Import claude session_ids from existing tmux runs into the daemon's store.

Background: before this daemon existed, users ran ``claude --channels
plugin:telegram@...`` (or discord) inside tmux. Each tmux'd claude wrote its
transcript to ``~/.claude/projects/<dirhash>/<session-id>.jsonl``. Inside that
jsonl, every inbound IM message gets stored as a ``<channel source="..."
chat_id="...">`` tag in the user content blocks.

This importer reads those jsonls and rebuilds the (platform, chat_id) →
session_id mapping, so the new daemon can ``--resume <session_id>`` from the
exact point the tmux'd claude left off — preserving the running conversation.

Why this works:
- ``claude -p --resume <session_id>`` is the standard, supported way to
  continue a session. The session_id format is the .jsonl filename without
  extension; the file lives on disk regardless of whether the original claude
  process is still running.
- We pick the *most recently modified* session for each (platform, chat_id),
  on the assumption that users only run one claude per chat at a time.

Run via:  ``python -m im_claude_channel import-tmux-sessions [--dry-run]``
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .session_store import SessionStore

log = logging.getLogger(__name__)

# Match the channel tag the official plugins emit. The leading slash escapes
# are how it lands inside a JSON string in the .jsonl record.
#
#   <channel source="plugin:telegram:telegram" chat_id="123" message_id="..." ...>
#
# We only care about source platform + chat_id. Other attributes vary.
_CHANNEL_RE = re.compile(
    r'<channel\s+source="plugin:(?P<platform>telegram|discord):(?:telegram|discord)"\s+'
    r'chat_id="(?P<chat_id>[^"]+)"'
)


@dataclass
class ScannedSession:
    session_id: str  # the jsonl basename (no extension)
    path: Path
    mtime: float
    # platform → chat_id → match count, so we can resolve dominance.
    counts: dict[str, dict[str, int]]


def _scan_jsonl(path: Path) -> ScannedSession:
    counts: dict[str, dict[str, int]] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # The channel tags live inside JSON-encoded "content" strings, so
            # the < and " are escaped as \" — but the raw .jsonl bytes contain
            # them literally once you decode each line. We scan the line text
            # which already has escapes intact (\"plugin:..."), and the
            # _CHANNEL_RE was authored to match the unescaped form. So we
            # decode the JSON line first, fall back to raw line on error.
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                # Quick reject — must mention "channel" before we pay JSON parse.
                if "<channel" not in line:
                    continue
                content = ""
                try:
                    rec = json.loads(line)
                    # Channel tags live inside record["content"] (queue-op
                    # records) or inside record["message"]["content"][i]["text"]
                    # (user-message records). Walk both shapes.
                    content = _extract_content(rec)
                except (TypeError, ValueError):
                    content = line
                if not content:
                    continue
                for m in _CHANNEL_RE.finditer(content):
                    p = m.group("platform")
                    c = m.group("chat_id")
                    counts.setdefault(p, {}).setdefault(c, 0)
                    counts[p][c] += 1
    except OSError as e:
        log.warning("scan: cannot read %s: %s", path, e)

    return ScannedSession(
        session_id=path.stem,
        path=path,
        mtime=path.stat().st_mtime,
        counts=counts,
    )


def _extract_content(rec: dict) -> str:
    """Pull every string content fragment out of a single jsonl record."""
    parts: list[str] = []
    if isinstance(rec.get("content"), str):
        parts.append(rec["content"])
    msg = rec.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
    return "\n".join(parts)


def _dominant(scanned: ScannedSession) -> tuple[str, str] | None:
    """Decide which (platform, chat_id) this session 'belongs to'.

    A session can have stray mentions of the *other* platform's plugin
    (e.g. boilerplate in MCP tool descriptions). We pick the platform/chat
    with the most matches; reject if the leader has fewer than 2 matches.
    """
    best: tuple[str, str, int] | None = None
    for platform, by_chat in scanned.counts.items():
        for chat, n in by_chat.items():
            if best is None or n > best[2]:
                best = (platform, chat, n)
    if best is None or best[2] < 2:
        return None
    return best[0], best[1]


def discover(projects_dir: Path) -> dict[tuple[str, str], ScannedSession]:
    """Scan all .jsonls under projects_dir; return one (platform,chat)→session map.

    Picks the *most recently modified* session per (platform, chat_id), since
    that's the one the live tmux'd claude is actively writing to.
    """
    if not projects_dir.is_dir():
        log.warning("projects dir does not exist: %s", projects_dir)
        return {}

    candidates: dict[tuple[str, str], ScannedSession] = {}
    for jsonl in projects_dir.rglob("*.jsonl"):
        scanned = _scan_jsonl(jsonl)
        if not scanned.counts:
            continue
        key = _dominant(scanned)
        if key is None:
            continue
        existing = candidates.get(key)
        if existing is None or scanned.mtime > existing.mtime:
            candidates[key] = scanned
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="im-claude-channel import-tmux-sessions",
        description=(
            "Scan ~/.claude/projects/-/*.jsonl for live channel sessions and "
            "seed the daemon's session store so the next message resumes the "
            "existing conversation."
        ),
    )
    parser.add_argument("-c", "--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--projects-dir",
        default=str(Path("~/.claude/projects").expanduser()),
        help="root of claude session jsonls (default: ~/.claude/projects)",
    )
    parser.add_argument(
        "--platform",
        choices=("telegram", "discord", "both"),
        default="both",
        help="restrict import to one platform",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the (platform, chat_id, session_id) plan without writing.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cfg = Config.load(args.config)
    cfg.expand_paths()

    projects_dir = Path(args.projects_dir).expanduser()
    log.info("scanning %s ...", projects_dir)
    discovered = discover(projects_dir)

    if not discovered:
        log.warning("no telegram/discord channel sessions found")
        return 0

    log.info("discovered %d session(s):", len(discovered))
    for (platform, chat_id), s in sorted(
        discovered.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        log.info(
            "  %-8s chat=%-22s session=%s mtime=%.0f matches=%d",
            platform, chat_id, s.session_id, s.mtime,
            sum(s.counts.get(platform, {}).values()),
        )

    if args.platform != "both":
        discovered = {k: v for k, v in discovered.items() if k[0] == args.platform}
        log.info("filtered to platform=%s: %d session(s)", args.platform, len(discovered))

    if args.dry_run:
        log.info("dry-run: not writing to session store at %s", cfg.session.state_dir)
        return 0

    store = SessionStore(cfg.session.state_dir)
    written = 0
    for (platform, chat_id), s in discovered.items():
        existing = store.get(platform, chat_id)
        if existing == s.session_id:
            log.info("  %s/%s already imported (%s)", platform, chat_id, s.session_id)
            continue
        store.upsert(platform, chat_id, s.session_id)
        written += 1
        log.info("  imported %s/%s → %s", platform, chat_id, s.session_id)

    log.info("done. wrote %d new mapping(s) to %s", written, store.db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
