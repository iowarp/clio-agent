"""Connected-CLIO marketplace operation history."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Query

from clio_agent.gact.blueprint_operations import list_blueprint_operations


def register_blueprint_operation_routes(app: FastAPI) -> None:
    """Expose durable progress and outcomes independently of the initiating request."""

    @app.get("/v1/agent-blueprints/operations")
    async def operations(limit: int = Query(default=30, ge=1, le=100)) -> dict[str, Any]:
        rows = await asyncio.to_thread(list_blueprint_operations, app, limit=limit)
        return {"operations": rows}
