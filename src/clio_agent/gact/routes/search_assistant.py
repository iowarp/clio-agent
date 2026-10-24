"""Isolated read-only sessions that run the CLIO Search agent."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from clio_agent.gact.agents.resolution import SessionAgentUnavailable, prepare_session_agent
from clio_agent.gact.fleet_settings import fleet_settings
from clio_agent.gact.off_loop import join_off_loop
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.search_assistant import SEARCH_AGENT, read_search_source
from clio_agent.gact.side_sessions import READ_ONLY_SIDE_PROFILE
from clio_agent.gact.types import Session


class SearchSessionRequest(BaseModel):
    """The search scope and optional conversation whose model is inherited."""

    model_config = ConfigDict(extra="forbid")
    workspace_id: str = "ws_default"
    parent_session_id: str = ""


def register_search_assistant_routes(app: FastAPI) -> None:
    """Create an isolated read-only session only when a person submits an AI search."""

    @app.get("/v1/workspaces/{workspace_id}/files/passage")
    async def file_passage(
        workspace_id: str, path: str = Query(min_length=1), line: int = Query(default=1, ge=1)
    ) -> dict[str, object]:
        if app.state.workspaces.get(workspace_id) is None:
            raise HTTPException(404, detail="Workspace is unavailable")
        try:
            return await read_search_source(
                app,
                workspace_id=workspace_id,
                path=path,
                start_line=max(1, line - 20),
                max_lines=120,
            )
        except ValueError as exc:
            raise HTTPException(415, detail=str(exc)) from exc

    @app.post("/v1/search/sessions", response_model=Session)
    async def open_search(req: SearchSessionRequest, request: Request) -> Session | JSONResponse:
        if app.state.workspaces.get(req.workspace_id) is None:
            raise HTTPException(404, detail="Workspace is unavailable")
        parent = app.state.sessions.get(req.parent_session_id) if req.parent_session_id else None
        if req.parent_session_id and (parent is None or parent.workspace_id != req.workspace_id):
            raise HTTPException(404, detail="Conversation is unavailable in this workspace")
        metadata: dict[str, object] = {
            "search_session": {"ephemeral": True},
            "side_session": {"read_only": True, "ephemeral": True},
        }
        try:
            resolution = await join_off_loop(prepare_session_agent, app, metadata, SEARCH_AGENT)
        except SessionAgentUnavailable as exc:
            from clio_agent.gact.routes.session_creation import (  # noqa: PLC0415
                agent_unavailable_envelope,
            )

            raise HTTPException(422, detail=agent_unavailable_envelope(exc)) from exc
        choice = fleet_settings()["search"]
        defaults = app.state.session_defaults.get()
        model = (
            choice
            or (parent.model if parent else None)
            or (
                {"provider_id": defaults.provider_id, "model_id": defaults.model_id}
                if defaults.provider_id or defaults.model_id
                else None
            )
        )
        session = app.state.sessions.create(
            workspace_id=req.workspace_id,
            title="CLIO Search",
            metadata=metadata,
            model=model,
            agent={"id": resolution.agent["id"]},
            approval_profile=READ_ONLY_SIDE_PROFILE,
        )
        return project_for_request(
            request,
            v3=lambda: JSONResponse(content=session_to_v3(session), status_code=201),
            v2=lambda: Session(**session.to_wire()),
        )
