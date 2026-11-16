"""Conversation-scoped work lifecycle routes."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from clio_agent.gact import conversation_stop_hold
from clio_agent.gact.work_controls import control_work


class WorkAction(BaseModel):
    """An explicit human lifecycle action."""

    model_config = ConfigDict(extra="forbid")
    action: Literal["stop", "restart", "delete"]


class ScheduleEnabled(BaseModel):
    """Stop or restart future occurrences without changing an executing turn."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


def register_work_controls_routes(app: FastAPI) -> None:
    """Register authenticated, user-owned lifecycle actions."""

    @app.post("/v1/sessions/{sid}/work/{kind}/{identity}")
    async def update_work(
        sid: str, kind: Literal["goal", "loop"], identity: str, body: WorkAction
    ) -> dict[str, Any]:
        from clio_agent.gact.goal_control import launch_pending_goal
        from clio_agent.gact.off_loop import run_off_loop

        try:
            result = await run_off_loop(control_work, app, sid, kind, identity, body.action)
        except LookupError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        if body.action == "restart":
            conversation_stop_hold.resume(app.state.sessions, sid)
        if kind == "goal" and body.action == "restart":
            await launch_pending_goal(app, sid)
        return result

    @app.patch("/v1/sessions/{sid}/schedules/{identity}")
    def update_schedule(sid: str, identity: str, body: ScheduleEnabled) -> dict[str, Any]:
        from clio_agent.gact.scheduler import CronError

        row = app.state.schedules.get(identity)
        if row is None or row.session_id != sid:
            raise HTTPException(404, detail="Schedule not found in this conversation")
        try:
            updated = app.state.schedules.set_enabled(identity, body.enabled)
        except CronError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        if not body.enabled:
            app.state.deferred_schedules.discard(identity)
        return updated.to_wire()
