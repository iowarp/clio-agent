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
import sys

from clio_agent.runtime import package_overlay as _package_overlay

# Before anything imports a package CLIO installed outside a sealed desktop runtime.
_package_overlay.activate()

from clio_agent import _dspy_lazy_proxies  # noqa: E402

_dspy_lazy_proxies.install()

__version__ = "0.9.6.5"
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
            # ``machine`` is Python's own non-WMI answer; the Copilot SDK names its
            # runtime from it and refuses an empty one.
            machine = os.environ.get("PROCESSOR_ARCHITEW6432", "") or os.environ.get(
                "PROCESSOR_ARCHITECTURE", ""
            )
            platform._uname_cache = platform.uname_result(  # type: ignore[attr-defined]
                "Windows",
                os.environ.get("COMPUTERNAME", ""),
                "",
                "",
                machine,
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
    except Exception:  # noqa: BLE001 - optional runtime registration; skip on any failure
        return


_avoid_windows_wmi_platform_probe()


def _default_litellm_local_cost_map() -> None:
    """Keep litellm's OWN community model-cost-map fetch off by default.

    ``litellm>=1.100`` fetches its community ``model_prices_and_context_window.json``
    over the network on first ``import litellm`` (with retries and a background
    retry thread) unless ``LITELLM_LOCAL_MODEL_COST_MAP`` is set. clio-agent now
    owns an equivalent, but properly governed, live fetch of that SAME map for its
    own lookups (:mod:`clio_agent.providers.handshake.sources.litellm_catalog`,
    via :mod:`clio_agent.providers.fetched_catalog`: disk cache, TTL, ETag,
    last-good-on-failure, and a typed reason logged for every degradation).
    Leaving litellm's internal fetch enabled would mean two independent,
    uncoordinated network fetches of the same document, with litellm's own
    attempt invisible to clio's trace/log (a silent-fallback violation) and a
    source of import-time network flakiness (every bare ``import litellm`` —
    including in tests that never touch a model — would attempt a live fetch).

    Forcing local-only restores litellm's pre-upgrade behavior (``1.91.3`` had no
    such fetch at all) for litellm's OWN internal consumers of the map (e.g. its
    Anthropic adaptive-thinking capability lookups in
    ``providers.capabilities.dialects.cloud_thinking``), while clio's own
    lookups go through the live, cached, typed-degradation path instead.
    ``setdefault`` so an operator who explicitly wants litellm's live fetch can
    still opt back in.
    """

    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")


_default_litellm_local_cost_map()


def _default_uv_managed_python() -> None:
    """Keep every ``uv`` CLIO starts on macOS and Windows on uv-managed Pythons.

    On macOS uv's interpreter discovery executes ``/usr/bin/python3``; on a Mac without
    the Command Line Tools that is a stub which opens their install dialog. On Windows
    uv otherwise prefers an installed Microsoft Store Python over downloading one, and
    a packaged (MSIX) interpreter cannot run inside protected execution (MXC refuses
    to launch packaged apps). Children inherit this, so ``uv sync``/``uv tool
    install``/``uv run`` use uv-managed Pythons. ``UV_MANAGED_PYTHON`` (not
    ``UV_PYTHON_PREFERENCE``, which uv refuses next to an explicit
    ``--no-managed-python``) still yields to such a flag or an explicit interpreter
    path, and ``setdefault`` keeps an operator's explicit choice.
    """

    if sys.platform in ("darwin", "win32"):
        os.environ.setdefault("UV_MANAGED_PYTHON", "1")


_default_uv_managed_python()


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
