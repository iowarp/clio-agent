"""Workspace resource custody and resumable upload routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from clio_agent.gact.events import Event
from clio_agent.gact.resource_custody import (
    ResourceConflictError,
    ResourceLimitError,
    ResourceRecord,
)
from clio_agent.gact.resource_processing import (
    ResourceConverterUnavailable,
    ResourceProcessingRecord,
)
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

_MAX_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024
_PREVIEWABLE_APPLICATION_TYPES = {
    "application/json",
    "application/javascript",
    "application/pdf",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
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


def _emit_workspace_event(
    app: FastAPI, workspace_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Publish lifecycle events to every session that belongs to the workspace."""

    for session in app.state.sessions.list(workspace_id=workspace_id):
        app.state.bus.publish(Event(type=event_type, session_id=session.id, payload=payload))


def _previewable(record: ResourceRecord) -> bool:
    media_type = record.detected_mime
    return (
        media_type.startswith("text/")
        or media_type.startswith("image/")
        or media_type.startswith("audio/")
        or media_type.startswith("video/")
        or media_type in _PREVIEWABLE_APPLICATION_TYPES
    )


def register_resource_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register workspace-scoped resource custody routes."""

    del deps

    def resource_wire(record: ResourceRecord) -> dict[str, Any]:
        """Project resource identity with its current converter lifecycle state."""

        payload = record.to_wire()
        payload["processing"] = app.state.resource_processing_store.state(record).model_dump()
        return payload

    def persist_completed_processing(
        record: ResourceRecord,
        state: ResourceProcessingRecord,
        result: dict[str, Any],
    ) -> ResourceProcessingRecord:
        """Persist a converter result without letting malformed output break resource reads."""

        try:
            completed = app.state.resource_processing_store.save_result(record, state, result)
        except ValueError:
            failed = state.model_copy(
                update={
                    "state": "failed",
                    "failure": {"code": "processor_result_invalid"},
                }
            )
            app.state.resource_processing_store.save_state(record, failed)
            _emit_workspace_event(
                app,
                record.workspace_id,
                "resource.processing_failed",
                failed.model_dump(),
            )
            return failed
        _emit_workspace_event(
            app,
            record.workspace_id,
            "resource.processing_completed",
            completed.model_dump(),
        )
        return completed

    async def refresh_processing(record: ResourceRecord) -> ResourceProcessingRecord:
        state = app.state.resource_processing_store.state(record)
        if state.state not in {"submitted", "processing"} or not state.job_id:
            return state
        try:
            payload = await app.state.resource_converter_factory.status(state)
        except (httpx.HTTPError, ResourceConverterUnavailable, RuntimeError, ValueError):
            return state
        remote_state = str(payload.get("status") or "processing")
        if remote_state == "complete":
            result = payload.get("result")
            if not isinstance(result, dict):
                failed = state.model_copy(
                    update={
                        "state": "failed",
                        "failure": {"code": "processor_result_invalid"},
                    }
                )
                app.state.resource_processing_store.save_state(record, failed)
                return failed
            return persist_completed_processing(record, state, result)
        if remote_state in {"failed", "cancelled"}:
            failure = payload.get("failure")
            failed = state.model_copy(
                update={
                    "state": "failed",
                    "failure": failure if isinstance(failure, dict) else {"code": remote_state},
                }
            )
            app.state.resource_processing_store.save_state(record, failed)
            _emit_workspace_event(
                app,
                record.workspace_id,
                "resource.processing_failed",
                failed.model_dump(),
            )
            return failed
        progress = payload.get("progress", state.progress)
        updated = state.model_copy(
            update={
                "state": "processing",
                "progress": int(progress) if isinstance(progress, int | float) else state.progress,
            }
        )
        app.state.resource_processing_store.save_state(record, updated)
        return updated

    async def submit_processing(
        record: ResourceRecord,
        *,
        raise_unavailable: bool,
        reprocess: bool = False,
    ) -> ResourceProcessingRecord:
        """Select and submit through the converter registry, preserving lifecycle events."""

        current = app.state.resource_processing_store.state(record)
        queued_locally = current.state == "submitted" and not current.job_id
        if (current.state in {"submitted", "processing"} and not queued_locally) or (
            current.state == "complete" and not reprocess
        ):
            return current
        if current.state == "cancelled" and not reprocess:
            return current
        try:
            submission = await app.state.resource_converter_factory.submit(
                record,
                app.state.resource_store.content_path(record),
                reprocess=reprocess,
            )
        except ResourceConverterUnavailable as exc:
            if raise_unavailable:
                raise
            failed = current.model_copy(
                update={
                    "state": "failed",
                    "failure": {
                        "code": "resource_converter_unavailable",
                        "media_type": record.detected_mime,
                        "attempted": [converter_id for converter_id, _error in exc.failures],
                    },
                }
            )
            app.state.resource_processing_store.save_state(record, failed)
            _emit_workspace_event(
                app,
                record.workspace_id,
                "resource.processing_failed",
                failed.model_dump(),
            )
            return failed

        converter = submission.converter
        submitted = submission.payload
        processing = ResourceProcessingRecord(
            workspace_id=record.workspace_id,
            resource_id=record.id,
            resource_revision=record.revision,
            source_sha256=record.sha256,
            processor=converter.id,
            processor_url=converter.endpoint,
            job_id=str(submitted["id"]),
            state="submitted",
            derivatives_available=current.derivatives_available,
        )
        latest = app.state.resource_processing_store.state(record)
        if latest.state == "cancelled" and not reprocess:
            try:
                remote = await app.state.resource_converter_factory.cancel(processing)
            except (
                httpx.HTTPError,
                ResourceConverterUnavailable,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                remote = {
                    "remote_cancelled": False,
                    "remote_error": type(exc).__name__,
                }
            cancelled = latest.model_copy(
                update={
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "cancellation": {**latest.cancellation, **remote},
                }
            )
            app.state.resource_processing_store.save_state(record, cancelled)
            return cancelled
        app.state.resource_processing_store.save_state(record, processing)
        if not queued_locally:
            _emit_workspace_event(
                app,
                record.workspace_id,
                "resource.processing_started",
                processing.model_dump(),
            )
        if str(submitted.get("status")) == "complete" and isinstance(submitted.get("result"), dict):
            processing = persist_completed_processing(record, processing, submitted["result"])
        return processing

    def schedule_processing(record: ResourceRecord, background_tasks: BackgroundTasks) -> None:
        """Start automatic conversion only when a registered converter supports the MIME."""

        converter = app.state.resource_converter_factory.get_converter(record)
        if converter is None:
            return
        current = app.state.resource_processing_store.state(record)
        if current.state in {"submitted", "processing", "complete"}:
            return
        queued = ResourceProcessingRecord(
            workspace_id=record.workspace_id,
            resource_id=record.id,
            resource_revision=record.revision,
            source_sha256=record.sha256,
            processor=converter.id,
            processor_url=converter.endpoint,
            state="submitted",
            derivatives_available=current.derivatives_available,
        )
        app.state.resource_processing_store.save_state(record, queued)
        _emit_workspace_event(
            app,
            record.workspace_id,
            "resource.processing_started",
            queued.model_dump(),
        )
        background_tasks.add_task(submit_processing, record, raise_unavailable=False)

    @app.get("/v1/workspaces/{workspace_id}/resources")
    async def list_resources(workspace_id: str) -> dict[str, Any]:
        _workspace(app, workspace_id)
        resources = app.state.resource_store.list(workspace_id)
        for record in resources:
            await refresh_processing(record)
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
        payload = resource_wire(record)
        payload["idempotent_replay"] = idempotent_replay
        payload["upload_url"] = f"/v1/workspaces/{workspace_id}/resources/{record.id}/content"
        if not idempotent_replay:
            _emit_workspace_event(app, workspace_id, "resource.created", payload)
            if record.state == "ready":
                _emit_workspace_event(app, workspace_id, "resource.ready", payload)
        if record.state == "ready":
            schedule_processing(record, background_tasks)
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
                if int(content_length) > _MAX_UPLOAD_CHUNK_BYTES:
                    raise _error(
                        413,
                        "upload_chunk_too_large",
                        f"upload chunks are limited to {_MAX_UPLOAD_CHUNK_BYTES} bytes",
                    )
            except ValueError as exc:
                raise _error(
                    400, "invalid_content_length", "Content-Length must be an integer"
                ) from exc
        data = await request.body()
        if len(data) > _MAX_UPLOAD_CHUNK_BYTES:
            raise _error(
                413,
                "upload_chunk_too_large",
                f"upload chunks are limited to {_MAX_UPLOAD_CHUNK_BYTES} bytes",
            )
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
        event_type = "resource.ready" if updated.state == "ready" else "resource.upload_progress"
        _emit_workspace_event(app, workspace_id, event_type, updated.to_wire())
        if updated.state == "ready":
            schedule_processing(updated, background_tasks)
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
        await refresh_processing(record)
        return resource_wire(record)

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/content")
    async def get_resource_content(workspace_id: str, resource_id: str) -> FileResponse:
        record = _resource(app, workspace_id, resource_id)
        try:
            path = app.state.resource_store.content_path(record)
        except ResourceConflictError as exc:
            raise _error(409, "resource_not_ready", str(exc), resource=record.to_wire()) from exc
        return FileResponse(
            path=path,
            media_type=record.detected_mime or "application/octet-stream",
            filename=record.name,
            content_disposition_type="attachment",
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
            and path.stat().st_size > _MAX_TEXT_PREVIEW_BYTES
        ):
            raise _error(
                413,
                "preview_too_large",
                "text preview exceeds the bounded preview limit",
                max_preview_bytes=_MAX_TEXT_PREVIEW_BYTES,
            )
        return FileResponse(
            path=path,
            media_type=record.detected_mime,
            filename=record.name,
            content_disposition_type="inline",
        )

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/search")
    async def search_resource(workspace_id: str, resource_id: str, q: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        if not record.detected_mime.startswith("text/"):
            raise _error(415, "search_unavailable", "bounded search requires textual content")
        path: Path = app.state.resource_store.content_path(record)
        if path.stat().st_size > _MAX_TEXT_PREVIEW_BYTES:
            raise _error(
                413,
                "search_input_too_large",
                "resource exceeds the bounded direct-search limit; use a structured processor",
            )
        needle = q.strip().casefold()
        if not needle:
            raise _error(400, "invalid_request", "search query cannot be empty")
        matches: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if needle in line.casefold():
                matches.append({"line": line_number, "text": line[:500]})
            if len(matches) == 50:
                break
        return {
            "resource_id": resource_id,
            "query": q,
            "matches": matches,
            "truncated": len(matches) == 50,
        }

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/derivatives")
    async def list_resource_derivatives(workspace_id: str, resource_id: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        processing = await refresh_processing(record)
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

        remote: dict[str, Any] = {"remote_cancelled": False}
        if current.job_id:
            try:
                remote = await app.state.resource_converter_factory.cancel(current)
            except (
                httpx.HTTPError,
                ResourceConverterUnavailable,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                remote = {
                    "remote_cancelled": False,
                    "remote_error": type(exc).__name__,
                }
        cancelled = cancelled.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cancellation": {**cancelled.cancellation, **remote},
            }
        )
        app.state.resource_processing_store.save_state(record, cancelled)
        _emit_workspace_event(
            app,
            workspace_id,
            "resource.processing_cancelled",
            cancelled.model_dump(),
        )
        return cancelled.model_dump()

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/structure")
    async def resource_structure(workspace_id: str, resource_id: str) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        state = await refresh_processing(record)
        if not state.derivatives_available:
            raise _error(409, "resource_processing_incomplete", "document structure is not ready")
        return app.state.resource_processing_store.structure_outline(record)

    @app.get("/v1/workspaces/{workspace_id}/resources/{resource_id}/structure/{collection}/{index}")
    async def resource_structure_node(
        workspace_id: str, resource_id: str, collection: str, index: int
    ) -> dict[str, Any]:
        record = _resource(app, workspace_id, resource_id)
        try:
            node = app.state.resource_processing_store.node(record, collection, index)
        except (FileNotFoundError, IndexError, KeyError) as exc:
            raise _error(404, "structure_node_not_found", "structured node not found") from exc
        except ValueError as exc:
            raise _error(413, "structure_node_too_large", str(exc)) from exc
        return {"collection": collection, "index": index, "node": node}

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
        media_type = str(entry.get("media_type") or "application/octet-stream")
        response = FileResponse(
            path,
            media_type=media_type,
            filename=str(entry.get("name") or derivative_id),
            content_disposition_type="inline",
        )
        if media_type == "text/html":
            response.headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
            )
        return response

    @app.delete("/v1/workspaces/{workspace_id}/resources/{resource_id}")
    async def delete_resource(workspace_id: str, resource_id: str) -> Response:
        record = _resource(app, workspace_id, resource_id)
        if not app.state.resource_store.delete(workspace_id, resource_id):
            raise _error(404, "not_found", f"resource not found: {resource_id}")
        app.state.resource_delivery_store.delete_resource(workspace_id, resource_id)
        _emit_workspace_event(app, workspace_id, "resource.deleted", record.to_wire())
        return Response(status_code=204)


__all__ = ["register_resource_routes"]
