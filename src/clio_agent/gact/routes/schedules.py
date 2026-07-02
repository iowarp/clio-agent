"""Scheduled-turn routes for the GACT server (#714, iowarp/clio-agent#21).

The "schedules" concern is CLIO's cron-driven turn surface: a session can
register cron expressions that the boot-time scheduler tick
(``_scheduler_tick`` in :mod:`clio_agent.gact.app`) fires as background turns.
These routes are the CRUD over the :class:`~clio_agent.gact.scheduler.ScheduleStore`
that the tick loop reads:

* ``GET /v1/sessions/{sid}/schedules`` -- list a session's scheduled turns.
* ``POST /v1/sessions/{sid}/schedules`` -- add a ``cron`` + ``question`` schedule.
* ``DELETE /v1/schedules/{schedule_id}`` -- delete a schedule (policy-gated).

Cron expressions are evaluated in UTC only (#766); the list envelope carries
``cron_timezone: "utc"`` so clients need not guess.

The store lives on ``app.state.schedules`` and the scheduler tick task owns the
actual firing, so these handlers only mutate the store; they never duplicate the
background-turn launch path. The delete route is a direct destructive action, so
it routes through the shared permission/audit guard carried on
:class:`~clio_agent.gact.routes.deps.GactDeps`. The module imports only leaf
packages (types, stdlib, FastAPI) and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_schedules_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the scheduled-turn routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the schedule store / sessions through ``app.state``. The delete route
    reaches the shared direct-destructive-action guard through ``deps``; the
    actual scheduled-turn firing is owned by the scheduler tick task in
    :mod:`clio_agent.gact.app`, so these handlers only mutate the store.
    """

    @app.get("/v1/sessions/{sid}/schedules")
    async def list_schedules(sid: str) -> dict[str, Any]:
        """List a session's scheduled turns. Cron fires in UTC only."""
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = [s.to_wire() for s in app.state.schedules.list(session_id=sid)]
        # Cron expressions are evaluated in UTC only; say so on the wire (#766).
        return {"schedules": rows, "cron_timezone": "utc"}

    @app.post("/v1/sessions/{sid}/schedules")
    async def add_schedule(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cron = (body.get("cron") or "").strip()
        question = (body.get("question") or "").strip()
        if not cron or not question:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required fields: cron + question",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sch = app.state.schedules.add(session_id=sid, cron=cron, question=question)
        return sch.to_wire()

    @app.delete("/v1/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> Response:
        sch = app.state.schedules.get(schedule_id)
        if sch is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sess = app.state.sessions.get(sch.session_id)
        deps.guard_direct_destructive_action(
            app,
            session_id=sch.session_id,
            workspace_id=getattr(sess, "workspace_id", ""),
            tool_name="gact.schedule.delete",
            args={"schedule_id": schedule_id, "session_id": sch.session_id},
            summary=f"delete schedule {schedule_id}",
            reason="user_requested_schedule_delete",
        )
        existed = app.state.schedules.delete(schedule_id)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Response(status_code=204)
