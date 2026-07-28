"""Agent-task API (#948 S2, #950): read the task projection + cancel a task.

Three routes over :class:`~clio_agent.gact.agent_tasks.AgentTaskRegistry` — the
projection the model tools (S5/S6), the UI redo, and mcpui/a2ui all consume:

* ``GET  /v1/sessions/{sid}/agent-tasks`` — every task spawned by a parent session.
* ``GET  /v1/agent-tasks/{task_id}``       — one task record.
* ``POST /v1/agent-tasks/{task_id}/cancel`` — cancel the child's in-flight turn +
  a typed ``cancelled`` transition (idempotent on an already-terminal task).

The paths are ``agent-tasks``, NOT ``tasks``: ``/v1/sessions/{sid}/tasks`` +
``/v1/tasks/{tid}`` are the #18 per-session manual task CRUD (a shipped TUI
Inspector surface over ``app.state.session_tasks``), and S2's original claim of
the same paths silently shadowed its GET by registration order while splitting
``/v1/tasks/{id}`` across two unrelated stores by HTTP method.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from clio_agent.gact.agent_tasks import (
    AGENT_TASK_EVENTS,
    STATUS_CANCELLED,
    persist_agent_task,
    publish_agent_task_event,
)
from clio_agent.gact.live_handle import enqueue_steer_or_raise, project_live_handle
from clio_agent.gact.messaging import raise_on_reserved_metadata
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


class SteerRequest(BaseModel):
    """POST /v1/agent-tasks/{id}/steer body (#1037): a human mid-turn steer.

    ``text`` is the user's out-of-band message to the running child; ``metadata`` is
    optional bookkeeping forwarded verbatim onto the inbox event (mirrors the
    within-session steer POST body).
    """

    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def _not_found(kind: str, ident: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"{kind} not found: {ident}",
                details={f"{kind}_id": ident},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def register_agent_task_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the agent-task read + cancel routes on ``app``."""

    del deps  # symmetry with the other register_*_routes; state is on app.state

    @app.get("/v1/sessions/{sid}/agent-tasks")
    async def list_session_agent_tasks(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _not_found("session", sid)
        tasks = app.state.agent_task_registry.for_parent(sid)
        return {"tasks": [asdict(t) for t in tasks]}

    @app.get("/v1/agent-tasks/{task_id}")
    async def get_agent_task(task_id: str) -> dict[str, Any]:
        task = app.state.agent_task_registry.get(task_id)
        if task is None:
            raise _not_found("task", task_id)
        return asdict(task)

    @app.post("/v1/agent-tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        registry = app.state.agent_task_registry
        task = registry.get(task_id)
        if task is None:
            raise _not_found("task", task_id)
        if task.is_terminal:
            return asdict(task)  # idempotent — already settled

        # No silent divergence: if the child session is gone there is no
        # authoritative store to back a cancel transition — refuse with a typed
        # 409 rather than mutate the projection alone (persist_agent_task raises
        # the same reason as a backstop).
        if app.state.sessions.get(task.child_session_id) is None:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="child_session_gone",
                        message=f"child session for task {task_id} is gone; cannot cancel",
                        details={"task_id": task_id, "child_session_id": task.child_session_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Cancel the child's in-flight turn: cooperative flag/event + a hard task
        # cancel (the same machinery POST /cancel uses), so a running child stops.
        child_sid = task.child_session_id
        app.state.cancel_flags.add(child_sid)
        event = app.state.cancel_events.get(child_sid)
        if event is not None:
            event.set()
        in_flight = app.state.in_flight_turns.get(child_sid)
        if in_flight is not None and not in_flight.done():
            in_flight.cancel()

        now = datetime.now(timezone.utc).isoformat()
        updated = registry.transition(task_id, STATUS_CANCELLED, updated_at=now)
        persist_agent_task(app, updated)
        publish_agent_task_event(app, updated, AGENT_TASK_EVENTS[STATUS_CANCELLED])
        return asdict(updated)

    @app.get("/v1/agent-tasks/{task_id}/live")
    async def get_agent_task_live(task_id: str) -> dict[str, Any]:
        # PURE read-only projection (#1037): assembles task + timeline + handoff +
        # bounded child head from existing stores, mutating nothing. A gone child is
        # tolerated (empty head/timeline); an unknown task is the typed not_found.
        handle = project_live_handle(app, task_id)
        if handle is None:
            raise _not_found("task", task_id)
        return asdict(handle)

    @app.post("/v1/agent-tasks/{task_id}/steer")
    async def steer_task(task_id: str, body: SteerRequest, response: Response) -> dict[str, Any]:
        task = app.state.agent_task_registry.get(task_id)
        if task is None:
            raise _not_found("task", task_id)
        # #1057 B2 (BLOCKER): steer is a THIRD client-writable ingest onto a turn's
        # ``user_msg.metadata``. ``body.metadata`` rides ``enqueue_user_steer`` onto the
        # CHILD inbox event; if the child's running turn ends before the drain,
        # ``drain_inbox_to_new_turn`` merges it into the promoted turn's
        # ``user_msg.metadata`` and a smuggled ``hook_defer_resume`` makes the
        # UserPromptSubmit once-gate skip hook dispatch — the same B2 bypass POST
        # /messages and /retry already reject. Reject (never strip) the reserved key
        # via the shared chokepoint, keyed on the CHILD session where it would land.
        raise_on_reserved_metadata(task.child_session_id, body.metadata)
        # No silent stranding: a terminal/gone/idle child never drains its inbox
        # again, so enqueue_steer_or_raise refuses with a typed 409 child_not_running
        # unless the child has a genuinely running turn; only then does it reuse
        # #1036's producer against the CHILD session.
        enqueue_steer_or_raise(app, task, body.text, body.metadata)
        response.status_code = 202  # accepted-as-steer into the running child's inbox
        return {
            "accepted": True,
            "task_id": task_id,
            "child_session_id": task.child_session_id,
        }
