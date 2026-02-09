#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dspy-ai>=3.0.3",
#   "fastmcp>=2.13.0",
# ]
# ///

"""
ClioAgent Data Expert Module

Specializes in scientific data file optimization (HDF5, ADIOS, Parquet).
Uses ReAct pattern with IOWarp MCP tools for autonomous tool-augmented reasoning.

Identity:
    I am the CLIO Data Expert, a specialized agent within the CLIO Framework.
    I focus on data I/O, compression, and format optimization.

Key Capabilities:
- HDF5 compression and chunking optimization
- ADIOS format conversion and tuning
- Parquet layout optimization
- SLURM job submission and monitoring
- Darshan I/O trace analysis
- Near-data processing (NDP) strategies
- I/O performance analysis

IOWarp MCP Tools (via external servers):
- HDF5: analyze_hdf5, optimize_chunks, check_compression
- ADIOS: analyze_bp_file
- Parquet: analyze_parquet, optimize_parquet
- SLURM: submit_job, check_job_status
- Darshan: analyze_log, get_io_summary

Tool results are automatically cached via ARC memory (1-hour TTL).

Example:
    >>> from clio_agent.experts import DataExpert
    >>> from clio_agent.config import setup_dspy
    >>> from clio_agent.arc import ARCMemory
    >>>
    >>> lm = setup_dspy(use_lm_studio=True)
    >>> arc = ARCMemory()
    >>> expert = DataExpert(use_tools=True, arc_memory=arc)
    >>>
    >>> result = expert(
    ...     question="How do I optimize HDF5 compression for my 100GB simulation output?",
    ...     file_context="Using parallel HDF5 on 64 cores, mostly float64 data"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)
"""

import dspy
from typing import Dict, Any, Optional
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/clio_agent/experts/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

# Import signature
from clio_agent.signatures.expert_sig import DataExpertSignature

# Import IOWarp MCP connector
from clio_agent.tools.mcp_connector import IOWarpMCPTools, create_iowarp_tool_function

# ============================================================================
# MOCK TOOLS (Placeholders until FastMCP servers ready)
# ============================================================================

def hdf5_analyze(filepath: str) -> dict:
    """Analyze HDF5 file structure and compression.

    Args:
        filepath: Path to HDF5 file

    Returns:
        Analysis dict with compression_ratio, chunking, datasets
    """
    # Mock implementation - will be replaced with real MCP call
    return {
        "filepath": filepath,
        "compression": "gzip-6",
        "compression_ratio": 2.3,
        "chunking": "auto",
        "datasets": ["temperature", "pressure"],
        "size_gb": 45,
        "recommendations": [
            "Current compression is good",
            "Consider blosc for better parallel performance"
        ]
    }


def hdf5_optimize(filepath: str, compression: str = "gzip-6", chunking: str = "auto") -> dict:
    """Optimize HDF5 file compression and chunking.

    Args:
        filepath: Path to HDF5 file
        compression: Compression algorithm (gzip-6, blosc, lzf)
        chunking: Chunking strategy (auto or tuple like "100,100,100")

    Returns:
        Optimization results
    """
    # Mock implementation
    return {
        "original_size_gb": 100,
        "optimized_size_gb": 45,
        "compression_ratio": 2.2,
        "settings": {"compression": compression, "chunking": chunking},
        "time_seconds": 120
    }


# ============================================================================
# DATA EXPERT MODULE (ReAct Pattern)
# ============================================================================

