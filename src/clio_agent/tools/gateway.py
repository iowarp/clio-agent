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

import inspect
from typing import Any, cast

from fastmcp import Client, FastMCP

from clio_agent.tools.servers.adios_server import adios_server
from clio_agent.tools.servers.fs_server import fs_server
from clio_agent.tools.servers.hdf5_server import hdf5_server
from clio_agent.tools.servers.ndp_server import ndp_server
from clio_agent.tools.servers.parquet_server import parquet_server
from clio_agent.tools.servers.sac_server import sac_server
from clio_agent.tools.servers.shell_server import shell_server


def _mount_with_namespace(parent: FastMCP, server: FastMCP, namespace: str) -> None:
    """Mount a server with stable namespaced tool names across FastMCP versions."""
    mount_params = inspect.signature(parent.mount).parameters
    mount = cast(Any, parent.mount)
    if "namespace" in mount_params:
        mount(server, namespace=namespace)
    else:
        mount(server, prefix=namespace)


# Gateway singleton: composes all tool servers under namespaced prefixes.
gateway = FastMCP("clio-gateway")
_mount_with_namespace(gateway, hdf5_server, "hdf5")
_mount_with_namespace(gateway, parquet_server, "parquet")
_mount_with_namespace(gateway, adios_server, "adios")
_mount_with_namespace(gateway, ndp_server, "ndp")
_mount_with_namespace(gateway, sac_server, "sac")
_mount_with_namespace(gateway, fs_server, "fs")
_mount_with_namespace(gateway, shell_server, "shell")


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
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(_list())
    else:
        # If already in an event loop, create a new one in a thread.
        # Avoid calling asyncio.run(_list()) first: that creates a
        # coroutine object before asyncio.run raises, which leaks an
        # unawaited-coroutine warning even though the fallback succeeds.
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
        elif t.name.startswith("adios_"):
            server = "adios"
        elif t.name.startswith("ndp_"):
            server = "ndp"
        elif t.name.startswith("sac_"):
            server = "sac"
        elif t.name.startswith("fs_"):
            server = "fs"
        elif t.name.startswith("shell_"):
            server = "shell"
        else:
            server = "unknown"
        capabilities.append(
            {
                "name": t.name,
                "description": first_sentence,
                "server": server,
            }
        )
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
