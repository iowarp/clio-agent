"""Workspace-scoped declared MCP specification assembly."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
)
from clio_agent.gact.blueprint_activation import blueprint_mcp_servers, blueprint_server_map
from clio_agent.tools.mcp_config import MCPServerSpec, redact_mcp_spec
from clio_agent.tools.mcp_inventory_snapshot import workspace_mcp_snapshot


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


def session_mcp_server_rows(
    app: Any,
    *,
    cwd: Path | None,
    session_id: str,
) -> list[dict[str, Any]]:
    """Project one session's declared MCPs and resident connection state.

    Declaration discovery is file-only. The optional runtime snapshot reads an
    already-resident workspace fleet and must not create an executor, connect a
    client, or launch a server.
    """

    if not session_id:
        return []
    blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    session = app.state.sessions.get(session_id)
    metadata = getattr(session, "metadata", {}) or {}
    blueprint_name = (
        str(metadata.get("active_agent_blueprint_name") or blueprint_id)
        if isinstance(metadata, Mapping)
        else blueprint_id
    )
    specs = declared_mcp_specs(app, cwd=cwd, session_id=session_id)
    snapshot: Mapping[str, Mapping[str, Any]] = {}
    if cwd is not None:
        snapshot = workspace_mcp_snapshot(getattr(app.state, "agent", None), str(cwd))
    return [
        _session_mcp_server_row(
            namespace,
            spec,
            runtime=snapshot.get(namespace, {}),
            session_id=session_id,
            blueprint_id=blueprint_id,
            blueprint_name=blueprint_name,
        )
        for namespace, spec in sorted(specs.items())
    ]


def _session_mcp_server_row(
    namespace: str,
    spec: MCPServerSpec,
    *,
    runtime: Mapping[str, Any],
    session_id: str,
    blueprint_id: str,
    blueprint_name: str,
) -> dict[str, Any]:
    """Shape one declared namespace without exposing credentials."""

    tools = [str(name) for name in runtime.get("tools", [])]
    pack_owned = spec.source == f"pack:{blueprint_id}"
    status = str(runtime.get("status") or "available") if spec.usable else "unavailable"
    row: dict[str, Any] = {
        "id": f"session_mcp_{session_id}_{namespace}",
        "name": namespace,
        "status": status,
        "transport": spec.transport,
        "tools_count": len(tools),
        "tools": tools,
        "source": "agent_blueprint" if pack_owned else spec.source or "workspace",
        "enabled": spec.usable,
        "session_id": session_id,
        "spec": redact_mcp_spec(asdict(spec)),
    }
    if spec.validation_errors:
        row["error"] = "; ".join(spec.validation_errors)
    if pack_owned:
        row["agent_blueprint_id"] = blueprint_id
        row["agent_blueprint_name"] = blueprint_name
    return row
