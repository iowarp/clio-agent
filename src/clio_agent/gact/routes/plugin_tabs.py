"""Plugin tab routes (host semantics HS3): list, open, close, and act on a tab."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.plugin_tabs import PluginTabError, plugin_tabs
from clio_agent.gact.protocol_v3 import A2UI_V091
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def register_plugin_tab_routes(app: FastAPI) -> None:
    """Register the per-session plugin tab routes."""

    def require_session(sid: str) -> None:
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")

    @app.get("/v1/sessions/{sid}/plugin-tabs")
    async def list_plugin_tabs(sid: str) -> dict[str, Any]:
        require_session(sid)
        return {"tabs": plugin_tabs(app).list(sid)}

    @app.post("/v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/open")
    async def open_plugin_tab(sid: str, plugin: str, tab: str) -> dict[str, Any]:
        require_session(sid)
        try:
            return await plugin_tabs(app).open(sid, plugin, tab)
        except PluginTabError as exc:
            raise _error(exc.status, exc.reason, str(exc)) from exc

    @app.post("/v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/close")
    async def close_plugin_tab(sid: str, plugin: str, tab: str) -> dict[str, Any]:
        require_session(sid)
        return await plugin_tabs(app).close(sid, plugin, tab)

    @app.post("/v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/actions")
    async def plugin_tab_action(
        sid: str, plugin: str, tab: str, request: Request
    ) -> dict[str, Any]:
        if getattr(request.state, "a2ui_protocol_version", None) != A2UI_V091:
            raise _error(406, "unsupported_protocol", f"A2UI {A2UI_V091} must be negotiated")
        require_session(sid)
        body = await json_body(
            request, route="POST /v1/sessions/{sid}/plugin-tabs/{plugin}/{tab}/actions"
        )
        try:
            return await plugin_tabs(app).action(sid, plugin, tab, body)
        except PluginTabError as exc:
            raise _error(exc.status, exc.reason, str(exc)) from exc


__all__ = ["register_plugin_tab_routes"]
