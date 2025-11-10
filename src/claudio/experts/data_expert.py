#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Data Expert Module

Specializes in scientific data file optimization (HDF5, ADIOS, Parquet).
Uses ReAct pattern with IOWarp MCP tools for autonomous tool-augmented reasoning.

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
    >>> from claudio.experts import DataExpert
    >>> from claudio.config import setup_dspy
    >>> from claudio.arc import ARCMemory
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
_src_root = _current_file.parent.parent.parent  # src/claudio/experts/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

# Import signature
from claudio.signatures.expert_sig import DataExpertSignature

# Import IOWarp MCP connector
from claudio.tools.mcp_connector import IOWarpMCPTools, create_iowarp_tool_function

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
        >>> from claudio.arc import ARCMemory
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

        if use_tools:
            # Initialize IOWarp MCP connector
            self.mcp_tools = IOWarpMCPTools(arc_memory=arc_memory)

            # Create tool functions for DSPy ReAct
            self.tools = [
                # HDF5 tools
                create_iowarp_tool_function("hdf5", "analyze_hdf5", arc_memory),
                create_iowarp_tool_function("hdf5", "optimize_chunks", arc_memory),
                create_iowarp_tool_function("hdf5", "check_compression", arc_memory),

                # ADIOS tools
                create_iowarp_tool_function("adios", "analyze_bp_file", arc_memory),

                # Parquet tools
                create_iowarp_tool_function("parquet", "analyze_parquet", arc_memory),
                create_iowarp_tool_function("parquet", "optimize_parquet", arc_memory),

                # SLURM tools
                create_iowarp_tool_function("slurm", "submit_job", arc_memory),
                create_iowarp_tool_function("slurm", "check_job_status", arc_memory),

                # Darshan tools
                create_iowarp_tool_function("darshan", "analyze_log", arc_memory),
                create_iowarp_tool_function("darshan", "get_io_summary", arc_memory),

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

    def forward(self, question: str, file_context: str = "", history = None) -> dspy.Prediction:
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
        return self.agent(question=question, file_context=file_context, history=history or dspy.History(messages=[]))

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority

        Used by ClaudIO agent to route questions to this expert.
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
    print("ClaudIO Data Expert Test")
    print("=" * 60)

    from claudio.config import setup_dspy

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
            context="Float64 data, 64 parallel cores, need 2x compression"
        )

        print(f"\nAnswer: {result.answer[:400]}...")
        if hasattr(result, 'reasoning'):
            print(f"\nReasoning: {result.reasoning[:200]}...")

        # Test 2: ReAct mode (with tool calling)
        print("\n" + "=" * 60)
        print("TEST 2: ReAct Mode (Reasoning + Tools)")
        print("=" * 60)

        expert_react = DataExpert(use_tools=True)

        result = expert_react(
            question="Analyze /data/simulation.h5 and recommend optimizations",
            context="File is 100GB, using parallel HDF5"
        )

        print(f"\nAnswer: {result.answer[:400]}...")

        if hasattr(result, 'trajectory'):
            print(f"\nReAct Trajectory:")
            print(f"  {result.trajectory}")

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