class DataExpert(dspy.Module):
    """Scientific data file optimization expert using ReAct pattern with IOWarp MCP tools.

    Uses ReAct (Reasoning + Acting) pattern to:
    1. Reason about the data optimization problem
    2. Call appropriate IOWarp MCP tools (HDF5, ADIOS, Parquet, SLURM, Darshan)
    3. Observe tool results (cached via ARC with 1-hour TTL)
    4. Iterate until solution found

    Attributes:
        agent: DSPy ReAct module with IOWarp MCP tools
        mcp_tools: IOWarpMCPTools connector instance
        tools: List of DSPy-compatible tool functions

    IOWarp MCP Tools Available:
        - HDF5: analyze_hdf5, optimize_chunks, check_compression
        - ADIOS: analyze_bp_file
        - Parquet: analyze_parquet, optimize_parquet
        - SLURM: submit_job, check_job_status
        - Darshan: analyze_log, get_io_summary

    Example:
        >>> from clio_agent.arc import ARCMemory
        >>> arc = ARCMemory()
        >>> expert = DataExpert(use_tools=True, arc_memory=arc)
        >>> result = expert(
        ...     question="Optimize my 100GB HDF5 file",
        ...     file_context="Float64 climate data, 64 cores available"
        ... )
        >>> print(result.analysis)  # Technical analysis
        >>> print(result.recommendations)  # Actionable steps
    """

    def __init__(self, use_tools: bool = True, arc_memory: Optional[Any] = None):
        """Initialize Data Expert with ReAct.

        Args:
            use_tools: If True, use ReAct with IOWarp MCP tools. If False, use ChainOfThought.
            arc_memory: Optional ARCMemory instance for tool result caching
        """
        super().__init__()
        self.use_tools = use_tools
        self.arc_memory = arc_memory
        self.mcp_connector = None

        if use_tools:
            # Create ONE shared MCP connector for all tools
            from clio_agent.tools.mcp_connector import IOWarpMCPConnector
            self.mcp_connector = IOWarpMCPConnector(arc_memory=arc_memory)

            # Create tool functions for DSPy ReAct
            # Pass shared connector to all tools to avoid thread proliferation
            self.tools = [
                # HDF5 tools
                create_iowarp_tool_function("hdf5", "analyze_hdf5", self.mcp_connector),
                create_iowarp_tool_function("hdf5", "optimize_chunks", self.mcp_connector),
                create_iowarp_tool_function("hdf5", "check_compression", self.mcp_connector),

                # ADIOS tools
                create_iowarp_tool_function("adios", "analyze_bp_file", self.mcp_connector),

                # Parquet tools
                create_iowarp_tool_function("parquet", "analyze_parquet", self.mcp_connector),
                create_iowarp_tool_function("parquet", "optimize_parquet", self.mcp_connector),

                # SLURM tools
                create_iowarp_tool_function("slurm", "submit_job", self.mcp_connector),
                create_iowarp_tool_function("slurm", "check_job_status", self.mcp_connector),

                # Darshan tools
                create_iowarp_tool_function("darshan", "analyze_log", self.mcp_connector),
                create_iowarp_tool_function("darshan", "get_io_summary", self.mcp_connector),

                # Keep legacy mock tools as fallback
                hdf5_analyze,
                hdf5_optimize,
            ]

            # ReAct: Reasoning + Acting with IOWarp MCP tools
            self.agent = dspy.ReAct(
                DataExpertSignature,
                tools=self.tools,
                max_iters=5  # Max reasoning iterations
            )
        else:
            # Fallback to pure reasoning
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
                - [trajectory]: Tool calls if ReAct mode

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

    def shutdown(self) -> None:
        """Clean up MCP connector resources.

        Closes the shared IOWarpMCPConnector instance if it was initialized.
        Should be called when the expert is no longer needed.

        Example:
            >>> expert = DataExpert(use_tools=True)
            >>> result = expert(question="...", file_context="...")
            >>> expert.shutdown()  # Clean up resources
        """
        if hasattr(self, 'mcp_connector') and self.mcp_connector:
            self.mcp_connector.shutdown()

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
            "priority": 1,  # High priority for data-related questions
        }


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClioAgent Data Expert Test")
    print("=" * 60)

    from clio_agent.config import setup_dspy

    try:
        print("\nInitializing with LM Studio...")
        lm = setup_dspy()

        # Test 1: ChainOfThought mode (pure reasoning)
        print("\n" + "=" * 60)
        print("TEST 1: ChainOfThought Mode (Pure Reasoning)")
        print("=" * 60)

        expert_cot = DataExpert(use_tools=False)

        result = expert_cot(
            question="How should I compress my 100GB HDF5 simulation output?",
            file_context="Float64 data, 64 parallel cores, need 2x compression"
        )

        print(f"\nAnalysis: {result.analysis[:400]}...")
        if hasattr(result, 'recommendations'):
            print(f"\nRecommendations: {result.recommendations[:200]}...")

        # Test 2: ReAct mode (with tool calling)
        print("\n" + "=" * 60)
        print("TEST 2: ReAct Mode (Reasoning + Tools)")
        print("=" * 60)

        expert_react = DataExpert(use_tools=True)

        result = expert_react(
            question="Analyze /data/simulation.h5 and recommend optimizations",
            file_context="File is 100GB, using parallel HDF5"
        )

        print(f"\nAnalysis: {result.analysis[:400]}...")

        if hasattr(result, 'trajectory'):
            print(f"\nReAct Trajectory:")
            print(f"  {result.trajectory}")

        # Clean up resources
        expert_cot.shutdown()
        expert_react.shutdown()

        print("\n" + "=" * 60)
        print("✅ Data Expert working in both modes!")
        print("\nKey Features:")
        print("  • ChainOfThought: Pure reasoning, no tool calls")
        print("  • ReAct: Autonomous tool calling with reasoning")
        print("  • Both work with LM Studio (local, zero-cost)")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("Ensure LM Studio is running at configured URL")
        import traceback
        traceback.print_exc()
