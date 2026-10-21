"""User conversation forks, separate from delegated execution lifecycles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from clio_agent.gact.conversation_origin import FORK_ORIGIN_KEY
from clio_agent.gact.message_marks import saved_message_marks
from clio_agent.gact.protocol_v3 import project_for_request, session_to_v3
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.runtime.permission_policies import inherit_child_session_policies
from clio_agent.gact.session_kind import session_kind
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Session


def register_session_fork_route(
    app: FastAPI, *, copy_context: Callable[[str, str, str], Awaitable[int]]
) -> None:
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
        title = body.get("title") or f"{sess.title} (fork)"
        # Copy conversation configuration, without task ownership or pending work.
        metadata = {
            key: value
            for key, value in sess.metadata.items()
            if key
            in {
                "agent_ref",
                "agent_delta",
                "agent_resolution",
                "effort",
                "effort_source",
                "thinking_level",
                "branch",
                "git_branch",
            }
        }
        metadata["session_kind"] = "branch"
        agent = {key: value for key, value in sess.agent.items() if key != "mode"}
        new_sess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=title,
            parent_session_id=sid,
            model=dict(sess.model),
            agent=agent,
            metadata=metadata,
            mode=sess.mode,
            edit_mode=sess.edit_mode,
            routing_mode=sess.routing_mode,
            approval_mode=sess.approval_mode,
            approval_profile=sess.approval_profile,
        )
        inherit_child_session_policies(app, sid, new_sess.id)
        copied = await copy_context(sid, new_sess.id, target)
        copied_messages = app.state.messages.get(new_sess.id, [])
        app.state.sessions.update(
            new_sess.id,
            message_count=copied,
            metadata_patch={
                FORK_ORIGIN_KEY: {
                    "session_id": sid,
                    "message_id": copied_messages[-1].id if copied_messages else "",
                    "message_count": copied,
                    "title": sess.title,
                    "created_at": new_sess.created_at,
                },
                "message_marks": saved_message_marks(
                    sess.metadata,
                    {message.id for message in app.state.messages.get(new_sess.id, [])},
                ),
            },
        )
        return project_for_request(
            request,
            v2=lambda: JSONResponse(
                status_code=201,
                content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
            ),
            v3=lambda: JSONResponse(status_code=201, content=session_to_v3(new_sess)),
        )
