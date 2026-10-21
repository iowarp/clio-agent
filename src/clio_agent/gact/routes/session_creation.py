"""New-session creation, defaults and agent resolution."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact import session_warmup
from clio_agent.gact.agents.resolution import SessionAgentUnavailable, prepare_session_agent
from clio_agent.gact.off_loop import join_off_loop
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.routes.search_assistant import register_search_assistant_routes
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.session_defaults import apply_default_effort
from clio_agent.gact.spotter_watcher import sync_watcher_for_mode
from clio_agent.gact.types import CreateSessionRequest, ErrorEnvelope, ErrorInfo, Session


def register_session_creation_route(app: FastAPI) -> None:
    """Register creation only after the selected agent resolves."""
    register_search_assistant_routes(app)

    @app.post("/v1/sessions", response_model=Session)
    async def create_session(req: CreateSessionRequest, request: Request) -> Session | JSONResponse:
        wid = req.workspace_id or "ws_default"
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        defaults = app.state.session_defaults.get()
        supplied = req.model_fields_set
        metadata = dict(req.metadata)
        apply_default_effort(metadata, defaults)  # provenance-stamped starting level
        requested = req.agent.id if req.agent is not None and req.agent.id else ""
        try:
            resolution = await join_off_loop(prepare_session_agent, app, metadata, requested)
        except SessionAgentUnavailable as exc:
            raise HTTPException(status_code=422, detail=agent_unavailable_envelope(exc)) from exc
        if "model" in supplied:
            model = req.model.model_dump(exclude_none=True) if req.model else None
        elif defaults.provider_id or defaults.model_id:
            model = {"provider_id": defaults.provider_id, "model_id": defaults.model_id}
        else:
            model = None
        sess = await join_off_loop(
            app.state.sessions.create,
            workspace_id=wid,
            title=req.title,
            metadata=metadata,
            model=model,
            agent={"id": resolution.agent["id"]},
            mode=req.mode if "mode" in supplied else defaults.mode,
            edit_mode=req.edit_mode if "edit_mode" in supplied else defaults.edit_mode,
            routing_mode=(
                req.routing_mode if "routing_mode" in supplied else defaults.routing_mode
            ),
            approval_mode=(
                req.approval_mode if "approval_mode" in supplied else defaults.approval_mode
            ),
        )
        bringup_timing.timer_for_session(app, sess.id).start_phase("session.create")
        # Session creation inherits territory and emits no fabricated grant (#979.2).
        await asyncio.to_thread(sync_watcher_for_mode, app, sess)
        bringup_timing.timer_for_session(app, sess.id).end_phase("session.create")
        # Start the session's servers now, not on its first message.
        session_warmup.start_session_warmup(app, sess.id, trigger="session_created")
        return project_for_request(
            request,
            v3=lambda: JSONResponse(content=session_to_v3(sess), status_code=201),
            v2=lambda: Session(**sess.to_wire()),
        )


def agent_unavailable_envelope(exc: SessionAgentUnavailable) -> dict[str, object]:
    """The 422 body for a session whose agent does not resolve."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="agent_unavailable",
            message=str(exc),
            details={"agent_ref": exc.ref, "reason": exc.reason, "errors": exc.errors},
            recoverable=True,
        )
    ).model_dump(exclude_none=True)
