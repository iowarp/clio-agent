"""GitHub Copilot ``ThinkingSpec`` sourcing.

The Copilot runtime's ``list_models()`` reports, per model, whether it takes a
reasoning effort (``capabilities.supports.reasoning_effort``) and which efforts
it accepts (``supported_reasoning_efforts``). That is per-model account truth,
recorded by :mod:`clio_agent.providers.model_discovery.copilot`; this module
only translates the recorded row (no I/O) into a
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec`.

``COPILOT_TO_LEVEL`` is dialect vocabulary (the SDK's own effort spelling,
identical for every Copilot model), never a per-model fact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, ThinkingSpec, unknown

#: Copilot SDK ``reasoning_effort`` values -> CLIO thinking levels.
COPILOT_TO_LEVEL: dict[str, str] = {
    "none": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

#: A CLIO level -> the SDK's own spelling (fills ``ThinkingSpec.effort_by_level``).
LEVEL_TO_COPILOT: dict[str, str] = {level: sdk for sdk, level in COPILOT_TO_LEVEL.items()}

_SOURCE = "copilot list_models supported_reasoning_efforts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_thinking_spec(raw: dict[str, Any]) -> Fact[ThinkingSpec]:
    """Build the ``ThinkingSpec`` fact from one discovered Copilot overlay row.

    ``raw["reasoning_effort_supported"]`` is present only on a row Copilot
    discovery wrote: absent is unknown, and a row that reports no efforts is a
    known ``mechanism="none"`` (nothing is guessed).
    """

    supported = raw.get("reasoning_effort_supported")
    if supported is None:
        return unknown("no copilot list_models evidence (no copilot catalog refresh yet)")
    reported = raw.get("supported_reasoning_efforts")
    if supported is False or not isinstance(reported, list) or not reported:
        return Fact(
            ThinkingSpec(mechanism="none"),
            "server_report",
            _now_iso(),
            "copilot list_models reports no reasoning efforts for this model",
        )
    levels = tuple(
        COPILOT_TO_LEVEL[str(value)] for value in reported if str(value) in COPILOT_TO_LEVEL
    )
    if not levels:
        return unknown(f"copilot list_models reported unmapped efforts: {reported!r}")
    effort_by_level = {level: LEVEL_TO_COPILOT[level] for level in levels}
    return Fact(
        ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level=effort_by_level),
        "server_report",
        _now_iso(),
        _SOURCE,
    )


__all__ = ["COPILOT_TO_LEVEL", "LEVEL_TO_COPILOT", "build_thinking_spec"]
