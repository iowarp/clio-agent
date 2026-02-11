"""
ClioAgent Tools Module

FastMCP gateway and tool servers for scientific computing.
The gateway composes all MCP servers under namespaced prefixes.
"""

from clio_agent.tools.gateway import gateway, get_gateway, list_gateway_tools

__all__ = ["gateway", "get_gateway", "list_gateway_tools"]
