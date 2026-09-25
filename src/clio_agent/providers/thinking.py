"""Provider-generic thinking (extended-reasoning) level → per-provider mapping (#895).

One external vocabulary — ``off | minimal | low | medium | high | xhigh | max`` —
maps to whatever each provider's transport actually understands. The vocabulary
is the union of the levels real providers report (the ChatGPT/Codex backend's
``reasoning.effort``: none/minimal/low/medium/high/xhigh; the Claude Code CLI's
per-model ``supportedEffortLevels``: low/medium/high/xhigh/max); a level is
extended here rather than dropped when a provider reports it. This module is the
SINGLE level → kwargs mapping; the provider catalog offers a model exactly the
levels this mapping supports for that model
(:mod:`clio_agent.providers.reasoning_levels`).

``effort_levels`` is the model's own reported effort levels (clio vocabulary),
when the provider reports them; ``None`` means "no per-model effort evidence".

* **claude_code** (Claude Agent SDK): with ``effort_levels`` (the CLI reports
  ``supportedEffortLevels`` for the model) a level becomes the SDK ``effort``
  option on adaptive thinking — ``{"type":"adaptive","display":"summarized",
  "effort":<level>}`` (``build_sdk_options`` splits ``effort`` into
  ``ClaudeAgentOptions.effort`` → CLI ``--effort``). Without it (haiku reports no
  effort support) a level is a thinking budget:
  ``{"type":"enabled","budget_tokens":N,"display":"summarized"}``. ``off`` is
  ``{"type":"disabled"}`` for every model (CLI ``--thinking disabled``; verified
  live on sonnet 2026-09-23). ``display`` is load-bearing: claude CLI >= 2.1.x
  defaults the thinking display to ``omitted``, so without it no CoT text
  reaches clio.
* **anthropic** (native LiteLLM): with ``effort_levels`` (an adaptive-thinking
  model per LiteLLM's model map) a level is ``reasoning_effort=<level>``, which
  LiteLLM sends as ``thinking={"type":"adaptive"}`` +
  ``output_config={"effort":<level>}``. Otherwise
  ``thinking={"type":"enabled","budget_tokens":N}``. ``off`` omits the kwarg
  (the API default is thinking off).
* **chatgpt** (direct Codex backend): ``chatgpt_reasoning_effort`` becomes the
  Responses API's ``reasoning.effort``; ``off`` → the backend's explicit
  ``none`` (never omitted, which would leave the effort unset for a model
  that requires one).
* **openai**: ``reasoning_effort=<level>``; with ``effort_levels`` only reported
  levels are accepted and ``off`` is ``reasoning_effort="none"`` where the model
  reports a ``none`` effort.
* **lm_studio / ollama / argonne** (OpenAI-compatible servers):
  ``reasoning_effort`` low/medium/high.
* **any other provider**: no mapping — a typed ``unsupported`` plan carrying a
  structured reason, surfaced by the caller. Never a silent no-op.

This module is pure data mapping and imports nothing provider-specific.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

#: Every external level, in ascending order (``None``/unset means "provider default").
LEVEL_ORDER: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

#: Valid external levels (``None``/unset means "provider default").
THINKING_LEVELS: frozenset[str] = frozenset(LEVEL_ORDER)

#: Canonical token budget for the budget ladder. An explicit ``thinking_budget``
#: overrides these for budget-based transports. Levels outside the ladder are
#: effort-only and have no budget.
LEVEL_BUDGET: dict[str, int] = {"low": 2048, "medium": 8192, "high": 24576}

_BUDGET_LEVELS: frozenset[str] = frozenset({"off", "low", "medium", "high"})

#: ChatGPT (Codex backend) effort vocabulary, in the Responses API's own
#: ``reasoning.effort`` values. ``off`` -> ``none``. ``max``/``ultra`` are
#: reported by newer models -- every effort the catalog defines gets a clio
#: level; none are dropped.
_CHATGPT_EFFORT: dict[str, str] = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultra": "ultra",
}

#: The levels each provider's transport can express at all. A model's own
#: ``effort_levels`` narrow this further. A level outside the set resolves to a
#: typed ``unsupported`` plan, never a silent drop or neighbour substitution.
ACCEPTED_LEVELS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"off", "low", "medium", "high", "xhigh", "max"}),
    "claude_code": frozenset({"off", "low", "medium", "high", "xhigh", "max"}),
    "chatgpt": frozenset(_CHATGPT_EFFORT),
    "openai": frozenset({"off", "minimal", "low", "medium", "high", "xhigh"}),
    "lm_studio": _BUDGET_LEVELS,
    "ollama": _BUDGET_LEVELS,
    "argonne": _BUDGET_LEVELS,
}


def accepted_levels(provider: str) -> tuple[str, ...]:
    """Return the levels ``provider``'s transport can express, in ascending order."""

    accepted = ACCEPTED_LEVELS.get(provider, frozenset())
    return tuple(level for level in LEVEL_ORDER if level in accepted)


