"""Provider-generic thinking (extended-reasoning) level → per-provider mapping (#895).

One external vocabulary — ``off | low | medium | high`` — maps to whatever each
provider's transport actually understands. The knob is deliberately *not*
Claude-shaped (design constraint from #896):

* **anthropic** (native LiteLLM): ``thinking={"type":"enabled","budget_tokens":N}``.
* **claude_code** (Claude Agent SDK transport): the SDK
  ``ClaudeAgentOptions.thinking`` config — ``{"type":"disabled"}`` for ``off`` and
  ``{"type":"enabled","budget_tokens":N}`` for a level. Verified empirically that
  the SDK/CLI default for haiku is thinking-**ON**, so ``off`` must send
  ``disabled`` *explicitly*; a zero ``thinking_budget`` alone cannot express it
  (0 = "unset / let the provider default govern"). That is exactly why the
  external level is a sibling knob to ``thinking_budget``.
* **openai / codex / openai-compatible** (lm_studio, ollama, argonne):
  ``reasoning_effort`` bucketed to the level.
* **any other provider**: no mapping — a typed ``unsupported`` plan carrying a
  structured reason, surfaced by the caller. Never a silent no-op.

This module is pure data mapping: it builds plain dicts (the SDK ``thinking``
config is a plain ``{"type": ...}`` TypedDict-shaped dict) and imports nothing
provider-specific, so it is trivially unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ThinkingLevel = Literal["off", "low", "medium", "high"]

#: Valid external levels (``None``/unset means "provider default").
THINKING_LEVELS: frozenset[str] = frozenset({"off", "low", "medium", "high"})

#: Canonical token budget for each non-off level. An explicit ``thinking_budget``
#: overrides these for budget-based providers.
LEVEL_BUDGET: dict[str, int] = {"low": 2048, "medium": 8192, "high": 24576}

# Providers whose transport expresses thinking as a token budget.
_BUDGET_PROVIDERS: frozenset[str] = frozenset({"anthropic", "claude_code"})
# Providers (OpenAI + OpenAI-compatible) that take a ``reasoning_effort`` string.
_EFFORT_PROVIDERS: frozenset[str] = frozenset({"openai", "codex", "lm_studio", "ollama", "argonne"})


@dataclass(frozen=True)
class ThinkingPlan:
    """Resolved, provider-specific thinking configuration.

    Attributes:
        provider: The provider the plan was resolved for.
        requested_level: The external level requested (``None`` = unset/default).
        effective_level: What actually applies — ``"default"`` (nothing sent),
            ``"off"``/``"low"``/``"medium"``/``"high"``, or ``"unsupported"``.
        budget_tokens: Resolved thinking token budget (0 = off / none).
        supported: False only when a non-default request hit a provider with no
            mapping — the caller must surface ``unsupported_reason``.
        unsupported_reason: Structured reason string when ``supported`` is False.
        litellm_kwargs: Passthrough kwargs for native LiteLLM providers
            (anthropic ``thinking``; effort providers ``reasoning_effort``).
        sdk_thinking: The claude_code ``ClaudeAgentOptions.thinking`` dict, or
            ``None`` when not applicable / unset.
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
        if self.effective_level == "off":
            return "off"
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
        raise ValueError(f"thinking level must be one of off|low|medium|high (got {level!r})")
    return lvl


