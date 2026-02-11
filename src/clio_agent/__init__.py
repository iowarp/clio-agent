"""
ClioAgent - Cognitive Layer for Adaptive Universal Data & Intelligent Operations

ClioAgent Agent Framework (IOWarp Intelligence Layer) for scientific computing.

Core Architecture:
- Declarative Intelligence: Agent signatures without prompt engineering
- ReAct Pattern: Reasoning + Acting with FastMCP tools for data I/O
- Multi-Agent Orchestration: Intelligent routing to domain experts
- UV-Native: Self-contained scripts with inline dependencies
- Local LM Support: Privacy-preserving HPC computing (LM Studio)

Example:
    >>> from clio_agent import ClioAgent, setup_dspy
    >>>
    >>> # Setup LM (LM Studio)
    >>> lm = setup_dspy()
    >>>
    >>> # Create ClioAgent agent
    >>> agent = ClioAgent()
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
from clio_agent.agent import ClioAgent
from clio_agent.config import LMStudioConfig, setup_dspy

__all__ = [
    "ClioAgent",
    "setup_dspy",
    "LMStudioConfig",
]
