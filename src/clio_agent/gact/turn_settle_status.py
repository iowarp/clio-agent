"""Publish a finishing turn's terminal session status once its slot is released.

A turn settles its session (``idle`` / ``error`` / ``cancelled``) from inside the
turn task, but the task keeps running afterwards: the Stop / loop hooks, the
awaited GOAL judge, the stall monitor. For that whole tail the ``TurnRunner``
still holds the session's slot, so the busy gate answers "a turn is running"
while ``GET /v1/sessions/{sid}`` and ``session.status_changed`` already said
``idle``. A client that waited for the idle status and then posted got a steer
(``delivery=auto``) or a ``session_busy`` 409 (``delivery=start``) for a session
the server had just told it was idle.

:func:`publish_turn_settled_status` makes the ordering truthful: the terminal
status flip and its ``session.status_changed`` event run through
:meth:`TurnRunner.run_when_released`, i.e. after the slot clears and before the
idle hook starts any follow-up turn. Everything else a settle writes (message
count, token/cost roll-up, metadata) still lands immediately.

Only TERMINAL settles go through here. A turn-ending yield to the user
(``waiting_user``: ask-user, plan-exit, hook defer) publishes at once, because
its consumers (question answer, a2ui delivery) act on ``waiting_user`` and
already defer their resume behind the busy gate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent.gact.events import Event

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Typed reason logged when a deferred settle is dropped because another writer
# (e.g. ``POST /cancel``) moved the session status during the turn's tail. That
# later transition is the one the old immediate write would have ended on, so it
# stands; the drop is logged, never silent.
SETTLE_SUPERSEDED_REASON = "turn_settle_superseded"


def publish_turn_settled_status(
    app: "FastAPI",
    sid: str,
    status: str,
    *,
    payload_extra: dict[str, Any] | None = None,
) -> None:
    """Flip ``sid`` to the terminal ``status`` once its turn slot is released.

    Args:
        app: The GACT app (reads ``state.sessions``, ``state.bus``, ``state.turn_runner``).
        sid: The session whose turn is settling.
        status: The terminal status (``idle``, ``error`` or ``cancelled``).
        payload_extra: Extra fields merged into the ``session.status_changed`` payload.
    """

    current = app.state.sessions.get(sid)
    observed = current.status if current is not None else None
    extra = dict(payload_extra or {})

    def _apply() -> None:
        sess = app.state.sessions.get(sid)
        if sess is None:
            logger.info("turn settle skipped: session deleted session=%s status=%s", sid, status)
            return
        if observed is not None and sess.status != observed:
            logger.info(
                "turn settle dropped reason=%s session=%s status=%s superseded_by=%s",
                SETTLE_SUPERSEDED_REASON,
                sid,
                status,
                sess.status,
            )
            return
        app.state.sessions.update(sid, status=status)
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={"session_id": sid, "status": status, "prev_status": "running", **extra},
            )
        )

    runner = getattr(app.state, "turn_runner", None)
    if runner is None:
        _apply()
        return
    runner.run_when_released(sid, _apply)


__all__ = ["SETTLE_SUPERSEDED_REASON", "publish_turn_settled_status"]
