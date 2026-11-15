"""A connector's server is mounted only while the connector is on (LZ-D8).

Enabling a connector is the person's consent to install it: for a clio-kit server,
to download its tools. So a resolution mounts a server bound to a connector only
while that connector is on. Bound means either a saved connection of the connector
(``{"type": "clio-connection", "connection": <id>}``, a following plugin or a carrier)
or the connector's own clio-kit server (``clio-kit mcp-server <id>``) declared by a
plugin of the agent.

A server whose connector is off leaves the resolution with a typed diagnostic,
never a failure: ``{"severity": "warning", "code": "connector_off" |
"connector_not_found", "plugin", "server", "because": <connector>}``. The plugin
stays, with its skills; turning the connector on brings the server back
(:func:`clio_agent.connectors.follow.drifted` pins the conversation again). This
replaces the failure an agent's own following plugin used to cause
(``mcp_connection_unavailable``) and the server-name collision of a plugin's
clio-kit server with its connector (PL-F5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.connectors import kit_entries

CONNECTOR_OFF = "connector_off"
CONNECTOR_GONE = "connector_not_found"
_CODES = frozenset({CONNECTOR_OFF, CONNECTOR_GONE})


def bound_connector(spec: Mapping[str, Any], entries: Mapping[str, Any]) -> str:
    """The connector a server declaration is bound to ("" when none)."""

    spec = dict(spec or {})
    if spec.get("type") == "clio-connection":
        connection = str(spec.get("connection") or "")
        return connection if connection in entries else ""
    server = kit_entries.kit_server(spec)
    return server if server and server in entries else ""


def _entries() -> dict[str, Any]:
    from clio_agent.connectors import state  # noqa: PLC0415

    return state.all_entries()


def bound_connectors(resolution: Any) -> set[str]:
    """The connectors whose servers ``resolution`` mounts."""

    entries = _entries()
    return {
        cid
        for row in (resolution.mcp_servers or {}).values()
        if (cid := bound_connector(row.get("spec") or {}, entries))
    }


def left_out(resolution: Any) -> list[str]:
    """The connectors whose servers ``resolution`` left out because they were off."""

    return [
        str(row.get("because") or "")
        for row in resolution.diagnostics or ()
        if row.get("code") in _CODES and row.get("server")
    ]


def left_out_rows(resolution: Any) -> list[dict[str, Any]]:
    """The diagnostics of the servers left out (plugin, server, because)."""

    return [
        dict(row)
        for row in resolution.diagnostics or ()
        if row.get("code") in _CODES and row.get("server")
    ]


def gate(resolution: Any) -> bool:
    """Leave out the servers whose connector is off; ``True`` when one was left out.

    The caller re-digests the resolution when it changed.
    """

    from clio_agent.connectors.follow import enabled_ids  # noqa: PLC0415

    servers = dict(resolution.mcp_servers or {})
    if not servers:
        return False
    entries = _entries()
    on: set[str] | None = None
    removed: list[dict[str, Any]] = []
    for name, row in sorted(servers.items()):
        cid = bound_connector(row.get("spec") or {}, entries)
        if not cid:
            continue
        on = enabled_ids() if on is None else on
        if cid in on:
            continue
        del servers[name]
        state = "off" if cid in entries else "gone"
        removed.append(
            {
                "severity": "warning",
                "code": CONNECTOR_OFF if state == "off" else CONNECTOR_GONE,
                "message": f"MCP server {name!r} of {row.get('plugin')} is left out while "
                f"its connector {cid!r} is {'off' if state == 'off' else 'not found'}",
                "plugin": str(row.get("plugin") or ""),
                "server": name,
                "because": cid,
            }
        )
    if not removed:
        return False
    resolution.mcp_servers = servers
    gone = {f"{row['server']}_" for row in removed}
    resolution.tools = [row for row in resolution.tools if row.get("name") not in gone]
    resolution.diagnostics = [*resolution.diagnostics, *removed]
    return True
