"""Agent Toolkit MCP Connector - Optimal Async/Sync Bridge

This module provides connectivity to Agent Toolkit (iowarp-mcps) MCP servers from:
https://github.com/iowarp/iowarp-mcps

Architecture:
    ClaudIO Agent (sync DSPy code)
        ↓ calls tool
    IOWarpMCPConnector (this module - thread-safe bridge)
        ↓ FastMCP Client protocol (async)
    Agent Toolkit MCP Server (external: uvx iowarp-mcps <server>)
        ↓ executes operation
    Scientific data system (HDF5 files, SLURM cluster, etc.)

Optimal Async/Sync Bridge Pattern:
    - Long-lived event loop in dedicated thread
    - Persistent client connections (enter context once, keep alive)
    - Thread-safe via asyncio.run_coroutine_threadsafe()
    - Proper cleanup in shutdown()

Usage:
    >>> # Sync wrapper for DSPy agents
    >>> tools = IOWarpMCPTools(arc_memory=arc)
    >>> result = tools.call_tool("hdf5", "analyze_file", {"filepath": "/data/sim.h5"})
    >>> # Cleanup on exit
    >>> tools.shutdown()
"""

import asyncio
import os
import threading
import time
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
# OPTIMAL ASYNC/SYNC BRIDGE
# ============================================================================


