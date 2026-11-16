"""Conversation Stop: the turn, and on request everything the conversation started.

``children="keep"`` ends the turn and its waiter; accepted work continues and reports
back. ``children="stop"`` also cancels the conversation's current assignment through
the ordinary subtree cancellation (or, once that assignment has ended, each open task
it started), recorded with ``reason: conversation_stop``. See
``docs/design/stop-and-restart.md``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from clio_agent.gact import conversation_stop_hold
from clio_agent.gact.events import Event
from clio_agent.gact.human_controls import intervene

#: What Stop does to the work the conversation started.
StopChildren = Literal["keep", "stop"]

#: The reason recorded on a cancellation that a conversation Stop requested.
CONVERSATION_STOP = "conversation_stop"


class StopRequest(BaseModel):
    """The optional body of ``POST /v1/sessions/{sid}/cancel``."""

    model_config = ConfigDict(extra="forbid")
    children: StopChildren = "keep"


def stop_scope(app: Any, sid: str) -> list[dict[str, Any]]:
    """The open work Stop can reach: the conversation's own tasks and direct children.

    The conversation's own assignments are the turn itself and are not listed; a
    child's descendants are covered by cancelling the child.
    """
    history = app.state.task_history
    rows: list[dict[str, Any]] = []
    with history.read_view():
        own = history.session_handles(sid)
        for handle in history.tree_handles(sid):
            task = history.task_snapshot(handle)
            if task is None or task.terminal_at is not None:
                continue
            if task.owner.session_id == sid and task.kind in {"Session", "Subagent"}:
                continue
            if task.owner.session_id != sid and task.owner.parent_handle not in own:
                continue
            rows.append(
                {
                    "handle": task.handle,
                    "kind": task.kind,
                    "label": task.label,
                    "description": task.description or "",
                    "status": task.status,
                    "activity": task.activity,
                    "session_id": task.owner.session_id,
                    "created_at": task.created_at,
                }
            )
    return sorted(rows, key=lambda row: row["created_at"])


async def _stop_children(app: Any, sid: str, assignment: Any) -> dict[str, Any]:
    """Cancel what the conversation started; every request names ``conversation_stop``."""
    scope = await app.state.task_history.io.run(stop_scope, app, sid)
    current = (
        await app.state.task_history.io.run(app.state.task_history.task_snapshot, assignment.handle)
        if assignment is not None
        else None
    )
    # Everything open, each by its own handle: a turn's cancel does not reach a tool call
    # the turn is waiting on (that call outlives the reply on "Stop reply only").
    targets = [row["handle"] for row in scope]
    if current is not None and current.terminal_at is None and scope:
        targets.insert(0, current.handle)
    requests: list[dict[str, Any]] = []
    for handle in targets:
        try:
            effect = await intervene(app, handle, "cancel", reason=CONVERSATION_STOP)
        except ValueError as error:
            requests.append({"handle": handle, "error": str(error)})
        else:
            requests.append(
                {
                    "handle": handle,
                    "cancellation_requested": bool(effect.get("cancellation_requested")),
                    "status": effect.get("status", "cancellation_requested"),
                }
            )
    return {"stopped_children": scope, "cancellations": requests}


async def cancel_session_state_async(
    app: Any, deps: Any, sid: str, children: StopChildren = "keep"
) -> dict[str, Any]:
    """Show Stopping until the session's actual worker retires.

    ``children`` says what happens to the work the conversation started: ``keep``
    leaves it running, ``stop`` cancels it with a recorded ``conversation_stop``.
    """
    if children not in {"keep", "stop"}:
        raise ValueError("children must be 'keep' or 'stop'")
    from fastapi import HTTPException

    from clio_agent.gact.autonomous_loop import stop_session_loop
    from clio_agent.gact.composer_runtime import stop_session_composer_autostart
    from clio_agent.gact.goal import stop_session_goal

    session = app.state.sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session unavailable")
    assignment = app.state.task_history.latest_assignment(sid)
    runner = app.state.turn_runner
    reservation = runner.request_stop(sid)
    stopping = reservation is not None
    await app.state.task_history.io.run(stop_session_loop, app, sid)
    await app.state.task_history.io.run(stop_session_goal, app, sid)
    composer = await app.state.task_history.io.run(stop_session_composer_autostart, app, sid)
    if assignment is None:
        effect = {
            "status": "stopping" if stopping else "idle",
            "accepted_tasks_continue": True,
            "composer_autostart": composer,
            "children": children,
        }
        await app.state.task_history.io.run(app.state.sessions.update, sid, status=effect["status"])

        async def retire() -> None:
            await runner.stop(sid, reservation=reservation)

            def project_idle() -> None:
                with app.state.task_history.serialized():
                    if runner.busy(sid):
                        return
                    app.state.sessions.update(sid, status="idle")
                    app.state.bus.publish(
                        Event(
                            type="session.status_changed",
                            session_id=sid,
                            payload={"status": "idle"},
                        )
                    )

            await app.state.task_history.io.run(project_idle)

        if stopping:
            app.state.conversations.retain_control(
                "stop-reservation:" + sid, asyncio.create_task(retire())
            )
        app.state.bus.publish(
            Event(
                type="session.status_changed", session_id=sid, payload={"session_id": sid, **effect}
            )
        )
        return {"session_id": sid, **effect}
    effect = await intervene(
        app, assignment.handle, "stop", stop_reservation=reservation, stop_reservation_captured=True
    )
    effect = {**effect, "composer_autostart": composer, "children": children}
    if children == "stop":
        await app.state.task_history.io.run(conversation_stop_hold.stop, app.state.sessions, sid)
        effect.update(await _stop_children(app, sid, assignment), accepted_tasks_continue=False)
    await app.state.task_history.io.run(app.state.sessions.update, sid, status=effect["status"])
    app.state.bus.publish(
        Event(type="session.status_changed", session_id=sid, payload={"session_id": sid, **effect})
    )
    return {"session_id": sid, **effect}


def cancel_session_state(
    app: Any, deps: Any, sid: str, children: StopChildren = "keep"
) -> dict[str, Any]:
    """Synchronous action callers use the same application-owned Stop operation."""
    from clio_agent.gact.tool_dispatch import call_on_application

    return call_on_application(
        app.state.mcp_app_loop, cancel_session_state_async(app, deps, sid, children)
    )
