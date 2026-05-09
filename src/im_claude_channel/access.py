"""Per-platform allowlist."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class AccessControl:
    """Allowlist gate for one platform.

    An empty list means "allow everyone" — useful for bot owner-only use where
    the bot is invite-only at the platform level. For public bots, populate the
    allowed list.
    """

    def __init__(self, allowed_user_ids: list[str], group_only_when_mentioned: bool) -> None:
        self._allowed = {str(x) for x in allowed_user_ids if x is not None}
        self._group_only_when_mentioned = group_only_when_mentioned

    def allow(self, user_id: str | int | None) -> bool:
        if not self._allowed:
            return True
        return str(user_id) in self._allowed

    def group_only_when_mentioned(self) -> bool:
        return self._group_only_when_mentioned
