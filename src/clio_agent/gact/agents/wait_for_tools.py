"""``wait_for_tools``: let an agent wait for an MCP server that is still preparing.

A turn never waits for a server it may not use (:mod:`clio_agent.gact.
mcp_background_prepare`). When one of the agent's servers is still preparing as the
turn starts, the agent gets this tool, whose description names those servers. Calling it
blocks until they are ready or failed (or the timeout passes) and reports each one's
state; the servers' tools join the agent's tool list at its next step
(:mod:`clio_agent.gact.agents.live_tool_refresh`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

WAIT_FOR_TOOLS = "wait_for_tools"
_MAX_WAIT_S = 600.0


def preparing_namespaces(namespaces: Iterable[str], picked: Mapping[str, Any]) -> list[str]:
    """The namespaces among ``namespaces`` with no listed tools and a preparation running."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    return sorted(
        namespace
        for namespace in namespaces
        if mcp_prepare.is_preparing(namespace)
        and not any(name.startswith(f"{namespace}_") for name in picked)
    )


def wait_for_servers(namespaces: list[str], timeout_s: float) -> dict[str, Any]:
    """Wait for ``namespaces`` and describe each one's preparation state."""

    from clio_agent.tools import mcp_prepare  # noqa: PLC0415

    rows = mcp_prepare.wait_for_namespaces(namespaces, min(max(timeout_s, 0.0), _MAX_WAIT_S))
    servers: dict[str, Any] = {}
    for namespace, row in rows.items():
        if row is None:
            servers[namespace] = {"state": "unknown"}
            continue
        entry: dict[str, Any] = {"state": row.get("state"), "phase": row.get("phase")}
        for key in ("reason", "detail", "tool_count"):
            if key in row:
                entry[key] = row[key]
        if row.get("state") == mcp_prepare.READY:
            entry["note"] = "its tools are available from your next step"
        servers[namespace] = entry
    return {"servers": servers}


def build_wait_for_tools_tool(preparing: list[str]) -> Any:
    """The ``wait_for_tools`` tool for an agent whose ``preparing`` servers are starting."""

    from clio_agent.gact.agents.tool_instrumentation import plugin_tool  # noqa: PLC0415

    names = ", ".join(preparing)

    def wait_for_tools(servers: list[str] | None = None, timeout_s: float = 120.0) -> dict:
        """Wait until the named MCP servers finish preparing, then report their state."""

        return wait_for_servers(list(servers or preparing), timeout_s)

    return plugin_tool(
        wait_for_tools,
        name=WAIT_FOR_TOOLS,
        desc=(
            f"These MCP servers are still starting, so their tools are not in your tool "
            f"list yet: {names}. Call this to wait for them (servers defaults to all of "
            f"them; timeout_s defaults to 120). When a server is ready its tools are "
            f"available from your next step; a failed one reports a typed reason."
        ),
        read_only=True,
    )


__all__ = [
    "WAIT_FOR_TOOLS",
    "build_wait_for_tools_tool",
    "preparing_namespaces",
    "wait_for_servers",
]
