"""
ClaudIO - Cognitive Layer for Adaptive Universal Data & Intelligent Operations

DSPy-powered system for scientific data I/O optimization.

Core Architecture:
- Programming Over Prompting: DSPy signatures instead of hand-crafted prompts
- ReAct Agent: Reasoning + Acting with MCP tools for data I/O
- Single Expert Focus: DataExpert for HDF5, ADIOS, Parquet optimization
- UV-Native: Self-contained scripts with inline dependencies
- LM Studio Provider: Local LLM support

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

__version__ = "0.1.0"
__author__ = "IOWarp Team"

# Core imports
from claudio.config import setup_dspy, LMStudioConfig
from claudio.claudio import ClaudIO

__all__ = [
    "ClaudIO",
    "setup_dspy",
    "LMStudioConfig",
]
