#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
# ]
# ///

"""
ClioAgent MCP Wrapper

FastMCP-based client for calling ClioAgent MCP tool servers.
Provides clean integration between DSPy agents and MCP tools.

Architecture:
    DSPy ReAct Agent
        ↓ calls tool function
    MCP Wrapper (this module)
        ↓ FastMCP Client
    MCP Server (FastMCP)
        ↓ executes tool
    Returns result to agent

Usage:
    >>> from clio_agent.tools.mcp_wrapper import get_mcp_client, call_tool
    >>>
    >>> # Get client for HDF5 server
    >>> async with get_mcp_client("hdf5") as client:
    ...     result = await client.call_tool(
    ...         "hdf5_analyze",
    ...         {"filepath": "/data/sim.h5"}
    ...     )
    >>>
    >>> # Or use synchronous wrapper for DSPy
    >>> result = call_tool("hdf5", "hdf5_analyze", {"filepath": "/data/sim.h5"})
"""

from fastmcp import Client
from typing import Dict, Any, Optional
import asyncio
from dataclasses import dataclass


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class MCPConfig:
    """Configuration for ClioAgent MCP servers.

    Follows FastMCP client configuration pattern.
    Each server can be HTTP (URL) or stdio (command).
    """
    servers: Dict[str, Dict[str, Any]] = None

    def __post_init__(self):
        if self.servers is None:
            # Default server configuration
            # These can be overridden via environment or config file
            self.servers = {
                "hdf5": {
                    "url": "http://localhost:8000/mcp",
                    "transport": "sse"  # Server-Sent Events
                },
                # Future servers
                # "slurm": {
                #     "url": "http://localhost:8001/mcp",
                #     "transport": "sse"
                # },
                # "darshan": {
                #     "command": "python",
                #     "args": ["src/clio_agent/tools/servers/darshan_server.py"]
                # }
            }

    def to_fastmcp_config(self) -> dict:
        """Convert to FastMCP client configuration format.

        Returns:
            Configuration dict compatible with fastmcp.Client
        """
        return {"mcpServers": self.servers}


# Global configuration
_config = MCPConfig()


# ============================================================================
# EXCEPTIONS
# ============================================================================

class MCPError(Exception):
    """Base exception for MCP-related errors."""
    pass


class MCPServerUnavailable(MCPError):
    """Raised when MCP server is not available."""
    pass


class MCPToolNotFound(MCPError):
    """Raised when MCP tool doesn't exist."""
    pass


# ============================================================================
# FASTMCP CLIENT FUNCTIONS
# ============================================================================

def get_mcp_client(server: Optional[str] = None) -> Client:
    """Get FastMCP client for server(s).

    Args:
        server: Optional specific server name. If None, connects to all servers.

    Returns:
        FastMCP Client instance

    Example:
        >>> # Connect to specific server
        >>> async with get_mcp_client("hdf5") as client:
        ...     result = await client.call_tool("hdf5_analyze", {...})
        >>>
        >>> # Connect to all configured servers
        >>> async with get_mcp_client() as client:
        ...     # Tools are prefixed: hdf5_analyze, slurm_submit, etc.
        ...     result = await client.call_tool("hdf5_analyze", {...})
    """
    config = _config.to_fastmcp_config()

    if server:
        # Single server config
        if server not in _config.servers:
            raise MCPError(f"Unknown server: {server}")

        config = {"mcpServers": {server: _config.servers[server]}}

    return Client(config)


async def call_tool_async(
    server: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None
) -> Any:
    """Call MCP tool asynchronously using FastMCP client.

    Args:
        server: Server name (hdf5, slurm, etc.)
        tool_name: Tool name (hdf5_analyze, slurm_submit, etc.)
        arguments: Tool arguments dict

    Returns:
        Tool result

    Raises:
        MCPServerUnavailable: If server not reachable
        MCPToolNotFound: If tool doesn't exist

    Example:
        >>> result = await call_tool_async(
        ...     "hdf5",
        ...     "hdf5_analyze",
        ...     {"filepath": "/data/sim.h5"}
        ... )
    """
    arguments = arguments or {}

    try:
        async with get_mcp_client(server) as client:
            # FastMCP client automatically handles tool calling
            result = await client.call_tool(tool_name, arguments)
            return result

    except ConnectionError as e:
        raise MCPServerUnavailable(
            f"Cannot connect to MCP server '{server}'. "
            f"Ensure server is running. Error: {e}"
        )
    except Exception as e:
        if "not found" in str(e).lower():
            raise MCPToolNotFound(f"Tool '{tool_name}' not found on server '{server}'")
        raise MCPError(f"MCP call failed: {e}")


