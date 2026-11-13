"""Routes a surface uses for its view's context and tools (MA9, MA11, MA13).

Bound to the app by the same capability as the other app routes (``data_ref``, checked
by the ``resolve`` the app routes pass in).

* ``PUT .../model-context``: the view's latest ``ui/update-model-context``, stored in
  clio-core (:mod:`clio_agent.gact.mcp_app_context`); over 128 KiB is a 413.
* ``PUT .../app-tools`` ``{tools: [{name, description, inputSchema}]}``: what the view
  offers now; an empty list withdraws them. A list over the limits offers nothing and
  answers with the typed reason. Registering re-sends any call still waiting on the app
  (a surface that mounted after the call was published).
* ``POST .../app-tool-results/{call_id}`` ``{result: CallToolResult}`` or
  ``{error: {message}}``: the view's answer to one waiting call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.mcp_app_context import AppContextTooLarge, store_app_context
from clio_agent.gact.mcp_app_tools import AppToolsRejected, app_tool_table, publish_call
from clio_agent.gact.routes._body import json_body
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

__all__ = ["register_app_context_and_tool_routes"]


def register_app_context_and_tool_routes(
    app: FastAPI, resolve: Callable[[str, str, str], "MCPAppRecord"]
) -> None:
    """Register the context and app-tool routes beside the other app routes."""

    @app.put("/v1/sessions/{sid}/mcp-apps/{app_id}/model-context")
    async def update_mcp_app_context(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="PUT MCP App model context")
        try:
            await asyncio.to_thread(store_app_context, app, record, body)
        except AppContextTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        return {}

    @app.put("/v1/sessions/{sid}/mcp-apps/{app_id}/app-tools")
    async def update_mcp_app_tools(
        sid: str, app_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        record = resolve(sid, app_id, data_ref)
        body = await json_body(request, route="PUT MCP App app tools")
        tools = body.get("tools")
        if not isinstance(tools, list):
            raise HTTPException(status_code=422, detail="tools must be a list")
        table = app_tool_table(app)
        try:
            offered, skipped = table.set_tools(record, tools)
        except AppToolsRejected as exc:
            trace.event("APPS", "%s app=%s %s", exc.reason, app_id, exc)
            return {"offered": [], "reason": exc.reason, "message": str(exc)}
        for row in skipped:
            trace.event("APPS", "%s app=%s tool=%r", row["reason"], app_id, row["name"])
        if offered:
            for call in table.pending_for_app(sid, app_id):
                publish_call(app, call)
        return {"offered": offered, "skipped": skipped}

    @app.post("/v1/sessions/{sid}/mcp-apps/{app_id}/app-tool-results/{call_id}")
    async def post_mcp_app_tool_result(
        sid: str, app_id: str, call_id: str, data_ref: str, request: Request
    ) -> dict[str, Any]:
        resolve(sid, app_id, data_ref)
        body = await json_body(request, route="POST MCP App app tool result")
        result = body.get("result")
        error = body.get("error")
        if isinstance(result, Mapping):
            settled = app_tool_table(app).settle(sid, app_id, call_id, result=result)
        elif isinstance(error, Mapping):
            message = str(error.get("message") or "the view returned an error")
            settled = app_tool_table(app).settle(sid, app_id, call_id, error=message)
        else:
            raise HTTPException(status_code=422, detail="result or error is required")
        if not settled:
            raise HTTPException(status_code=404, detail="No call of this app is waiting")
        return {}
