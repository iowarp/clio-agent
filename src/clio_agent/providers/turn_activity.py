"""Which sessions have a turn running, for providers that pool a connection per session.

A pooled provider connection (the Claude Code CLI) keeps the conversation it has sent,
so the next model call sends only the new messages and the provider's prompt cache is
reused. Two things used to drop it: the turn runner released every session's
connection after every turn, and the idle reaper dropped a connection that had been
unused for 15 s, which includes a turn waiting on a long tool. The turn runner now marks
a session's turn as started and ended here, the idle reaper leaves a connection alone
while its session's turn runs, and a clean turn end keeps the connection for the next
turn; Stop and failures still release it.
"""

from __future__ import annotations

import threading
from collections import Counter

__all__ = ["active", "ended", "started"]

_LOCK = threading.Lock()
_ACTIVE: Counter[str] = Counter()


def started(session_id: str) -> None:
    """A turn of ``session_id`` began."""
    if session_id:
        with _LOCK:
            _ACTIVE[session_id] += 1


def ended(session_id: str) -> None:
    """A turn of ``session_id`` ended (each :func:`started` is matched by one call)."""
    if session_id:
        with _LOCK:
            _ACTIVE[session_id] -= 1
            if _ACTIVE[session_id] <= 0:
                del _ACTIVE[session_id]


def active(session_id: str) -> bool:
    """Whether a turn of ``session_id`` is running."""
    with _LOCK:
        return _ACTIVE.get(session_id, 0) > 0
