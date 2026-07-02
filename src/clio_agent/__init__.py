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

import os

__version__ = "0.5.16"
__author__ = "IOWarp Team"

__all__ = [
    "ClioAgent",
    "setup_dspy",
]


def _avoid_windows_wmi_platform_probe() -> None:
    """Prevent CLIO-triggered imports from hanging in Python's Windows WMI probe.

    Some dependency import paths call ``platform.system()`` only to branch on
    the OS. On this Windows workstation, Python 3.12's first ``platform.uname``
    call can block inside ``platform._wmi_query`` long enough to break GACT
    startup and provider-save flows. Seeding the private uname cache with the
    OS fields those dependencies need avoids the WMI path while keeping the
    visible platform answer correct for CLIO's process.
    """

    if os.name != "nt":
        return
    try:
        import platform

        if getattr(platform, "_uname_cache", None) is None:
            platform._uname_cache = platform.uname_result(  # type: ignore[attr-defined]
                "Windows",
                os.environ.get("COMPUTERNAME", ""),
                "",
                "",
                "",
            )
        processor = os.environ.get("PROCESSOR_IDENTIFIER", "") or os.environ.get(
            "PROCESSOR_ARCHITECTURE",
            "",
        )
        if hasattr(platform, "_Processor"):
            platform._Processor.get = classmethod(lambda cls: processor)  # type: ignore[attr-defined]
        release = os.environ.get("OS", "") or "Windows"
        version = os.environ.get("PROCESSOR_ARCHITECTURE", "") or ""
        if hasattr(platform, "_platform_cache"):
            platform._platform_cache[(False, False)] = platform._platform(  # type: ignore[attr-defined]
                "Windows",
                release,
                version,
            )
            platform._platform_cache[(True, False)] = platform._platform(  # type: ignore[attr-defined]
                "Windows",
                release,
                version,
            )
            platform._platform_cache[(False, True)] = "Windows"  # type: ignore[attr-defined]
            platform._platform_cache[(True, True)] = "Windows"  # type: ignore[attr-defined]
        if hasattr(platform, "_win32_ver"):
            platform._win32_ver = lambda version="", csd="", ptype="": (  # type: ignore[attr-defined]
                version,
                csd,
                ptype,
                True,
            )
    except Exception:
        return


_avoid_windows_wmi_platform_probe()


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
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
