"""
ClioAgent Experts Module

Domain-specific expert modules for scientific computing tasks.
Each expert is a DSPy module specialized for a particular domain.

Production Experts:
- DataExpert: HDF5, ADIOS, Parquet optimization (file format level)
- AnalysisExpert: Statistical analysis, data profiling (data content level)

Usage:
    >>> from clio_agent.experts import DataExpert, AnalysisExpert, get_all_experts
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy(use_lm_studio=True)
    >>> expert = AnalysisExpert()
    >>>
    >>> result = expert(
    ...     question="What are the statistics for the temperature column?",
    ...     file_context="data.parquet, weather sensor data"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)

Expert Registry:
    >>> experts = get_all_experts()
    >>> capabilities = get_expert_capabilities()
    >>>
    >>> # Access by ID
    >>> data_expert = experts["data"]
    >>> analysis_expert = experts["analysis"]
"""

from typing import Any, Dict

import dspy

from clio_agent.experts.analysis_expert import AnalysisExpert
from clio_agent.experts.data_expert import DataExpert

# ============================================================================
# EXPERT REGISTRY
# ============================================================================

def get_all_experts() -> Dict[str, dspy.Module]:
    """Get all available expert instances.

    Returns:
        Dictionary mapping expert IDs to expert instances

    Example:
        >>> experts = get_all_experts()
        >>> result = experts["data"](question="Optimize HDF5?", context="")
        >>> result = experts["analysis"](question="Column stats?", file_context="")
    """
    return {
        "data": DataExpert(),
        "analysis": AnalysisExpert(),
    }


def get_expert_capabilities() -> Dict[str, Dict[str, Any]]:
    """Get capabilities metadata for all experts.

    Returns:
        Dictionary mapping expert IDs to capability metadata

    Example:
        >>> caps = get_expert_capabilities()
        >>> print(caps["data"]["description"])
        >>> print(caps["analysis"]["keywords"])
    """
    return {
        "data": DataExpert.get_capabilities(),
        "analysis": AnalysisExpert.get_capabilities(),
    }


__all__ = [
    "DataExpert",
    "AnalysisExpert",
    "get_all_experts",
    "get_expert_capabilities",
]
