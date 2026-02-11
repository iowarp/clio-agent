"""
ClioAgent Signatures Module

DSPy signature definitions for ClioAgent components.

Signatures define the input/output interface for DSPy modules without
hand-crafted prompts. Each signature specifies:
- Input fields: What information the module receives
- Output fields: What the module should produce
- Field descriptions: Guide DSPy's automatic prompt generation

Available Signatures:
- MainAgentSignature: Routes questions to experts (preserved for future expansion)
- DataExpertSignature: HDF5, ADIOS, Parquet optimization

Example:
    >>> import dspy
    >>> from clio_agent.signatures import DataExpertSignature
    >>>
    >>> # Create predictor from signature
    >>> predictor = dspy.ChainOfThought(DataExpertSignature)
    >>>
    >>> # Use it (DSPy generates prompts automatically)
    >>> result = predictor(
    ...     question="How to optimize HDF5?",
    ...     file_context="100GB file, 64 cores"
    ... )
"""

from clio_agent.signatures.expert_sig import (
    DataExpertSignature,
)
from clio_agent.signatures.main_agent_sig import MainAgentSignature

__all__ = [
    "MainAgentSignature",
    "DataExpertSignature",
]
