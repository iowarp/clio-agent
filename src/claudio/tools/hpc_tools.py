#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.25.0",
# ]
# ///

"""
ClaudIO HPC Tools

MCP tool wrappers for HPC cluster operations.
Tools for SLURM, Darshan I/O analysis, and MPI profiling.

Available Tools:
- slurm_analyze: Analyze SLURM job scripts and configurations
- slurm_optimize: Optimize resource allocation
- darshan_report: Parse Darshan I/O logs
- darshan_analyze: Identify I/O bottlenecks
- mpi_profiling: MPI performance insights

Example (in DSPy ReAct):
    >>> import dspy
    >>> from claudio.tools.hpc_tools import slurm_analyze, darshan_report
    >>>
    >>> agent = dspy.ReAct(
    ...     signature=HPCExpertSignature,
    ...     tools=[slurm_analyze, darshan_report]
    ... )
"""

from typing import Dict, Any, List
from claudio.tools.mcp_wrapper import call_mcp, MCPServerUnavailable


# ============================================================================
# SLURM TOOLS
# ============================================================================

def slurm_analyze(job_script: str) -> Dict[str, Any]:
    """Analyze SLURM job script for optimization opportunities.

    Use when: User provides a SLURM job script and wants recommendations
    for better resource allocation or configuration.

    Args:
        job_script: Content of SLURM batch script

    Returns:
        Dictionary with analysis:
        - resource_allocation: Nodes, cores, memory analysis
        - potential_issues: Identified problems
        - recommendations: Optimization suggestions
        - estimated_cost: Resource cost estimate

    Example:
        >>> script = '''#!/bin/bash
        ... #SBATCH --nodes=4
        ... #SBATCH --ntasks-per-node=32
        ... srun ./simulation
        ... '''
        >>> result = slurm_analyze(job_script=script)
    """
    try:
        return call_mcp(
            server="slurm",
            method="analyze",
            params={"job_script": job_script}
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "SLURM analysis requires MCP server. Providing general guidance."
        }


def slurm_optimize(
    job_script: str,
    optimization_goal: str = "performance"
) -> Dict[str, Any]:
    """Optimize SLURM job configuration.

    Use when: User wants to optimize SLURM job for performance,
    cost, or queue wait time.

    Args:
        job_script: Content of SLURM batch script
        optimization_goal: "performance", "cost", or "wait_time"

    Returns:
        Dictionary with:
        - optimized_script: Improved SLURM script
        - changes_made: List of modifications
        - expected_improvement: Performance/cost impact

    Example:
        >>> result = slurm_optimize(
        ...     job_script=script,
        ...     optimization_goal="performance"
        ... )
    """
    try:
        return call_mcp(
            server="slurm",
            method="optimize",
            params={
                "job_script": job_script,
                "optimization_goal": optimization_goal
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True
        }


# ============================================================================
# DARSHAN TOOLS
# ============================================================================

def darshan_report(log_file: str) -> Dict[str, Any]:
    """Parse Darshan I/O log and generate report.

    Use when: User has Darshan logs and wants to understand
    I/O performance characteristics.

    Args:
        log_file: Path to Darshan log file

    Returns:
        Dictionary with:
        - total_bytes_read: Total read volume
        - total_bytes_written: Total write volume
        - file_access_patterns: Per-file statistics
        - io_time: Time spent in I/O operations
        - collective_operations: MPI-IO collective stats

    Example:
        >>> result = darshan_report(
        ...     log_file="/logs/simulation_id12345.darshan"
        ... )
    """
    try:
        return call_mcp(
            server="darshan",
            method="report",
            params={"log_file": log_file}
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True,
            "message": "Darshan reporting requires MCP server"
        }


def darshan_analyze(log_file: str) -> Dict[str, Any]:
    """Analyze Darshan logs for I/O bottlenecks.

    Use when: User wants detailed I/O bottleneck analysis
    and optimization recommendations from Darshan logs.

    Args:
        log_file: Path to Darshan log file

    Returns:
        Dictionary with:
        - bottlenecks: Identified I/O bottlenecks
        - recommendations: Optimization strategies
        - collective_efficiency: MPI-IO collective usage
        - filesystem_load: Per-filesystem statistics

    Example:
        >>> result = darshan_analyze(
        ...     log_file="/logs/simulation_id12345.darshan"
        ... )
        >>> for bottleneck in result['bottlenecks']:
        ...     print(f"Issue: {bottleneck['description']}")
        ...     print(f"Fix: {bottleneck['recommendation']}")
    """
    try:
        return call_mcp(
            server="darshan",
            method="analyze",
            params={"log_file": log_file}
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True
        }


# ============================================================================
# MPI TOOLS
# ============================================================================

def mpi_profiling(
    profile_data: str,
    analysis_type: str = "communication"
) -> Dict[str, Any]:
    """Analyze MPI profiling data.

    Use when: User has MPI profiling data and wants to identify
    communication bottlenecks or load imbalance.

    Args:
        profile_data: Path to MPI profile data or JSON string
        analysis_type: "communication", "load_balance", or "collective"

    Returns:
        Dictionary with:
        - hotspots: Communication hotspots
        - load_balance: Load imbalance analysis
        - collective_efficiency: Collective operation efficiency
        - recommendations: Optimization suggestions

    Example:
        >>> result = mpi_profiling(
        ...     profile_data="/profiles/mpi_trace.json",
        ...     analysis_type="communication"
        ... )
    """
    try:
        return call_mcp(
            server="mpi",
            method="profile",
            params={
                "profile_data": profile_data,
                "analysis_type": analysis_type
            }
        )
    except MCPServerUnavailable:
        return {
            "error": "MCP server unavailable",
            "fallback": True
        }


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO HPC Tools Test")
    print("=" * 60)

    print("\nAvailable HPC tools:")
    tools = [
        slurm_analyze, slurm_optimize,
        darshan_report, darshan_analyze,
        mpi_profiling
    ]

    for tool in tools:
        print(f"\n  {tool.__name__}:")
        doc_first_line = tool.__doc__.split('\n')[0] if tool.__doc__ else "No description"
        print(f"    {doc_first_line}")

    print("\n" + "=" * 60)
    print("Testing tool call (will use fallback without MCP server):")

    # Test with fallback
    test_script = """#!/bin/bash
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=32
#SBATCH --time=01:00:00
srun ./simulation
"""
    result = slurm_analyze(job_script=test_script)
    print(f"\nslurm_analyze result: {result}")

    print("\n" + "=" * 60)
    print("✅ HPC tools template created")
    print("\nTo use these tools:")
    print("1. Start MCP HPC server (SLURM, Darshan, MPI)")
    print("2. Configure server endpoints")
    print("3. Use tools in HPCExpert with dspy.ReAct")
