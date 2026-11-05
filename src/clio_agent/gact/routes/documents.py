"""Document artifact manifests, reviews, renditions, and working copies."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.artifact_bytes import ArtifactBytesError, artifact_bytes_path
from clio_agent.gact.documents.comment_sessions import (
    dispatch_native,
)
from clio_agent.gact.documents.models import (
    ArtifactReview,
    CreateRenditionRequest,
    CreateWorkingCopyRequest,
    DocumentManifest,
    DocumentWorkingCopy,
    ResolveWorkingCopyConflictRequest,
)
from clio_agent.gact.documents.profiles import document_format
from clio_agent.gact.documents.renditions import (
    RenditionError,
    RenditionUnavailableError,
    find_pdf_rendition,
    render_pdf,
)
from clio_agent.gact.documents.store import (
    DocumentStoreError,
    WorkingCopyConflictError,
    WorkingCopyLeaseError,
    get_document_store,
)
from clio_agent.gact.session_input import accept_session_input

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def _artifact(app: FastAPI, artifact_id: str) -> tuple[ArtifactRecord, ArtifactVersion]:
    registry = await asyncio.to_thread(get_registry, app)
    found = registry.get_by_artifact_id(artifact_id)
    if found is None:
        raise _error(404, "not_found", f"artifact not found: {artifact_id}")
    return found


def _artifact_source(
    app: FastAPI,
    record: ArtifactRecord,
    version: ArtifactVersion,
) -> Path:
    try:
        return artifact_bytes_path(app, record, version)
    except ArtifactBytesError as exc:
        raise _error(409, exc.code, str(exc), artifact_id=version.artifact_id) from exc


def _manifest(
    record: ArtifactRecord, version: ArtifactVersion, app: FastAPI | None = None
) -> DocumentManifest:
    format_row = document_format(record.name)
    rendition = (
        find_pdf_rendition(app, record, version) if app and format_row.rendition_formats else None
    )
    return DocumentManifest(
        artifact_id=version.artifact_id,
        workspace_id=record.workspace_id,
        name=record.name,
        version=version.version,
        sha256=version.sha256 or "",
        mime_type=format_row.mime_type,
        profile=format_row.profile,
        content_url=f"/v1/artifacts/{version.artifact_id}/document/content",
        anchors=list(format_row.anchors),
        native_open=format_row.native_open,
        rendition_formats=list(format_row.rendition_formats),
        pdf_rendition_artifact_id=rendition.version.artifact_id if rendition else "",
        provenance={
            "custody": version.custody.value,
            "mechanism": version.mechanism.value,
            "kind": version.kind.value,
            "created_at": version.created_at,
            "prior_version": version.prior_version,
            "prior_sha256": version.prior_sha256,
            "producer": version.producer,
        },
    )


def _dispatch_checkpoint(
    app: FastAPI,
    deps: "GactDeps",
    working_copy: DocumentWorkingCopy,
    reviews: list[ArtifactReview],
) -> None:
    dispatch_native(app, deps, reviews)
    runner = getattr(app.state, "turn_runner", None)
    if runner is None or not runner.busy(working_copy.session_id):
        return
    text = (
        f"The user saved {working_copy.artifact_name} as immutable version "
        f"{working_copy.head_version} ({working_copy.head_artifact_id}). Continue against "
        "this newest version; do not overwrite it from a stale base."
    )
    accept_session_input(
        app,
        working_copy.session_id,
        text,
        steer_message_id=f"document-save:{working_copy.id}:{working_copy.head_version}",
        metadata={
            "document_working_copy_id": working_copy.id,
            "coalesce_key": f"document-save:{working_copy.id}",
        },
    )


def register_document_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the additive document-artifact protocol surface."""

    store = get_document_store(app)

    def checkpoint_callback(
        working_copy: DocumentWorkingCopy,
        reviews: list[ArtifactReview],
    ) -> None:
        runner = getattr(app.state, "turn_runner", None)
        if runner is None:
            return
        _dispatch_checkpoint(app, deps, working_copy, reviews)

    store.set_checkpoint_callback(checkpoint_callback)

    @app.get("/v1/artifacts/{artifact_id}/document", response_model=DocumentManifest)
    async def get_document_manifest(artifact_id: str) -> DocumentManifest:
        record, version = await _artifact(app, artifact_id)
        return _manifest(record, version, app)

    @app.get("/v1/artifacts/{artifact_id}/document/content")
    async def get_document_content(artifact_id: str) -> FileResponse:
        record, version = await _artifact(app, artifact_id)
        source = await asyncio.to_thread(_artifact_source, app, record, version)
        row = document_format(record.name)
        headers = {
            "ETag": f'"sha256:{version.sha256}"' if version.sha256 else "",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(Path(record.name).name)}",
        }
        if row.profile == "html-static":
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src data: blob:; "
                "style-src 'unsafe-inline'; font-src data:"
            )
        return FileResponse(source, media_type=row.mime_type, headers=headers)

    @app.post("/v1/artifacts/{artifact_id}/renditions")
    async def create_rendition(
        artifact_id: str,
        request: CreateRenditionRequest,
        session_id: str,
    ) -> dict[str, Any]:
        session = app.state.sessions.get(session_id)
        if session is None:
            raise _error(404, "not_found", f"session not found: {session_id}")
        record, version = await _artifact(app, artifact_id)
        if record.workspace_id != session.workspace_id:
            raise _error(403, "artifact_workspace_mismatch", "artifact is outside this session")
        try:
            result = await asyncio.to_thread(render_pdf, app, session_id, record, version)
        except RenditionUnavailableError as exc:
            raise _error(501, "rendition_unavailable", str(exc)) from exc
        except RenditionError as exc:
            raise _error(422, "rendition_failed", str(exc)) from exc
        return {
            "source_artifact_id": artifact_id,
            "converter": result.converter,
            "artifact": _manifest(result.record, result.version).model_dump(mode="json"),
        }

    @app.post(
        "/v1/artifacts/{artifact_id}/working-copies",
        response_model=DocumentWorkingCopy,
    )
    async def create_working_copy(
        artifact_id: str,
        request: CreateWorkingCopyRequest,
    ) -> DocumentWorkingCopy:
        session = app.state.sessions.get(request.session_id)
        if session is None:
            raise _error(404, "not_found", f"session not found: {request.session_id}")
        record, version = await _artifact(app, artifact_id)
        if record.workspace_id != session.workspace_id:
            raise _error(403, "artifact_workspace_mismatch", "artifact is outside this session")
        try:
            return await asyncio.to_thread(
                store.create_working_copy,
                session_id=request.session_id,
                workspace_id=record.workspace_id,
                record=record,
                version=version,
                writable=request.writable,
                auto_checkpoint=request.auto_checkpoint,
            )
        except WorkingCopyLeaseError as exc:
            raise _error(409, "working_copy_lease_conflict", str(exc)) from exc
        except DocumentStoreError as exc:
            raise _error(422, "working_copy_failed", str(exc)) from exc

    @app.get(
        "/v1/document-working-copies/{working_copy_id}",
        response_model=DocumentWorkingCopy,
    )
    async def get_working_copy(working_copy_id: str) -> DocumentWorkingCopy:
        row = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if row is None:
            raise _error(404, "not_found", "working copy not found")
        return row

    @app.delete(
        "/v1/document-working-copies/{working_copy_id}",
        response_model=DocumentWorkingCopy,
    )
    async def close_working_copy(working_copy_id: str) -> DocumentWorkingCopy:
        try:
            return await asyncio.to_thread(store.close_working_copy, working_copy_id)
        except KeyError as exc:
            raise _error(404, "not_found", "working copy not found") from exc

    @app.post(
        "/v1/document-working-copies/{working_copy_id}/conflict",
        response_model=DocumentWorkingCopy,
    )
    async def resolve_working_copy_conflict(
        working_copy_id: str,
        request: ResolveWorkingCopyConflictRequest,
    ) -> DocumentWorkingCopy:
        try:
            return await asyncio.to_thread(
                store.resolve_conflict,
                working_copy_id,
                resolution=request.resolution,
                expected_head_artifact_id=request.expected_head_artifact_id,
            )
        except KeyError as exc:
            raise _error(404, "not_found", "working copy not found") from exc
        except WorkingCopyConflictError as exc:
            raise _error(409, "working_copy_conflict_changed", str(exc)) from exc


__all__ = ["register_document_routes"]
