"""Per-model reasoning levels for the provider catalog, derived from provider truth.

The catalog used to advertise only ``reasoning: {supported, parameter}``, and
clients filled the gap with a hard-coded ``off | low | medium | high | xhigh``
list that no model actually matched. This module answers, for one discovered
model, which thinking levels a person can really choose and which is the
model's default, and says where that answer came from:

* **codex** — the Codex SDK catalog reports ``supportedReasoningEfforts`` and
  ``defaultReasoningEffort`` per model (persisted by
  :func:`clio_agent.providers.model_discovery.codex.discover_codex`). Codex
  ``none`` is clio ``off``; ``minimal`` has no clio level and is not offered.
* **claude_code** — clio drives Claude Code's thinking through the SDK's
  budget-based ``thinking`` config (:func:`~clio_agent.providers.thinking.
  resolve_thinking`), which every model Claude Code serves accepts. The SDK also
  defines an ``effort`` knob (low/medium/high/xhigh/max), but clio does not send
  it, so the offered levels are exactly the budget ladder
  ``off | low | medium | high``. The default is clio's shipped per-model default
  (``low`` for haiku/sonnet), else the SDK's own default.
* **anthropic** — LiteLLM's model info says whether the model supports extended
  thinking; when it does, the same budget ladder applies and the API default is
  thinking off.
* **argonne / lm_studio / ollama** — the served model decides. vLLM reports its
  ``reasoning_parser``; gpt-oss models honor ``reasoning_effort`` low/medium/high
  (default medium) and nothing else. Other reasoning parsers think, but ignore
  ``reasoning_effort``, so no level is offered.
* **openai** — LiteLLM's model info (``supports_reasoning`` and the per-effort
  ``supports_xhigh_reasoning_effort`` flag); OpenAI's default effort is medium.

Whatever the source says, a level is offered only if
:func:`~clio_agent.providers.thinking.resolve_thinking` maps it for that provider
(``resolve_thinking`` stays the single level -> kwargs mapping).
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.providers.handshake.model import ModelProfile
from clio_agent.providers.thinking import LEVEL_ORDER, resolve_thinking, shipped_default_level

logger = logging.getLogger(__name__)

#: Codex ``ReasoningEffort`` values -> clio thinking levels (``minimal`` has none).
_CODEX_TO_LEVEL: dict[str, str] = {
    "none": "off",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}

_BUDGET_LADDER: tuple[str, ...] = ("off", "low", "medium", "high")
_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high")


def _is_gpt_oss(profile: ModelProfile) -> bool:
    parser = (profile.reasoning_param or "").lower().replace("-", "_")
    return "gpt_oss" in parser or "gptoss" in parser or "gpt-oss" in profile.id.lower()


def _litellm_info(model: str) -> dict[str, Any]:
    """LiteLLM's offline model info, or ``{}`` (logged) when it has no entry."""

    try:
        import litellm  # noqa: PLC0415

        info = litellm.get_model_info(model)
    except Exception as exc:  # noqa: BLE001 - an unknown model is "no evidence", logged
        logger.debug("reasoning levels: no litellm model info for %r: %s", model, exc)
        return {}
    return dict(info) if isinstance(info, dict) else {}


def _codex(profile: ModelProfile) -> tuple[list[str], str, str]:
    reported = profile.raw.get("supported_reasoning_efforts")
    if not isinstance(reported, list):
        return [], "", "codex_sdk_unreported"
    levels = [_CODEX_TO_LEVEL[e] for e in (str(v) for v in reported) if e in _CODEX_TO_LEVEL]
    default = _CODEX_TO_LEVEL.get(str(profile.raw.get("default_reasoning_effort") or ""), "")
    return levels, default, "codex_sdk"


def _anthropic(profile: ModelProfile) -> tuple[list[str], str, str]:
    info = _litellm_info(f"anthropic/{profile.id}")
    if not info.get("supports_reasoning"):
        return [], "", "litellm_model_info"
    return list(_BUDGET_LADDER), "off", "litellm_model_info"


def _openai(profile: ModelProfile) -> tuple[list[str], str, str]:
    info = _litellm_info(profile.id)
    if not info.get("supports_reasoning"):
        return [], "", "litellm_model_info"
    levels = list(_EFFORT_LADDER)
    if info.get("supports_xhigh_reasoning_effort"):
        levels.append("xhigh")
    return levels, "medium", "litellm_model_info"


def _served(profile: ModelProfile) -> tuple[list[str], str, str]:
    if _is_gpt_oss(profile):
        return list(_EFFORT_LADDER), "medium", "served_model_reasoning_parser"
    return [], "", "served_model_reasoning_parser"


def model_reasoning(provider_kind: str, profile: ModelProfile) -> dict[str, Any]:
    """Return the catalog ``reasoning`` block for one model.

    Returns:
        ``{"supported", "parameter", "levels", "default", "source"}``. ``levels``
        is ascending and only holds levels ``resolve_thinking`` maps for
        ``provider_kind``; empty means there is nothing to choose (clients hide the
        selector). ``default`` is one of ``levels`` or ``""`` (provider default
        with no named level).
    """

    if provider_kind == "codex":
        levels, default, source = _codex(profile)
    elif provider_kind == "claude_code":
        levels = list(_BUDGET_LADDER)
        default = shipped_default_level("claude_code", profile.id, None, 0) or ""
        source = "claude_code_sdk_thinking"
    elif provider_kind == "anthropic":
        levels, default, source = _anthropic(profile)
    elif provider_kind == "openai":
        levels, default, source = _openai(profile)
    elif provider_kind in {"argonne", "lm_studio", "ollama"}:
        levels, default, source = _served(profile)
    else:
        levels, default, source = [], "", "no_thinking_mapping"
    mapped = [
        level
        for level in LEVEL_ORDER
        if level in levels and resolve_thinking(provider_kind, level, 0).supported
    ]
    return {
        "supported": bool(mapped) or profile.is_reasoning,
        "parameter": profile.reasoning_param or "",
        "levels": mapped,
        "default": default if default in mapped else "",
        "source": source,
    }


__all__ = ["model_reasoning"]
