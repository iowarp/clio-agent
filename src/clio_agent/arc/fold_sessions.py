"""A folded session's in-memory state, and how a release retires it (owner module).

A :class:`FoldSession` holds one session's index and context views; it is re-derivable
from clio-core and dropped on release. A write changes the index under the state's lock
and puts it before releasing that lock. A release used to drop the state while such a
write was in flight: a reload could then read the stored index before the write's put
landed, and the reloaded index -- missing the write's chunk -- overwrote it on its next
put, so the chunk's messages no longer read back.

:func:`retire` therefore marks the state retired under its lock before dropping it, so a
reload only ever reads the index after every in-flight put; and :func:`live_session`
hands a writer a state that is not retired, re-fetching the current one when a release
won the race.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from clio_agent.arc.context_view import ContextView
from clio_agent.arc.lane_index import SessionIndex

__all__ = ["FoldSession", "live_session", "retire"]


@dataclass
class FoldSession:
    """One session's index and views (in memory, re-derivable from clio-core)."""

    index: SessionIndex
    lock: threading.RLock = field(default_factory=threading.RLock)
    views: dict[str, ContextView] = field(default_factory=dict)
    retired: bool = False  # set under ``lock`` by :func:`retire`; never cleared


@contextmanager
def live_session(load: Callable[[], FoldSession]) -> Iterator[FoldSession]:
    """Yield the session's current state with its lock held, never a retired one."""
    while True:
        state = load()
        with state.lock:
            if not state.retired:
                yield state
                return


def retire(
    sessions: dict[str, FoldSession], sessions_lock: threading.Lock, session_id: str
) -> None:
    """Drop a session's state once no write holds it (lock order: state, then registry)."""
    with sessions_lock:
        state = sessions.get(session_id)
    if state is None:
        return
    with state.lock:
        state.retired = True
        with sessions_lock:
            if sessions.get(session_id) is state:
                del sessions[session_id]
