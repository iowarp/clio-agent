"""A settled root assignment to own work a person starts before a session's first turn.

Every task is owned by its session's latest assignment. A turn creates one, so work the
agent starts always has an owner. A person can start work with no turn: a click in a
view or plugin tab calls a tool on an external server. When the session has never run a
turn there is no assignment, so :func:`owning_assignment` records one already settled,
as a finished turn leaves it. It holds no mail, so no turn runs, and the session's next
message starts its successor as after any finished turn.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import (
    AssignmentDetails,
    BackendReference,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.gact.native_work import now
from clio_agent.tools import call_origin

#: Invocation id prefix of an anchor, so its records name where it came from.
ANCHOR_INVOCATION = "person-anchor:"


async def owning_assignment(app: Any, sid: str, description: str) -> TaskSnapshot | None:
    """The assignment that owns a call in ``sid``, anchored for a person's call if needed.

    Returns the session's latest assignment. With none, a call a person makes from a
    view (:mod:`clio_agent.tools.call_origin`) gets a new anchor; the agent's own call
    gets ``None``, since a turn always created its owner first.
    """

    history = app.state.task_history
    # A view read: it never waits behind queued writes on the history writer.
    current = await asyncio.to_thread(history.latest_assignment, sid)
    if current is not None or call_origin.current() is None:
        return current
    return await history.io.run(anchor_assignment, app, sid, description)


def anchor_assignment(app: Any, sid: str, description: str) -> TaskSnapshot:
    """The latest assignment of ``sid``, or a new settled root one when it has none."""

    history = app.state.task_history
    with history.serialized():
        current = history.latest_assignment(sid)
        if current is not None:
            return current
        session = app.state.sessions.get(sid)
        if session is None:
            raise ValueError("Assignment session is unavailable")
        startup_error = app.state.sessions.startup_retirement_error(sid)
        if startup_error:
            raise ValueError(startup_error)
        human = str(session.metadata.get("owner_human_id") or app.state.human_principal)
        handle, stamp = "task_" + uuid4().hex, now()
        details = history.write_content(
            sid, AssignmentDetails(disposition="complete").model_dump(mode="json")
        )
        task = TaskSnapshot(
            handle=handle,
            kind="Session",
            owner=TaskOwner(human_id=human, session_id=sid, assignment_id=handle),
            label=session.title,
            description=description,
            description_missing=False,
            invocation_id=ANCHOR_INVOCATION + uuid4().hex,
            tool="conversation",
            created_at=stamp,
            updated_at=stamp,
            backend=BackendReference(
                owner="conversation", server="clio", session=sid, task_id=handle
            ),
            raw_status="completed",
            status="completed",
            settlement="confirmed",
            terminal_at=stamp,
            details=details,
            supported_actions=("observe", "message"),
        )
        history.record_snapshot(task, observation_id=uuid4().hex)
        return task
