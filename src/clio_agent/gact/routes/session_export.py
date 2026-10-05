"""Authenticated raw transcripts and portable session export downloads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from clio_agent.gact.routes.content_disposition import content_disposition
from clio_agent.gact.session_export import build_transcript
from clio_agent.gact.session_export_archive import ExportMode, build_archive
from clio_agent.gact.session_export_downloads import ExportDownloads
from clio_agent.gact.session_export_review import review_summary
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


class VisualReview(BaseModel):
    """Frontend renderer and its captured, credential-free visual dependencies."""

    javascript: str = Field(min_length=1, max_length=30_000_000)
    stylesheet: str = Field(max_length=5_000_000)
    snapshot: dict[str, Any]


class VisualExport(BaseModel):
    """An offline review request; optional payloads follow the chosen scope."""

    mode: ExportMode = "transcript"
    visual_review: VisualReview


class _ExportFileResponse(FileResponse):
    """Remove the temporary ZIP even when its browser download is interrupted."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # FileResponse's background task runs only after a successful send.
            # Its file handle is closed before this finally clause, including
            # cancellation; a single unlink must not itself await cancellation.
            Path(self.path).unlink(missing_ok=True)


def _error(status: int, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=reason, message=message, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def register_session_export_routes(app: FastAPI) -> None:
    """Register downloads without projecting away recorded payloads."""
    downloads = ExportDownloads()
    app.state.session_export_downloads = downloads

    @app.get("/v1/sessions/{sid}/export-review", response_model=None)
    async def export_review_summary(sid: str) -> dict[str, Any]:
        """Return references and child IDs for visual capture, without large raw trace payloads."""
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        try:
            transcript = await asyncio.to_thread(build_transcript, app, sid)
        except (OSError, ValueError) as exc:
            raise _error(409, "session_export_failed", str(exc)) from exc

        return await asyncio.to_thread(review_summary, app, transcript)

    @app.get("/v1/session-export-downloads/{ticket}", response_model=None)
    async def download_prepared_export(ticket: str) -> FileResponse:
        """Stream one authenticated-prepared ZIP directly to the browser's download manager."""
        item = downloads.take(ticket)
        if item is None:
            raise _error(
                404, "export_download_unavailable", "Export download expired or was already used."
            )
        return _ExportFileResponse(
            item.path,
            media_type="application/zip",
            headers={
                "Content-Disposition": content_disposition(item.filename),
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @app.post("/v1/sessions/{sid}/export-download", response_model=None)
    async def prepare_export_download(sid: str, body: VisualExport) -> dict[str, str]:
        """Prepare an archive without sending its potentially large bytes through UI memory."""
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        try:
            transcript = await asyncio.to_thread(build_transcript, app, sid)
            path = await asyncio.to_thread(
                build_archive, app, transcript, body.mode, body.visual_review.model_dump()
            )
            name = str(transcript["session"].get("title") or sid)
            name = "".join(c if c.isalnum() or c in "._-" else "-" for c in name).strip(".-") or sid
            filename = f"{name}.{body.mode}.zip"
            return {"download_path": downloads.add(path, filename), "filename": filename}
        except (OSError, ValueError) as exc:
            raise _error(409, "session_export_failed", str(exc)) from exc

    @app.get("/v1/sessions/{sid}/export", response_model=None)
    async def export_session(sid: str, mode: ExportMode | None = None) -> Any:
        """Return raw JSON by default, or a portable ZIP for an explicit export mode."""
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        try:
            transcript = await asyncio.to_thread(build_transcript, app, sid)
            if mode is None:
                return transcript
            path: Path = await asyncio.to_thread(build_archive, app, transcript, mode)
        except (OSError, ValueError) as exc:
            raise _error(409, "session_export_failed", str(exc)) from exc
        return _ExportFileResponse(
            path,
            media_type="application/zip",
            headers={"Content-Disposition": content_disposition(f"{sid}.{mode}.zip")},
        )

    @app.post("/v1/sessions/{sid}/export", response_model=None)
    async def export_visual_session(sid: str, body: VisualExport) -> FileResponse:
        """Package the client's offline renderer and captured visual dependencies.

        The server remains the authority for the raw transcript and effect/workspace
        bytes. Client snapshots are review data, never interpreted as file paths.
        """
        if app.state.sessions.get(sid) is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        try:
            transcript = await asyncio.to_thread(build_transcript, app, sid)
            path = await asyncio.to_thread(
                build_archive, app, transcript, body.mode, body.visual_review.model_dump()
            )
        except (OSError, ValueError) as exc:
            raise _error(409, "session_export_failed", str(exc)) from exc
        return _ExportFileResponse(
            path,
            media_type="application/zip",
            headers={"Content-Disposition": content_disposition(f"{sid}.{body.mode}.zip")},
        )
