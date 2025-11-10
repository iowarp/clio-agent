"""Agent Toolkit MCP Connector - Client for external Agent Toolkit MCP servers

This module provides connectivity to Agent Toolkit (iowarp-mcps) MCP servers from:
https://github.com/iowarp/iowarp-mcps

Connects to external servers (HDF5, ADIOS, Parquet, SLURM, Darshan, etc.)
using FastMCP Client protocol. Does NOT implement the servers themselves.

Architecture:
    ClaudIO Agent
        ↓ calls tool
    IOWarpMCPConnector (this module)
        ↓ FastMCP Client protocol
    Agent Toolkit MCP Server (external: uvx iowarp-mcps <server>)
        ↓ executes operation
    Scientific data system (HDF5 files, SLURM cluster, etc.)

Usage:
    >>> connector = IOWarpMCPConnector(arc_memory=arc)
    >>> result = await connector.call_tool(
    ...     "hdf5",
    ...     "analyze_file",
    ...     {"filepath": "/data/sim.h5"}
    ... )
    >>> # Or use sync wrapper for DSPy
    >>> tools = IOWarpMCPTools(arc)
    >>> result = tools.call_tool("hdf5", "analyze_file", {...})
"""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastmcp import Client

# ============================================================================
# CONFIGURATION
# ============================================================================


@dataclass
class MCPServerConfig:
    """Configuration for an Agent Toolkit MCP server connection.

    Supports both stdio (local command) and HTTP/SSE (remote) connections.
    Default assumes servers are installed via Agent Toolkit (iowarp-mcps) package.

    Args:
        name: Server name (e.g., "hdf5", "adios")
        url: Optional HTTP/SSE endpoint for remote connection
        command: Optional local command to spawn server
        args: Optional command arguments
        env: Optional environment variables

    Examples:
        >>> # Local stdio connection (Agent Toolkit pattern)
        >>> config = MCPServerConfig(
        ...     name="hdf5",
        ...     command="uvx",
        ...     args=["iowarp-mcps", "hdf5"]
        ... )
        >>>
        >>> # Remote HTTP/SSE connection
        >>> config = MCPServerConfig(
        ...     name="hdf5",
        ...     url="http://hpc-cluster:8000/mcp"
        ... )
    """
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None


# ============================================================================
# CONNECTOR
# ============================================================================


