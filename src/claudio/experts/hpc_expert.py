#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO HPC Expert Module

Specializes in HPC cluster optimization, SLURM job management, and MPI performance.

Key Capabilities:
- SLURM job script generation and debugging
- MPI performance optimization
- I/O performance analysis via Darshan
- Resource allocation strategies
- Cluster utilization optimization

MCP Tools (to be implemented):
- slurm_analyze: Analyze SLURM job configurations
- slurm_optimize: Optimize SLURM resource allocation
- darshan_report: Parse Darshan I/O logs
- darshan_analyze: Identify I/O bottlenecks
- mpi_profiling: MPI performance insights
"""

import dspy
from typing import Dict, Any
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/experts/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from claudio.signatures.expert_sig import HPCExpertSignature


class HPCExpert(dspy.Module):
    """HPC cluster and performance optimization expert."""

    def __init__(self, use_tools: bool = False):
        """Initialize HPC Expert.

        Args:
            use_tools: If True, use ReAct with MCP tools (not yet implemented)
        """
        super().__init__()
        # TODO: Upgrade to ReAct when tools are ready
        self.generate = dspy.ChainOfThought(HPCExpertSignature)

    def forward(self, question: str, cluster_context: str = "") -> dspy.Prediction:
        """Generate HPC performance diagnosis and solutions.

        Args:
            question: User's question about HPC, SLURM, MPI
            cluster_context: Job scripts, node counts, performance metrics

        Returns:
            dspy.Prediction with:
                - diagnosis: Performance bottleneck analysis
                - solution: Specific SLURM/MPI optimizations
        """
        return self.generate(question=question, cluster_context=cluster_context)

    @staticmethod
    def get_capabilities() -> Dict[str, Any]:
        """Return expert capabilities for orchestrator routing."""
        return {
            "name": "HPC Expert",
            "description": (
                "Specializes in HPC cluster optimization, SLURM job management, "
                "MPI performance tuning, and I/O performance analysis"
            ),
            "keywords": [
                "slurm", "mpi", "hpc", "cluster", "parallel", "darshan",
                "job script", "sbatch", "performance", "scaling",
                "mpi-io", "collective", "load balance", "node", "core",
                "walltime", "queue", "partition", "allocation"
            ],
            "priority": 1,
        }


if __name__ == "__main__":
    print("ClaudIO HPC Expert Test")
    print("=" * 60)

    from claudio.config import setup_dspy

    try:
        lm = setup_dspy(use_lm_studio=True)
        expert = HPCExpert()

        test_question = "My SLURM job is running slow, how do I debug it?"
        test_context = "Using 256 nodes, 64 cores each, job takes 10 hours instead of expected 2"

        print(f"\nQuestion: {test_question}")
        print(f"Context: {test_context}")
        print("-" * 60)

        result = expert(question=test_question, context=test_context)
        print(f"Answer: {result.answer[:400]}...")

        print("\n✅ HPC Expert working!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
