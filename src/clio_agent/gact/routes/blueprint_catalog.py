"""Nonblocking installed-blueprint catalog route."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from clio_agent.gact.agent_blueprints import discover_agent_blueprints
from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd


def register_blueprint_catalog_route(app: FastAPI) -> None:
    """Run synchronous discovery in FastAPI's worker pool, not the ASGI loop."""

    @app.get("/v1/agent-blueprints")
    def list_agent_blueprints(workspace_id: str | None = None) -> dict[str, Any]:
        """Return the authoritative installed catalog for the requested workspace."""
        cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id or "")
        blueprints = [row.to_wire() for row in discover_agent_blueprints(cwd=cwd)]
        return {"agent_blueprints": blueprints}
