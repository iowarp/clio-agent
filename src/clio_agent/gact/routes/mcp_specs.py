"""Workspace-scoped declared MCP specification assembly."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.errors import MCP_YAML_DECLARATION_UNREADABLE
from clio_agent.gact.agents.resolution import (
    SessionAgentUnavailable,
    agent_server_set,
    default_agent_ref,
    session_resolution,
    session_server_set,
)
from clio_agent.tools.mcp_config import MCPServerSpec, unreadable_mcp_yaml_snapshot
from clio_agent.tools.mcp_inventory_snapshot import WorkspaceMcpSnapshot, workspace_mcp_snapshot
from clio_agent.tools.mcp_redaction import redact_mcp_spec


def declared_mcp_specs(
    app: Any, cwd: Path | None = None, *, session_id: str = ""
) -> dict[str, Any]:
    """Return MCP specs declared for the selected session and workspace.

    The session agent's plugin servers, then user and workspace ``mcp.yaml``.
    """

    from clio_agent.plugins.servers import server_set_declarations  # noqa: PLC0415
    from clio_agent.search.web_mcp_default import load_agent_mcp_servers  # noqa: PLC0415

    server_set = session_server_set(app, session_id) if session_id else ""
    plugin_servers = server_set_declarations(server_set) if server_set else {}
    return load_agent_mcp_servers(cwd=cwd, plugin_servers=plugin_servers)


def _draft_mcp_specs(app: Any, cwd: Path | None, agent_ref: str) -> dict[str, Any]:
    """The MCP specs a new conversation on ``agent_ref`` in this workspace would declare."""

    from clio_agent.plugins.servers import server_set_declarations  # noqa: PLC0415
    from clio_agent.search.web_mcp_default import load_agent_mcp_servers  # noqa: PLC0415

    server_set = agent_server_set(app, agent_ref)
    plugin_servers = server_set_declarations(server_set) if server_set else {}
    return load_agent_mcp_servers(cwd=cwd, plugin_servers=plugin_servers)


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
    """Project one session's (with none, a new conversation's) declared MCPs and state.

    Declaration discovery is file-only. The optional runtime snapshot reads an
    already-resident workspace fleet and must not create an executor, connect a
    client, or launch a server.

    BLOCKING: reads ``mcp.yaml`` / plugin records and takes the fleet's
    ``threading.Lock``. Async callers run it on a worker (see ``routes.mcp``).
    """

    if session_id:
        try:
            agent_ref = str(session_resolution(app, session_id).agent.get("ref") or "")
        except SessionAgentUnavailable as exc:
            agent_ref = exc.ref
        try:
            specs = declared_mcp_specs(app, cwd=cwd, session_id=session_id)
            connection_error = ""
        except SessionAgentUnavailable as exc:
            specs = {}
            connection_error = str(exc)
    else:
        # No conversation chosen: what a new one here would get, on the default agent.
        agent_ref = default_agent_ref(app)
        try:
            specs = _draft_mcp_specs(app, cwd, agent_ref)
            connection_error = ""
        except SessionAgentUnavailable as exc:
            specs = {}
            connection_error = str(exc)
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
    if connection_error:
        degradations.append({"reason": "mcp_connection_unavailable", "detail": connection_error})
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
                agent_ref=agent_ref,
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
    agent_ref: str,
) -> dict[str, Any]:
    """Shape one declared namespace without exposing credentials."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    tools = [str(name) for name in runtime.get("tools", [])]
    plugin_owned = spec.source.startswith("plugin:")
    status = str(runtime.get("status") or "available") if spec.usable else "unavailable"
    # A server being prepared (or whose preparation failed) says so, with its phase and
    # typed reason, until its connection is up.
    preparation = mcp_prepare.preparation_state(namespace)
    if spec.usable and status != "ready" and preparation is not None:
        if preparation["state"] in (mcp_prepare.PREPARING, mcp_prepare.FAILED):
            status = str(preparation["state"])
    row: dict[str, Any] = {
        "id": f"session_mcp_{session_id}_{namespace}" if session_id else f"draft_mcp_{namespace}",
        "name": namespace,
        "status": status,
        "transport": spec.transport,
        "tools_count": len(tools),
        "tools": tools,
        "source": "plugin" if plugin_owned else spec.source or "workspace",
        "enabled": spec.usable,
        "session_id": session_id,
        "spec": redact_mcp_spec(asdict(spec)),
    }
    if preparation is not None:
        row["preparation"] = preparation
        if status == mcp_prepare.FAILED:
            row["reason"] = preparation.get("reason", "")
    if runtime_unavailable:
        # ``available`` alone conflates "declared, fleet never started" with
        # "declared, fleet reaped" with "declared, reader is broken".
        row["runtime_unavailable"] = runtime_unavailable
    if spec.validation_errors:
        row["error"] = "; ".join(spec.validation_errors)
    if plugin_owned:
        row["plugin"] = spec.source.removeprefix("plugin:")
        row["agent_ref"] = agent_ref
    return row