@dataclass(frozen=True)
class ThinkingPlan:
    """Resolved, provider-specific thinking configuration.

    Attributes:
        provider: The provider the plan was resolved for.
        requested_level: The external level requested (``None`` = unset/default).
        effective_level: What actually applies — ``"default"`` (nothing sent), a
            level from :data:`LEVEL_ORDER`, or ``"unsupported"``.
        budget_tokens: Resolved thinking token budget (0 = off / effort-based).
        supported: False only when a non-default request hit no mapping — the
            caller must surface ``unsupported_reason``.
        unsupported_reason: Structured reason string when ``supported`` is False.
        litellm_kwargs: Passthrough kwargs for LiteLLM providers.
        sdk_thinking: The claude_code SDK thinking config (with an ``effort`` key
            when the level is an SDK effort), or ``None`` when not applicable.
    """

    provider: str
    requested_level: str | None
    effective_level: str
    budget_tokens: int
    supported: bool
    unsupported_reason: str | None
    litellm_kwargs: dict[str, Any]
    sdk_thinking: dict[str, Any] | None

    @property
    def display(self) -> str:
        """Human-readable effective level for doctor/status surfaces."""

        if not self.supported:
            return f"unsupported ({self.unsupported_reason})"
        if self.effective_level == "default":
            return "default (provider default)"
        if self.budget_tokens <= 0:
            return self.effective_level
        return f"{self.effective_level} (budget {self.budget_tokens})"


def _bucket_level(budget: int) -> str:
    """Bucket an explicit token budget into a level name (effort providers)."""

    if budget < 2000:
        return "low"
    if budget < 8000:
        return "medium"
    return "high"


def _normalize_level(level: str | None) -> str | None:
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


def _plan(
    provider: str,
    lvl: str | None,
    effective: str,
    *,
    budget: int = 0,
    litellm_kwargs: dict[str, Any] | None = None,
    sdk_thinking: dict[str, Any] | None = None,
) -> ThinkingPlan:
    return ThinkingPlan(
        provider=provider,
        requested_level=lvl,
        effective_level=effective,
        budget_tokens=budget,
        supported=True,
        unsupported_reason=None,
        litellm_kwargs=litellm_kwargs or {},
        sdk_thinking=sdk_thinking,
    )


def _unsupported(provider: str, lvl: str | None, reason: str, budget: int = 0) -> ThinkingPlan:
    return ThinkingPlan(
        provider=provider,
        requested_level=lvl,
        effective_level="unsupported",
        budget_tokens=budget,
        supported=False,
        unsupported_reason=reason,
        litellm_kwargs={},
        sdk_thinking=None,
    )


def _claude_code(
    lvl: str | None, effective: str, budget: int, effort: frozenset[str] | None
) -> ThinkingPlan:
    if effective == "off":
        return _plan("claude_code", lvl, "off", sdk_thinking={"type": "disabled"})
    if effort and lvl is not None:
        # The model reports SDK effort levels: the level IS the effort, on
        # adaptive thinking ("display" keeps CoT text streaming).
        return _plan(
            "claude_code",
            lvl,
            effective,
            sdk_thinking={"type": "adaptive", "display": "summarized", "effort": effective},
        )
    return _plan(
        "claude_code",
        lvl,
        effective,
        budget=budget,
        sdk_thinking={"type": "enabled", "budget_tokens": budget, "display": "summarized"},
    )


def _anthropic(
    lvl: str | None, effective: str, budget: int, effort: frozenset[str] | None
) -> ThinkingPlan:
    if effective == "off":
        return _plan("anthropic", lvl, "off")
    if effort and lvl is not None:
        # LiteLLM maps this to thinking=adaptive + output_config.effort.
        return _plan("anthropic", lvl, effective, litellm_kwargs={"reasoning_effort": effective})
    return _plan(
        "anthropic",
        lvl,
        effective,
        budget=budget,
        litellm_kwargs={"thinking": {"type": "enabled", "budget_tokens": budget}},
    )


def _openai_compatible(
    provider: str, lvl: str | None, effective: str, effort: frozenset[str] | None
) -> ThinkingPlan:
    if effective == "off":
        if provider == "openai" and effort and "off" in effort:
            return _plan(provider, lvl, "off", litellm_kwargs={"reasoning_effort": "none"})
        return _plan(provider, lvl, "off")
    return _plan(provider, lvl, effective, litellm_kwargs={"reasoning_effort": effective})


