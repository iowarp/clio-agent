"""Server resource routes: read a mounted server's resource, and watch it for updates."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.server_resources import ServerResourceError, server_resources
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def _uri(body: Any) -> str:
    uri = body.get("uri") if isinstance(body, dict) else None
    if not isinstance(uri, str) or not uri:
        raise _error(400, "invalid_request", "the request names no resource uri")
    return uri


def register_server_resource_routes(app: FastAPI) -> None:
    """Register the per-session server resource routes."""

    def require_session(sid: str) -> None:
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")

    @app.post("/v1/sessions/{sid}/mcp/{namespace}/resources/read")
    async def read_server_resource(sid: str, namespace: str, request: Request) -> dict[str, Any]:
        require_session(sid)
        body = await json_body(
            request, route="POST /v1/sessions/{sid}/mcp/{namespace}/resources/read"
        )
        try:
            return await server_resources(app).read(sid, namespace, _uri(body))
        except ServerResourceError as exc:
            raise _error(exc.status, exc.reason, str(exc)) from exc

    @app.put("/v1/sessions/{sid}/mcp/{namespace}/resources/watch")
    async def watch_server_resource(sid: str, namespace: str, request: Request) -> dict[str, Any]:
        require_session(sid)
        body = await json_body(
            request, route="PUT /v1/sessions/{sid}/mcp/{namespace}/resources/watch"
        )
        holder = body.get("holder") if isinstance(body, dict) else None
        watching = body.get("watching", True) if isinstance(body, dict) else True
        try:
            return await server_resources(app).watch(
                sid,
                namespace,
                _uri(body),
                holder if isinstance(holder, str) and holder else "default",
                watching=watching is not False,
            )
        except ServerResourceError as exc:
            raise _error(exc.status, exc.reason, str(exc)) from exc


__all__ = ["register_server_resource_routes"]
