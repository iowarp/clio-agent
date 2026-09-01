"""SPOTTER clearance barrier: typed outcomes + per-exchange progress wait.

Owner module for the half of the ``spotter-ai`` standing watcher
(:mod:`clio_agent.gact.spotter_watcher`) that a PROTECTED PARENT blocks on: the
condition event a watcher signals, and the wait a mutating tool call performs
before it is allowed to overtake pending surveillance.

Three contracts this module owns, none of which the barrier had when it lived
inline in ``spotter_watcher``:

* **Fail CLOSED, always typed.** Every non-clearance outcome is a key of
  :data:`SPOTTER_CLEARANCE_REASONS` (the ``_stream_fallback_reasons`` closed-set
  shape) that reaches the module logger, the trace, the permission audit row and
  the model-facing denial. "Armed mode with no live watcher" is a DENIAL
  (``spotter_watcher_unavailable``), never silent clearance — a session that
  still advertises ``spotter-ai`` must never run mutations unobserved.
* **A per-exchange progress window, never a global wall clock.** The wait is
  bounded by the gap BETWEEN observable watcher progress signals (a wake
  started/enqueued, a check turn finalized, the watcher's turn slot released),
  restarting on every signal — the same no-progress doctrine as
  :mod:`clio_agent.gact.turn_watchdog` ([[clio-no-session-timeout]]). A
  long-but-progressing watcher runs to completion; only a watcher that goes
  silent for a whole window fails closed, with its OWN typed reason
  (``spotter_clearance_progress_stalled``) distinct from a crashed watcher's.
* **Bounded retention.** The per-session event map is released on disarm and
  pruned of sessions with no live watcher left, so it cannot grow one
  :class:`threading.Event` per session id for the process lifetime.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: Clearance granted — the protected call may proceed.
CLEARANCE_GRANTED = ""

#: The session is armed into ``spotter-ai`` but has no live watcher task
#: (arming failed, or the standing task was cancelled — e.g. by the speculative
#: ``POST /v1/sessions/{sid}/cancel`` the TUI fires on Esc, which cascades to
#: every child). Surveillance is OFF while the session still advertises it.
CLEARANCE_WATCHER_UNAVAILABLE = "spotter_watcher_unavailable"

#: The watcher is armed and live, but its most recent check turn FAILED
#: (``live_state == "error"``, carrying its own typed ``error_reason``). The
#: review finished and errored — no waiting was involved.
CLEARANCE_WATCHER_FAILED = "spotter_watcher_check_failed"

#: The watcher is armed, live and healthy, but published no observable progress
#: for a whole progress window (see :func:`clearance_progress_timeout_s`).
CLEARANCE_PROGRESS_STALLED = "spotter_clearance_progress_stalled"

#: Closed set of typed clearance denials -> the model-facing explanation. Every
#: fail-closed return of :func:`wait_for_spotter_clearance` is a key here, so a
#: caller can neither invent a reason nor lose the distinction between them.
SPOTTER_CLEARANCE_REASONS: dict[str, str] = {
    CLEARANCE_WATCHER_UNAVAILABLE: (
        "SPOTTER surveillance is armed for this session but no watcher is running, "
        "so this tool call was not run."
    ),
    CLEARANCE_WATCHER_FAILED: (
        "SPOTTER's review of the preceding workload evidence failed, so this tool call was not run."
    ),
    CLEARANCE_PROGRESS_STALLED: (
        "SPOTTER stopped making observable progress while reviewing the preceding "
        "workload evidence, so this tool call was not run."
    ),
}


def max_clearance_events() -> int:
    """Retention bound for the per-session clearance-event map.

    Config: ``spotter.max_clearance_events`` /
    ``CLIO_SPOTTER_MAX_CLEARANCE_EVENTS`` (default 256, in entries). Reaching it
    triggers a prune of every session with no live watcher left (bounded-memory
    doctrine); entries for sessions that ARE armed are never evicted, so the
    residual bound is the live armed-watcher count, not the process's session
    history. Raise it only for a deployment running many concurrent protected
    sessions.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.max_clearance_events",
        env="CLIO_SPOTTER_MAX_CLEARANCE_EVENTS",
        default=256,
        cast=conf.as_int,
    )


