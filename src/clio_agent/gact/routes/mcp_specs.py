"""Workspace-scoped declared MCP specification assembly."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
)
from clio_agent.gact.blueprint_activation import blueprint_mcp_servers, blueprint_server_map


def declared_mcp_specs(
    app: Any,
    cwd: Path | None = None,
    *,
    session_id: str = "",
    active_blueprint_id: Callable[[Any, str], str] = _runtime_active_agent_blueprint_id,
    active_blueprint_path: Callable[[Any, str], Path | None] = (
        _runtime_active_agent_blueprint_path
    ),
    load_blueprint_servers: Callable[..., dict[str, dict[str, Any]]] = blueprint_mcp_servers,
) -> dict[str, Any]:
    """Return MCP specs declared for the selected session and workspace."""

    from clio_agent.tools.mcp_config import load_mcp_servers  # noqa: PLC0415

    blueprint_id = active_blueprint_id(app, session_id)
    pack_servers: dict[str, dict[str, Any]] = {}
    if blueprint_id:
        blueprint_path = active_blueprint_path(app, session_id)
        if blueprint_path is not None:
            try:
                from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415
                    parse_agent_blueprint_root,
                )

                blueprint = parse_agent_blueprint_root(blueprint_path, scope="session")
                if blueprint.enabled and blueprint.id == blueprint_id:
                    servers = blueprint_server_map(blueprint)
                    if servers:
                        pack_servers[blueprint_id] = servers
            except Exception:  # noqa: BLE001,S110 - mcp.yaml remains available
                pass
        else:
            pack_servers = load_blueprint_servers(blueprint_id, cwd=cwd)
    return load_mcp_servers(cwd=cwd, pack_servers=pack_servers)
