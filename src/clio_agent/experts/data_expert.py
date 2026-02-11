"""
ClioAgent Data Expert Module

Specializes in scientific data file optimization (HDF5, ADIOS, Parquet).
Currently uses ChainOfThought for all queries. Plan 02 will add ReAct + MCP tools.

Key Capabilities:
- HDF5 compression and chunking optimization
- ADIOS format conversion and tuning
- Parquet layout optimization
- I/O performance analysis

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

from typing import Any, Dict, Optional

import dspy

from clio_agent.signatures.expert_sig import DataExpertSignature


class DataExpert(dspy.Module):
    """Scientific data file optimization expert.

    Currently uses ChainOfThought for all queries. Plan 02 will replace
    the use_tools=True path with ReAct + real MCP tools via FastMCP gateway.

    Attributes:
        agent: DSPy ChainOfThought module
        use_tools: Whether tools mode was requested (reserved for Plan 02)
        arc_memory: Optional ARC memory instance for caching

    Example:
        >>> expert = DataExpert()
        >>> result = expert(
        ...     question="Optimize my 100GB HDF5 file",
        ...     file_context="Float64 climate data, 64 cores available"
        ... )
        >>> print(result.analysis)
        >>> print(result.recommendations)
    """

    def __init__(self, use_tools: bool = True, arc_memory: Optional[Any] = None):
        """Initialize Data Expert.

        Args:
            use_tools: Reserved for Plan 02 (ReAct + MCP tools). Currently uses CoT regardless.
            arc_memory: Optional ARCMemory instance for tool result caching
        """
        super().__init__()
        self.use_tools = use_tools
        self.arc_memory = arc_memory

        # Both paths use ChainOfThought for now.
        # Plan 02 will wire use_tools=True to ReAct with real MCP tools.
        self.agent = dspy.ChainOfThought(DataExpertSignature)

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate data I/O analysis and recommendations.

        Args:
            question: User's question about data files or I/O optimization
            file_context: File information (paths, sizes, formats)

        Returns:
            dspy.Prediction with:
                - analysis: Technical analysis of the problem
                - recommendations: Actionable optimization steps

        Example:
            >>> expert = DataExpert()
            >>> result = expert(
            ...     question="How to optimize 100GB HDF5 file?",
            ...     file_context="Float64 data, 64 cores, need 2x compression"
            ... )
            >>> print(result.analysis)
            >>> print(result.recommendations)
        """
        return self.agent(question=question, file_context=file_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority

        Used by ClioAgent agent to route questions to this expert.
        """
        return {
            "name": "Data Expert",
            "description": (
                "Specializes in scientific data file optimization (HDF5, ADIOS, Parquet), "
                "compression strategies, I/O performance, and format conversion"
            ),
            "keywords": [
                "hdf5", "adios", "parquet", "compression", "chunking",
                "data format", "file optimization", "i/o performance",
                "netcdf", "zarr", "parallel io", "mpi-io",
                "near-data processing", "ndp", "data layout"
            ],
            "priority": 1,
        }
