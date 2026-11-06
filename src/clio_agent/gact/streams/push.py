"""Tell the views watching a stream that it changed; nothing is sent to no one.

A view on screen holds a lease on the stream for its session (``POST
/v1/streams/{id}/watch``, renewed while shown). A write announces the stream to each
watching session as a transient ``stream.updated`` event (no replay, no cursor), at most
once per ``streams.push_interval_s``: writes inside the interval are announced together at
its end with the latest state (latest wins). The view then reads the rows, which are a
bounded window. With no lease held, a write announces nothing.

A lease is held per view: ``(session, lease_id)``. Two views of one session that each
name their own ``lease_id`` hold two leases, and one's release leaves the other's; a
view that names none shares its session's single lease (the older clients). When a
stream closes, its last state is announced at once and its watch is forgotten.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.streams.store import Stream

#: The transient event a watching session gets when a stream changes.
STREAM_UPDATED_EVENT = "stream.updated"
#: How long one watch lasts without being renewed, in seconds.
LEASE_S = 30.0


def push_interval_s() -> float:
    """Seconds between two announcements of one stream (``streams.push_interval_s``)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "streams.push_interval_s", env="CLIO_STREAMS_PUSH_INTERVAL_S", default=0.1, cast=float
    )
    return max(0.0, value)


class _Watch:
    def __init__(self) -> None:
        self.leases: dict[tuple[str, str], float] = {}  # (session, lease id) -> expiry
        self.last = 0.0
        self.timer: threading.Timer | None = None
        self.lock = threading.Lock()


_WATCHES: dict[str, _Watch] = {}
_WATCHES_LOCK = threading.Lock()


def watch(stream_id: str, session_id: str, *, release: bool = False, lease_id: str = "") -> int:
    """Hold (or release) one view's lease (``session_id``, ``lease_id``) on the stream;
    how many sessions watch it. A stream nobody watches keeps no watch."""
    now = time.monotonic()
    with _WATCHES_LOCK:  # held throughout, so a lease never lands on a dropped watch
        state = _WATCHES.setdefault(stream_id, _Watch())
        with state.lock:
            if release:
                state.leases.pop((session_id, lease_id), None)
            elif session_id:
                state.leases[(session_id, lease_id)] = now + LEASE_S
            state.leases = {key: until for key, until in state.leases.items() if until > now}
            if not state.leases and state.timer is None:
                del _WATCHES[stream_id]
            return len({sid for sid, _lease in state.leases})


def forget(stream_id: str) -> None:
    """Drop the stream's watch (it stopped, or it is gone): no announcement is due."""
    with _WATCHES_LOCK:
        state = _WATCHES.pop(stream_id, None)
    if state is not None:
        with state.lock:
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None


def _publish(app: Any, stream: "Stream") -> int:
    from clio_agent.gact.events import Event  # noqa: PLC0415

    with _WATCHES_LOCK:
        state = _WATCHES.get(stream.id)
    if state is None:
        return 0  # nobody watches (or the watch was forgotten)
    now = time.monotonic()
    with state.lock:
        state.timer, state.last = None, now
        sessions = sorted({sid for (sid, _lease), until in state.leases.items() if until > now})
    bus = getattr(app.state, "bus", None)
    if bus is None or not sessions:
        return 0
    with stream.lock:
        payload = {
            "stream_id": stream.id,
            "workspace_id": stream.workspace_id,
            "name": stream.name,
            "state": stream.state,
            "writes": stream.seq,
            "updated_at": stream.updated_at,
            "closed_at": stream.closed_at,
        }
    for session_id in sessions:
        bus.publish(
            Event(type=STREAM_UPDATED_EVENT, session_id=session_id, payload=payload, transient=True)
        )
    return len(sessions)


def announce(app: Any, stream: "Stream", *, now: bool = False) -> None:
    """Announce a change to the watching sessions, coalesced (module doc); ``now`` (a
    stop) announces at once, in place of any announcement that is due."""
    with _WATCHES_LOCK:
        state = _WATCHES.get(stream.id)
    if state is None:
        return  # nothing is sent to no one
    interval = 0.0 if now else push_interval_s()
    with state.lock:
        if not any(until > time.monotonic() for until in state.leases.values()):
            return  # nothing is sent to no one
        if state.timer is not None and now:
            state.timer.cancel()
            state.timer = None
        if state.timer is not None:
            return  # an announcement is due; it carries the latest state
        wait = state.last + interval - time.monotonic()
        if wait > 0:
            state.timer = threading.Timer(wait, _publish, args=(app, stream))
            state.timer.daemon = True
            state.timer.start()
            return
    _publish(app, stream)


__all__ = ["LEASE_S", "STREAM_UPDATED_EVENT", "announce", "forget", "push_interval_s", "watch"]