def resolve_thinking(
    provider: str,
    level: str | None,
    budget: int | None,
    *,
    effort_levels: Collection[str] | None = None,
) -> ThinkingPlan:
    """Map an external thinking level (+ optional budget override) to a provider plan.

    Precedence:
      1. ``level`` set → it wins ("off" → disabled; a level → its effort, or its
         budget / the explicit ``budget`` when > 0 on a budget transport).
      2. ``level`` unset but ``budget`` > 0 → explicit budget override (back-compat
         with the pre-#895 ``thinking_budget`` behavior); level is inferred by bucket.
      3. Neither → ``"default"``: nothing is sent.

    Args:
        provider: Provider kind (e.g. ``"claude_code"``, ``"anthropic"``, ``"openai"``).
        level: External level (one of :data:`LEVEL_ORDER`) or ``None``/"".
        budget: Explicit token budget override (0/``None`` = none).
        effort_levels: The model's own reported effort levels in clio vocabulary,
            when its provider reports them (see
            :func:`clio_agent.providers.reasoning_levels.model_effort_levels`).

    Returns:
        A :class:`ThinkingPlan`; unsupported requests carry a typed reason.
    """

    lvl = _normalize_level(level)
    n = int(budget or 0)
    if lvl is None and n <= 0:
        return _plan(provider, None, "default")

    effort = frozenset(effort_levels) if effort_levels else None
    accepted = ACCEPTED_LEVELS.get(provider)
    if accepted is None:
        return _unsupported(
            provider,
            lvl,
            f"thinking control (level={lvl or _bucket_level(n)!r}, budget={n}) has no "
            f"mapping for provider {provider!r}",
            n,
        )
    if lvl is not None and lvl not in accepted:
        return _unsupported(
            provider,
            lvl,
            f"provider {provider!r} has no {lvl!r} thinking level "
            f"(accepts {'|'.join(accepted_levels(provider))})",
        )
    if lvl not in (None, "off") and effort is not None and lvl not in effort:
        return _unsupported(
            provider,
            lvl,
            f"this model reports no {lvl!r} effort (reports {'|'.join(sorted(effort))})",
        )
    uses_budget = provider in {"anthropic", "claude_code"} and not effort
    if lvl is not None and uses_budget and lvl not in _BUDGET_LEVELS:
        return _unsupported(
            provider,
            lvl,
            f"{lvl!r} is an effort level and this model reports no effort support "
            f"(thinking budget levels: {'|'.join(sorted(_BUDGET_LEVELS))})",
        )

    if lvl == "off":
        effective, budget_tokens = "off", 0
    elif lvl is not None:
        effective = lvl
        budget_tokens = (n if n > 0 else LEVEL_BUDGET[lvl]) if uses_budget else 0
    else:  # explicit budget override, no level
        effective = _bucket_level(n)
        budget_tokens = n

    if provider == "claude_code":
        return _claude_code(lvl, effective, budget_tokens, effort)
    if provider == "anthropic":
        return _anthropic(lvl, effective, budget_tokens, effort)
    if provider == "chatgpt":
        return _plan(
            provider,
            lvl,
            effective,
            litellm_kwargs={"chatgpt_reasoning_effort": _CHATGPT_EFFORT[effective]},
        )
    return _openai_compatible(provider, lvl, effective, effort)


def log_unsupported_thinking(plan: ThinkingPlan) -> None:
    """Emit a structured warning when a thinking request could not be mapped.

    This is the "no silent no-op" surface for the LM-construction path: a
    requested thinking level on a provider without a mapping is recorded with a
    typed reason instead of being dropped silently.
    """

    if plan.supported:
        return
    logger.warning(
        "thinking_unsupported provider=%s requested_level=%s budget=%s reason=%s",
        plan.provider,
        plan.requested_level,
        plan.budget_tokens,
        plan.unsupported_reason,
    )


__all__ = [
    "ACCEPTED_LEVELS",
    "LEVEL_BUDGET",
    "LEVEL_ORDER",
    "THINKING_LEVELS",
    "accepted_levels",
    "ThinkingLevel",
    "ThinkingPlan",
    "log_unsupported_thinking",
    "resolve_thinking",
    "validate_thinking_level",
]


def shipped_default_level(provider: str, model: str, level: str | None, budget: int) -> str | None:
    """Resolve the shipped per-model default thinking level (#895).

    The owner's acceptance rule: ship the lowest level that passes verification.
    haiku via claude_code ships ``low`` — verified on the 2-turn EarthScope
    probe at 2.9x less wall-clock / 3.2x fewer output tokens than the SDK
    default, WITH the marketplace follow-up fix. sonnet via claude_code also
    ships ``low``: probed live 2026-08-05 (claude CLI 2.1.222 / claude-agent-sdk
    0.2.128) — with no thinking config the CLI runs sonnet thinking-OFF (zero
    thinking blocks in the partial-message stream), so a sonnet main and leaves
    inheriting the provider default model (``suggested_model="sonnet"``) had no
    provider-CoT lane at all; ``low`` (2048 budget) is the conservative floor,
    the same shipped level haiku verified. Applies only when the user set
    neither a level nor a budget; explicit settings always win; other
    models/providers keep ``None`` (the provider/SDK default governs).
    """
    lowered = model.lower()
    if (
        level is None
        and not budget
        and provider == "claude_code"
        and ("haiku" in lowered or "sonnet" in lowered)
    ):
        return "low"
    return level
