"""User conversation forks, separate from delegated execution lifecycles."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session


def register_session_fork_route(app: FastAPI) -> None:
    """Register branching for user conversations and existing user branches only."""

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> Response:
        """Copy history through an optional message into a new user conversation branch."""
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        kind = session_kind(sess)
        if kind not in {"conversation", "branch"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="fork_not_allowed",
                        message=(
                            "Only conversations and user branches can be branched. "
                            "Branch from the parent conversation instead."
                        ),
                        details={"session_id": sid, "session_kind": kind},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        body = await json_body(request, route="POST /v1/sessions/{sid}/fork")
        target = body.get("at_message_id", "")
        if target is None:
            target = ""
        if not isinstance(target, str):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="at_message_id must be a string",
                        details={"field": "at_message_id"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if target and not any(message.id == target for message in app.state.messages.get(sid, [])):
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found in this conversation: {target}",
                        details={"session_id": sid, "message_id": target},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        from clio_agent.gact.conversation_copy import create_branch  # noqa: PLC0415
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        new_sess = await run_off_loop(
            create_branch, app, sess, title=str(body.get("title") or ""), at=target
        )
        return project_for_request(
            request,
            v2=lambda: JSONResponse(
                status_code=201,
                content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
            ),
            v3=lambda: JSONResponse(status_code=201, content=session_to_v3(new_sess)),
        )
