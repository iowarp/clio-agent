"""Canonical session-cancellation state transition."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.autonomous_loop import stop_session_loop
from clio_agent.gact.events import Event
from clio_agent.gact.goal import stop_session_goal
from clio_agent.gact.runtime.globals import _new_cancellation_attempt_id
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    import threading

    from clio_agent.gact.routes.deps import GactDeps


def cancellation_grace_s() -> float:
    """Seconds a cooperative cancel gets before the turn task is hard-cancelled.

    Config: ``gact.cancellation_grace_s`` / ``CLIO_GACT_CANCELLATION_GRACE_S``
    (default 0.1). The executor is asked to stop cooperatively first; only a
    task still running after this window is cancelled outright. Raise it to
    give a mid-tool-call turn longer to unwind cleanly.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "gact.cancellation_grace_s",
        env="CLIO_GACT_CANCELLATION_GRACE_S",
        default=0.1,
        cast=conf.as_float,
    )


def cancel_session_state(app: FastAPI, deps: "GactDeps", sid: str) -> dict[str, Any]:
    """Apply the canonical best-effort cancellation transition for a session."""

    sess = app.state.sessions.get(sid)
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"session not found: {sid}",
                    details={"session_id": sid},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )
    app.state.cancel_flags.add(sid)
    event = app.state.cancel_events.get(sid)
    if event is not None:
        event.set()
    from clio_agent.gact.composer_runtime import (  # noqa: PLC0415
        stop_session_composer_autostart,
    )
    from clio_agent.gact.turn_spawn import cancel_children_of  # noqa: PLC0415
    from clio_agent.providers.claude_code_cancel import abort_session_streams  # noqa: PLC0415

    # L1: capture what each stop primitive actually did (real counts/flags) instead
    # of discarding them behind a fabricated ``hard_abort_supported: False`` /
    # ``upstream_abort: "not_supported"`` pair -- see ``_new_cancellation_attempt``.
    children_cancelled = cancel_children_of(app, sid)
    provider_streams_killed = abort_session_streams(sid)
    stop_session_loop(app, sid)
    stop_session_goal(app, sid)
    # The composer planes are turn PRODUCERS too: a residual steer and a queued
    # head would each re-drive the agent the moment the cancelled turn's slot
    # cleared. Quiesce them like the loop/goal producers -- retaining, never
    # deleting, the user's durable intent.
    composer_autostart = stop_session_composer_autostart(app, sid)
    in_flight = app.state.in_flight_turns.get(sid)
    cancellation_pending = in_flight is not None and not in_flight.done()
    attempt = _new_cancellation_attempt(
        sid,
        event=event,
        cancellation_pending=cancellation_pending,
        children_cancelled=children_cancelled,
        provider_streams_killed=provider_streams_killed,
        composer_autostart=composer_autostart,
    )
    app.state.cancel_attempts[sid] = attempt
    if cancellation_pending:
        asyncio.create_task(_cancel_after_grace(app, in_flight, sid, attempt))
    app.state.sessions.update(sid, status="cancelled")
    payload = {
        "session_id": sid,
        "status": "cancelled",
        "prev_status": sess.status,
        "execution_cancellation": "cooperative_pending" if cancellation_pending else "none",
        "cancellation_attempt": deps.cancellation_attempt_summary(attempt),
        "composer_autostart": composer_autostart,
    }
    app.state.bus.publish(Event(type="session.status_changed", session_id=sid, payload=payload))
    return payload


def _new_cancellation_attempt(
    sid: str,
    *,
    event: "threading.Event | None",
    cancellation_pending: bool,
    children_cancelled: int,
    provider_streams_killed: int,
    composer_autostart: dict[str, Any],
) -> dict[str, Any]:
    """Build the durable cancellation-attempt record from what actually happened.

    L1: replaces the always-``False``/``"not_supported"`` ``hard_abort_supported``/
    ``upstream_abort`` pair and the ``executor_work_may_continue`` flag (redundant
    with ``asyncio_task_cancel_scheduled``) with the REAL counts each stop
    primitive returned -- a genuinely-stopped fact instead of a constant.
    """

    return {
        "id": _new_cancellation_attempt_id(),
        "session_id": sid,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "in_flight": cancellation_pending,
        "cooperative_signal_sent": event is not None,
        "asyncio_task_cancel_scheduled": cancellation_pending,
        "asyncio_task_cancel_sent": False,
        "children_cancelled": children_cancelled,
        "provider_streams_killed": provider_streams_killed,
        "composer_autostart_suspended": bool(composer_autostart.get("suspended", False)),
    }


async def _cancel_after_grace(
    app: FastAPI,
    task: asyncio.Task[Any],
    session_id: str,
    attempt: dict[str, Any],
) -> None:
    await asyncio.sleep(cancellation_grace_s())
    if session_id in app.state.cancel_flags and not task.done():
        if app.state.cancel_attempts.get(session_id) is attempt:
            attempt["asyncio_task_cancel_sent"] = True
            attempt["asyncio_task_cancelled_at"] = datetime.now(timezone.utc).isoformat()
        task.cancel()
