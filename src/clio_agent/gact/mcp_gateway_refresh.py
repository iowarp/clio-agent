"""Atomic refresh operations for the process-owned MCP gateway."""

from __future__ import annotations

from typing import Any

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.fleet_server_merge import stamp_default_fleet
from clio_agent.tools.gateway import namespace_proxies


def execution_client_factory() -> Any:
    """Return the normal SDK factory; product task/input owners bind execution."""
    from clio_agent.tools.mcp_runtime import make_mcp_client

    return make_mcp_client


def refresh_declared_mcp_servers(agent: Any) -> Any:
    """Replace an agent's default executor after MCP configuration changes."""

    with agent._tool_gateway_refresh_lock:
        gateway = agent._build_tool_gateway(set_catalog=True)
        executor = create_sync_tool_executor(
            gateway,
            preloaded_tools=agent._tool_definitions,
            namespace_servers=namespace_proxies(gateway),
            server_id="gateway:default",
            client_factory=execution_client_factory(),
        )
        replaced = agent.tool_executor
        agent._mcp_configuration_revision = getattr(agent, "_mcp_configuration_revision", 0) + 1
        stamp_default_fleet(agent, executor, gateway)
        agent._tool_gateway = gateway
        agent.tool_executor = executor
        return replaced
