"""
CLIO Agent MCP Gateway

FastMCP gateway that composes all tool servers under namespaced prefixes.
Currently mounts the HDF5 server; Phase 2 will add Parquet and others.

The gateway exposes all HDF5 tools namespaced as:
    hdf5_list_datasets, hdf5_analyze_dataset, hdf5_check_compression,
    hdf5_optimize_chunking, hdf5_analyze_file

Usage:
    >>> from clio_agent.tools.gateway import gateway, get_gateway
    >>> # Use with FastMCP Client
    >>> from fastmcp import Client
    >>> async with Client(gateway) as client:
    ...     tools = await client.list_tools()
    ...     result = await client.call_tool("hdf5_analyze_file", {"filepath": "data.h5"})
"""

from typing import Any

from fastmcp import Client, FastMCP

from clio_agent.tools.servers.hdf5_server import hdf5_server

# Gateway singleton: composes all tool servers under namespaced prefixes.
# Phase 2 will add: gateway.mount(parquet_server, prefix="parquet")
gateway = FastMCP("clio-gateway")
gateway.mount(hdf5_server, prefix="hdf5")


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance.

    Returns:
        The singleton FastMCP gateway with all tool servers mounted.
    """
    return gateway


async def list_gateway_tools() -> list[dict[str, Any]]:
    """List all tools available through the gateway with their metadata.

    Useful for debugging and introspection. Returns tool names, descriptions,
    and input schemas from all mounted servers.

    Returns:
        List of dicts with name, description, and input_schema for each tool.

    Example:
        >>> import asyncio
        >>> tools = asyncio.run(list_gateway_tools())
        >>> for t in tools:
        ...     print(f"{t['name']}: {t['description']}")
    """
    async with Client(gateway) as client:
        mcp_tools = await client.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.inputSchema,
            }
            for t in mcp_tools
        ]
