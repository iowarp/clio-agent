"""
ClioAgent Data Expert Module

Specializes in scientific data file optimization (HDF5, Parquet).
Uses ReAct with real MCP tools from the CLIO gateway via the CLIO tool
execution boundary for tool-backed analysis.

The DataExpert connects to the FastMCP gateway, loads HDF5 tools, and uses
DSPy ReAct to reason and act with those tools.

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

import logging
from typing import Any, Optional

import dspy

from clio_agent.signatures.expert_sig import DataExpertSignature
from clio_agent.tools import execution as tool_execution
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)

MCPToolBridge = tool_execution.MCPToolBridge


class DataExpert(dspy.Module):
    """Scientific data expert with ReAct + real HDF5 MCP tools.

    Connects to the CLIO MCP gateway through a sync tool executor to load real
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

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        tool_executor: Optional[ToolExecutor] = None,
    ):
        """Initialize Data Expert with ReAct and real MCP tools.

        Args:
            arc_memory: Optional ARCMemory instance for tool result caching
            tool_executor: Optional sync executor for MCP-backed tools
        """
        super().__init__()
        self.arc_memory = arc_memory

        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        self._bridge = self._tool_executor
        self._tools = self._tool_executor.to_dspy_tools()

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

    def close(self) -> None:
        """Release tool execution resources."""
        self._tool_executor.close()

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
