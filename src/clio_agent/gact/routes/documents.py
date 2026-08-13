"""Document artifact manifests, reviews, renditions, and editor adapters."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse

from clio_agent.gact.artifacts.cas import CASStore, sha256_file
from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion, Custody
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.documents.editor_callbacks import (
    download_editor_save,
    write_working_copy,
)
from clio_agent.gact.documents.editors import (
    editor_url,
    endpoint_health,
    issue_access_token,
    onlyoffice_jwt,
    public_gact_url,
    verify_access_token,
    wopi_acquire_lock,
    wopi_current_lock,
    wopi_refresh_lock,
    wopi_release_lock,
)
from clio_agent.gact.documents.models import (
    ArtifactReview,
    CreateArtifactReviewRequest,
    CreateEditorSessionRequest,
    CreateRenditionRequest,
    CreateWorkingCopyRequest,
    DocumentEditorSession,
    DocumentManifest,
    DocumentWorkingCopy,
    ResolveWorkingCopyConflictRequest,
)
from clio_agent.gact.documents.profiles import document_format
from clio_agent.gact.documents.renditions import (
    RenditionError,
    RenditionUnavailableError,
    render_pdf,
)
from clio_agent.gact.documents.store import (
    DocumentStoreError,
    WorkingCopyConflictError,
    WorkingCopyLeaseError,
    get_document_store,
)
from clio_agent.gact.loop_inbox import enqueue_user_steer
from clio_agent.gact.types import Part

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
    workspace = app.state.workspaces.get(record.workspace_id)
    workspace_root = Path(str(getattr(workspace, "root_path", "") or "")).resolve(strict=False)
    if version.custody == Custody.CAS and version.sha256:
        cas_candidate = CASStore(workspace_root).blob_path(version.sha256)
        if cas_candidate.is_file():
            return cas_candidate
    candidate = Path(version.path) if version.path else None
    if candidate is None or not candidate.is_file():
        raise _error(409, "artifact_bytes_unavailable", "artifact bytes are unavailable")
    if version.sha256 and sha256_file(candidate) != version.sha256:
        raise _error(
            409,
            "integrity_violation",
            "artifact bytes do not match the immutable version hash",
            artifact_id=version.artifact_id,
        )
    return candidate


def _manifest(record: ArtifactRecord, version: ArtifactVersion) -> DocumentManifest:
    format_row = document_format(record.name)
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
        embedded_editors=list(format_row.embedded_editors),
        rendition_formats=list(format_row.rendition_formats),
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


def _review_prompt(review: ArtifactReview) -> str:
    anchor = json.dumps(review.anchor.model_dump(mode="json"), sort_keys=True)
    return (
        f"Artifact review instruction for {review.artifact_name} "
        f"(artifact {review.artifact_id}, immutable version {review.artifact_version}, "
        f"sha256 {review.artifact_sha256}).\n"
        f"Selected anchor: {anchor}\n"
        f"User comment: {review.text}\n"
        "Edit the canonical source artifact, preserve unrelated content and unsupported "
        "package parts, then produce a new immutable artifact version. Do not reinterpret "
        "the anchor against another version."
    )


def _review_part(review: ArtifactReview) -> Part:
    return Part(
        id=f"part_{uuid.uuid4().hex}",
        type="artifact_review",
        review_id=review.id,
        artifact_id=review.artifact_id,
        artifact_version=review.artifact_version,
        artifact_sha256=review.artifact_sha256,
        review_text=review.text,
        anchor=review.anchor.model_dump(mode="json"),
        metadata={
            "artifact_name": review.artifact_name,
            "workspace_id": review.workspace_id,
            "native": review.native,
        },
    )


def _emit_review(app: FastAPI, review: ArtifactReview, event_type: str) -> None:
    from clio_agent.gact.runtime.globals import _emit_semantic_event

    _emit_semantic_event(
        app,
        review.session_id,
        event_type,
        status="failed" if review.status == "failed" else "completed",
        summary=f"Document review {review.id} {review.status}.",
        actor={"role": "user"},
        subject={
            "review_id": review.id,
            "artifact_id": review.artifact_id,
            "artifact_name": review.artifact_name,
        },
        payload={
            "review_id": review.id,
            "artifact_id": review.artifact_id,
            "artifact_version": review.artifact_version,
            "artifact_sha256": review.artifact_sha256,
            "status": review.status,
            "native": review.native,
            "message_id": review.message_id,
        },
        detail_level="semantic",
    )


def _dispatch_review(
    app: FastAPI,
    deps: "GactDeps",
    review: ArtifactReview,
) -> ArtifactReview:
    store = get_document_store(app)
    if review.status == "dispatched":
        return review
    session = app.state.sessions.get(review.session_id)
    if session is None:
        failed = store.update_review(review.id, status="failed", error="session not found")
        _emit_review(app, failed, "document.review.dispatched")
        return failed
    prompt = _review_prompt(review)
    part = _review_part(review)
    runner = getattr(app.state, "turn_runner", None)
    try:
        if runner is not None and runner.busy(review.session_id):
            message_id = f"user_{uuid.uuid4().hex}"
            enqueue_user_steer(
                app,
                review.session_id,
                prompt,
                {"artifact_review_id": review.id},
                steer_message_id=message_id,
                steer_created_at=_now_iso(),
                steer_parts=[part],
            )
        else:
            message = deps.start_background_user_turn(
                review.session_id,
                session,
                prompt,
                request_parts=[part],
                metadata={"artifact_review_id": review.id},
                prev_status=str(session.status),
            )
            message_id = message.id
    except Exception as exc:  # noqa: BLE001 - persist dispatch failure for recovery
        failed = store.update_review(review.id, status="failed", error=str(exc))
        _emit_review(app, failed, "document.review.dispatched")
        return failed
    dispatched = store.update_review(
        review.id,
        status="dispatched",
        message_id=message_id,
        error="",
    )
    _emit_review(app, dispatched, "document.review.dispatched")
    return dispatched


def _dispatch_checkpoint(
    app: FastAPI,
    deps: "GactDeps",
    working_copy: DocumentWorkingCopy,
    reviews: list[ArtifactReview],
) -> None:
    for review in reviews:
        if review.status == "queued":
            _dispatch_review(app, deps, review)
    runner = getattr(app.state, "turn_runner", None)
    if runner is None or not runner.busy(working_copy.session_id):
        return
    text = (
        f"The user saved {working_copy.artifact_name} as immutable version "
        f"{working_copy.head_version} ({working_copy.head_artifact_id}). Continue against "
        "this newest version; do not overwrite it from a stale base."
    )
    enqueue_user_steer(
        app,
        working_copy.session_id,
        text,
        {
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
        runner.call_soon_threadsafe(_dispatch_checkpoint, app, deps, working_copy, reviews)

    store.set_checkpoint_callback(checkpoint_callback)

    @app.get("/v1/artifacts/{artifact_id}/document", response_model=DocumentManifest)
    async def get_document_manifest(artifact_id: str) -> DocumentManifest:
        record, version = await _artifact(app, artifact_id)
        return _manifest(record, version)

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

    @app.get("/v1/artifacts/{artifact_id}/reviews")
    async def list_artifact_reviews(artifact_id: str) -> dict[str, Any]:
        record, _version = await _artifact(app, artifact_id)
        reviews = await asyncio.to_thread(store.list_reviews, record.workspace_id, record.name)
        return {"reviews": [row.model_dump(mode="json") for row in reviews]}

    @app.post("/v1/sessions/{session_id}/artifact-reviews")
    async def create_artifact_review(
        session_id: str,
        request: CreateArtifactReviewRequest,
    ) -> Response:
        session = app.state.sessions.get(session_id)
        if session is None:
            raise _error(404, "not_found", f"session not found: {session_id}")
        record, version = await _artifact(app, request.artifact_id)
        if record.workspace_id != session.workspace_id:
            raise _error(403, "artifact_workspace_mismatch", "artifact is outside this session")
        if (
            version.version != request.expected_version
            or (version.sha256 or "") != request.expected_sha256
        ):
            raise _error(
                409,
                "stale_artifact_anchor",
                "review identity does not match the selected immutable artifact version",
                expected_version=request.expected_version,
                actual_version=version.version,
                expected_sha256=request.expected_sha256,
                actual_sha256=version.sha256 or "",
            )
        registry = await asyncio.to_thread(get_registry, app)
        current = registry.get(record.workspace_id, record.name)
        if (
            not request.allow_historical
            and current is not None
            and current.head is not None
            and current.head.artifact_id != version.artifact_id
        ):
            raise _error(
                409,
                "stale_artifact_anchor",
                "artifact head advanced; reselect the intended content or explicitly review history",
                selected_artifact_id=version.artifact_id,
                head_artifact_id=current.head.artifact_id,
            )
        review = ArtifactReview(
            id=f"docreview_{uuid.uuid4().hex}",
            session_id=session_id,
            workspace_id=record.workspace_id,
            artifact_id=version.artifact_id,
            artifact_name=record.name,
            artifact_version=version.version,
            artifact_sha256=version.sha256 or "",
            anchor=request.anchor,
            text=request.text,
            status="queued",
            idempotency_key=request.idempotency_key,
            created_at=_now_iso(),
        )
        persisted = await asyncio.to_thread(store.create_review, review)
        if persisted.id == review.id:
            _emit_review(app, persisted, "document.review.created")
        dispatched = _dispatch_review(app, deps, persisted)
        status = 202 if dispatched.status == "dispatched" else 503
        return Response(
            content=dispatched.model_dump_json(),
            media_type="application/json",
            status_code=status,
        )

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
        format_row = document_format(record.name)
        if request.provider != "native" and request.provider not in format_row.embedded_editors:
            raise _error(
                422,
                "editor_format_unsupported",
                f"{request.provider} cannot edit {format_row.profile}",
            )
        try:
            return await asyncio.to_thread(
                store.create_working_copy,
                session_id=request.session_id,
                workspace_id=record.workspace_id,
                record=record,
                version=version,
                provider=request.provider,
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

    @app.get("/v1/document-editors/health")
    async def document_editor_health() -> dict[str, Any]:
        rows = await asyncio.gather(
            asyncio.to_thread(endpoint_health, "onlyoffice"),
            asyncio.to_thread(endpoint_health, "collabora"),
        )
        return {"editors": [row.__dict__ for row in rows]}

    @app.post(
        "/v1/document-working-copies/{working_copy_id}/editor-sessions",
        response_model=DocumentEditorSession,
    )
    async def create_editor_session(
        working_copy_id: str,
        request: CreateEditorSessionRequest,
    ) -> DocumentEditorSession:
        working_copy = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if working_copy is None:
            raise _error(404, "not_found", "working copy not found")
        health = await asyncio.to_thread(endpoint_health, request.provider)
        if not health.healthy:
            return DocumentEditorSession(
                id=f"doceditor_{uuid.uuid4().hex}",
                working_copy_id=working_copy_id,
                provider=request.provider,
                status="unavailable",
                error=health.error,
            )
        token, expires = issue_access_token(
            app,
            working_copy_id=working_copy_id,
            provider=request.provider,
            writable=working_copy.writable,
        )
        public_url = public_gact_url()
        session_id = f"doceditor_{uuid.uuid4().hex}"
        expires_at = datetime.fromtimestamp(expires, timezone.utc).isoformat()
        if request.provider == "onlyoffice":
            file_url = (
                f"{public_url}/v1/internal/document-editors/onlyoffice/"
                f"{working_copy_id}/content?access_token={quote(token)}"
            )
            callback_url = (
                f"{public_url}/v1/internal/document-editors/onlyoffice/"
                f"{working_copy_id}/callback?access_token={quote(token)}"
            )
            config: dict[str, Any] = {
                "document": {
                    "fileType": Path(working_copy.path).suffix.lstrip("."),
                    "key": working_copy.head_artifact_id,
                    "title": working_copy.artifact_name,
                    "url": file_url,
                    "permissions": {"edit": working_copy.writable},
                },
                "documentType": (
                    "word"
                    if Path(working_copy.path).suffix.lower() == ".docx"
                    else "cell"
                    if Path(working_copy.path).suffix.lower() == ".xlsx"
                    else "slide"
                ),
                "editorConfig": {
                    "callbackUrl": callback_url,
                    "mode": "edit" if working_copy.writable else "view",
                },
            }
            jwt = onlyoffice_jwt(config)
            if jwt:
                config["token"] = jwt
            launch_url = f"{health.url}/web-apps/apps/api/documents/index.html"
        else:
            wopi_source = quote(
                f"{public_url}/v1/internal/document-editors/collabora/wopi/files/{working_copy_id}",
                safe="",
            )
            launch_url = (
                f"{health.url}/browser/dist/cool.html?WOPISrc={wopi_source}"
                f"&access_token={quote(token)}"
            )
            config = {"wopi_source": wopi_source}
        return DocumentEditorSession(
            id=session_id,
            working_copy_id=working_copy_id,
            provider=request.provider,
            status="ready",
            editor_url=launch_url,
            token=token,
            expires_at=expires_at,
            config=config,
        )

    @app.get("/v1/internal/document-editors/onlyoffice/{working_copy_id}/content")
    async def onlyoffice_content(
        working_copy_id: str,
        access_token: str,
    ) -> FileResponse:
        try:
            verify_access_token(
                app,
                access_token,
                working_copy_id=working_copy_id,
                provider="onlyoffice",
            )
        except ValueError as exc:
            raise _error(401, "invalid_editor_token", str(exc)) from exc
        row = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if row is None:
            raise _error(404, "not_found", "working copy not found")
        return FileResponse(row.path, filename=Path(row.path).name)

    @app.post("/v1/internal/document-editors/onlyoffice/{working_copy_id}/callback")
    async def onlyoffice_callback(
        working_copy_id: str,
        access_token: str,
        request: Request,
    ) -> dict[str, int]:
        try:
            verify_access_token(
                app,
                access_token,
                working_copy_id=working_copy_id,
                provider="onlyoffice",
                require_write=True,
            )
        except ValueError as exc:
            raise _error(401, "invalid_editor_token", str(exc)) from exc
        row = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if row is None:
            raise _error(404, "not_found", "working copy not found")
        body = await request.json()
        status = int(body.get("status", 0))
        if status not in {2, 6}:
            return {"error": 0}
        download_url = str(body.get("url", ""))
        try:
            payload = await asyncio.to_thread(
                download_editor_save, download_url, editor_url("onlyoffice")
            )
            await asyncio.to_thread(write_working_copy, Path(row.path), payload)
            await asyncio.to_thread(store.checkpoint, working_copy_id)
        except (OSError, ValueError, DocumentStoreError):
            return {"error": 1}
        return {"error": 0}

    @app.api_route(
        "/v1/internal/document-editors/collabora/wopi/files/{working_copy_id}",
        methods=["GET", "POST"],
        response_model=None,
    )
    async def collabora_file_info(
        working_copy_id: str,
        request: Request,
        access_token: str,
    ) -> Response | dict[str, Any]:
        require_write = request.method == "POST"
        try:
            verify_access_token(
                app,
                access_token,
                working_copy_id=working_copy_id,
                provider="collabora",
                require_write=require_write,
            )
        except ValueError as exc:
            raise _error(401, "invalid_editor_token", str(exc)) from exc
        row = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if row is None:
            raise _error(404, "not_found", "working copy not found")
        if request.method == "POST":
            operation = request.headers.get("X-WOPI-Override", "").upper()
            supplied_lock = request.headers.get("X-WOPI-Lock", "")
            if operation in {"LOCK", "REFRESH_LOCK", "UNLOCK"} and (
                not supplied_lock or len(supplied_lock) > 1024 or not supplied_lock.isascii()
            ):
                return Response(status_code=400)
            if operation == "LOCK":
                matched, current = wopi_acquire_lock(
                    app,
                    working_copy_id,
                    supplied_lock,
                    old_value=request.headers.get("X-WOPI-OldLock", ""),
                )
            elif operation == "REFRESH_LOCK":
                matched, current = wopi_refresh_lock(app, working_copy_id, supplied_lock)
            elif operation == "UNLOCK":
                matched, current = wopi_release_lock(app, working_copy_id, supplied_lock)
            elif operation == "GET_LOCK":
                return Response(
                    status_code=200,
                    headers={
                        "X-WOPI-Lock": wopi_current_lock(app, working_copy_id),
                        "X-WOPI-ItemVersion": row.last_sha256,
                    },
                )
            else:
                return Response(status_code=501)
            if not matched:
                return Response(
                    status_code=409,
                    headers={
                        "X-WOPI-Lock": current,
                        "X-WOPI-LockFailureReason": "lock mismatch",
                    },
                )
            return Response(
                status_code=200,
                headers={"X-WOPI-ItemVersion": row.last_sha256},
            )
        path = Path(row.path)
        return {
            "BaseFileName": path.name,
            "Size": path.stat().st_size,
            "Version": row.last_sha256,
            "UserId": row.session_id,
            "UserFriendlyName": "CLIO user",
            "UserCanWrite": row.writable,
            "SupportsUpdate": row.writable,
            "SupportsLocks": row.writable,
            "SupportsGetLock": row.writable,
        }

    @app.api_route(
        "/v1/internal/document-editors/collabora/wopi/files/{working_copy_id}/contents",
        methods=["GET", "POST"],
    )
    async def collabora_contents(
        working_copy_id: str,
        request: Request,
        access_token: str,
    ) -> Response:
        require_write = request.method == "POST"
        try:
            verify_access_token(
                app,
                access_token,
                working_copy_id=working_copy_id,
                provider="collabora",
                require_write=require_write,
            )
        except ValueError as exc:
            raise _error(401, "invalid_editor_token", str(exc)) from exc
        row = await asyncio.to_thread(store.get_working_copy, working_copy_id)
        if row is None:
            raise _error(404, "not_found", "working copy not found")
        if request.method == "GET":
            return FileResponse(row.path)
        if request.headers.get("X-WOPI-Override", "").upper() != "PUT":
            return Response(status_code=501)
        supplied_lock = request.headers.get("X-WOPI-Lock", "")
        current_lock = wopi_current_lock(app, working_copy_id)
        if not current_lock or supplied_lock != current_lock:
            return Response(
                status_code=409,
                headers={
                    "X-WOPI-Lock": current_lock,
                    "X-WOPI-LockFailureReason": "lock mismatch",
                },
            )
        payload = await request.body()
        try:
            await asyncio.to_thread(write_working_copy, Path(row.path), payload)
            updated = await asyncio.to_thread(store.checkpoint, working_copy_id)
        except (OSError, ValueError, DocumentStoreError) as exc:
            raise _error(422, "editor_save_failed", str(exc)) from exc
        return Response(
            status_code=200,
            headers={"X-WOPI-ItemVersion": updated.last_sha256},
        )


__all__ = ["register_document_routes"]
