"""Tiles, grid facts and section samples for registered gridded artifacts.

The layered map's raster layers and the section view read registered grids
through these routes, same-origin and authenticated like every artifact
route, so the browser never receives the source file.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from pydantic import BaseModel, Field

from clio_agent.gact.artifacts.raster_query import RasterQueryError
from clio_agent.gact.artifacts.raster_tiles import grid_info, render_tile, section_sample
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.routes.artifact_table_preview import _error
from clio_agent.gact.routes.table_route_shared import table_source


class SectionQueryRequest(BaseModel):
    """Section variable and the largest sample the view will draw."""

    variable: str | None = None
    width: int = Field(default=600, ge=2, le=2000)
    height: int = Field(default=300, ge=2, le=1000)


def _select(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RasterQueryError("raster_selection_invalid", "select must be a JSON object") from exc
    if not isinstance(value, dict):
        raise RasterQueryError("raster_selection_invalid", "select must be a JSON object")
    return value


def register_artifact_grid_view_routes(app: FastAPI) -> None:
    """Add the tile, grid-info and section-query routes."""

    semaphore = asyncio.Semaphore(8)

    async def _source(artifact_id: str) -> tuple[Path, str]:
        registry = await asyncio.to_thread(get_registry, app)
        found = registry.get_by_artifact_id(artifact_id)
        if found is None:
            raise _error(
                404, "not_found", f"artifact not found: {artifact_id}", artifact_id=artifact_id
            )
        record, version = found
        path = await asyncio.to_thread(table_source, app, record, version)
        return path, record.name

    async def _bounded(fn: Any, *args: Any, **kwargs: Any) -> Any:
        async with semaphore:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=30)

    @app.get("/v1/artifacts/{artifact_id}/tiles/{z}/{x}/{y}.png")
    async def artifact_grid_tile(
        artifact_id: str,
        z: int,
        x: int,
        y: int,
        variable: str | None = None,
        select: str | None = None,
        colormap: str = "viridis",
        vmin: float | None = Query(default=None),
        vmax: float | None = Query(default=None),
        opacity: float = Query(default=1.0, ge=0.0, le=1.0),
    ) -> Response:
        """One XYZ tile of a registered grid, coloured with a fixed value range."""
        try:
            path, name = await _source(artifact_id)
            value_range = (
                (vmin, vmax) if vmin is not None and vmax is not None and vmax > vmin else None
            )
            png = await _bounded(
                render_tile,
                path,
                z=z,
                x=x,
                y=y,
                variable=variable,
                select=_select(select),
                colormap=colormap,
                value_range=value_range,
                opacity=opacity,
                name=name,
            )
        except RasterQueryError as exc:
            raise _error(exc.status_code, exc.code, str(exc), artifact_id=artifact_id) from exc
        except TimeoutError as exc:
            raise _error(504, "tile_timeout", "Tile rendering exceeded 30 seconds.") from exc
        return Response(
            png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"}
        )

    @app.get("/v1/artifacts/{artifact_id}/grid-info")
    async def artifact_grid_info(
        artifact_id: str, variable: str | None = None, select: str | None = None
    ) -> dict[str, Any]:
        """Variables, extent, times and a legend range of a registered grid."""
        try:
            path, name = await _source(artifact_id)
            return await _bounded(
                grid_info, path, variable=variable, select=_select(select), name=name
            )
        except RasterQueryError as exc:
            raise _error(exc.status_code, exc.code, str(exc), artifact_id=artifact_id) from exc
        except TimeoutError as exc:
            raise _error(504, "grid_info_timeout", "Grid facts exceeded 30 seconds.") from exc

    @app.post("/v1/artifacts/{artifact_id}/section-query")
    async def artifact_section_query(
        artifact_id: str, body: SectionQueryRequest, request: Request
    ) -> dict[str, Any]:
        """A bounded sample of a registered section (distance x vertical)."""
        try:
            path, name = await _source(artifact_id)
            if await request.is_disconnected():
                raise _error(499, "client_disconnected", "Section query client disconnected.")
            return await _bounded(
                section_sample,
                path,
                variable=body.variable,
                width=body.width,
                height=body.height,
                name=name,
            )
        except RasterQueryError as exc:
            raise _error(exc.status_code, exc.code, str(exc), artifact_id=artifact_id) from exc
        except TimeoutError as exc:
            raise _error(
                504, "section_query_timeout", "Section query exceeded 30 seconds."
            ) from exc
