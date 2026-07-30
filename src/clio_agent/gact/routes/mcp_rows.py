"""Wire-row shaping for MCP handshake responses (owner module, no-accretion).

``routes/mcp.py`` is at its size-ratchet baseline, so the per-server row shape
for ``GET /v1/mcp/handshake`` lives here instead of inline in the route. The row
carries the readiness fields the TUI shows plus the ``server/discover`` output
recorded on :class:`~clio_agent.providers.handshake.mcp.MCPServerReport`
(protocol era, backend version, instructions), so the endpoint surfaces *what*
answered — not merely that something did (#1111).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.providers.handshake.mcp import MCPServerReport

__all__ = ["handshake_server_row"]


def handshake_server_row(report: "MCPServerReport") -> dict[str, Any]:
    """Shape one declared server's handshake report into its wire row.

    Args:
        report: The per-server connectivity + discover report from the probe.

    Returns:
        The JSON-serializable row for the ``/v1/mcp/handshake`` ``servers`` list,
        including the discovered ``protocol_version`` / ``server_version`` /
        ``instructions`` (``None`` when the server did not report them).
    """

    status = report.to_integration_status()
    return {
        "name": report.name,
        "reachable": report.ok,
        "state": status.state.value,
        "transport": report.transport,
        "tools_count": report.tool_count,
        "tools": list(report.tools),
        "error": report.error,
        "latency_ms": report.latency_ms,
        "protocol_version": report.protocol_version,
        "server_version": report.server_version,
        "instructions": report.instructions,
    }
