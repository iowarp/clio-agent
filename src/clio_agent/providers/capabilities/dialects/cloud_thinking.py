"""anthropic/openai ``ThinkingSpec`` sourcing (model-capabilities brief Part 7 follow-up).

Both are real cloud APIs LiteLLM already translates end-to-end, so their
per-model thinking/reasoning support is read straight out of LiteLLM's OWN
introspection -- a pure, network-free lookup (no live probe; LiteLLM ships its
model-cost/capability map locally). This is "dialect knowledge" in the ground
rules' sense: a fact about what the PROVIDER's transport (as LiteLLM already
understands it) supports, not a per-model quirk invented here.

Moved out of the deleted ``providers/reasoning_levels.py``
(``_anthropic_effort_levels``/``_openai_effort_levels``/``_litellm_info``) with
the same logic, now feeding a real
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec` instead of a
catalog-display level list -- the SAME source, one fewer consumer-specific
re-derivation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, ThinkingSpec, unknown
from clio_agent.providers.thinking_levels import LEVEL_ORDER

logger = logging.getLogger(__name__)

_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ordered(levels: Any) -> tuple[str, ...]:
    values = {str(v) for v in levels} if isinstance(levels, (list, tuple, set)) else set()
    return tuple(level for level in LEVEL_ORDER if level in values)


def _litellm_info(model: str) -> dict[str, Any]:
    """The fetched LiteLLM community map's raw entry for ``model``, or ``{}``.

    Reads through :mod:`clio_agent.providers.handshake.sources.litellm_catalog`
    -- the SAME source the live model map uses -- instead of calling
    ``litellm.get_model_info()`` directly. ``allow_fetch=False``: this runs on
    a handshake/discovery path and must never block on a live fetch; it reads
    the disk cache from an earlier successful fetch only.
    """

    from clio_agent.providers.handshake.sources.litellm_catalog import (  # noqa: PLC0415
        lookup_litellm_info,
    )

    matched = lookup_litellm_info(model, allow_fetch=False)
    if matched is not None and matched[1]:
        return matched[1]
    logger.debug("cloud_thinking: no litellm model info for %r", model)
    return {}


def anthropic_effort_levels(model: str) -> tuple[str, ...] | None:
    """Effort levels LiteLLM will send as ``output_config.effort`` for ``model``."""

    try:
        from litellm.llms.anthropic.chat.transformation import (  # noqa: PLC0415
            AnthropicConfig,
        )

        if not AnthropicConfig._is_adaptive_thinking_model(model, "anthropic"):
            return None
        levels = ["low", "medium", "high", "max"]
        if AnthropicConfig._supports_effort_level(model, "xhigh", "anthropic"):
            levels.append("xhigh")
    except Exception as exc:  # noqa: BLE001 - no LiteLLM evidence means budget ladder, logged
        logger.debug("cloud_thinking: no anthropic effort evidence for %r: %s", model, exc)
        return None
    return _ordered(levels)


def openai_effort_levels(model: str) -> tuple[str, ...] | None:
    info = _litellm_info(model)
    if not info.get("supports_reasoning"):
        return None
    levels = list(_EFFORT_LADDER)
    for level, flag in (("minimal", "minimal"), ("xhigh", "xhigh"), ("off", "none")):
        if info.get(f"supports_{flag}_reasoning_effort"):
            levels.append(level)
    return _ordered(levels)


def build_thinking_spec_anthropic(model_id: str) -> Fact[ThinkingSpec]:
    """The anthropic ``ThinkingSpec`` fact for one model (effort levels, else budget)."""

    observed_at = _now_iso()
    effort = anthropic_effort_levels(model_id)
    if effort:
        return Fact(
            ThinkingSpec(mechanism="effort_levels", levels=effort, effort_by_level={}),
            "litellm",
            observed_at,
            "litellm AnthropicConfig adaptive-thinking effort levels",
        )
    # Every Anthropic model accepts a thinking token budget (a transport-wide
    # fact, not a per-model guess -- extended thinking is a platform feature).
    # No model-specific ceiling is known, so budget_range stays None and the
    # request builder falls back to CLIO's own generic level->budget ladder
    # (providers.thinking_levels.LEVEL_BUDGET) rather than an invented range.
    return Fact(
        ThinkingSpec(mechanism="budget_tokens"),
        "litellm",
        observed_at,
        "anthropic API: thinking token budget (no adaptive-effort evidence)",
    )


def build_thinking_spec_openai(model_id: str) -> Fact[ThinkingSpec]:
    """The openai ``ThinkingSpec`` fact for one model, or unknown with no evidence."""

    observed_at = _now_iso()
    effort = openai_effort_levels(model_id)
    if not effort:
        return unknown("litellm: no supports_reasoning evidence for this model")
    return Fact(
        ThinkingSpec(mechanism="effort_levels", levels=effort, effort_by_level={}),
        "litellm",
        observed_at,
        "litellm model map supports_reasoning / supports_*_reasoning_effort",
    )


def local_thinking_spec(dialect: str, model_id: str) -> Fact[ThinkingSpec]:
    """The model's ``ThinkingSpec`` fact for a cloud dialect with a per-model story.

    Only anthropic and openai have one here (pure, network-free LiteLLM
    introspection); every other dialect answers unknown rather than a guess.
    Shared by the openai-compatible handshake and the request builder's
    local-first resolution, so both read the same fact.
    """
    if dialect == "anthropic":
        return build_thinking_spec_anthropic(model_id)
    if dialect == "openai":
        return build_thinking_spec_openai(model_id)
    return unknown("no per-model thinking source for this dialect")


__all__ = [
    "anthropic_effort_levels",
    "build_thinking_spec_anthropic",
    "build_thinking_spec_openai",
    "local_thinking_spec",
    "openai_effort_levels",
]
