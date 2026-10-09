"""Read authored dashboard artifacts and prepare their portable exports."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException

from clio_agent.gact.dashboard_reports import (
    export_dashboard_report,
    list_dashboard_reports,
    read_dashboard_report,
)
from clio_agent.gact.routes.session_export import VisualReview


def register_dashboard_report_routes(app: FastAPI) -> None:
    """Register session-owned report reads and credential-free download preparation."""

    def require_session(sid: str) -> None:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(404, "Session not found.")

    @app.get("/v1/sessions/{sid}/dashboards")
    async def list_reports(sid: str) -> list[dict[str, object]]:
        """List the newest saved version of each authored report."""
        require_session(sid)
        return await asyncio.to_thread(list_dashboard_reports, app, sid)

    @app.get("/v1/sessions/{sid}/dashboards/{artifact_id}")
    async def read_report(sid: str, artifact_id: str) -> dict[str, object]:
        """Read a specific saved report version."""
        require_session(sid)
        try:
            return await asyncio.to_thread(read_dashboard_report, app, sid, artifact_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/sessions/{sid}/dashboards/{artifact_id}/export")
    async def export_report(sid: str, artifact_id: str, body: VisualReview) -> dict[str, str]:
        """Build one HTML and route it through the shared native download door."""
        require_session(sid)
        try:
            path, exported_id = await asyncio.to_thread(
                export_dashboard_report, app, sid, artifact_id, body.model_dump()
            )
            report = await asyncio.to_thread(read_dashboard_report, app, sid, artifact_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        name = (
            "".join(c if c.isalnum() or c in "._-" else "-" for c in report["title"]).strip(".-")
            or "dashboard"
        )
        filename = name + ".html"
        return {
            "download_path": app.state.session_export_downloads.add(path, filename),
            "filename": filename,
            "artifact_id": exported_id,
        }
