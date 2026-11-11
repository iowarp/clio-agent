"""Comment sessions, drafts, threads and workspace-file comment targets (comments plan).

* ``GET /v1/workspaces/{wid}/comment-targets?path=`` names the artifact version a workspace
  file is, without registering it; ``POST`` registers it when it is not one (ledger FB22).
* ``GET /v1/artifacts/{id}/threads`` lists the artifact's threads, re-anchored to its newest
  version, and the label the next mark on version ``id`` takes.
* ``PUT /v1/artifacts/{id}/comment-drafts`` keeps a session's unsent marks as a draft.
* ``POST /v1/sessions/{sid}/comment-sessions`` sends one select session as one agent turn.
* ``GET /v1/document-threads/{thread_id}/crop?side=before|after`` is a region or element
  thread's crop on its own version or on the newest one.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Response

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.documents.comment_sessions import (
    CommentSessionError,
    save_drafts,
    send_session,
)
from clio_agent.gact.documents.comment_targets import (
    CommentTargetError,
    find_target,
    register_target,
)
from clio_agent.gact.documents.comment_threads import list_threads, thread_crop
from clio_agent.gact.documents.models import (
    CommentDraftsRequest,
    CommentSessionRequest,
    CommentTargetRequest,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

__all__ = ["register_comment_routes"]


def _error(status_code: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "error": {
                "error": code,
                "message": message,
                "details": details,
                "recoverable": status_code < 500,
            }
        },
    )


def _identity(record: ArtifactRecord, version: ArtifactVersion) -> dict[str, Any]:
    return {
        "artifact_id": version.artifact_id,
        "name": record.name,
        "workspace_id": record.workspace_id,
        "version": version.version,
        "sha256": version.sha256 or "",
        "path": version.path or "",
        "producer": version.producer or {},
    }


def register_comment_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the comment routes."""

    @app.get("/v1/workspaces/{workspace_id}/comment-targets")
    async def get_comment_target(workspace_id: str, path: str) -> dict[str, Any]:
        try:
            found = await asyncio.to_thread(find_target, app, workspace_id, path)
        except CommentTargetError as exc:
            raise _error(exc.status, exc.code, str(exc)) from exc
        return {"artifact": _identity(*found) if found else None}

    @app.post("/v1/workspaces/{workspace_id}/comment-targets")
    async def create_comment_target(
        workspace_id: str, request: CommentTargetRequest
    ) -> dict[str, Any]:
        session = app.state.sessions.get(request.session_id)
        if session is None:
            raise _error(404, "not_found", f"session not found: {request.session_id}")
        if session.workspace_id != workspace_id:
            raise _error(403, "artifact_workspace_mismatch", "the file is outside this session")
        try:
            record, version = await asyncio.to_thread(
                register_target, app, request.session_id, workspace_id, request.path
            )
        except CommentTargetError as exc:
            raise _error(exc.status, exc.code, str(exc)) from exc
        return {"artifact": _identity(record, version)}

    @app.get("/v1/artifacts/{artifact_id}/threads")
    async def get_threads(artifact_id: str, session_id: str = "") -> dict[str, Any]:
        try:
            listed = await asyncio.to_thread(list_threads, app, artifact_id, session_id=session_id)
        except CommentSessionError as exc:
            raise _error(exc.status, exc.code, str(exc), **exc.details) from exc
        return {
            "artifact_id": artifact_id,
            "head": _identity(listed.record, listed.head),
            "next_label": listed.next_label,
            "threads": [row.model_dump(mode="json") for row in listed.threads],
        }

    @app.put("/v1/artifacts/{artifact_id}/comment-drafts")
    async def put_comment_drafts(artifact_id: str, request: CommentDraftsRequest) -> dict[str, Any]:
        if app.state.sessions.get(request.session_id) is None:
            raise _error(404, "not_found", f"session not found: {request.session_id}")
        try:
            drafts = await asyncio.to_thread(
                lambda: save_drafts(
                    app,
                    request.session_id,
                    artifact_id,
                    expected_version=request.expected_version,
                    expected_sha256=request.expected_sha256,
                    marks=request.marks,
                )
            )
        except CommentSessionError as exc:
            raise _error(exc.status, exc.code, str(exc), **exc.details) from exc
        return {"drafts": [row.model_dump(mode="json") for row in drafts]}

    @app.post("/v1/sessions/{session_id}/comment-sessions")
    async def post_comment_session(session_id: str, request: CommentSessionRequest) -> Response:
        try:
            sent = await asyncio.to_thread(send_session, app, deps, session_id, request)
        except CommentSessionError as exc:
            raise _error(exc.status, exc.code, str(exc), **exc.details) from exc
        queued = [row for row in sent.threads if row.send_to_agent]
        failed = any(row.status == "failed" for row in queued)
        body = {
            "session_key": sent.session_key,
            "message_id": sent.message_id,
            "threads": [row.model_dump(mode="json") for row in sent.threads],
        }
        return Response(
            content=json.dumps(body),
            media_type="application/json",
            status_code=503 if failed else 202 if queued else 200,
        )

    @app.get("/v1/document-threads/{thread_id}/crop")
    async def get_thread_crop(
        thread_id: str, side: Literal["before", "after"] = "before"
    ) -> Response:
        try:
            crop = await asyncio.to_thread(thread_crop, app, thread_id, side)
        except CommentSessionError as exc:
            raise _error(exc.status, exc.code, str(exc), **exc.details) from exc
        if not crop:
            raise _error(404, "crop_unavailable", "this thread has no crop on that version")
        return Response(content=crop, media_type="image/png")
