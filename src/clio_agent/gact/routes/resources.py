"""Workspace resource custody and resumable upload routes.

HTTP only. The converter lifecycle lives in
:mod:`clio_agent.gact.resource_lifecycle` and the bounded read operations
(search, direct read, structure, nodes) live in
:mod:`clio_agent.gact.resource_tools` — these routes call those owners rather
than carrying second copies with their own bounds and readiness gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from clio_agent import conf
from clio_agent.gact.resource_custody import (
    ResourceConflictError,
    ResourceDeleteError,
    ResourceLimitError,
    ResourceRecord,
)
from clio_agent.gact.resource_lifecycle import (
    cancel_remote_job,
    emit_workspace_event,
    refresh_processing,
    schedule_processing,
    submit_processing,
)
from clio_agent.gact.resource_processing import ResourceConverterUnavailable
from clio_agent.gact.resource_tools import (
    ResourceQueryError,
    read_workspace_resource_structure,
    search_workspace_resource,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

_PREVIEWABLE_APPLICATION_TYPES = {
    "application/json",
    "application/javascript",
    "application/pdf",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}

# Media types that execute in the viewer's origin when served inline. An
# uploaded document is untrusted input, so serving one same-origin without a
# policy is stored XSS against the authenticated API — the derivative route
# already knew this; the preview route did not.
_ACTIVE_INLINE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}
_INLINE_SANDBOX_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"

# ``ResourceQueryError.code`` -> HTTP status. Bounded refusals from the owner
# implementations keep their exact status instead of surfacing as a 500.
_QUERY_ERROR_STATUS: dict[str, int] = {
    "not_found": 404,
    "invalid_request": 400,
    "search_unavailable": 415,
    "resource_not_decodable": 415,
    "search_input_too_large": 413,
    "read_input_too_large": 413,
    "derivative_not_found": 404,
    "structure_node_not_found": 404,
    "structure_node_too_large": 413,
}


def _error(status: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=code,
                message=message,
                details=details,
                recoverable=status < 500,
            )
        ).model_dump(exclude_none=True),
    )


def _query_error(exc: ResourceQueryError) -> HTTPException:
    return _error(
        _QUERY_ERROR_STATUS.get(exc.code, 422),
        exc.code,
        str(exc),
        **exc.details,
    )


def _workspace(app: FastAPI, workspace_id: str) -> Any:
    workspace = app.state.workspaces.get(workspace_id)
    if workspace is None:
        raise _error(404, "not_found", f"workspace not found: {workspace_id}")
    return workspace


def _resource(app: FastAPI, workspace_id: str, resource_id: str) -> ResourceRecord:
    _workspace(app, workspace_id)
    record = app.state.resource_store.get(workspace_id, resource_id)
    if record is None:
        raise _error(
            404,
            "not_found",
            f"resource not found: {resource_id}",
            workspace_id=workspace_id,
            resource_id=resource_id,
        )
    return record


def _previewable(record: ResourceRecord) -> bool:
    media_type = record.detected_mime
    return (
        media_type.startswith("text/")
        or media_type.startswith("image/")
        or media_type.startswith("audio/")
        or media_type.startswith("video/")
        or media_type in _PREVIEWABLE_APPLICATION_TYPES
    )


def _inline_response(
    path: Any, *, media_type: str, filename: str, disposition: str = "inline"
) -> FileResponse:
    """Serve custody bytes with the inline-safety headers applied uniformly."""

    response = FileResponse(
        path=path,
        media_type=media_type or "application/octet-stream",
        filename=filename,
        content_disposition_type=disposition,
    )
    if disposition != "inline":
        return response
    response.headers["X-Content-Type-Options"] = "nosniff"
    if media_type in _ACTIVE_INLINE_TYPES:
        response.headers["Content-Security-Policy"] = _INLINE_SANDBOX_CSP
    return response


async def _read_capped_body(request: Request, cap: int) -> bytes:
    """Read the request body, enforcing ``cap`` DURING the read.

    ``await request.body()`` buffers the whole body before any check, so a
    chunked upload with no ``Content-Length`` sailed past the declared chunk
    ceiling and was only rejected after the server had already held it in
    memory. Streaming makes the cap real for every transfer encoding.
    """

    chunks: list[bytes] = []
    received = 0
    async for piece in request.stream():
        received += len(piece)
        if received > cap:
            raise _error(
                413,
                "upload_chunk_too_large",
                f"upload chunks are limited to {cap} bytes",
                max_chunk_bytes=cap,
            )
        chunks.append(piece)
    return b"".join(chunks)


def register_resource_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register workspace-scoped resource custody routes."""

    del deps

    max_upload_chunk_bytes = conf.resolve(
        "resources.upload_chunk_bytes",
        env="CLIO_RESOURCE_UPLOAD_CHUNK_BYTES",
        default=8 * 1024 * 1024,
        cast=conf.as_int,
    )
    max_text_preview_bytes = conf.resolve(
        "resources.text_preview_bytes",
        env="CLIO_RESOURCE_TEXT_PREVIEW_BYTES",
        default=2 * 1024 * 1024,
        cast=conf.as_int,
    )

    def resource_wire(record: ResourceRecord) -> dict[str, Any]:
        """Project resource identity with its current converter lifecycle state."""

        payload = record.to_wire()
        payload["processing"] = app.state.resource_processing_store.state(record).model_dump()
        return payload

    def lifecycle_payload(
        record: ResourceRecord, *, workspace_id: str, idempotent_replay: bool
    ) -> dict[str, Any]:
        """The ONE ``resource.created`` / ``resource.ready`` body.

        Both completion paths (a zero-byte create and the final append) publish
        this exact shape; they used to differ, so a client had to know which
        path produced the event before it could read it.
        """

        payload = resource_wire(record)
        payload["idempotent_replay"] = idempotent_replay
        payload["upload_url"] = f"/v1/workspaces/{workspace_id}/resources/{record.id}/content"
        return payload

    @app.get("/v1/workspaces/{workspace_id}/resources")
    async def list_resources(workspace_id: str) -> dict[str, Any]:
        _workspace(app, workspace_id)
        resources = app.state.resource_store.list(workspace_id)
        for record in resources:
            await refresh_processing(app, record)
        return {"resources": [resource_wire(row) for row in resources]}

    @app.get("/v1/workspaces/{workspace_id}/resource-deliveries")
    async def list_resource_deliveries(workspace_id: str) -> dict[str, Any]:
        _workspace(app, workspace_id)
        return {
            "records": [
                row.model_dump() for row in app.state.resource_delivery_store.list(workspace_id)
            ]
        }

    @app.post("/v1/workspaces/{workspace_id}/resources", status_code=201)
    async def create_resource(
        workspace_id: str, request: Request, background_tasks: BackgroundTasks
    ) -> dict[str, Any]:
        _workspace(app, workspace_id)
        body = await json_body(request, route="POST /v1/workspaces/{workspace_id}/resources")
        try:
            declared_size = int(body.get("size", body.get("declared_size", -1)))
            record, idempotent_replay = app.state.resource_store.create_or_resume(
                workspace_id=workspace_id,
                name=str(body.get("name") or ""),
                declared_size=declared_size,
                claimed_mime=str(body.get("media_type") or body.get("claimed_mime") or ""),
                client_upload_id=str(
                    body.get("client_upload_id") or body.get("idempotency_key") or ""
                ),
            )
        except ResourceLimitError as exc:
            raise _error(
                413,
                "resource_too_large",
                str(exc),
                max_resource_bytes=app.state.resource_store.max_resource_bytes,
            ) from exc
        except ResourceConflictError as exc:
            raise _error(
                409,
                "resource_upload_identity_conflict",
                str(exc),
                current=exc.record.to_wire(),
            ) from exc
        except (TypeError, ValueError) as exc:
            raise _error(400, "invalid_request", str(exc)) from exc
        payload = lifecycle_payload(
            record, workspace_id=workspace_id, idempotent_replay=idempotent_replay
        )
        if not idempotent_replay:
            emit_workspace_event(app, workspace_id, "resource.created", payload)
            if record.state == "ready":
                emit_workspace_event(app, workspace_id, "resource.ready", payload)
        if record.state == "ready":
            schedule_processing(app, record, background_tasks)
        return payload

    @app.head("/v1/workspaces/{workspace_id}/resources/{resource_id}/content")
    async def head_resource_content(workspace_id: str, resource_id: str) -> Response:
        record = _resource(app, workspace_id, resource_id)
        return Response(
            status_code=200,
            headers={
                "Upload-Offset": str(record.received_size),
                "Upload-Length": str(record.declared_size),
                "Upload-State": record.state,
            },
        )

    @app.patch("/v1/workspaces/{workspace_id}/resources/{resource_id}/content")
    async def append_resource_content(
        workspace_id: str,
        resource_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> Response:
        _resource(app, workspace_id, resource_id)
        try:
            offset = int(request.headers.get("Upload-Offset", "-1"))
        except ValueError as exc:
            raise _error(400, "invalid_upload_offset", "Upload-Offset must be an integer") from exc
        content_length = request.headers.get("Content-Length")
        if content_length:
            try:
                declared_chunk = int(content_length)
            except ValueError as exc:
                raise _error(
                    400, "invalid_content_length", "Content-Length must be an integer"
                ) from exc
            if declared_chunk > max_upload_chunk_bytes:
                raise _error(
                    413,
                    "upload_chunk_too_large",
                    f"upload chunks are limited to {max_upload_chunk_bytes} bytes",
                    max_chunk_bytes=max_upload_chunk_bytes,
                )
        data = await _read_capped_body(request, max_upload_chunk_bytes)
        try:
            updated = app.state.resource_store.append(resource_id, offset=offset, data=data)
        except KeyError as exc:
            raise _error(404, "not_found", f"resource not found: {resource_id}") from exc
        except ResourceLimitError as exc:
            raise _error(413, "resource_too_large", str(exc)) from exc
        except ResourceConflictError as exc:
            raise _error(
                409,
                "upload_conflict",
                str(exc),
                current=exc.record.to_wire(),
            ) from exc
        if updated.state == "ready":
            emit_workspace_event(
                app,
                workspace_id,
                "resource.ready",
                lifecycle_payload(updated, workspace_id=workspace_id, idempotent_replay=False),
            )
            schedule_processing(app, updated, background_tasks)
        else:
            emit_workspace_event(app, workspace_id, "resource.upload_progress", updated.to_wire())
        return Response(
            status_code=204,
            headers={
                "Upload-Offset": str(updated.received_size),
                "Upload-Length": str(updated.declared_size),
                "Upload-State": updated.state,
            },
        )

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}")
    async def get_resource(workspace_id: str, resource_id: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        await refresh_processing(app, record)
        return resource_wire(record)

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/content")
    async def get_resource_content(workspace_id: str, resource_id: str) -> FileResponse:
        record = _resource(app, workspace_id, resource_id)
        try:
            path = app.state.resource_store.content_path(record)
        except ResourceConflictError as exc:
            raise _error(409, "resource_not_ready", str(exc), resource=record.to_wire()) from exc
        return _inline_response(
            path,
            media_type=record.detected_mime,
            filename=record.name,
            disposition="attachment",
        )

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/preview")
    async def preview_resource(workspace_id: str, resource_id: str) -> FileResponse:
        record = _resource(app, workspace_id, resource_id)
        if record.state != "ready":
            raise _error(409, "resource_not_ready", "resource preview is not ready")
        if not _previewable(record):
            raise _error(
                415,
                "preview_unavailable",
                "this resource has an honest metadata-only preview",
                resource=record.to_wire(),
            )
        path = app.state.resource_store.content_path(record)
        if (
            record.detected_mime.startswith("text/")
            and path.stat().st_size > max_text_preview_bytes
        ):
            raise _error(
                413,
                "preview_too_large",
                "text preview exceeds the bounded preview limit",
                max_preview_bytes=max_text_preview_bytes,
            )
        return _inline_response(path, media_type=record.detected_mime, filename=record.name)

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/search")
    async def search_resource(workspace_id: str, resource_id: str, q: str) -> dict[str, Any]:
        _resource(app, workspace_id, resource_id)
        try:
            return search_workspace_resource(app, workspace_id, resource_id, q)
        except ResourceQueryError as exc:
            raise _query_error(exc) from exc

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/derivatives")
    async def list_resource_derivatives(workspace_id: str, resource_id: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        processing = await refresh_processing(app, record)
        manifest = app.state.resource_processing_store.manifest(record)
        entries: list[dict[str, Any]] = []
        if manifest is not None:
            for raw in manifest.get("entries", []):
                entry = {key: value for key, value in raw.items() if key != "content_path"}
                if raw.get("content_path"):
                    entry["content_url"] = (
                        f"/v1/workspaces/{workspace_id}/resources/{resource_id}/"
                        f"derivatives/{entry['id']}/content"
                    )
                entries.append(entry)
        return {
            "resource_id": record.id,
            "revision": record.revision,
            "derivatives": entries,
            "truncated": bool((manifest or {}).get("entries_truncated")),
            "processor": processing.model_dump(),
        }

    @app.post("/v1/workspaces/{workspace_id}/resources/{resource_id}/reprocess")
    async def reprocess_resource(workspace_id: str, resource_id: str) -> JSONResponse:
        record = _resource(app, workspace_id, resource_id)
        if record.state != "ready":
            raise _error(409, "resource_not_ready", "resource upload is not complete")
        if app.state.resource_converter_factory.get_converter(record) is None:
            raise _error(
                503,
                "resource_converter_unavailable",
                "no configured converter accepts this detected resource type",
                detected_mime=record.detected_mime,
            )
        current = app.state.resource_processing_store.state(record)
        if current.state in {"submitted", "processing"}:
            return JSONResponse(status_code=202, content=current.model_dump())
        try:
            processing = await submit_processing(
                app,
                record,
                raise_unavailable=True,
                reprocess=True,
            )
        except ResourceConverterUnavailable as exc:
            raise _error(
                502,
                "resource_converter_unavailable",
                "registered resource converters could not accept this resource",
                detected_mime=record.detected_mime,
                attempted=[converter_id for converter_id, _error in exc.failures],
            ) from exc
        status_code = 200 if processing.state == "complete" else 202
        return JSONResponse(status_code=status_code, content=processing.model_dump())

    @app.post("/v1/workspaces/{workspace_id}/resources/{resource_id}/processing/cancel")
    async def cancel_resource_processing(workspace_id: str, resource_id: str) -> dict[str, Any]:
        """Durably cancel conversion without imposing a hidden elapsed-time limit."""

        record = _resource(app, workspace_id, resource_id)
        current = app.state.resource_processing_store.state(record)
        if current.state == "cancelled":
            return current.model_dump()
        if current.state not in {"submitted", "processing"}:
            raise _error(
                409,
                "resource_processing_not_cancellable",
                "resource conversion is not active",
                state=current.state,
            )

        requested_at = datetime.now(timezone.utc).isoformat()
        cancelled = current.model_copy(
            update={
                "state": "cancelled",
                "updated_at": requested_at,
                "cancellation": {
                    "requested_at": requested_at,
                    "remote_cancelled": False,
                },
            }
        )
        app.state.resource_processing_store.save_state(record, cancelled)

        remote = (
            await cancel_remote_job(app, current) if current.job_id else {"remote_cancelled": False}
        )
        cancelled = cancelled.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cancellation": {**cancelled.cancellation, **remote},
            }
        )
        app.state.resource_processing_store.save_state(record, cancelled)
        emit_workspace_event(
            app, workspace_id, "resource.processing_cancelled", cancelled.model_dump()
        )
        return cancelled.model_dump()

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/structure")
    async def resource_structure(workspace_id: str, resource_id: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        await refresh_processing(app, record)
        outline = read_workspace_resource_structure(app, workspace_id, resource_id)
        if not outline.get("available"):
            raise _error(
                409,
                "resource_processing_incomplete",
                "document structure is not ready",
                processing=outline.get("processing", {}),
            )
        return outline

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/structure/{collection}/{index}")
    async def resource_structure_node(
        workspace_id: str, resource_id: str, collection: str, index: int
    ) -> dict[str, Any]:
        _resource(app, workspace_id, resource_id)
        try:
            node = read_workspace_resource_structure(
                app, workspace_id, resource_id, collection, index
            )
        except ResourceQueryError as exc:
            raise _query_error(exc) from exc
        if not node.get("available"):
            raise _error(
                409,
                "resource_processing_incomplete",
                "document structure is not ready",
                processing=node.get("processing", {}),
            )
        return {"collection": collection, "index": index, "node": node["node"]}

    @app.get(
        "/v1/workspaces/{workspace_id}/resources/{resource_id}/derivatives/{derivative_id}/content"
    )
    async def resource_derivative_content(
        workspace_id: str, resource_id: str, derivative_id: str
    ) -> FileResponse:
        record = _resource(app, workspace_id, resource_id)
        try:
            path, entry = app.state.resource_processing_store.derivative_path(record, derivative_id)
        except KeyError as exc:
            raise _error(404, "derivative_not_found", "resource derivative not found") from exc
        return _inline_response(
            path,
            media_type=str(entry.get("media_type") or "application/octet-stream"),
            filename=str(entry.get("name") or derivative_id),
        )

    @app.delete("/v1/workspaces/{workspace_id}/resources/{resource_id}")
    async def delete_resource(workspace_id: str, resource_id: str) -> Response:
        record = _resource(app, workspace_id, resource_id)
        # Stop the remote job BEFORE the bytes go, so a converter is not left
        # working on a resource nobody will ever read, and so its late result
        # cannot try to re-create the custody tree.
        processing = app.state.resource_processing_store.state(record)
        cancellation: dict[str, Any] = {"remote_cancelled": False}
        if processing.state in {"submitted", "processing"} and processing.job_id:
            cancellation = await cancel_remote_job(app, processing)
        try:
            deleted = app.state.resource_store.delete(workspace_id, resource_id)
        except ResourceDeleteError as exc:
            raise _error(
                409,
                "resource_delete_failed",
                str(exc),
                resource_id=resource_id,
                reason=exc.reason,
                recovery_actions=["close_open_readers", "retry"],
            ) from exc
        if not deleted:
            raise _error(404, "not_found", f"resource not found: {resource_id}")
        app.state.resource_delivery_store.delete_resource(workspace_id, resource_id)
        emit_workspace_event(
            app,
            workspace_id,
            "resource.deleted",
            {**record.to_wire(), "cancellation": cancellation},
        )
        return Response(status_code=204)


__all__ = ["register_resource_routes"]
