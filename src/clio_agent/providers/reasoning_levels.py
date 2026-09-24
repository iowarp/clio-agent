"""Per-model reasoning levels for the provider catalog, derived from provider truth.

The catalog used to advertise only ``reasoning: {supported, parameter}``, and
clients filled the gap with a hard-coded list no model actually matched. This
module answers, for one discovered model, which thinking levels a person can
really choose, which is the model's default, and where that answer came from.
The same answer feeds :func:`~clio_agent.providers.thinking.resolve_thinking`
(via :func:`model_effort_levels`) when a turn's LM is built, so what the catalog
offers is exactly what is sent:

* **codex** — the Codex SDK catalog reports ``supportedReasoningEfforts`` and
  ``defaultReasoningEffort`` per model (persisted by discovery). Every effort
  the SDK defines has a clio level (``none`` is ``off``).
* **claude_code** — the Claude Code CLI reports each model's
  ``supportedEffortLevels`` in its ``initialize`` response (read once per
  discovery through the Agent SDK, no model turn; persisted on the overlay row).
  A model with effort levels offers ``off`` plus exactly those levels, sent as
  the SDK ``effort`` option. A model without (haiku) offers the thinking-budget
  ladder ``off|low|medium|high``. ``off`` is the SDK's ``thinking: disabled``,
  which every model accepts.
* **anthropic** — LiteLLM's model map: an adaptive-thinking model accepts
  ``output_config.effort`` low/medium/high/max (+``xhigh`` where the map says
  so); other reasoning models take the budget ladder.
* **openai** — LiteLLM's model map: low/medium/high plus ``minimal``/``xhigh``
  where flagged, and ``off`` (``reasoning_effort="none"``) where the model
  supports a ``none`` effort.
* **argonne / lm_studio / ollama** — the served model decides. gpt-oss models
  honor ``reasoning_effort`` low/medium/high (default medium); other reasoning
  parsers think but ignore ``reasoning_effort``, so no level is offered.
"""

from __future__ import annotations

import logging
from typing import Any

from clio_agent.providers.handshake.model import ModelProfile
from clio_agent.providers.thinking import LEVEL_ORDER, resolve_thinking, shipped_default_level

logger = logging.getLogger(__name__)

#: Codex ``ReasoningEffort`` values -> clio thinking levels. ``max``/``ultra``
#: are reported by newer models (#1436) and map onto themselves like ``xhigh``.
_CODEX_TO_LEVEL: dict[str, str] = {
    "none": "off",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}

_BUDGET_LADDER: tuple[str, ...] = ("off", "low", "medium", "high")
_EFFORT_LADDER: tuple[str, ...] = ("low", "medium", "high")


def _ordered(levels: Any) -> tuple[str, ...]:
    values = {str(v) for v in levels} if isinstance(levels, (list, tuple, set)) else set()
    return tuple(level for level in LEVEL_ORDER if level in values)


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


def _anthropic_effort_levels(model: str) -> tuple[str, ...] | None:
    """Effort levels LiteLLM will send as ``output_config.effort`` for ``model``."""

    try:
        from litellm.llms.anthropic.chat.transformation import (  # noqa: PLC0415
            AnthropicConfig,
        )

        if not AnthropicConfig._is_adaptive_thinking_model(model):
            return None
        levels = ["low", "medium", "high", "max"]
        if AnthropicConfig._supports_effort_level(model, "xhigh"):
            levels.append("xhigh")
    except Exception as exc:  # noqa: BLE001 - no LiteLLM evidence means budget ladder, logged
        logger.debug("reasoning levels: no anthropic effort evidence for %r: %s", model, exc)
        return None
    return _ordered(levels)


def _openai_effort_levels(model: str) -> tuple[str, ...] | None:
    info = _litellm_info(model)
    if not info.get("supports_reasoning"):
        return None
    levels = list(_EFFORT_LADDER)
    for level, flag in (("minimal", "minimal"), ("xhigh", "xhigh"), ("off", "none")):
        if info.get(f"supports_{flag}_reasoning_effort"):
            levels.append(level)
    return _ordered(levels)