def resolve_thinking(provider: str, level: str | None, budget: int | None) -> ThinkingPlan:
    """Map an external thinking level (+ optional budget override) to a provider plan.

    Precedence:
      1. ``level`` set → it wins ("off" → disabled; a level → its budget, or the
         explicit ``budget`` when > 0 for budget-based providers).
      2. ``level`` unset but ``budget`` > 0 → explicit budget override (back-compat
         with the pre-#895 ``thinking_budget`` behavior); level is inferred by bucket.
      3. Neither → ``"default"``: nothing is sent, byte-for-byte today's behavior.

    Args:
        provider: Provider id (e.g. ``"claude_code"``, ``"anthropic"``, ``"openai"``).
        level: External level ``off|low|medium|high`` or ``None``/"".
        budget: Explicit token budget override (0/``None`` = none).

    Returns:
        A :class:`ThinkingPlan`. For an unsupported provider with a non-default
        request, ``supported`` is False and ``unsupported_reason`` is set.
    """

    lvl = _normalize_level(level)
    n = int(budget or 0)

    # (3) Nothing requested → default (unset). Preserves today's behavior exactly.
    if lvl is None and n <= 0:
        return ThinkingPlan(
            provider=provider,
            requested_level=None,
            effective_level="default",
            budget_tokens=0,
            supported=True,
            unsupported_reason=None,
            litellm_kwargs={},
            sdk_thinking=None,
        )

    # Resolve the effective level + numeric budget.
    if lvl == "off":
        effective, budget_tokens = "off", 0
    elif lvl in LEVEL_BUDGET:
        effective = lvl
        budget_tokens = n if n > 0 else LEVEL_BUDGET[lvl]
    else:  # lvl is None but n > 0: explicit budget override
        effective = _bucket_level(n)
        budget_tokens = n

    if provider in _BUDGET_PROVIDERS:
        if provider == "claude_code":
            sdk_thinking = (
                {"type": "disabled"}
                if effective == "off"
                else {"type": "enabled", "budget_tokens": budget_tokens}
            )
            return ThinkingPlan(
                provider=provider,
                requested_level=lvl,
                effective_level=effective,
                budget_tokens=budget_tokens,
                supported=True,
                unsupported_reason=None,
                litellm_kwargs={},
                sdk_thinking=sdk_thinking,
            )
        # anthropic (native LiteLLM): omit the kwarg entirely for "off" (thinking
        # is off by default when the parameter is not sent).
        litellm_kwargs: dict[str, Any] = (
            {}
            if effective == "off"
            else {"thinking": {"type": "enabled", "budget_tokens": budget_tokens}}
        )
        return ThinkingPlan(
            provider=provider,
            requested_level=lvl,
            effective_level=effective,
            budget_tokens=budget_tokens,
            supported=True,
            unsupported_reason=None,
            litellm_kwargs=litellm_kwargs,
            sdk_thinking=None,
        )

    if provider in _EFFORT_PROVIDERS:
        litellm_kwargs = {} if effective == "off" else {"reasoning_effort": effective}
        return ThinkingPlan(
            provider=provider,
            requested_level=lvl,
            effective_level=effective,
            budget_tokens=budget_tokens,
            supported=True,
            unsupported_reason=None,
            litellm_kwargs=litellm_kwargs,
            sdk_thinking=None,
        )

    # No mapping for this provider — typed unsupported, never a silent no-op.
    return ThinkingPlan(
        provider=provider,
        requested_level=lvl,
        effective_level="unsupported",
        budget_tokens=budget_tokens,
        supported=False,
        unsupported_reason=(
            f"thinking control (level={effective!r}, budget={budget_tokens}) has no "
            f"mapping for provider {provider!r}"
        ),
        litellm_kwargs={},
        sdk_thinking=None,
    )


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
    "LEVEL_BUDGET",
    "THINKING_LEVELS",
    "ThinkingLevel",
    "ThinkingPlan",
    "log_unsupported_thinking",
    "resolve_thinking",
]


def shipped_default_level(provider: str, model: str, level: str | None, budget: int) -> str | None:
    """Resolve the shipped per-model default thinking level (#895).

    The owner's acceptance rule: ship the lowest level that passes verification.
    haiku via claude_code ships ``low`` — verified on the 2-turn EarthScope
    probe at 2.9x less wall-clock / 3.2x fewer output tokens than the SDK
    default, WITH the marketplace follow-up fix. Applies only when the user set
    neither a level nor a budget; explicit settings always win; other
    models/providers keep ``None`` (the provider/SDK default governs).
    """
    if level is None and not budget and provider == "claude_code" and "haiku" in model.lower():
        return "low"
    return level
