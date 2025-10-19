"""
ClaudIO Tools Module

MCP (Model Context Protocol) tool wrappers for scientific computing.
Each tool is wrapped as a Python function with proper error handling
and fallbacks for use in DSPy ReAct agents.

Tool Categories:
- Data Tools: HDF5, ADIOS, Parquet manipulation
- HPC Tools: SLURM, Darshan, MPI utilities
- Analysis Tools: Visualization, statistics

Usage:
    >>> from claudio.tools.data_tools import hdf5_analyze
    >>> from claudio.tools import call_mcp
    >>>
    >>> # Use tool directly
    >>> result = hdf5_analyze(filepath="/path/to/file.h5")
    >>>
    >>> # Or use generic MCP caller
    >>> result = call_mcp(
    ...     server="hdf5",
    ...     method="analyze",
    ...     params={"filepath": "/path/to/file.h5"}
    ... )

Tool Development:
    1. Each tool is a Python function with clear docstring
    2. Docstring includes "Use when:" section for DSPy
    3. Implements error handling and graceful fallbacks
    4. Returns structured data (dict/list)
    5. Can be used in dspy.ReAct(..., tools=[tool1, tool2])
"""

from claudio.tools.mcp_wrapper import call_mcp, MCPError

# Import tool categories
# TODO: Uncomment when tools are implemented
# from claudio.tools.data_tools import (
#     hdf5_analyze,
#     hdf5_optimize,
#     adios_convert,
#     parquet_optimize,
# )
# from claudio.tools.hpc_tools import (
#     slurm_analyze,
#     darshan_report,
# )

__all__ = [
    "call_mcp",
    "MCPError",
    # TODO: Add tool exports
]