class IOWarpMCPConnector:
    """Connector for Agent Toolkit MCP servers.

    Connects to external Agent Toolkit (iowarp-mcps) MCP servers using FastMCP Client protocol.
    Provides tool calling, resource access, and ARC caching integration.

    Supports Agent Toolkit MCP servers from: https://github.com/iowarp/iowarp-mcps
    - hdf5: HDF5 file analysis and optimization
    - adios: ADIOS BP file operations
    - parquet: Parquet analytics
    - slurm: SLURM job management
    - darshan: Darshan I/O trace analysis
    - compression: Compression testing
    - pandas: DataFrame operations
    - plot: Scientific plotting

    All servers launched via: uvx iowarp-mcps <server-name>

    Args:
        arc_memory: Optional ARCMemory instance for tool result caching
        config_file: Optional path to MCP server configuration JSON

    Examples:
        >>> # Basic usage
        >>> connector = IOWarpMCPConnector()
        >>> result = await connector.call_tool(
        ...     "hdf5",
        ...     "analyze_file",
        ...     {"filepath": "/data/simulation.h5"}
        ... )
        >>>
        >>> # With ARC caching
        >>> from claudio.arc import ARCMemory
        >>> arc = ARCMemory()
        >>> connector = IOWarpMCPConnector(arc_memory=arc)
        >>> result = await connector.call_tool("hdf5", "analyze_file", {...})
        >>> # Second call hits cache
        >>> result = await connector.call_tool("hdf5", "analyze_file", {...})
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        config_file: Optional[str] = None
    ):
        """Initialize Agent Toolkit MCP connector.

        Args:
            arc_memory: Optional ARCMemory for caching tool results
            config_file: Optional custom configuration file path
        """
        self.arc = arc_memory
        self.servers: Dict[str, MCPServerConfig] = {}
        self.clients: Dict[str, Any] = {}  # Client types vary by transport

        # Initialize Agent Toolkit server configurations
        if config_file:
            self._load_config_file(config_file)
        else:
            self._initialize_iowarp_servers()

    def _initialize_iowarp_servers(self) -> None:
        """Initialize Agent Toolkit (iowarp-mcps) server configurations.

        Connects to Agent Toolkit MCP servers using uvx launcher.
        Each server runs via: uvx iowarp-mcps <server-name>

        Environment variable overrides:
            AGENT_TOOLKIT_<SERVER>_URL: HTTP/SSE endpoint for server
            Examples:
                AGENT_TOOLKIT_HDF5_URL=http://server:8000/mcp
                AGENT_TOOLKIT_ADIOS_URL=http://server:8001/mcp
        """
        # HDF5 Server - File analysis and optimization
        hdf5_url = os.environ.get("AGENT_TOOLKIT_HDF5_URL")
        self.servers["hdf5"] = MCPServerConfig(
            name="hdf5",
            url=hdf5_url,
            command="uvx" if not hdf5_url else None,
            args=["iowarp-mcps", "hdf5"] if not hdf5_url else None,
            env=None
        )

        # ADIOS Server - BP file operations
        adios_url = os.environ.get("AGENT_TOOLKIT_ADIOS_URL")
        self.servers["adios"] = MCPServerConfig(
            name="adios",
            url=adios_url,
            command="uvx" if not adios_url else None,
            args=["iowarp-mcps", "adios"] if not adios_url else None,
            env=None
        )

        # Parquet Server - Analytics and optimization
        parquet_url = os.environ.get("AGENT_TOOLKIT_PARQUET_URL")
        self.servers["parquet"] = MCPServerConfig(
            name="parquet",
            url=parquet_url,
            command="uvx" if not parquet_url else None,
            args=["iowarp-mcps", "parquet"] if not parquet_url else None,
            env=None
        )

        # SLURM Server - Job management
        slurm_url = os.environ.get("AGENT_TOOLKIT_SLURM_URL")
        self.servers["slurm"] = MCPServerConfig(
            name="slurm",
            url=slurm_url,
            command="uvx" if not slurm_url else None,
            args=["iowarp-mcps", "slurm"] if not slurm_url else None,
            env=None
        )

        # Darshan Server - I/O trace analysis
        darshan_url = os.environ.get("AGENT_TOOLKIT_DARSHAN_URL")
        self.servers["darshan"] = MCPServerConfig(
            name="darshan",
            url=darshan_url,
            command="uvx" if not darshan_url else None,
            args=["iowarp-mcps", "darshan"] if not darshan_url else None,
            env=None
        )

        # Compression Server - Compression algorithm testing
        compression_url = os.environ.get("AGENT_TOOLKIT_COMPRESSION_URL")
        self.servers["compression"] = MCPServerConfig(
            name="compression",
            url=compression_url,
            command="uvx" if not compression_url else None,
            args=["iowarp-mcps", "compression"] if not compression_url else None,
            env=None
        )

        # Pandas Server - DataFrame operations
        pandas_url = os.environ.get("AGENT_TOOLKIT_PANDAS_URL")
        self.servers["pandas"] = MCPServerConfig(
            name="pandas",
            url=pandas_url,
            command="uvx" if not pandas_url else None,
            args=["iowarp-mcps", "pandas"] if not pandas_url else None,
            env=None
        )

        # Plot Server - Scientific plotting
        plot_url = os.environ.get("AGENT_TOOLKIT_PLOT_URL")
        self.servers["plot"] = MCPServerConfig(
            name="plot",
            url=plot_url,
            command="uvx" if not plot_url else None,
            args=["iowarp-mcps", "plot"] if not plot_url else None,
            env=None
        )

    def _load_config_file(self, config_file: str) -> None:
        """Load server configurations from JSON file.

        Args:
            config_file: Path to configuration JSON file

        Expected format:
            {
                "hdf5": {
                    "url": "http://server:8000/mcp"
                },
                "adios": {
                    "command": "uvx",
                    "args": ["iowarp-mcps", "adios"]
                }
            }
        """
        import json

        config_path = Path(config_file)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_file}")

        with open(config_path) as f:
            config_data = json.load(f)

        for server_name, server_config in config_data.items():
            self.servers[server_name] = MCPServerConfig(
                name=server_name,
                **server_config
            )

    async def connect_server(self, server_name: str) -> Any:
        """Connect to an Agent Toolkit MCP server.

        Creates FastMCP Client instance for the specified server.
        Reuses existing connections when available.

        Args:
            server_name: Server name (e.g., "hdf5", "adios")

        Returns:
            FastMCP Client instance

        Raises:
            ValueError: If server name is unknown
            ConnectionError: If connection fails

        Examples:
            >>> client = await connector.connect_server("hdf5")
            >>> async with client:
            ...     tools = await client.list_tools()
        """
        # Return existing client if already connected
        if server_name in self.clients:
            return self.clients[server_name]

        # Get server configuration
        config = self.servers.get(server_name)
        if not config:
            raise ValueError(
                f"Unknown Agent Toolkit MCP server: {server_name}. "
                f"Available: {list(self.servers.keys())}"
            )

        # Create client based on connection type
        try:
            if config.url:
                # HTTP/SSE connection (remote server)
                client = Client(config.url)
            elif config.command:
                # Stdio connection (local command)
                # Build MCP config format for FastMCP
                mcp_config = {
                    "mcpServers": {
                        config.name: {
                            "command": config.command,
                            "args": config.args or [],
                            "env": config.env or {}
                        }
                    }
                }
                client = Client(mcp_config)  # type: ignore[assignment]
            else:
                raise ValueError(
                    f"Invalid configuration for server '{server_name}': "
                    "must specify either 'url' or 'command'"
                )

            self.clients[server_name] = client
            return client

        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to Agent Toolkit MCP server '{server_name}': {e}"
            ) from e

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        use_cache: bool = True
    ) -> Any:
        """Call tool on Agent Toolkit MCP server with optional ARC caching.

        Checks ARC cache before calling tool (if enabled). Caches results
        with 1 hour TTL for repeated queries.

        Args:
            server_name: Server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments as dict
            use_cache: Check ARC cache before calling (default: True)

        Returns:
            Tool result (format depends on tool)

        Raises:
            ValueError: If server or tool is unknown
            ConnectionError: If server connection fails

        Examples:
            >>> # Analyze HDF5 file
            >>> result = await connector.call_tool(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"filepath": "/data/simulation.h5"}
            ... )
            >>>
            >>> # Submit SLURM job
            >>> result = await connector.call_tool(
            ...     "slurm",
            ...     "submit_job",
            ...     {"script": "#!/bin/bash\\n#SBATCH -N 1\\n..."}
            ... )
        """
        # Build cache key from server, tool, and arguments
        cache_key = f"iowarp_{server_name}_{tool_name}_{str(sorted(arguments.items()))}"

        # Check ARC cache if enabled
        if use_cache and self.arc:
            cached = self.arc._cache.get(cache_key)
            if cached is not None:
                return cached

        # Connect to server
        client = await self.connect_server(server_name)

        # Call tool via FastMCP
        async with client:
            # FastMCP multi-server pattern: prefix tool name with server name
            full_tool_name = f"{server_name}_{tool_name}"
            result = await client.call_tool(full_tool_name, arguments)

        # Cache result in ARC if enabled
        if use_cache and self.arc:
            # Cache for 1 hour (tool results are relatively stable)
            self.arc._cache.put(cache_key, result, ttl_seconds=3600)

        return result

    async def list_tools(self, server_name: str) -> List[Any]:
        """List available tools on Agent Toolkit MCP server.

        Args:
            server_name: Server name (e.g., "hdf5")

        Returns:
            List of tool definitions with schemas

        Examples:
            >>> tools = await connector.list_tools("hdf5")
            >>> for tool in tools:
            ...     print(f"{tool.name}: {tool.description}")
        """
        client = await self.connect_server(server_name)
        async with client:
            tools = await client.list_tools()
        return tools  # type: ignore[no-any-return]

    async def read_resource(
        self,
        server_name: str,
        uri: str
    ) -> Any:
        """Read resource from Agent Toolkit MCP server.

        Resources provide read-only access to data sources (files, metadata, etc.)

        Args:
            server_name: Server name
            uri: Resource URI (server-specific format)

        Returns:
            Resource content

        Examples:
            >>> # Read HDF5 dataset metadata
            >>> content = await connector.read_resource(
            ...     "hdf5",
            ...     "file:///data/sim.h5/dataset/temperature"
            ... )
        """
        client = await self.connect_server(server_name)
        async with client:
            # Prefix URI with server name for multi-server routing
            prefixed_uri = f"{server_name}://{uri}"
            content = await client.read_resource(prefixed_uri)
        return content

    def get_available_servers(self) -> List[str]:
        """Get list of configured Agent Toolkit MCP servers.

        Returns:
            List of server names

        Examples:
            >>> connector.get_available_servers()
            ['hdf5', 'adios', 'parquet', 'slurm', 'darshan', 'compression', 'pandas', 'plot']
        """
        return list(self.servers.keys())

    def get_server_config(self, server_name: str) -> Optional[MCPServerConfig]:
        """Get configuration for a specific server.

        Args:
            server_name: Server name

        Returns:
            Server configuration or None if not found
        """
        return self.servers.get(server_name)

    async def close_all(self) -> None:
        """Close all MCP client connections.

        Gracefully closes all active connections to Agent Toolkit servers.
        Automatically called when using connector as async context manager.

        Examples:
            >>> async with IOWarpMCPConnector() as connector:
            ...     result = await connector.call_tool(...)
            >>> # Connections automatically closed
        """
        for _client in self.clients.values():
            # FastMCP clients close automatically with context manager
            # No explicit close needed
            pass
        self.clients.clear()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close_all()


# ============================================================================
# SYNCHRONOUS WRAPPER FOR DSPY
# ============================================================================


class IOWarpMCPTools:
    """Synchronous wrapper for Agent Toolkit MCP connector.

    Provides sync interface for DSPy agents that require synchronous tool functions.
    Wraps async connector calls in asyncio.run().

    Args:
        arc_memory: Optional ARCMemory instance for caching
        config_file: Optional path to server configuration

    Examples:
        >>> # Use with DSPy agents
        >>> from claudio.arc import ARCMemory
        >>> arc = ARCMemory()
        >>> tools = IOWarpMCPTools(arc)
        >>>
        >>> # Define DSPy tool
        >>> def analyze_hdf5(filepath: str) -> dict:
        ...     return tools.call_tool(
        ...         "hdf5",
        ...         "analyze_file",
        ...         {"filepath": filepath}
        ...     )
        >>>
        >>> # Use in ReAct agent
        >>> import dspy
        >>> agent = dspy.ReAct(signature, tools=[analyze_hdf5])
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        config_file: Optional[str] = None
    ):
        """Initialize synchronous Agent Toolkit MCP tools wrapper.

        Args:
            arc_memory: Optional ARCMemory for caching
            config_file: Optional custom configuration file
        """
        self.connector = IOWarpMCPConnector(arc_memory, config_file)

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        use_cache: bool = True
    ) -> Any:
        """Synchronous tool call wrapper.

        Args:
            server_name: Server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments
            use_cache: Use ARC caching (default: True)

        Returns:
            Tool result

        Examples:
            >>> tools = IOWarpMCPTools()
            >>> result = tools.call_tool(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"filepath": "/data/sim.h5"}
            ... )
        """
        return asyncio.run(
            self.connector.call_tool(server_name, tool_name, arguments, use_cache)
        )

    def list_tools(self, server_name: str) -> List[Any]:
        """List tools on server (synchronous).

        Args:
            server_name: Server name

        Returns:
            List of tool definitions
        """
        return asyncio.run(self.connector.list_tools(server_name))

    def get_available_servers(self) -> List[str]:
        """Get available Agent Toolkit MCP servers.

        Returns:
            List of server names
        """
        return self.connector.get_available_servers()

    def read_resource(self, server_name: str, uri: str) -> Any:
        """Read resource from server (synchronous).

        Args:
            server_name: Server name
            uri: Resource URI

        Returns:
            Resource content
        """
        return asyncio.run(self.connector.read_resource(server_name, uri))


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================


