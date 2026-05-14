"""
ClioAgent - Cognitive Layer for Adaptive Universal Data & Intelligent Operations

ClioAgent Agent Framework (IOWarp Intelligence Layer) for scientific computing.

Core Architecture:
- Declarative Intelligence: Agent signatures without prompt engineering
- CLIO Harness: validated routing, explicit tool traces, ARC-backed memory
- FastMCP Tool Boundary: scientific tools exposed through the MCP gateway
- Expert Orchestration: deterministic scientific paths with DSPy reasoning where useful
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
    >>> print(f"Answer: {result.answer}")
"""

__version__ = "0.5.0"
__author__ = "IOWarp Team"

__all__ = [
    "ClioAgent",
    "setup_dspy",
    "LMStudioConfig",
]

# PEP 562 lazy attribute access. ``from clio_agent import ClioAgent``
# still works, but plain ``import clio_agent`` (or anything that just
# touches a submodule like ``clio_agent.gact.app``) no longer drags in
# DSPy + every expert + ARC at import time. The boot-time win matters
# for ``clio-agent-gact``: gact-tui's ``agent deploy`` probe only waits
# 3 s for /v1/capabilities, and an eager import here ate the budget.
def __getattr__(name: str):
    if name == "ClioAgent":
        from clio_agent.agent import ClioAgent  # noqa: PLC0415

        return ClioAgent
    if name == "setup_dspy":
        from clio_agent.config import setup_dspy  # noqa: PLC0415

        return setup_dspy
    if name == "LMStudioConfig":
        from clio_agent.config import LMStudioConfig  # noqa: PLC0415

        return LMStudioConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
