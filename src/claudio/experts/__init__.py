"""
ClaudIO Experts Module

Domain-specific expert modules for scientific computing tasks.
Each expert is a DSPy module specialized for a particular domain.

Production Expert:
- DataExpert: HDF5, ADIOS, Parquet optimization

Usage:
    >>> from claudio.experts import DataExpert, get_all_experts
    >>> from claudio.config import setup_dspy
    >>>
    >>> lm = setup_dspy(use_lm_studio=True)
    >>> expert = DataExpert()
    >>>
    >>> result = expert(
    ...     question="How do I optimize HDF5 compression?",
    ...     file_context="100GB file on 64 cores"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)

Expert Registry:
    >>> experts = get_all_experts()
    >>> capabilities = get_expert_capabilities()
    >>>
    >>> # Access by ID
    >>> data_expert = experts["data"]
    >>> result = data_expert(question="...")
"""

import sys
from pathlib import Path

# Add src to path for UV script execution
_current_file = Path(__file__).resolve()
_src_root = _current_file.parent.parent  # src/claudio/experts/__init__.py -> src/
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

from claudio.experts.data_expert import DataExpert

from typing import Dict, Any
import dspy


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
    """
    return {
        "data": DataExpert(),
    }


def get_expert_capabilities() -> Dict[str, Dict[str, Any]]:
    """Get capabilities metadata for all experts.

    Returns:
        Dictionary mapping expert IDs to capability metadata

    Example:
        >>> caps = get_expert_capabilities()
        >>> print(caps["data"]["description"])
        >>> print(caps["data"]["keywords"])
    """
    return {
        "data": DataExpert.get_capabilities(),
    }


__all__ = [
    "DataExpert",
    "get_all_experts",
    "get_expert_capabilities",
]
