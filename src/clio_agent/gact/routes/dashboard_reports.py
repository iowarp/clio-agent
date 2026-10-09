"""Read authored dashboard artifacts and prepare their portable exports."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.gact.dashboard_reports import (
    export_dashboard_image,
    export_dashboard_report,
    list_dashboard_reports,
    read_dashboard_report,
)
from clio_agent.gact.routes.session_export import VisualReview


class DashboardImage(BaseModel):
    """Bounded displayed PNG, prepared with the owning saved dashboard identity."""

    model_config = ConfigDict(extra="forbid")
    png_base64: str = Field(min_length=1, max_length=6_666_668)


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
    async def export_report(
        sid: str, artifact_id: str, body: VisualReview | DashboardImage
    ) -> dict[str, str]:
        """Retain one HTML or PNG and use the shared native download door."""
        require_session(sid)
        try:
            if isinstance(body, DashboardImage):
                path, exported_id = await asyncio.to_thread(
                    export_dashboard_image, app, sid, artifact_id, body.png_base64
                )
                extension = "png"
            else:
                path, exported_id = await asyncio.to_thread(
                    export_dashboard_report, app, sid, artifact_id, body.model_dump()
                )
                extension = "html"
            report = await asyncio.to_thread(read_dashboard_report, app, sid, artifact_id)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        name = (
            "".join(c if c.isalnum() or c in "._-" else "-" for c in report["title"]).strip(".-")
            or "dashboard"
        )
        filename = name + "." + extension
        return {
            "download_path": app.state.session_export_downloads.add(path, filename),
            "filename": filename,
            "artifact_id": exported_id,
        }
