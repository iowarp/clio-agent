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

from fastmcp import FastMCP

from clio_agent.tools.servers.hdf5_server import hdf5_server

gateway = FastMCP("clio-gateway")
gateway.mount(hdf5_server, prefix="hdf5")


def get_gateway() -> FastMCP:
    """Return the CLIO gateway instance.

    Returns:
        The singleton FastMCP gateway with all tool servers mounted.
    """
    return gateway
