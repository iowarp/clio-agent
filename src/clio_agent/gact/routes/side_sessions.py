"""``POST /v1/sessions/{sid}/side-sessions``: open a read-only aside on a session.

The lifecycle and posture live in :mod:`clio_agent.gact.side_sessions`; this
module is only the HTTP surface. Closing an aside is the ordinary
``DELETE /v1/sessions/{side_id}``; its turns are ordinary
``POST /v1/sessions/{side_id}/messages``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact.routes._body import json_body
from clio_agent.gact.side_sessions import is_read_only_side_session, open_side_session
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session


def _error(status: int, error: str, message: str, sid: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=error,
                message=message,
                details={"session_id": sid},
                recoverable=status != 404,
            )
        ).model_dump(exclude_none=True),
    )


def register_side_session_routes(
    app: FastAPI,
    *,
    copy_context: Callable[[str, str], Awaitable[int]],
    delete_session: Callable[[str], Awaitable[Any]],
) -> None:
    """Register the aside route.

    Args:
        app: The GACT app.
        copy_context: The fork's context copy (messages + context files).
        delete_session: The ordinary session-delete handler, used to retire a
            superseded aside with the full cleanup a user delete performs.
    """

    @app.post("/v1/sessions/{sid}/side-sessions", status_code=201)
    async def create_side_session(sid: str, request: Request) -> JSONResponse:
        """Open a read-only aside on ``sid`` with its context.

        Body: ``{"selection": {"text": "...", "message_id": "..."}}`` (optional).
        """

        parent = app.state.sessions.get(sid)
        if parent is None:
            raise _error(404, "not_found", f"session not found: {sid}", sid)
        if is_read_only_side_session(parent):
            raise _error(
                422,
                "side_session_nested",
                "a side conversation cannot open another side conversation",
                sid,
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/side-sessions")
        side = await open_side_session(
            app,
            parent,
            selection=body.get("selection"),
            copy_context=copy_context,
            retire=delete_session,
        )
        return JSONResponse(
            status_code=201,
            content=Session(**side.to_wire()).model_dump(exclude_none=True),
        )


__all__ = ["register_side_session_routes"]
