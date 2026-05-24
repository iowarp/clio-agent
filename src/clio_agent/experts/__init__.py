"""
ClioAgent Experts Module

Domain-specific expert modules for scientific computing tasks.
Each expert is a DSPy module specialized for a particular domain.

Production Experts:
- DataExpert: HDF5, ADIOS, Parquet optimization (file format level)
- AnalysisExpert: Statistical analysis, data profiling (data content level)
- VisualizationExpert: Charts, plots, visual data summaries

Usage:
    >>> from clio_agent.experts import DataExpert, AnalysisExpert, VisualizationExpert
    >>> from clio_agent.experts import get_all_experts, get_expert_capabilities
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
    >>> visualization_expert = experts["visualization"]
"""

from typing import Any, Dict

import dspy

from clio_agent.experts.analysis_expert import AnalysisExpert
from clio_agent.experts.data_expert import DataExpert
from clio_agent.experts.ndp_expert import NDPExpert
from clio_agent.experts.sac_format_expert import SACFormatExpert
from clio_agent.experts.visualization_expert import VisualizationExpert

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
        >>> result = experts["visualization"](question="Plot distribution?", file_context="")
    """
    return {
        "data": DataExpert(),
        "ndp_catalog": NDPExpert(),
        "analysis": AnalysisExpert(),
        "sac_format": SACFormatExpert(),
        "visualization": VisualizationExpert(),
    }


def get_expert_capabilities() -> Dict[str, Dict[str, Any]]:
    """Get capabilities metadata for all experts.

    Returns:
        Dictionary mapping expert IDs to capability metadata

    Example:
        >>> caps = get_expert_capabilities()
        >>> print(caps["data"]["description"])
        >>> print(caps["analysis"]["keywords"])
        >>> print(caps["visualization"]["keywords"])
    """
    return {
        "data": DataExpert.get_capabilities(),
        "ndp_catalog": NDPExpert.get_capabilities(),
        "analysis": AnalysisExpert.get_capabilities(),
        "sac_format": SACFormatExpert.get_capabilities(),
        "visualization": VisualizationExpert.get_capabilities(),
    }


__all__ = [
    "DataExpert",
    "NDPExpert",
    "AnalysisExpert",
    "SACFormatExpert",
    "VisualizationExpert",
    "get_all_experts",
    "get_expert_capabilities",
]
