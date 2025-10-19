"""
ClaudIO Signatures Module

DSPy signature definitions for all ClaudIO components.

Signatures define the input/output interface for DSPy modules without
hand-crafted prompts. Each signature specifies:
- Input fields: What information the module receives
- Output fields: What the module should produce
- Field descriptions: Guide DSPy's automatic prompt generation

Available Signatures:
- OrchestratorSignature: Routes questions to experts
- DataExpertSignature: HDF5, ADIOS, Parquet optimization
- HPCExpertSignature: SLURM, MPI, performance tuning
- AnalysisExpertSignature: Visualization, statistics, ML
- ResearchExpertSignature: Paper search, citations
- WorkflowExpertSignature: Automation, pipeline orchestration

Example:
    >>> import dspy
    >>> from claudio.signatures import DataExpertSignature
    >>>
    >>> # Create predictor from signature
    >>> predictor = dspy.ChainOfThought(DataExpertSignature)
    >>>
    >>> # Use it (DSPy generates prompts automatically)
    >>> result = predictor(
    ...     question="How to optimize HDF5?",
    ...     context="100GB file, 64 cores"
    ... )
"""

from claudio.signatures.orchestrator_sig import OrchestratorSignature
from claudio.signatures.expert_sig import (
    DataExpertSignature,
    HPCExpertSignature,
    AnalysisExpertSignature,
    ResearchExpertSignature,
    WorkflowExpertSignature,
)

__all__ = [
    "OrchestratorSignature",
    "DataExpertSignature",
    "HPCExpertSignature",
    "AnalysisExpertSignature",
    "ResearchExpertSignature",
    "WorkflowExpertSignature",
]
