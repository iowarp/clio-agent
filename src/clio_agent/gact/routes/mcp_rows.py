"""Wire-row shaping for MCP route responses (owner module, no-accretion).

``routes/mcp.py`` is at its size-ratchet baseline, so handshake and server-detail
row shapes live here instead of inline in the route. Handshake rows retain
``server/discover`` data, inventory rows shape tools/resources/prompts, and
``prompts/get`` results retain SDK aliases (#1111, #1117), so the endpoints surface *what*
answered — not merely that something did (#1111).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact.agents.tool_instrumentation import mcp_tool_title
from clio_agent.gact.permission_gate import _normalize_mcp_tool_annotations
from clio_agent.tools.mcp_runtime import wire_value

if TYPE_CHECKING:
    from clio_agent.providers.handshake.mcp import MCPServerReport

__all__ = [
    "bundled_server_tool_rows",
    "handshake_server_row",
    "mcp_inventory_row",
    "mcp_prompt_result_row",
]


def handshake_server_row(report: "MCPServerReport") -> dict[str, Any]:
    """Shape one declared server's handshake report into its wire row.

    Args:
        report: The per-server connectivity + discover report from the probe.

    Returns:
        The JSON-serializable row for the ``/v1/mcp/handshake`` ``servers`` list,
        including the discovered ``protocol_version`` / ``server_version`` /
        ``instructions`` (``None`` when the server did not report them), and
        (#1201) ``execution_era`` / ``execution_downgrade_reason`` -- the latest
        era ANY execution-path connection actually observed for this server,
        which may differ from this probe's own ``protocol_version`` above.
        (#1283, C1-S3) ``extensions`` -- the latest server-declared extension
        identifier SET (:mod:`clio_agent.tools.mcp_extension_registry`'s read
        side), an empty list when never observed. Previously
        ``ServerCapabilities.extensions`` was not surfaced on any wire row at
        all (LEG_C2.md avenue 8's finding).
    """

    status = report.to_integration_status()
    era = report.execution_era
    extensions = report.declared_extensions
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
        "execution_era": era.era if era else None,
        "execution_downgrade_reason": era.degrade_reason if era else None,
        "extensions": list(extensions.extensions) if extensions else [],
    }


def bundled_server_tool_rows(short_name: str) -> list[dict[str, Any]]:
    """Return catalog-detail tool rows for one bundled in-process MCP server."""
    try:
        from clio_agent.tools.gateway import list_capabilities  # noqa: PLC0415

        capabilities = list_capabilities()
    except Exception:  # noqa: BLE001 - optional gateway introspection degrades to empty
        return []
    return [
        {
            "id": tool.get("name", ""),
            "name": tool.get("name", ""),
            "title": mcp_tool_title(tool),
            "description": tool.get("description") or "",
        }
        for tool in capabilities
        if tool.get("server") == short_name
    ]


def mcp_inventory_row(
    item: Any,
    *,
    kind: Literal["tools", "resources", "prompts"],
) -> dict[str, Any]:
    """Shape one FastMCP list result for a GACT server-detail inventory."""
    if kind == "tools":
        return {
            "id": item.name,
            "name": item.name,
            "title": mcp_tool_title(item),
            "description": getattr(item, "description", "") or "",
            "annotations": _normalize_mcp_tool_annotations(item),
        }
    if kind == "resources":
        uri = str(getattr(item, "uri", ""))
        return {
            "id": uri or getattr(item, "name", ""),
            "name": getattr(item, "name", "") or uri,
            "description": getattr(item, "description", "") or "",
        }
    if kind == "prompts":
        return {
            "id": item.name,
            "name": item.name,
            "description": getattr(item, "description", "") or "",
        }
    raise ValueError(f"unsupported MCP inventory kind: {kind!r}")


def mcp_prompt_result_row(result: Any) -> dict[str, Any]:
    """Convert a FastMCP ``prompts/get`` result to its alias-preserving wire row."""
    row = wire_value(result, mode="mcp_results", exclude_none=True)
    if not isinstance(row, dict):
        raise TypeError(f"MCP prompt result did not serialize to an object: {type(row).__name__}")
    return row
