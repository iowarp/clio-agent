"""Scheduled-turn routes for the GACT server (#714, iowarp/clio-agent#21).

The "schedules" concern is CLIO's cron-driven turn surface: a session can
register cron expressions that the boot-time scheduler tick
(``_scheduler_tick`` in :mod:`clio_agent.gact.app`) fires as background turns.
These routes are the CRUD over the :class:`~clio_agent.gact.scheduler.ScheduleStore`
that the tick loop reads:

* ``GET /v1/sessions/{sid}/schedules`` -- list a session's scheduled turns.
* ``POST /v1/sessions/{sid}/schedules`` -- add a ``cron``/``run_at`` + ``question`` schedule.
* ``DELETE /v1/schedules/{schedule_id}`` -- delete a schedule (policy-gated, cancel-both).

Cron expressions are evaluated in each schedule's own ``timezone`` (P4.3 #1081 — local
wall clock, DST-correct); every row carries its ``timezone`` and the list envelope's
``cron_timezone`` reports the server's default local zone so clients need not guess.

The store lives on ``app.state.schedules`` and the scheduler tick task owns the
actual firing, so these handlers only mutate the store; they never duplicate the
background-turn launch path. The delete route is a direct destructive action, so
it routes through the shared permission/audit guard carried on
:class:`~clio_agent.gact.routes.deps.GactDeps`. The module imports only leaf
packages (types, stdlib, FastAPI) and never loads :mod:`clio_agent.gact.app`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

from clio_agent.gact.cron_tools import cancel_schedule
from clio_agent.gact.off_loop import join_off_loop, run_off_loop
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.scheduler import CronError, default_timezone_name
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.gact.work_state import retained_work_history, work_snapshot

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _work_rows(app: FastAPI, sid: str) -> tuple[list[Any], list[dict[str, Any]]]:
    """The session's messages and its current schedules, read with blocking store I/O."""
    messages = getattr(app.state, "messages", {})
    rows = list(messages.get(sid, [])) if hasattr(messages, "get") else []
    schedule_store = getattr(app.state, "schedules", None)
    schedules = (
        [row.to_wire() for row in schedule_store.list(session_id=sid)]
        if schedule_store is not None
        else []
    )
    return rows, schedules


def register_schedules_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the scheduled-turn routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the schedule store / sessions through ``app.state``. The delete route
    reaches the shared direct-destructive-action guard through ``deps``; the
    actual scheduled-turn firing is owned by the scheduler tick task in
    :mod:`clio_agent.gact.app`, so these handlers only mutate the store.
    """

    from clio_agent.gact.routes.work_controls import register_work_controls_routes
    from clio_agent.gact.session_plan import (
        PlanError,
        mark_person_edit,
        rail_plan,
        update_plan,
    )

    register_work_controls_routes(app)

    @app.get("/v1/sessions/{sid}/work")
    async def session_work(sid: str, cursor: int = Query(default=0, ge=0)) -> dict[str, Any]:
        """Read work state without changing, starting, pausing, or judging work."""
        session = app.state.sessions.get(sid)
        if session is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        snapshot = work_snapshot(session.metadata or {}, cursor)
        # Off the loop: the first read after a restart rehydrates the session's resident
        # ledger from the store, which the loop thread must never wait on (#1334).
        rows, current_schedules = await run_off_loop(_work_rows, app, sid)
        snapshot.update(
            retained_work_history(
                rows,
                current_todos=snapshot["todos"],
                current_schedules=current_schedules,
                cursor=cursor,
            )
        )
        snapshot["schedules"] = current_schedules
        snapshot["plan"] = rail_plan(session)
        return snapshot

    @app.put("/v1/sessions/{sid}/plan")
    async def save_session_plan(sid: str, request: Request) -> dict[str, Any]:
        """Save the person's edit of the session plan (the Work rail's Edit).

        The edited plan reaches the agent at its next step, mid-turn too, marked as the
        person's edit (working state, :func:`working_state.offer_person_edit`).
        """
        session = app.state.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"session not found: {sid}")
        body = await json_body(request, route="PUT /v1/sessions/{sid}/plan")
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(status_code=422, detail="content must be the whole plan as text")
        try:
            await join_off_loop(update_plan, app, sid, content=content)
        except PlanError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await join_off_loop(mark_person_edit, app, sid)
        return {"plan": rail_plan(app.state.sessions.get(sid))}

    @app.get("/v1/sessions/{sid}/schedules")
    async def list_schedules(sid: str) -> dict[str, Any]:
        """List a session's scheduled turns. Cron fires in UTC only."""
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = [s.to_wire() for s in app.state.schedules.list(session_id=sid)]
        # Each row carries its own ``timezone``; the envelope reports the server's default
        # local zone (P4.3 #1081 — cron is evaluated in local wall clock, DST-correct).
        return {"schedules": rows, "cron_timezone": default_timezone_name()}

    @app.post("/v1/sessions/{sid}/schedules")
    async def add_schedule(sid: str, request: Request) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/schedules")
        cron = (body.get("cron") or "").strip()
        run_at = (body.get("run_at") or "").strip()
        question = (body.get("question") or "").strip()
        if not question or not (cron or run_at or int(body.get("delay_s") or 0) > 0):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required fields: question + a trigger (cron | run_at | delay_s)",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            sch = app.state.schedules.create(
                session_id=sid,
                question=question,
                cron=cron,
                run_at=run_at,
                delay_s=int(body.get("delay_s") or 0),
                recurring=bool(body.get("recurring", True)),
                timezone_name=str(body.get("timezone") or ""),
                max_fires=int(body.get("max_fires") or 0),
                until=str(body.get("until") or ""),
                overlap_policy=str(body.get("overlap_policy") or "queue"),
            )
        except CronError as exc:
            # Typed clamp/validation rejection -> 422 with the machine-readable reason.
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(error=exc.reason, message=str(exc), recoverable=True)
                ).model_dump(exclude_none=True),
            ) from exc
        return sch.to_wire()

    def delete_schedule_sync(schedule_id: str) -> Response:
        sch = app.state.schedules.get(schedule_id)
        if sch is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
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
        # Cancel-both: drop the store row AND clear any daemon-side deferred entry so no
        # orphan tick survives (#1081).
        existed = cancel_schedule(app, schedule_id)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Response(status_code=204)

    @app.delete("/v1/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> Response:
        return await join_off_loop(delete_schedule_sync, schedule_id)
