"""Which sessions have a turn running, for providers that pool a connection per session.

A pooled provider connection (the Claude Code CLI) keeps the conversation it has sent,
so the next model call sends only the new messages and the provider's prompt cache is
reused. Two things used to drop it: the turn runner released every session's
connection after every turn, and the idle reaper dropped a connection that had been
unused for 15 s, which includes a turn waiting on a long tool. The turn runner now marks
a session's turn as started and ended here, the idle reaper leaves a connection alone
while its session's turn runs, and a clean turn end keeps the connection for the next
turn; Stop and failures still release it.

A session's connection becomes reapable the moment its last turn ends, so the idle reaper
subscribes with :func:`subscribe_ended` and re-arms its timer then instead of polling.
"""

from __future__ import annotations

import inspect
import logging
import threading
import weakref
from collections import Counter
from typing import Callable

__all__ = ["active", "ended", "started", "subscribe_ended"]

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ACTIVE: Counter[str] = Counter()
_ENDED_LISTENERS: list[Callable[[], Callable[[str], None] | None]] = []


def subscribe_ended(callback: Callable[[str], None]) -> None:
    """Call ``callback(session_id)`` whenever a session's last running turn ends.

    A bound method is held weakly, so subscribing never keeps its owner alive. The
    callback runs on the thread that ended the turn and must be cheap and non-blocking.
    """
    ref: Callable[[], Callable[[str], None] | None]
    if inspect.ismethod(callback):
        ref = weakref.WeakMethod(callback)
    else:
        ref = lambda: callback  # noqa: E731 - strong reference for plain callables
    with _LOCK:
        _ENDED_LISTENERS[:] = [r for r in _ENDED_LISTENERS if r() is not None]
        _ENDED_LISTENERS.append(ref)


def started(session_id: str) -> None:
    """A turn of ``session_id`` began."""
    if session_id:
        with _LOCK:
            _ACTIVE[session_id] += 1


def ended(session_id: str) -> None:
    """A turn of ``session_id`` ended (each :func:`started` is matched by one call)."""
    if not session_id:
        return
    with _LOCK:
        _ACTIVE[session_id] -= 1
        if _ACTIVE[session_id] > 0:
            return
        del _ACTIVE[session_id]
        listeners = [r() for r in _ENDED_LISTENERS]
    for listener in listeners:
        if listener is None:
            continue
        try:
            listener(session_id)
        except Exception:  # noqa: BLE001 - a listener must never break turn teardown
            logger.warning("turn-ended listener failed session=%s", session_id, exc_info=True)


def active(session_id: str) -> bool:
    """Whether a turn of ``session_id`` is running."""
    with _LOCK:
        return _ACTIVE.get(session_id, 0) > 0
