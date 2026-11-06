"""A folded session's in-memory state, and how a release retires it (owner module).

A :class:`FoldSession` holds one session's index and context views; it is re-derivable
from clio-core and dropped on release. A write is several steps on one state: take the
scope's view, put the index when it lists a new chunk, put the lane chunk, fold the atom
into the view. A release used to drop the state between those steps. A reload could then
read the stored index before the write's put landed and overwrite it without the write's
chunk, or build a new view before the chunk landed while the writer folded its atom into
the dropped view; either way the atom no longer read back.

So a write pins the state for all of its steps (:func:`pinned_session`), and
:func:`retire` marks the state retired, waits until no other thread pins it, and only
then drops it. A reload therefore reads the index and chunks after every write that
began on the old state, and builds its views from them. A writer arriving at a retired
state waits until it is dropped and pins the next one.

Lock order: a pin is counted under the state's lock, which is never held while a write
waits on anything else; :func:`retire` waits on the state's condition, which releases
that lock, and holds no other lock of the store, so a pinned write can always finish.
A thread that releases a session it pins itself is not waited for (it would wait on its
own write); its write keeps the dropped state, as before this module.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from clio_agent.arc.context_view import ContextView
from clio_agent.arc.lane_index import SessionIndex

__all__ = ["FoldSession", "live_session", "pinned_session", "retire"]


@dataclass
class FoldSession:
    """One session's index and views (in memory, re-derivable from clio-core)."""

    index: SessionIndex
    lock: threading.RLock = field(default_factory=threading.RLock)
    views: dict[str, ContextView] = field(default_factory=dict)
    retired: bool = False  # set under ``lock`` by :func:`retire`; never cleared
    dropped: bool = False  # set under ``lock`` once :func:`retire` removed the state
    pins: dict[int, int] = field(default_factory=dict)  # thread ident -> pin depth
    settled: threading.Condition = field(init=False)  # pins or ``dropped`` changed

    def __post_init__(self) -> None:
        self.settled = threading.Condition(self.lock)


@contextmanager
def pinned_session(load: Callable[[], FoldSession]) -> Iterator[FoldSession]:
    """Yield the session's current state, pinned: a release waits until this exits.

    Re-entrant per thread: a thread that pins a state may pin it again while a release
    waits on it. The state's lock is NOT held while the body runs.
    """
    me = threading.get_ident()
    while True:
        state = load()
        with state.lock:
            if state.retired and me not in state.pins:
                state.settled.wait_for(lambda: state.dropped)  # noqa: B023 - same state
                continue
            state.pins[me] = state.pins.get(me, 0) + 1
            break
    try:
        yield state
    finally:
        with state.lock:
            depth = state.pins.pop(me) - 1
            if depth:
                state.pins[me] = depth
            else:
                state.settled.notify_all()


@contextmanager
def live_session(load: Callable[[], FoldSession]) -> Iterator[FoldSession]:
    """Yield the session's current state pinned and with its lock held."""
    with pinned_session(load) as state, state.lock:
        yield state


def retire(
    sessions: dict[str, FoldSession], sessions_lock: threading.Lock, session_id: str
) -> None:
    """Drop a session's state once no other thread pins it (lock order: state, registry)."""
    with sessions_lock:
        state = sessions.get(session_id)
    if state is None:
        return
    me = threading.get_ident()
    with state.lock:
        state.retired = True
        state.settled.wait_for(lambda: not state.pins.keys() - {me})
        with sessions_lock:
            if sessions.get(session_id) is state:
                del sessions[session_id]
        state.dropped = True
        state.settled.notify_all()
