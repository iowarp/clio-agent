"""Agent-task and uniform run-projection API (#948 S2, #1037, #1127).

The established session-task list/get/cancel/live/steer routes remain intact. P2.10
adds list/detach/dismiss over the uniform runs projection; those reads/actions source
``AgentTaskRegistry`` plus existing durable relay handles and create no fifth store:

* ``GET  /v1/runs`` — local and relay runs with uniform live tickers.
* ``POST /v1/runs/{handle_id}/detach|dismiss`` — display lifecycle, never cancellation.
* ``GET  /v1/sessions/{sid}/agent-tasks`` — every task spawned by a parent session.
* ``GET  /v1/agent-tasks/{task_id}`` — one task record.
* ``POST /v1/agent-tasks/{task_id}/cancel`` — placement-aware cancellation.
* ``GET|POST /v1/agent-tasks/{task_id}/live|steer`` — human live interaction.

The paths are ``agent-tasks``, NOT ``tasks``: ``/v1/sessions/{sid}/tasks`` +
``/v1/tasks/{tid}`` are the #18 per-session manual task CRUD (a shipped TUI
Inspector surface over ``app.state.session_tasks``), and S2's original claim of
the same paths silently shadowed its GET by registration order while splitting
``/v1/tasks/{id}`` across two unrelated stores by HTTP method.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from clio_agent.gact.live_handle import enqueue_steer_or_raise, project_live_handle
from clio_agent.gact.messaging import raise_on_reserved_metadata
from clio_agent.gact.run_registry import detach_run, dismiss_run, project_runs
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

    @app.get("/v1/runs")
    async def list_runs() -> dict[str, Any]:
        # Pure union projection over AgentTaskRegistry + durable relay task handles.
        return {"runs": project_runs(app)}

    @app.post("/v1/runs/{handle_id}/detach")
    async def detach_run_handle(handle_id: str) -> dict[str, Any]:
        run = detach_run(app, handle_id)
        if run is None:
            raise _not_found("run", handle_id)
        return run

    @app.post("/v1/runs/{handle_id}/dismiss")
    async def dismiss_run_handle(handle_id: str) -> dict[str, Any]:
        if not dismiss_run(app, handle_id):
            raise _not_found("run", handle_id)
        return {"dismissed": True, "handle_id": handle_id}

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

        # Placement-aware cancellation uses the same invoker retained by the run.
        from clio_agent.gact.agents.invoker import TaskHandle  # noqa: PLC0415
        from clio_agent.gact.agents.spawn_placement import invoker_for_task  # noqa: PLC0415

        invoker_for_task(app, task).invoker.cancel(TaskHandle.from_task(task))
        updated = registry.get(task_id)
        return asdict(updated if updated is not None else task)

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
