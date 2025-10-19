#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
# ]
# ///

"""
ClaudIO Expert Signatures

Defines DSPy signatures for all domain experts.
Each signature specifies the input/output interface for expert reasoning.

Available Signatures:
- DataExpertSignature: Scientific data file optimization
- HPCExpertSignature: HPC cluster and performance optimization
- AnalysisExpertSignature: Data analysis and visualization
- ResearchExpertSignature: Scientific literature and citations
- WorkflowExpertSignature: Automation and pipeline orchestration
"""

import dspy
import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent.parent  # src/claudio/signatures/file.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))


# ============================================================================
# DATA EXPERT SIGNATURE
# ============================================================================

class DataExpertSignature(dspy.Signature):
    """Scientific data I/O expert for HDF5, ADIOS, Parquet optimization.

    Provides analysis and actionable recommendations for:
    - File format optimization (compression, chunking)
    - I/O performance tuning
    - Parallel HDF5/ADIOS configuration
    - Data layout strategies
    """

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about scientific data files, formats, or I/O optimization"
    )
    file_context: str = dspy.InputField(
        desc="File information: paths, sizes, formats, access patterns, cluster configuration",
        default=""
    )

    # Output fields - structured like POC
    analysis: str = dspy.OutputField(
        desc="Technical analysis of the data I/O problem, file characteristics, and bottlenecks"
    )
    recommendations: str = dspy.OutputField(
        desc="Specific actionable recommendations: compression settings, chunking strategies, tools to use"
    )


# ============================================================================
# HPC EXPERT SIGNATURE
# ============================================================================

class HPCExpertSignature(dspy.Signature):
    """HPC cluster optimization expert for SLURM, MPI, and performance tuning."""

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about HPC, SLURM jobs, MPI, or cluster performance"
    )
    cluster_context: str = dspy.InputField(
        desc="Cluster information: job scripts, node counts, performance metrics, error logs",
        default=""
    )

    # Output fields - HPC-specific structure
    diagnosis: str = dspy.OutputField(
        desc="Performance diagnosis: bottlenecks identified, resource utilization analysis"
    )
    solution: str = dspy.OutputField(
        desc="Specific solutions: SLURM configurations, MPI tuning, script improvements"
    )


# ============================================================================
# ANALYSIS EXPERT SIGNATURE
# ============================================================================

class AnalysisExpertSignature(dspy.Signature):
    """Data analysis, visualization, and statistical computing expert."""

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about data analysis, visualization, statistics, or ML"
    )
    data_context: str = dspy.InputField(
        desc="Data information: types, size, variables, analysis goals, constraints",
        default=""
    )

    # Output fields - analysis-specific structure
    approach: str = dspy.OutputField(
        desc="Analysis strategy: methods to use, statistical tests, visualization types"
    )
    code_example: str = dspy.OutputField(
        desc="Python code example using pandas/matplotlib/numpy for the analysis"
    )


# ============================================================================
# RESEARCH EXPERT SIGNATURE
# ============================================================================

class ResearchExpertSignature(dspy.Signature):
    """Scientific literature and research methodology expert."""

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about scientific papers, research, or domain knowledge"
    )
    research_context: str = dspy.InputField(
        desc="Research domain, authors, timeframe, related papers",
        default=""
    )

    # Output fields - research-specific structure
    findings: str = dspy.OutputField(
        desc="Relevant papers, citations, research trends, key findings"
    )
    methodology: str = dspy.OutputField(
        desc="Research methodology recommendations, domain-specific best practices"
    )


# ============================================================================
# WORKFLOW EXPERT SIGNATURE
# ============================================================================

class WorkflowExpertSignature(dspy.Signature):
    """Workflow automation and pipeline orchestration expert."""

    # Input fields
    question: str = dspy.InputField(
        desc="User's question about workflow automation, pipelines, or task orchestration"
    )
    workflow_context: str = dspy.InputField(
        desc="Existing workflows, task dependencies, automation goals, tools in use",
        default=""
    )

    # Output fields - workflow-specific structure
    design: str = dspy.OutputField(
        desc="Workflow design: architecture, task dependencies, execution strategy"
    )
    implementation: str = dspy.OutputField(
        desc="Implementation guide: tools to use, configuration, example scripts"
    )


# ============================================================================
# TEST MAIN
# ============================================================================

if __name__ == "__main__":
    print("ClaudIO Expert Signatures Test")
    print("=" * 60)

    # List all expert signatures
    signatures = [
        ("DataExpert", DataExpertSignature),
        ("HPCExpert", HPCExpertSignature),
        ("AnalysisExpert", AnalysisExpertSignature),
        ("ResearchExpert", ResearchExpertSignature),
        ("WorkflowExpert", WorkflowExpertSignature),
    ]

    for name, sig_class in signatures:
        print(f"\n{name}:")
        print(f"  Docstring: {sig_class.__doc__.split('.')[0]}...")

        print("  Input fields:")
        for field_name in sig_class.input_fields:
            print(f"    - {field_name}")

        print("  Output fields:")
        for field_name in sig_class.output_fields:
            print(f"    - {field_name}")

    print("\n" + "=" * 60)
    print("✅ All expert signatures defined")
    print("\nNext: Use these signatures in expert modules with dspy.ChainOfThought or dspy.ReAct")
