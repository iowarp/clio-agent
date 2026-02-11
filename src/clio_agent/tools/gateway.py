"""
CLIO Agent MCP Gateway

FastMCP gateway that composes all tool servers under namespaced prefixes.
Mounts HDF5 and Parquet servers with namespaced tool access.

The gateway exposes tools namespaced as:
    hdf5_list_datasets, hdf5_analyze_dataset, hdf5_check_compression,
    hdf5_optimize_chunking, hdf5_analyze_file,
    parquet_analyze_schema, parquet_query_data, parquet_compute_statistics

Usage:
    >>> from clio_agent.tools.gateway import gateway, get_gateway
    >>> # Use with FastMCP Client
    >>> from fastmcp import Client
    >>> async with Client(gateway) as client:
    ...     tools = await client.list_tools()
    ...     result = await client.call_tool("hdf5_analyze_file", {"filepath": "data.h5"})
    ...     result = await client.call_tool("parquet_analyze_schema", {"filepath": "data.parquet"})
"""

from typing import Any

from fastmcp import Client, FastMCP

from clio_agent.tools.servers.hdf5_server import hdf5_server
from clio_agent.tools.servers.parquet_server import parquet_server

# Gateway singleton: composes all tool servers under namespaced prefixes.
gateway = FastMCP("clio-gateway")
gateway.mount(hdf5_server, prefix="hdf5")
gateway.mount(parquet_server, prefix="parquet")


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance.

    Returns:
        The singleton FastMCP gateway with all tool servers mounted.
    """
    return gateway


def list_capabilities() -> list[dict[str, str]]:
    """Return lightweight tool capability summaries for context compilation.

    Returns compact tool summaries (~400 tokens total) instead of full schemas
    (~47K tokens). Each entry contains tool name, first sentence of description,
    and server prefix. Designed for injection into compiled context prompts
    without blowing token budgets.

    Returns:
        List of dicts with name, description (first sentence), and server keys.

    Example:
        >>> caps = list_capabilities()
        >>> for c in caps:
        ...     print(f"{c['server']}/{c['name']}: {c['description']}")
    """
    import asyncio

    async def _list():
        async with Client(gateway) as client:
            return await client.list_tools()

    try:
        tools = asyncio.run(_list())
    except RuntimeError:
        # If already in an event loop, create a new one in a thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            tools = pool.submit(lambda: asyncio.run(_list())).result()

    capabilities = []
    for t in sorted(tools, key=lambda x: x.name):
        # Extract first sentence of description
        desc = t.description or ""
        first_sentence = desc.split(".")[0].strip() + "." if desc else ""
        # Determine server from prefix
        if t.name.startswith("hdf5_"):
            server = "hdf5"
        elif t.name.startswith("parquet_"):
            server = "parquet"
        else:
            server = "unknown"
        capabilities.append({
            "name": t.name,
            "description": first_sentence,
            "server": server,
        })
    return capabilities


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
