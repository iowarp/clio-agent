"""Marketplace configuration saves do not reload running agents."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from clio_agent.gact.blueprint_revision import blueprint_revision_changed
from clio_agent.gact.blueprint_source_configuration import (
    SourceConfigurationConflict,
    update_configuration,
)


def register_source_configuration_route(app: FastAPI) -> None:
    """Register optimistic configuration editing for the connected CLIO."""

    @app.exception_handler(SourceConfigurationConflict)
    async def configuration_conflict(
        _request: Request, error: SourceConfigurationConflict
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.patch("/v1/agent-blueprints/sources/{source_id}")
    async def update_source(source_id: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            source = await asyncio.to_thread(update_configuration, source_id, request)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail=str(exc)) from exc
        except SourceConfigurationConflict as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        blueprint_revision_changed(app)
        return {"source": source}
