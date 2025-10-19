"""
ClaudIO - Cognitive Layer for Adaptive Universal Data & Intelligent Operations

DSPy-powered multi-agent system for scientific computing.

Core Architecture:
- Programming Over Prompting: DSPy signatures instead of hand-crafted prompts
- ReAct Agents: Reasoning + Acting with MCP tools
- Multi-Agent Coordination: Orchestrator routes to domain experts
- UV-Native: Self-contained scripts with inline dependencies
- Provider-Agnostic: LM Studio, Ollama, OpenAI support

Example:
    >>> from claudio import ClaudIOOrchestrator, setup_dspy
    >>>
    >>> # Setup LM (default: LM Studio)
    >>> lm = setup_dspy()
    >>>
    >>> # Create multi-agent orchestrator
    >>> orchestrator = ClaudIOOrchestrator()
    >>>
    >>> # Ask scientific computing questions
    >>> result = orchestrator(question="How do I optimize my HDF5 file?")
    >>>
    >>> # Inspect results
    >>> print(f"Expert used: {result.selected_expert}")  # "data"
    >>> print(f"Routing: {result.routing_reasoning}")  # ChainOfThought reasoning
    >>> print(f"Answer: {result.answer}")  # Expert's answer (via ReAct)
"""

__version__ = "0.1.0"
__author__ = "IOWarp Team"

# Core imports
from claudio.config import setup_dspy, LMStudioConfig, OllamaConfig, OpenAIConfig
from claudio.orchestrator import ClaudIOOrchestrator

__all__ = [
    "ClaudIOOrchestrator",
    "setup_dspy",
    "LMStudioConfig",
    "OllamaConfig",
    "OpenAIConfig",
]
