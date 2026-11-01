"""Timing for the ``lm.call`` event: when a call started, how long it took, and when its
first streamed piece arrived.

:class:`~clio_agent.lm.call_trace.LMCallTrace` marks the start of each call and adds
:func:`finish`'s fields to the record; the agent loop's stream tap
(:func:`clio_agent.gact.agents.clio_react_stream.route_pieces`) marks the first piece.
The first piece is keyed by the GACT session that owns the call, because the tap and the
callback run in different frames of the same call. Two calls in flight at once in one
session share that key, so time to first token is then the earlier call's; the duration
is always exact, since it comes from the call's own start.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

__all__ = ["CallClock", "finish", "first_piece", "start"]

_LOCK = threading.Lock()
_FIRST_PIECE: dict[str, float] = {}


class CallClock:
    """The start of one LM call: monotonic for durations, wall clock for the record."""

    __slots__ = ("monotonic", "session_id", "wall")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.monotonic = time.monotonic()
        self.wall = datetime.now(UTC).isoformat()


def _session_id() -> str:
    try:
        from clio_agent.gact.context import active_session_id  # noqa: PLC0415

        return active_session_id() or ""
    except Exception:  # noqa: BLE001 - off-turn callers (CLI, optimizer) have no session
        return ""


def start() -> CallClock:
    """Start timing a call in the active session; forget an earlier call's first piece."""
    clock = CallClock(_session_id())
    with _LOCK:
        _FIRST_PIECE.pop(clock.session_id, None)
    return clock


def first_piece() -> None:
    """Note a streamed piece; only the first one after a call's start is kept."""
    session_id = _session_id()
    with _LOCK:
        _FIRST_PIECE.setdefault(session_id, time.monotonic())


def finish(clock: CallClock) -> dict[str, Any]:
    """The timing fields for the ``lm.call`` record of the call ``clock`` started."""
    now = time.monotonic()
    with _LOCK:
        first = _FIRST_PIECE.pop(clock.session_id, None)
    fields: dict[str, Any] = {
        "started_at": clock.wall,
        "duration_ms": round((now - clock.monotonic) * 1000),
    }
    if first is not None and first >= clock.monotonic:
        fields["ttft_ms"] = round((first - clock.monotonic) * 1000)
    return fields
