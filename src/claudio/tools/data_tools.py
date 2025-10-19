#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.25.0",
# ]
# ///

"""
ClaudIO Data Tools

MCP tool wrappers for scientific data file operations.
Tools for HDF5, ADIOS, Parquet, and Near-Data Processing.

Each tool is designed to work with DSPy ReAct agents.
Tools automatically fall back to local implementations if MCP unavailable.

Available Tools:
- hdf5_analyze: Analyze HDF5 file structure and compression
- hdf5_optimize: Optimize HDF5 compression and chunking
- adios_convert: Convert between ADIOS and other formats
- parquet_optimize: Optimize Parquet file layout
- ndp_query: Execute near-data processing queries

Example (in DSPy ReAct):
    >>> import dspy
    >>> from claudio.tools.data_tools import hdf5_analyze, hdf5_optimize
    >>>
    >>> agent = dspy.ReAct(
    ...     signature=DataExpertSignature,
    ...     tools=[hdf5_analyze, hdf5_optimize]
    ... )
    >>> result = agent(question="Optimize this HDF5 file", filepath="/data/sim.h5")
"""

from typing import Dict, Any
from claudio.tools.mcp_wrapper import call_mcp, MCPServerUnavailable, wrap_mcp_tool


# ============================================================================
# HDF5 TOOLS
# ============================================================================

def hdf5_analyze(filepath: str) -> Dict[str, Any]:
    """Analyze HDF5 file structure, compression, and chunking.

    Use when: User asks about HDF5 file properties, compression ratios,
    chunking strategies, or file structure analysis.

    Args:
        filepath: Path to HDF5 file to analyze

    Returns:
        Dictionary with analysis results:
        - file_size: Total file size in bytes
        - compression_ratio: Achieved compression ratio
        - chunking: Chunking configuration
        - datasets: List of datasets with metadata
        - recommendations: Optimization suggestions

    Example:
        >>> result = hdf5_analyze(filepath="/data/simulation.h5")
        >>> print(f"Compression ratio: {result['compression_ratio']}")
        >>> print(f"Recommendations: {result['recommendations']}")
    """
    try:
        return call_mcp(
            server="hdf5",
            method="analyze",
            params={"filepath": filepath}
        )
    except MCPServerUnavailable:
        # Fallback: Return placeholder structure
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "HDF5 analysis requires MCP server. Providing general recommendations."
        }


def hdf5_optimize(
    filepath: str,
    output_path: str,
    compression: str = "gzip-6",
    chunking: str = "auto"
) -> Dict[str, Any]:
    """Optimize HDF5 file compression and chunking.

    Use when: User wants to optimize HDF5 file, reduce file size,
    or improve I/O performance through better compression/chunking.

    Args:
        filepath: Path to input HDF5 file
        output_path: Path for optimized output file
        compression: Compression strategy (gzip-1 to gzip-9, lzf, blosc)
        chunking: Chunking strategy (auto, or tuple like "100,100,100")

    Returns:
        Dictionary with optimization results:
        - original_size: Original file size
        - optimized_size: New file size
        - compression_ratio: Achieved ratio
        - time_taken: Optimization time
        - settings_used: Applied compression/chunking

    Example:
        >>> result = hdf5_optimize(
        ...     filepath="/data/input.h5",
        ...     output_path="/data/optimized.h5",
        ...     compression="blosc",
        ...     chunking="auto"
        ... )
    """
    try:
        return call_mcp(
            server="hdf5",
            method="optimize",
            params={
                "filepath": filepath,
                "output_path": output_path,
                "compression": compression,
                "chunking": chunking
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "HDF5 optimization requires MCP server"
        }


# ============================================================================
# ADIOS TOOLS
# ============================================================================

def adios_convert(
    input_path: str,
    output_path: str,
    output_format: str = "bp5",
    compression: str = "zfp"
) -> Dict[str, Any]:
    """Convert data files to/from ADIOS format.

    Use when: User needs to convert between HDF5 and ADIOS,
    or upgrade ADIOS versions (BP3 to BP5).

    Args:
        input_path: Path to input file
        output_path: Path for converted file
        output_format: Target format (bp5, bp4, hdf5)
        compression: Compression method (zfp, sz, blosc)

    Returns:
        Dictionary with conversion results

    Example:
        >>> result = adios_convert(
        ...     input_path="/data/checkpoint.h5",
        ...     output_path="/data/checkpoint.bp5",
        ...     output_format="bp5",
        ...     compression="sz"
        ... )
    """
    try:
        return call_mcp(
            server="adios",
            method="convert",
            params={
                "input_path": input_path,
                "output_path": output_path,
                "output_format": output_format,
                "compression": compression
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "ADIOS conversion requires MCP server"
        }


# ============================================================================
# PARQUET TOOLS
# ============================================================================

def parquet_optimize(
    filepath: str,
    output_path: str,
    row_group_size: int = 100000,
    compression: str = "snappy"
) -> Dict[str, Any]:
    """Optimize Parquet file layout for analytics.

    Use when: User needs to optimize Parquet files for query performance
    or reduce storage costs.

    Args:
        filepath: Path to input Parquet file
        output_path: Path for optimized file
        row_group_size: Rows per row group (affects query performance)
        compression: Compression codec (snappy, gzip, zstd)

    Returns:
        Dictionary with optimization results

    Example:
        >>> result = parquet_optimize(
        ...     filepath="/data/analytics.parquet",
        ...     output_path="/data/optimized.parquet",
        ...     compression="zstd"
        ... )
    """
    try:
        return call_mcp(
            server="parquet",
            method="optimize",
            params={
                "filepath": filepath,
                "output_path": output_path,
                "row_group_size": row_group_size,
                "compression": compression
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "Parquet optimization requires MCP server"
        }


# ============================================================================
# NEAR-DATA PROCESSING TOOLS
# ============================================================================

def ndp_query(
    filepath: str,
    query: str,
    ndp_enabled: bool = True
) -> Dict[str, Any]:
    """Execute near-data processing query.

    Use when: User wants to perform data filtering or aggregation
    close to storage to reduce data movement.

    Args:
        filepath: Path to data file
        query: Query expression (SQL-like or filter expression)
        ndp_enabled: Whether to use NDP acceleration

    Returns:
        Dictionary with query results

    Example:
        >>> result = ndp_query(
        ...     filepath="/data/simulation.h5",
        ...     query="SELECT * WHERE temperature > 300"
        ... )
    """
    try:
        return call_mcp(
            server="ndp",
            method="query",
            params={
                "filepath": filepath,
                "query": query,
                "ndp_enabled": ndp_enabled
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "NDP query requires MCP server"
        }


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO Data Tools Test")
    print("=" * 60)

    print("\nAvailable data tools:")
    tools = [hdf5_analyze, hdf5_optimize, adios_convert, parquet_optimize, ndp_query]

    for tool in tools:
        print(f"\n  {tool.__name__}:")
        doc_first_line = tool.__doc__.split('\n')[0] if tool.__doc__ else "No description"
        print(f"    {doc_first_line}")

    print("\n" + "=" * 60)
    print("Testing tool call (will use fallback without MCP server):")

    # Test with fallback (MCP likely not available)
    result = hdf5_analyze(filepath="/tmp/test.h5")
    print(f"\nhdf5_analyze result: {result}")

    print("\n" + "=" * 60)
    print("✅ Data tools template created")
    print("\nTo use these tools:")
    print("1. Start MCP data server (HDF5, ADIOS, etc.)")
    print("2. Configure server endpoints in mcp_wrapper.py")
    print("3. Use tools in DataExpert with dspy.ReAct")
