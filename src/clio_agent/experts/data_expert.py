"""
ClioAgent Data Expert Module

Specializes in scientific data file optimization (HDF5, Parquet).
Uses ReAct with real MCP tools from the CLIO gateway via the
dspy.Tool.from_mcp_tool() bridge for tool-backed analysis.

The DataExpert connects to the FastMCP gateway, loads HDF5 tools,
and uses DSPy ReAct to reason and act with those tools.

Example:
    >>> from clio_agent.experts import DataExpert
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> expert = DataExpert()
    >>> result = expert(
    ...     question="How do I optimize HDF5 compression for my 100GB simulation output?",
    ...     file_context="Using parallel HDF5 on 64 cores, mostly float64 data"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)
"""

import asyncio
import json
import logging
import threading
from typing import Any, Optional

import dspy
from fastmcp import Client

from clio_agent.signatures.expert_sig import DataExpertSignature
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)


class MCPToolBridge:
    """Bridge between async FastMCP tools and sync DSPy Tool objects.

    Runs a persistent background event loop with an open FastMCP Client
    connection, enabling synchronous tool calls from DSPy ReAct.

    This solves the event loop nesting problem: MCP tools are async but
    DSPy ReAct calls tools synchronously. A dedicated background thread
    owns the event loop and Client connection.

    Args:
        server: FastMCP server instance to connect to
        timeout: Timeout in seconds for tool calls
    """

    def __init__(self, server: Any, timeout: float = 30.0):
        self._server = server
        self._timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._client: Optional[Client] = None
        self._mcp_tools: dict[str, Any] = {}
        self._setup_done = threading.Event()
        self._loop.call_soon_threadsafe(asyncio.ensure_future, self._setup())
        if not self._setup_done.wait(timeout=10):
            raise RuntimeError("MCPToolBridge setup timed out")

    def _run_loop(self) -> None:
        """Run the background event loop."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _setup(self) -> None:
        """Open Client connection and discover tools."""
        self._client = Client(self._server)
        await self._client.__aenter__()
        tools = await self._client.list_tools()
        for t in tools:
            self._mcp_tools[t.name] = t
        self._setup_done.set()

    def call_tool(self, name: str, args: dict[str, Any]) -> str:
        """Call an MCP tool synchronously via the background event loop.

        Args:
            name: Tool name
            args: Tool arguments

        Returns:
            JSON string of the tool result
        """
        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(name, args), self._loop
        )
        result = future.result(timeout=self._timeout)
        data = result.data
        if isinstance(data, dict):
            return json.dumps(data)
        return str(data)

    def get_tool_names(self) -> list[str]:
        """Return names of all available tools."""
        return list(self._mcp_tools.keys())

    def to_dspy_tools(self) -> list[dspy.Tool]:
        """Convert MCP tools to DSPy Tool objects.

        Creates sync wrapper functions for each MCP tool that call through
        the background event loop bridge.

        Returns:
            List of dspy.Tool objects ready for ReAct
        """
        tools = []
        for name, mcp_tool in self._mcp_tools.items():
            tool = self._make_dspy_tool(name, mcp_tool)
            tools.append(tool)
        return tools

    def _make_dspy_tool(self, name: str, mcp_tool: Any) -> dspy.Tool:
        """Create a single DSPy Tool from an MCP tool definition.

        Args:
            name: Tool name
            mcp_tool: MCP tool object with description and inputSchema

        Returns:
            A dspy.Tool wrapping the MCP tool call
        """
        description = mcp_tool.description or name

        def tool_fn(**kwargs: Any) -> str:
            return self.call_tool(name, kwargs)

        tool_fn.__name__ = name
        tool_fn.__doc__ = description

        schema = mcp_tool.inputSchema or {}
        properties = schema.get("properties", {})

        return dspy.Tool(
            func=tool_fn,
            name=name,
            desc=description,
            args=properties,
        )

    def close(self) -> None:
        """Shut down the bridge, closing the Client and event loop."""
        if self._client:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._client.__aexit__(None, None, None), self._loop
                )
                future.result(timeout=5)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class DataExpert(dspy.Module):
    """Scientific data expert with ReAct + real HDF5 MCP tools.

    Connects to the CLIO MCP gateway via MCPToolBridge to load real
    HDF5 analysis tools, then uses DSPy ReAct for tool-augmented reasoning.

    Attributes:
        arc_memory: Optional ARC memory instance for caching
        agent: DSPy ReAct module with MCP tools

    Example:
        >>> expert = DataExpert()
        >>> print(f"Loaded {len(expert._tools)} tools")
        >>> result = expert(
        ...     question="Analyze compression in my_data.h5",
        ...     file_context="/path/to/my_data.h5, 2GB, climate simulation"
        ... )
        >>> print(result.analysis)
    """

    def __init__(self, arc_memory: Optional[Any] = None):
        """Initialize Data Expert with ReAct and real MCP tools.

        Args:
            arc_memory: Optional ARCMemory instance for tool result caching
        """
        super().__init__()
        self.arc_memory = arc_memory

        # Bridge MCP tools to DSPy tools via background event loop
        self._bridge = MCPToolBridge(gateway)
        self._tools = self._bridge.to_dspy_tools()

        logger.info(
            "DataExpert initialized with %d tools: %s",
            len(self._tools),
            [t.name for t in self._tools],
        )

        # ReAct agent with real MCP-backed tools
        self.agent = dspy.ReAct(
            DataExpertSignature,
            tools=self._tools,
            max_iters=5,
        )

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate data I/O analysis using ReAct with real MCP tools.

        Args:
            question: User's question about data files or I/O optimization
            file_context: File information (paths, sizes, formats)

        Returns:
            dspy.Prediction with analysis and recommendations fields
        """
        return self.agent(question=question, file_context=file_context)

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority.
            Used by ClioAgent to route questions to this expert.
        """
        return {
            "name": "Data Expert",
            "description": (
                "Specializes in scientific data file optimization (HDF5, Parquet), "
                "compression strategies, I/O performance, and format conversion"
            ),
            "keywords": [
                "hdf5",
                "parquet",
                "compression",
                "chunking",
                "data format",
                "file optimization",
                "i/o performance",
                "parallel io",
                "mpi-io",
            ],
            "priority": 1,
        }
