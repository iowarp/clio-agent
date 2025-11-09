"""
ClaudIO - Cognitive Layer for Adaptive Universal Data & Intelligent Operations

ClaudIO Agent Framework (IOWarp Intelligence Layer) for scientific computing.

Core Architecture:
- Declarative Intelligence: Agent signatures without prompt engineering
- ReAct Pattern: Reasoning + Acting with FastMCP tools for data I/O
- Multi-Agent Orchestration: Intelligent routing to domain experts
- UV-Native: Self-contained scripts with inline dependencies
- Local LM Support: Privacy-preserving HPC computing (LM Studio)

Example:
    >>> from claudio import ClaudIO, setup_dspy
    >>>
    >>> # Setup LM (LM Studio)
    >>> lm = setup_dspy()
    >>>
    >>> # Create ClaudIO agent
    >>> agent = ClaudIO()
    >>>
    >>> # Ask data I/O questions
    >>> result = agent(question="How do I optimize my HDF5 file?")
    >>>
    >>> # Inspect results
    >>> print(f"Expert used: {result.selected_expert}")  # "data"
    >>> print(f"Answer: {result.answer}")  # Expert's answer (via ReAct)
"""

__version__ = "0.2.0"
__author__ = "IOWarp Team"

# Core imports
from claudio.config import setup_dspy, LMStudioConfig
from claudio.claudio import ClaudIO

__all__ = [
    "ClaudIO",
    "setup_dspy",
    "LMStudioConfig",
]
