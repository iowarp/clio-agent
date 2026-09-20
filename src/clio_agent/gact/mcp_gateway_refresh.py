"""Atomic refresh operations for the process-owned MCP gateway."""

from __future__ import annotations

from functools import partial
from typing import Any

from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import namespace_proxies


def correlated_execution_client_factory() -> Any:
    """Build clients carrying CLIO's correlated elicitation handlers."""

    from clio_agent.gact.elicitation_correlation import (
        correlated_capabilities,
        make_correlated_handlers,
    )
    from clio_agent.tools.mcp_runtime import make_mcp_client

    return partial(
        make_mcp_client,
        handlers=make_correlated_handlers(),
        capabilities=correlated_capabilities(),
    )


def refresh_declared_mcp_servers(agent: Any) -> Any:
    """Replace an agent's default executor after MCP configuration changes."""

    with agent._tool_gateway_refresh_lock:
        gateway = agent._build_tool_gateway(set_catalog=True)
        executor = create_sync_tool_executor(
            gateway,
            preloaded_tools=agent._tool_definitions,
            namespace_servers=namespace_proxies(gateway),
            server_id="gateway:default",
            client_factory=correlated_execution_client_factory(),
        )
        replaced = agent.tool_executor
        agent._relay_federation_epoch = getattr(agent, "_relay_federation_epoch", 0) + 1
        agent._tool_gateway = gateway
        agent.tool_executor = executor
        return replaced
