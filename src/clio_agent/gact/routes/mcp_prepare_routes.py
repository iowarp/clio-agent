"""Reconnect a conversation's plugin MCP server.

``POST /v1/sessions/{sid}/mcp/{namespace}/reconnect`` stops the server's current
connection in the session's workspace fleet, clears its preparation state (a ``failed``
one included) and prepares it again in the background: provision, then start once and
list over that connection. The answer is immediate; ``GET /v1/mcp/servers?session_id=``
shows the preparation (``preparing`` with its phase, then ``ready`` or ``failed`` with a
typed reason), and the session's ``infrastructure.dependency.changed`` events follow it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

#: The declared namespace is not among the session agent's MCP servers.
MCP_SERVER_NOT_DECLARED = "mcp_server_not_declared"


def _not_found(error: str, message: str, details: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, details=details, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def reconnect_session_server(app: Any, sid: str, namespace: str) -> dict[str, Any]:
    """Restart ``namespace``'s preparation in ``sid``'s workspace fleet (blocking)."""

    from clio_agent.gact.mcp_background_prepare import prepare_in_background  # noqa: PLC0415
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _gact_app_context,
        _tool_session_context,
    )
    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    agent = getattr(app.state, "agent", None)
    resolve = getattr(agent, "_active_tool_executor", None)
    if not callable(resolve):
        raise HTTPException(status_code=503, detail="The agent is not ready yet")
    with _gact_app_context(app), _tool_session_context(sid):
        executor = resolve()
        specs = getattr(executor, "_clio_namespace_specs", None) or {}
        spec = specs.get(namespace)
        if spec is None:
            raise _not_found(
                MCP_SERVER_NOT_DECLARED,
                f"MCP server {namespace!r} is not declared for this conversation",
                {"session_id": sid, "namespace": namespace},
            )
        reset = getattr(executor, "reset_namespace", None)
        if callable(reset):
            reset(namespace)
        mcp_prepare.forget(namespace)
        prepare_in_background(executor, namespace, spec)
    return {
        "session_id": sid,
        "namespace": namespace,
        "state": mcp_prepare.PREPARING,
        "preparation": mcp_prepare.preparation_state(namespace),
    }


def register_mcp_prepare_routes(app: FastAPI) -> None:
    """Register the plugin-server reconnect route."""

    @app.post("/v1/sessions/{sid}/mcp/{namespace}/reconnect", status_code=202)
    async def reconnect_plugin_mcp_server(sid: str, namespace: str) -> dict[str, Any]:
        """Prepare one of the conversation's MCP servers again (a failed one included)."""

        if app.state.sessions.get(sid) is None:
            raise _not_found("session_not_found", f"session not found: {sid}", {"session_id": sid})
        return await asyncio.to_thread(reconnect_session_server, app, sid, namespace)
