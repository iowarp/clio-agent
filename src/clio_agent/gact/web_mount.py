"""The optional web UI (``clio web``): the built SPA bundle served same-origin.

Gated on ``CLIO_WEB_DIR`` so the default server (headless API) is unchanged unless web
mode is explicitly enabled. Mounted LAST so every ``/v1`` API route (and ``/docs``,
``/openapi.json``) takes precedence; an SPA fallback serves ``index.html`` for unknown
non-API paths so client-side (history) routing works. The bundle's API calls are
same-origin (relative ``/v1/...``), so no CORS or proxy is needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import FileResponse


class SPAStaticFiles(StaticFiles):
    """Static files with the SPA's ``index.html`` for unknown non-API paths."""

    async def get_response(self, path: str, scope: Any) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # An unknown API path stays a JSON 404, never the SPA's HTML.
            if exc.status_code == 404 and not (path == "v1" or path.startswith("v1/")):
                return FileResponse(Path(str(self.directory)) / "index.html")
            raise


def mount_web_ui(app: FastAPI, web_dir: str | None) -> None:
    """Mount the built web bundle at ``/`` when ``web_dir`` holds one."""
    if web_dir and (Path(web_dir) / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=web_dir, html=True), name="web")
