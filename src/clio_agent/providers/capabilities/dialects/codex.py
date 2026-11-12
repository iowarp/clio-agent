"""Codex ``ThinkingSpec`` sourcing (model-capabilities brief Part 7 follow-up).

Codex's own catalog (the backend's account model list, read by
:mod:`clio_agent.providers.model_discovery.codex`) reports the supported and
default reasoning efforts per model, so this is
real per-model account truth, not a guess. This module only translates that
already-discovered data (dict rows, no I/O) into a
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec`; the actual
read lives in ``model_discovery.codex``.

``_CODEX_TO_LEVEL`` is dialect (transport) vocabulary -- Codex's own effort
naming convention, applying identically to every codex model -- never a
per-model fact, so it is allowed here per the ground rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, ThinkingSpec, unknown

#: Codex ``ReasoningEffort`` values -> clio thinking levels. ``max`` is
#: reported by newer models and maps onto itself like ``xhigh``.
#:
#: ``ultra`` is deliberately absent, so it is never offered. The catalog lists
#: it ("Maximum reasoning with automatic task delegation"), but it is a Codex
#: client mode, not a wire effort: the Responses backend refuses it ("Supported
#: values are: 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', and
#: 'max'", live 2026-11-12), and the Codex CLI sends the model's
#: ``multi_agent_reasoning_effort`` (``xhigh`` for gpt-6.1-sol) and delegates
#: to subagents itself. CLIO has no such mode to run behind the name.
CODEX_TO_LEVEL: dict[str, str] = {
    "none": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

#: The inverse mapping (a CLIO level -> Codex's own wire spelling), used to
#: fill ``ThinkingSpec.effort_by_level`` so the request builder never has to
#: know Codex's vocabulary itself.
LEVEL_TO_CODEX: dict[str, str] = {level: codex for codex, level in CODEX_TO_LEVEL.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_thinking_spec(raw: dict[str, Any]) -> Fact[ThinkingSpec]:
    """Build the ``ThinkingSpec`` fact from one discovered/overlay codex row.

    ``raw`` carries ``supported_reasoning_efforts`` (a list of Codex's own
    effort strings) and ``default_reasoning_effort``, attached by
    :mod:`clio_agent.providers.model_discovery.codex` at discovery time and
    forwarded verbatim through the refresh overlay
    (:mod:`clio_agent.providers.handshake.cli_catalog`). Unknown (never a
    guessed level) when the row carries neither field yet (no discovery/
    refresh has ever run for this exact model).
    """

    reported = raw.get("supported_reasoning_efforts")
    if not isinstance(reported, list) or not reported:
        return unknown("no supported_reasoning_efforts evidence (no codex catalog refresh yet)")
    levels = tuple(CODEX_TO_LEVEL[str(value)] for value in reported if str(value) in CODEX_TO_LEVEL)
    if not levels:
        return unknown(f"codex catalog reported unmapped efforts: {reported!r}")
    effort_by_level = {level: LEVEL_TO_CODEX[level] for level in levels}
    return Fact(
        ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level=effort_by_level),
        "server_report",
        _now_iso(),
        "codex model list supported_reasoning_efforts",
    )


__all__ = ["CODEX_TO_LEVEL", "LEVEL_TO_CODEX", "build_thinking_spec"]