class IOWarpMCPConnector:
    """Optimal async/sync bridge with persistent clients and long-lived event loop.

    Provides thread-safe bridge between sync DSPy code and async FastMCP Client.
    Uses long-lived event loop in dedicated thread for optimal performance.

    Key Features:
        - One event loop for ALL MCP operations
        - Clients persist across calls (subprocess stays alive)
        - Proper FastMCP async context manager usage
        - Thread-safe from DSPy sync code
        - Clean shutdown with explicit close()
        - ARC caching integration
        - Agent Toolkit configuration (uvx iowarp-mcps pattern)

    Args:
        arc_memory: Optional ARCMemory instance for tool result caching
        config_file: Optional path to MCP server configuration JSON

    Examples:
        >>> connector = IOWarpMCPConnector(arc_memory=arc)
        >>> result = connector.call_tool("hdf5", "analyze_file", {"filepath": "/data/sim.h5"})
        >>> connector.shutdown()  # Clean shutdown
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        config_file: Optional[str] = None
    ):
        """Initialize connector with long-lived event loop.

        Args:
            arc_memory: Optional ARCMemory for caching tool results
            config_file: Optional custom configuration file path
        """
        self.arc = arc_memory
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._clients: Dict[str, Client] = {}
        self._client_lock = threading.Lock()
        self.servers: Dict[str, MCPServerConfig] = {}

        # Initialize Agent Toolkit server configurations
        if config_file:
            self._load_config_file(config_file)
        else:
            self._initialize_agent_toolkit_servers()

        # Start long-lived event loop thread
        self._start_event_loop()

    def _start_event_loop(self) -> None:
        """Start long-lived event loop in dedicated thread.

        Creates daemon thread running asyncio event loop for all MCP operations.
        Loop persists across multiple tool calls for optimal performance.
        """
        def run_loop():
            """Event loop runner in dedicated thread."""
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(
            target=run_loop,
            daemon=True,
            name="MCP-EventLoop"
        )
        self._loop_thread.start()

        # Wait for loop to be initialized
        while self._loop is None:
            time.sleep(0.001)

    def _initialize_agent_toolkit_servers(self) -> None:
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

    async def _connect_server_async(self, server_name: str) -> Client:
        """Connect to server with persistent client.

        Creates client and enters async context manager once.
        Client persists across calls for optimal subprocess reuse.

        Args:
            server_name: Server name (e.g., "hdf5", "adios")

        Returns:
            Connected FastMCP Client instance

        Raises:
            ValueError: If server name is unknown
            ConnectionError: If connection fails
        """
        # Use lock to prevent race conditions during connection check/create
        with self._client_lock:
            # Return existing client if already connected
            if server_name in self._clients:
                return self._clients[server_name]

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

                # CRITICAL: Enter context and keep alive
                await client.__aenter__()
                self._clients[server_name] = client
                return client

            except Exception as e:
                raise ConnectionError(
                    f"Failed to connect to Agent Toolkit MCP server '{server_name}': {e}"
                ) from e

    async def _call_tool_async(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> Any:
        """Call tool with caching (async implementation).

        Args:
            server_name: Server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments as dict

        Returns:
            Tool result (format depends on tool)
        """
        # Check ARC cache first
        if self.arc:
            cached = self.arc.get_cached_tool_result(server_name, tool_name, arguments)
            if cached is not None:
                return cached

        # Get persistent client (creates connection if needed)
        client = await self._connect_server_async(server_name)

        # Call tool (client is already in context)
        # FastMCP multi-server pattern: prefix tool name with server name
        full_tool_name = f"{server_name}_{tool_name}"
        result = await client.call_tool(full_tool_name, arguments)

        # Cache result in ARC
        if self.arc:
            # Cache for 1 hour (tool results are relatively stable)
            self.arc.cache_tool_result(
                server_name,
                tool_name,
                arguments,
                result,
                ttl_seconds=3600
            )

        return result

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any]
    ) -> Any:
        """Thread-safe sync wrapper for tool calling.

        Submits async operation to long-lived event loop via run_coroutine_threadsafe().
        Thread-safe for use from DSPy sync code.

        Args:
            server_name: Server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments as dict

        Returns:
            Tool result

        Raises:
            ValueError: If server or tool is unknown
            ConnectionError: If server connection fails
            TimeoutError: If tool call exceeds 60s timeout

        Examples:
            >>> connector = IOWarpMCPConnector()
            >>> result = connector.call_tool(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"filepath": "/data/simulation.h5"}
            ... )
        """
        if self._loop is None:
            raise RuntimeError("Event loop not initialized")

        # Submit to event loop thread
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(server_name, tool_name, arguments),
            self._loop
        )

        # Wait for result with timeout
        return future.result(timeout=60.0)

    async def _list_tools_async(self, server_name: str) -> List[Any]:
        """List available tools on server (async implementation).

        Args:
            server_name: Server name (e.g., "hdf5")

        Returns:
            List of tool definitions with schemas
        """
        client = await self._connect_server_async(server_name)
        tools = await client.list_tools()
        return tools  # type: ignore[no-any-return]

    def list_tools(self, server_name: str) -> List[Any]:
        """List available tools on server (sync wrapper).

        Args:
            server_name: Server name (e.g., "hdf5")

        Returns:
            List of tool definitions with schemas
        """
        if self._loop is None:
            raise RuntimeError("Event loop not initialized")

        future = asyncio.run_coroutine_threadsafe(
            self._list_tools_async(server_name),
            self._loop
        )
        return future.result(timeout=10.0)

    async def _read_resource_async(self, server_name: str, uri: str) -> Any:
        """Read resource from server (async implementation).

        Args:
            server_name: Server name
            uri: Resource URI (server-specific format)

        Returns:
            Resource content
        """
        client = await self._connect_server_async(server_name)
        # Prefix URI with server name for multi-server routing
        prefixed_uri = f"{server_name}://{uri}"
        content = await client.read_resource(prefixed_uri)
        return content

    def read_resource(self, server_name: str, uri: str) -> Any:
        """Read resource from server (sync wrapper).

        Resources provide read-only access to data sources (files, metadata, etc.)

        Args:
            server_name: Server name
            uri: Resource URI (server-specific format)

        Returns:
            Resource content

        Examples:
            >>> content = connector.read_resource(
            ...     "hdf5",
            ...     "file:///data/sim.h5/dataset/temperature"
            ... )
        """
        if self._loop is None:
            raise RuntimeError("Event loop not initialized")

        future = asyncio.run_coroutine_threadsafe(
            self._read_resource_async(server_name, uri),
            self._loop
        )
        return future.result(timeout=10.0)

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

    def shutdown(self) -> None:
        """Clean shutdown of event loop and clients.

        Exits all client async context managers and stops event loop.
        Should be called before program exit for clean shutdown.

        Examples:
            >>> connector = IOWarpMCPConnector()
            >>> # ... use connector ...
            >>> connector.shutdown()  # Clean shutdown
        """
        if self._loop is None:
            return

        # Exit all client contexts
        for client in list(self._clients.values()):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    client.__aexit__(None, None, None),
                    self._loop
                )
                future.result(timeout=5.0)
            except Exception:
                # Ignore errors during shutdown
                pass

        # Clear clients
        self._clients.clear()

        # Stop event loop
        self._loop.call_soon_threadsafe(self._loop.stop)

        # Wait for thread to finish
        if self._loop_thread:
            self._loop_thread.join(timeout=5.0)

    def __del__(self):
        """Cleanup on delete."""
        try:
            self.shutdown()
        except Exception:
            # Ignore errors during cleanup
            pass


# ============================================================================
# SYNCHRONOUS WRAPPER FOR DSPY
# ============================================================================


class IOWarpMCPTools:
    """Synchronous wrapper for Agent Toolkit MCP connector.

    Provides sync interface for DSPy agents that require synchronous tool functions.
    Uses optimal async/sync bridge with long-lived event loop.

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
        >>>
        >>> # Clean shutdown
        >>> tools.shutdown()
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
        arguments: Dict[str, Any]
    ) -> Any:
        """Synchronous tool call wrapper.

        Args:
            server_name: Server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments

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
        return self.connector.call_tool(server_name, tool_name, arguments)

    def list_tools(self, server_name: str) -> List[Any]:
        """List tools on server (synchronous).

        Args:
            server_name: Server name

        Returns:
            List of tool definitions
        """
        return self.connector.list_tools(server_name)

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
        return self.connector.read_resource(server_name, uri)

    def shutdown(self) -> None:
        """Clean shutdown of connector.

        Should be called before program exit.
        """
        self.connector.shutdown()

    def __del__(self):
        """Cleanup on delete."""
        try:
            self.shutdown()
        except Exception:
            pass


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================


def create_iowarp_tool_function(
    server_name: str,
    tool_name: str,
    connector: Optional[IOWarpMCPConnector] = None,
    arc_memory: Optional[Any] = None
) -> Callable:
    """Create DSPy-compatible tool function for Agent Toolkit MCP tool.

    Convenience function for creating individual tool functions suitable
    for use with DSPy ReAct agents.

    IMPORTANT: Pass a shared connector when creating multiple tools to avoid
    event loop thread proliferation. See examples below.

    Args:
        server_name: Agent Toolkit server name (e.g., "hdf5")
        tool_name: Tool name (e.g., "analyze_file")
        connector: Optional shared IOWarpMCPConnector (creates new if not provided)
        arc_memory: Optional ARCMemory for caching (ignored if connector provided)

    Returns:
        Synchronous function suitable for dspy.ReAct

    Examples:
        >>> # RECOMMENDED: Create single connector, reuse for all tools
        >>> from claudio.tools.mcp_connector import IOWarpMCPConnector
        >>> connector = IOWarpMCPConnector()
        >>> analyze_hdf5 = create_iowarp_tool_function("hdf5", "analyze_file", connector)
        >>> optimize_hdf5 = create_iowarp_tool_function("hdf5", "optimize_layout", connector)
        >>> submit_job = create_iowarp_tool_function("slurm", "submit_job", connector)
        >>>
        >>> # Use with DSPy
        >>> agent = dspy.ReAct(signature, tools=[analyze_hdf5, optimize_hdf5, submit_job])
        >>> # ... use agent ...
        >>> connector.shutdown()  # Clean up when done
        >>>
        >>> # LEGACY: Create individual tools (NOT RECOMMENDED - creates extra threads)
        >>> analyze_hdf5 = create_iowarp_tool_function("hdf5", "analyze_file")
    """
    # Use provided connector or create new one
    if connector is None:
        tools = IOWarpMCPTools(arc_memory)
        connector_to_use = tools.connector
    else:
        # Use shared connector (reuse existing event loop)
        connector_to_use = connector

    def tool_function(**kwargs):
        """Auto-generated Agent Toolkit tool function."""
        return connector_to_use.call_tool(server_name, tool_name, kwargs)

    tool_function.__name__ = f"{server_name}_{tool_name}"
    tool_function.__doc__ = f"Call {tool_name} on {server_name} Agent Toolkit MCP server"

    return tool_function