def _clearance_events(app: "FastAPI") -> dict[str, threading.Event]:
    """Return (creating on demand) the app's per-session clearance-event map."""

    events = getattr(app.state, "spotter_clearance_events", None)
    if not isinstance(events, dict):
        events = {}
        app.state.spotter_clearance_events = events
    return events


def _prune_clearance_events(app: "FastAPI", events: dict[str, threading.Event]) -> int:
    """Drop clearance events for sessions that no longer hold a live watcher.

    A dropped event is SET first: any thread still holding a reference wakes,
    re-reads the (now watcher-less) state and fails closed with
    :data:`CLEARANCE_WATCHER_UNAVAILABLE` rather than blocking on an event
    nothing will ever signal again.

    Args:
        app: The GACT app.
        events: The live clearance-event map to prune in place.

    Returns:
        The number of entries released.
    """

    from clio_agent.gact.spotter_watcher import _live_watcher_tasks  # noqa: PLC0415

    stale = [sid for sid in list(events) if not _live_watcher_tasks(app, sid)]
    for sid in stale:
        released = events.pop(sid, None)
        if released is not None:
            released.set()
    if stale:
        logger.info(
            "spotter_clearance_events_pruned reason=watcher_not_live count=%s remaining=%s",
            len(stale),
            len(events),
        )
        trace.event(
            "SPOTTER",
            "spotter_clearance_events_pruned reason=watcher_not_live count=%s remaining=%s",
            len(stale),
            len(events),
        )
    return len(stale)


def clearance_event(app: "FastAPI", parent_session_id: str) -> threading.Event:
    """Return the process-local condition event for one protected session.

    Args:
        app: The GACT app.
        parent_session_id: The protected (``spotter-ai``) session's id.

    Returns:
        The session's clearance event, created on demand. Creating an entry past
        :func:`max_clearance_events` first prunes every session with no live
        watcher left.
    """

    events = _clearance_events(app)
    existing = events.get(parent_session_id)
    if existing is not None:
        return existing
    if len(events) >= max_clearance_events():
        _prune_clearance_events(app, events)
    return events.setdefault(parent_session_id, threading.Event())


def signal_clearance(app: "FastAPI", parent_session_id: str) -> None:
    """Publish one observable watcher-progress signal for a protected session.

    Every signal both wakes a blocked waiter and RESTARTS its progress window
    (see :func:`wait_for_spotter_clearance`), so honest watcher work is never
    read as a stall.

    Args:
        app: The GACT app.
        parent_session_id: The protected (``spotter-ai``) session's id.
    """

    clearance_event(app, parent_session_id).set()


def release_clearance_event(app: "FastAPI", parent_session_id: str) -> bool:
    """Release a protected session's clearance event when its watcher disarms.

    Args:
        app: The GACT app.
        parent_session_id: The session whose watcher just went terminal.

    Returns:
        ``True`` when an entry was released (it is SET first, so any waiter
        wakes and re-evaluates instead of blocking on a dead event).
    """

    events = getattr(app.state, "spotter_clearance_events", None)
    if not isinstance(events, dict):
        return False
    released = events.pop(parent_session_id, None)
    if released is None:
        return False
    released.set()
    return True