def call_tool(
    server: str,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None
) -> Any:
    """Synchronous wrapper for call_tool_async (for DSPy compatibility).

    DSPy ReAct tools must be synchronous functions.
    This wrapper runs async call in event loop.

    Args:
        server: Server name
        tool_name: Tool name
        arguments: Tool arguments

    Returns:
        Tool result

    Example:
        >>> # Use in DSPy ReAct tool definition
        >>> def hdf5_analyze(filepath: str) -> dict:
        ...     return call_tool("hdf5", "hdf5_analyze", {"filepath": filepath})
    """
    try:
        # Get or create event loop
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Run async call
        return loop.run_until_complete(
            call_tool_async(server, tool_name, arguments)
        )

    except MCPError:
        raise
    except Exception as e:
        raise MCPError(f"Error calling tool: {e}")


def is_server_available(server: str) -> bool:
    """Check if MCP server is available.

    Args:
        server: Server name

    Returns:
        True if server reachable, False otherwise
    """
    try:
        # Try listing tools as health check
        async def check():
            async with get_mcp_client(server) as client:
                await client.list_tools()
                return True

        return asyncio.run(check())

    except:
        return False


def configure_mcp(config: MCPConfig):
    """Configure global MCP settings.

    Args:
        config: MCPConfig instance

    Example:
        >>> custom_config = MCPConfig(servers={
        ...     "hdf5": {"url": "http://hpc-cluster:8000/mcp"}
        ... })
        >>> configure_mcp(custom_config)
    """
    global _config
    _config = config


# ============================================================================
# DSPy TOOL WRAPPER PATTERN
# ============================================================================

def create_dspy_tool(server: str, tool_name: str, fallback: Optional[callable] = None):
    """Create DSPy-compatible tool function from MCP server.

    This pattern integrates FastMCP tools with DSPy ReAct agents.

    Args:
        server: MCP server name
        tool_name: Tool name on server
        fallback: Optional fallback if server unavailable

    Returns:
        Synchronous function suitable for dspy.ReAct

    Example:
        >>> # Create tool
        >>> hdf5_analyze = create_dspy_tool("hdf5", "hdf5_analyze")
        >>>
        >>> # Use in ReAct
        >>> agent = dspy.ReAct(
        ...     signature,
        ...     tools=[hdf5_analyze, hdf5_optimize]
        ... )
    """
    def tool_function(**kwargs):
        """Auto-generated DSPy tool function."""
        try:
            return call_tool(server, tool_name, kwargs)
        except MCPServerUnavailable:
            if fallback:
                return fallback(**kwargs)
            return {
                "error": f"MCP server '{server}' unavailable",
                "tool": tool_name,
                "fallback_available": fallback is not None
            }
        except Exception as e:
            return {
                "error": str(e),
                "tool": tool_name
            }

    tool_function.__name__ = tool_name
    return tool_function


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClioAgent MCP Wrapper Test (FastMCP-based)")
    print("=" * 60)

    # Test configuration
    print("\nMCP Configuration:")
    print(f"  Configured servers: {list(_config.servers.keys())}")

    for server, config in _config.servers.items():
        print(f"\n  {server}:")
        for key, value in config.items():
            print(f"    {key}: {value}")

    # Test server availability
    print("\nChecking server availability:")
    for server in _config.servers.keys():
        available = is_server_available(server)
        status = "✓ Available" if available else "✗ Unavailable"
        print(f"  {server}: {status}")

    if not any(is_server_available(s) for s in _config.servers.keys()):
        print("\n⚠ No MCP servers running.")
        print("\nTo start HDF5 server:")
        print("  uv run src/clio_agent/tools/servers/hdf5_server.py")

    print("\n" + "=" * 60)
    print("✅ FastMCP wrapper ready!")
    print("\nKey Features:")
    print("  • FastMCP Client for standard MCP protocol")
    print("  • Async/sync wrappers for DSPy compatibility")
    print("  • Graceful fallbacks when servers unavailable")
    print("  • Easy tool creation for ReAct agents")
