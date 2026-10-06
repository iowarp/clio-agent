"""Resample registered raster artifacts for an interactive viewport."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from clio_agent.gact.artifacts.raster_query import RasterQueryError, load_raster, resample_raster
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.routes.artifact_table_preview import _error
from clio_agent.gact.routes.table_route_shared import table_source


class RasterQueryRequest(BaseModel):
    """Visible extent and target sample count for one viewport request."""

    width: int = Field(default=512, ge=1, le=1024)
    height: int = Field(default=512, ge=1, le=1024)
    extent: tuple[float, float, float, float] | None = None
    variable: str | None = None
    band: int = Field(default=1, ge=1)


def register_artifact_raster_query_routes(app: FastAPI) -> None:
    """Add the bounded raster-query owner route to the GACT application."""

    semaphore = asyncio.Semaphore(4)

    @app.post("/v1/artifacts/{artifact_id}/raster-query")
    async def artifact_raster_query(
        artifact_id: str, body: RasterQueryRequest, request: Request
    ) -> dict[str, Any]:
        """Return finite/null samples for the visible extent of a registered grid."""

        registry = await asyncio.to_thread(get_registry, app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _error(
                404, "not_found", f"artifact not found: {artifact_id}", artifact_id=artifact_id
            )
        record, version = found
        try:
            path = await asyncio.to_thread(table_source, app, record, version)
            if await request.is_disconnected():
                raise _error(499, "client_disconnected", "Raster query client disconnected.")
            return await _run_bounded_query(semaphore, path, record.name, body, timeout=30)
        except RasterQueryError as exc:
            raise _error(exc.status_code, exc.code, str(exc), artifact_id=artifact_id) from exc
        except TimeoutError as exc:
            raise _error(504, "raster_query_timeout", "Raster query exceeded 30 seconds.") from exc


async def _run_bounded_query(
    semaphore: asyncio.Semaphore,
    path: Path,
    name: str,
    body: RasterQueryRequest,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Hold the concurrency slot until the worker thread actually exits."""

    await semaphore.acquire()
    try:
        worker = asyncio.create_task(asyncio.to_thread(_query, path, name, body))
    except BaseException:
        semaphore.release()
        raise

    def release_when_done(done: asyncio.Task[dict[str, Any]]) -> None:
        semaphore.release()
        if not done.cancelled():
            done.exception()  # Consume errors if the client stopped waiting.

    worker.add_done_callback(release_when_done)
    return await asyncio.wait_for(asyncio.shield(worker), timeout=timeout)


def _query(path: Path, name: str, body: RasterQueryRequest) -> dict[str, Any]:
    """Keep source decoding and sampling on the bounded worker pool."""

    grid = load_raster(path, name=name, variable=body.variable, band=body.band)
    return resample_raster(grid, width=body.width, height=body.height, extent=body.extent)