def clearance_progress_timeout_s() -> float:
    """The per-exchange watcher-progress window, in seconds.

    Config: ``spotter.clearance_progress_timeout_s`` /
    ``CLIO_SPOTTER_CLEARANCE_PROGRESS_TIMEOUT_S`` (default 180.0). This bounds
    the gap between observable watcher progress signals, NEVER the total time a
    progressing watcher may take.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "spotter.clearance_progress_timeout_s",
        env="CLIO_SPOTTER_CLEARANCE_PROGRESS_TIMEOUT_S",
        default=180.0,
        cast=conf.as_float,
    )


def _clearance_state(app: "FastAPI", parent_session_id: str) -> tuple[str, bool]:
    """Read one protected session's live surveillance state.

    Args:
        app: The GACT app.
        parent_session_id: The protected (``spotter-ai``) session's id.

    Returns:
        ``(denial_reason, pending)``. ``denial_reason`` is a
        :data:`SPOTTER_CLEARANCE_REASONS` key when the barrier must fail closed
        right now, else :data:`CLEARANCE_GRANTED`. ``pending`` is whether the
        watcher still holds active or buffered evidence to settle.
    """

    from clio_agent.gact.spotter_watcher import _live_watcher_tasks  # noqa: PLC0415

    task = next(iter(_live_watcher_tasks(app, parent_session_id)), None)
    if task is None:
        return CLEARANCE_WATCHER_UNAVAILABLE, False
    if task.live_state == "error":
        return CLEARANCE_WATCHER_FAILED, False
    runner = getattr(app.state, "turn_runner", None)
    if runner is not None and runner.busy(task.child_session_id):
        return CLEARANCE_GRANTED, True

    from clio_agent.gact.loop_inbox import inbox_for  # noqa: PLC0415

    return CLEARANCE_GRANTED, inbox_for(app, task.child_session_id).peek_nonempty()


def _record_denial(parent_session_id: str, reason: str, *, window_s: float) -> str:
    """Log + trace one typed fail-closed clearance denial, and return its reason."""

    logger.warning(
        "spotter_clearance_denied reason=%s session=%s progress_window_s=%s",
        reason,
        parent_session_id,
        window_s,
    )
    trace.event(
        "SPOTTER",
        "spotter_clearance_denied reason=%s session=%s progress_window_s=%s",
        reason,
        parent_session_id,
        window_s,
    )
    return reason


def wait_for_spotter_clearance(
    app: "FastAPI", parent_session_id: str, *, progress_timeout_s: Optional[float] = None
) -> str:
    """Wait until all watcher evidence preceding a protected call is settled.

    The watcher stays asynchronous while the parent renders/reasons, but a
    subsequent mutating tool call cannot overtake an active check or a coalesced
    wake. The wait is bounded by a PER-EXCHANGE progress window that restarts on
    every :func:`signal_clearance`, so a legitimately long check turn is never
    mistaken for a stall.

    Args:
        app: The GACT app.
        parent_session_id: The protected session's id. A session that is not in
            ``spotter-ai`` mode (or is gone) clears immediately.
        progress_timeout_s: Override for the progress window; ``None`` resolves
            :func:`clearance_progress_timeout_s` from config.

    Returns:
        :data:`CLEARANCE_GRANTED` (``""``) when the call may proceed, else the
        typed :data:`SPOTTER_CLEARANCE_REASONS` key for the fail-closed denial.
    """

    from clio_agent.gact.spotter_watcher import SPOTTER_APPROVAL_MODE  # noqa: PLC0415

    session = app.state.sessions.get(parent_session_id)
    if session is None or session.approval_mode != SPOTTER_APPROVAL_MODE:
        return CLEARANCE_GRANTED
    window = clearance_progress_timeout_s() if progress_timeout_s is None else progress_timeout_s
    window = max(0.0, window)
    event = clearance_event(app, parent_session_id)
    progress_deadline = time.monotonic() + window
    while True:
        # Clear BEFORE reading the state so a signal published during the read
        # is retained by the wait below instead of being lost.
        event.clear()
        reason, pending = _clearance_state(app, parent_session_id)
        if reason:
            return _record_denial(parent_session_id, reason, window_s=window)
        if not pending:
            return CLEARANCE_GRANTED
        remaining = progress_deadline - time.monotonic()
        if remaining <= 0:
            return _record_denial(parent_session_id, CLEARANCE_PROGRESS_STALLED, window_s=window)
        if event.wait(remaining):
            # Observable watcher progress: restart the per-exchange window.
            progress_deadline = time.monotonic() + window
