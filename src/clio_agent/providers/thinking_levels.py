"""CLIO's own thinking-level vocabulary (#895), independent of any provider.

This is the ONLY thing that survives ``providers/thinking.py``'s deletion
(follow-up review on the model-capabilities request-builder PR): the external
level names (``off``/``minimal``/``low``/.../``ultra``), their ascending
order, config validation, and the generic level -> token-budget ladder used
when a model's own :class:`~clio_agent.providers.capabilities.records.
ThinkingSpec` reports ``mechanism="budget_tokens"`` with no narrower per-level
schedule of its own. None of this is a fact about any one model or provider --
it is CLIO's OWN vocabulary and a generic default schedule, which is exactly
the kind of "dialect-adjacent" (not model) knowledge the ground rules allow in
code. Per-provider mapping now lives entirely in
:mod:`clio_agent.lm.dialect_wire` (the wire shape) and the
``providers.capabilities.dialects.*`` adapters (sourcing each model's own
``ThinkingSpec`` from real per-model/per-provider data) -- never here.
"""

from __future__ import annotations

from typing import Literal

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

#: Every external level, in ascending order (``None``/unset means "provider default").
LEVEL_ORDER: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: Valid external levels (``None``/unset means "provider default").
THINKING_LEVELS: frozenset[str] = frozenset(LEVEL_ORDER)

#: Canonical token budget ladder for a model whose ``ThinkingSpec`` reports
#: ``budget_tokens`` with no model-specific ``budget_range`` of its own. An
#: explicit ``thinking_budget`` override always wins over this default.
LEVEL_BUDGET: dict[str, int] = {"low": 2048, "medium": 8192, "high": 24576}


def bucket_level(budget: int) -> str:
    """Bucket an explicit token budget into a level name (effort providers)."""

    if budget < 2000:
        return "low"
    if budget < 8000:
        return "medium"
    return "high"


def normalize_level(level: str | None) -> str | None:
    """Return a validated lowercase level, or ``None`` for unset. Raises on junk."""

    if level is None:
        return None
    lvl = str(level).strip().lower()
    if not lvl:
        return None
    if lvl not in THINKING_LEVELS:
        raise ValueError(f"thinking level must be one of {'|'.join(LEVEL_ORDER)} (got {level!r})")
    return lvl


def validate_thinking_level(level: str) -> str:
    """Return a configured ``thinking_level`` normalized, or raise ``ValueError``."""

    normalized = str(level).strip().lower()
    if normalized not in THINKING_LEVELS:
        raise ValueError(f"thinking_level must be {'|'.join(LEVEL_ORDER)} (got {level!r})")
    return normalized


__all__ = [
    "LEVEL_BUDGET",
    "LEVEL_ORDER",
    "THINKING_LEVELS",
    "ThinkingLevel",
    "bucket_level",
    "normalize_level",
    "validate_thinking_level",
]