def create_iowarp_tool_function(
    server_name: str,
    tool_name: str,
    arc_memory: Optional[Any] = None
) -> Callable:
    """Create DSPy-compatible tool function for Agent Toolkit MCP tool.

    Convenience function for creating individual tool functions suitable
    for use with DSPy ReAct agents.

    Args:
        server_name: Agent Toolkit server name (e.g., "hdf5")
        tool_name: Tool name (e.g., "analyze_file")
        arc_memory: Optional ARCMemory for caching

    Returns:
        Synchronous function suitable for dspy.ReAct

    Examples:
        >>> # Create individual tools
        >>> analyze_hdf5 = create_iowarp_tool_function("hdf5", "analyze_file")
        >>> optimize_hdf5 = create_iowarp_tool_function("hdf5", "optimize_layout")
        >>> submit_job = create_iowarp_tool_function("slurm", "submit_job")
        >>>
        >>> # Use with DSPy
        >>> agent = dspy.ReAct(
        ...     signature,
        ...     tools=[analyze_hdf5, optimize_hdf5, submit_job]
        ... )
    """
    tools = IOWarpMCPTools(arc_memory)

    def tool_function(**kwargs):
        """Auto-generated Agent Toolkit tool function."""
        return tools.call_tool(server_name, tool_name, kwargs)

    tool_function.__name__ = f"{server_name}_{tool_name}"
    tool_function.__doc__ = f"Call {tool_name} on {server_name} Agent Toolkit MCP server"

    return tool_function
