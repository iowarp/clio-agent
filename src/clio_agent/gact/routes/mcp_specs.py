"""Workspace-scoped declared MCP specification assembly."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.errors import MCP_YAML_DECLARATION_UNREADABLE
from clio_agent.gact.agents.resolution import (
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
)
from clio_agent.gact.blueprint_activation import blueprint_mcp_servers, blueprint_server_map
from clio_agent.tools.mcp_config import MCPServerSpec, unreadable_mcp_yaml_snapshot
from clio_agent.tools.mcp_inventory_snapshot import WorkspaceMcpSnapshot, workspace_mcp_snapshot
from clio_agent.tools.mcp_redaction import redact_mcp_spec


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


@dataclass(frozen=True)
class SessionMcpInventory:
    """One session's declared MCP rows plus every typed reason they are partial."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    degradations: list[dict[str, str]] = field(default_factory=list)


def session_mcp_inventory(
    app: Any,
    *,
    cwd: Path | None,
    session_id: str,
) -> SessionMcpInventory:
    """Project one session's declared MCPs, resident state, and why it is partial.

    Declaration discovery is file-only. The optional runtime snapshot reads an
    already-resident workspace fleet and must not create an executor, connect a
    client, or launch a server.

    BLOCKING: reads ``mcp.yaml`` / blueprint files and takes the fleet's
    ``threading.Lock``. Async callers run it on a worker (see ``routes.mcp``).
    """

    if not session_id:
        return SessionMcpInventory()
    blueprint_id = _runtime_active_agent_blueprint_id(app, session_id)
    session = app.state.sessions.get(session_id)
    metadata = getattr(session, "metadata", {}) or {}
    blueprint_name = (
        str(metadata.get("active_agent_blueprint_name") or blueprint_id)
        if isinstance(metadata, Mapping)
        else blueprint_id
    )
    specs = declared_mcp_specs(app, cwd=cwd, session_id=session_id)
    # A declaration file that failed to PARSE is not the same as one that is
    # absent: without this the listing is simply shorter and says nothing.
    degradations: list[dict[str, str]] = [
        {
            "reason": MCP_YAML_DECLARATION_UNREADABLE,
            "detail": f"{row.get('path', '')}: {row.get('error', '')}".strip(": "),
        }
        for row in unreadable_mcp_yaml_snapshot()
    ]
    snapshot = WorkspaceMcpSnapshot()
    if cwd is not None:
        snapshot = workspace_mcp_snapshot(getattr(app.state, "agent", None), str(cwd))
    degraded = snapshot.degraded
    if degraded is not None:
        degradations.append(degraded)
    return SessionMcpInventory(
        rows=[
            _session_mcp_server_row(
                namespace,
                spec,
                runtime=snapshot.namespaces.get(namespace, {}),
                runtime_unavailable=snapshot.reason,
                session_id=session_id,
                blueprint_id=blueprint_id,
                blueprint_name=blueprint_name,
            )
            for namespace, spec in sorted(specs.items())
        ],
        degradations=degradations,
    )


def _session_mcp_server_row(
    namespace: str,
    spec: MCPServerSpec,
    *,
    runtime: Mapping[str, Any],
    runtime_unavailable: str,
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
    if runtime_unavailable:
        # ``available`` alone conflates "declared, fleet never started" with
        # "declared, fleet reaped" with "declared, reader is broken".
        row["runtime_unavailable"] = runtime_unavailable
    if spec.validation_errors:
        row["error"] = "; ".join(spec.validation_errors)
    if pack_owned:
        row["agent_blueprint_id"] = blueprint_id
        row["agent_blueprint_name"] = blueprint_name
    return row