def _claude_code_row(model: str) -> dict[str, Any] | None:
    """The claude_code overlay row for a configured model id or CLI alias."""

    from clio_agent.providers.model_discovery.overlay import (  # noqa: PLC0415
        OverlayMalformedError,
        read_overlay,
    )

    bare = model.removeprefix("claude_code/").removeprefix("cc-").removesuffix("[1m]")
    try:
        entry = read_overlay().get("claude_code")
    except OverlayMalformedError as exc:
        logger.warning("reasoning levels: reason=overlay_malformed provider=claude_code: %s", exc)
        return None
    rows = entry.get("models") if isinstance(entry, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw_aliases = row.get("cli_values")
        aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []
        if bare == row.get("id") or bare in aliases or model in aliases:
            return row
    return None


def resolve_configured_model_id(provider_kind: str, model: str) -> str:
    """Return the full catalog model id a configured ``model`` resolves to.

    Only claude_code reports CLI aliases today (e.g. ``sonnet`` ->
    ``claude-sonnet-5``); every other provider's configured ``model`` already
    IS its catalog id, so it is returned unchanged. Uses the same overlay row
    lookup the catalog's reasoning levels come from (:func:`_claude_code_row`)
    -- the provider's own resolution, not a hand-typed table -- so a client
    can find a configured alias's catalog row (and its reasoning levels)
    without knowing the mapping itself. Falls back to ``model`` unchanged when
    no overlay row matches (no discovery has run yet, or an unknown alias).
    """

    if provider_kind != "claude_code" or not model:
        return model
    row = _claude_code_row(model)
    resolved = str((row or {}).get("id") or "")
    return resolved or model


def model_effort_levels(
    provider_kind: str, model: str, *, raw: dict[str, Any] | None = None
) -> tuple[str, ...] | None:
    """The model's own reported effort levels (clio vocabulary), or ``None``.

    ``raw`` is a catalog profile row when the caller already has it; otherwise
    the persisted discovery evidence is read. ``None`` means the provider reports
    no per-model effort for this model (the budget / legacy mapping applies).
    """

    if provider_kind == "claude_code":
        row = raw if raw is not None else _claude_code_row(model)
        levels = _ordered((row or {}).get("supported_effort_levels"))
        return levels or None
    if provider_kind == "anthropic":
        return _anthropic_effort_levels(model)
    if provider_kind == "openai":
        return _openai_effort_levels(model)
    return None


def _codex(profile: ModelProfile) -> tuple[list[str], str, str, str]:
    reported = profile.raw.get("supported_reasoning_efforts")
    if not isinstance(reported, list):
        return [], "", "codex_sdk_unreported", ""
    unmapped = [str(v) for v in reported if str(v) not in _CODEX_TO_LEVEL]
    levels = [_CODEX_TO_LEVEL[str(v)] for v in reported if str(v) in _CODEX_TO_LEVEL]
    default = _CODEX_TO_LEVEL.get(str(profile.raw.get("default_reasoning_effort") or ""), "")
    reason = f"codex_effort_unmapped: {', '.join(unmapped)}" if unmapped else ""
    if unmapped:
        logger.warning("reasoning levels: %s (model=%s)", reason, profile.id)
    return levels, default, "codex_sdk", reason


def _claude_code(profile: ModelProfile) -> tuple[list[str], str, str, str]:
    effort = model_effort_levels("claude_code", profile.id, raw=profile.raw)
    shipped = shipped_default_level("claude_code", profile.id, None, 0) or ""
    reason = str(profile.raw.get("effort_evidence_failure") or "")
    if effort:
        # The SDK documents "high" as its default effort.
        default = shipped or ("high" if "high" in effort else "")
        return ["off", *effort], default, "claude_code_sdk", reason
    return list(_BUDGET_LADDER), shipped, "claude_code_sdk_thinking_budget", reason


def _anthropic(profile: ModelProfile) -> tuple[list[str], str, str, str]:
    effort = model_effort_levels("anthropic", profile.id)
    if effort:
        return ["off", *effort], "off", "litellm_model_info_effort", ""
    if not _litellm_info(f"anthropic/{profile.id}").get("supports_reasoning"):
        return [], "", "litellm_model_info", ""
    return list(_BUDGET_LADDER), "off", "litellm_model_info", ""


def _openai(profile: ModelProfile) -> tuple[list[str], str, str, str]:
    effort = model_effort_levels("openai", profile.id)
    if not effort:
        return [], "", "litellm_model_info", ""
    return list(effort), "medium", "litellm_model_info", ""


def _served(profile: ModelProfile) -> tuple[list[str], str, str, str]:
    if _is_gpt_oss(profile):
        return list(_EFFORT_LADDER), "medium", "served_model_reasoning_parser", ""
    return [], "", "served_model_reasoning_parser", ""


def model_reasoning(provider_kind: str, profile: ModelProfile) -> dict[str, Any]:
    """Return the catalog ``reasoning`` block for one model.

    Returns:
        ``{"supported", "parameter", "levels", "default", "source"}`` plus a typed
        ``reason`` when part of the evidence is missing. ``levels`` is ascending
        and holds only levels ``resolve_thinking`` maps for this model; empty
        means there is nothing to choose (clients hide the selector).
    """

    if provider_kind == "codex":
        levels, default, source, reason = _codex(profile)
    elif provider_kind == "claude_code":
        levels, default, source, reason = _claude_code(profile)
    elif provider_kind == "anthropic":
        levels, default, source, reason = _anthropic(profile)
    elif provider_kind == "openai":
        levels, default, source, reason = _openai(profile)
    elif provider_kind in {"argonne", "lm_studio", "ollama"}:
        levels, default, source, reason = _served(profile)
    else:
        levels, default, source, reason = [], "", "no_thinking_mapping", ""
    effort = (
        model_effort_levels(provider_kind, profile.id, raw=profile.raw)
        if provider_kind == "claude_code"
        else model_effort_levels(provider_kind, profile.id)
        if provider_kind in {"anthropic", "openai"}
        else None
    )
    mapped = [
        level
        for level in LEVEL_ORDER
        if level in levels
        and resolve_thinking(provider_kind, level, 0, effort_levels=effort).supported
    ]
    default = default if default in mapped else ""
    shipped = shipped_default_level(provider_kind, profile.id, None, 0) or ""
    block: dict[str, Any] = {
        "supported": bool(mapped) or profile.is_reasoning,
        "parameter": profile.reasoning_param or "",
        "levels": mapped,
        "default": default,
        # Who picks the default: CLIO's shipped per-model default (sonnet/haiku
        # via Claude Code ship "low") or the provider/model itself. Clients must
        # not call a shipped default "the model default".
        "default_source": ("clio_shipped" if default == shipped else "provider") if default else "",
        "source": source,
    }
    if reason:
        block["reason"] = reason
    return block


__all__ = ["model_effort_levels", "model_reasoning", "resolve_configured_model_id"]
