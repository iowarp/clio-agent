"""Provider-generic thinking level → per-provider mapping (#895).

Covers :mod:`clio_agent.providers.thinking`: the ``off|low|medium|high`` level
vocabulary, its per-provider mappings (anthropic budget / claude_code SDK config
/ openai reasoning_effort), the explicit-budget override + back-compat default,
and the typed-unsupported path (no silent no-op).
"""

from __future__ import annotations

import logging

import pytest

from clio_agent.providers.thinking import (
    LEVEL_BUDGET,
    ThinkingPlan,
    log_unsupported_thinking,
    resolve_thinking,
)


def test_default_unset_sends_nothing_for_every_provider() -> None:
    """level=None + budget=0 → the 'default' plan: nothing sent (back-compat)."""
    for provider in ("claude_code", "anthropic", "openai", "lm_studio", "mystery"):
        plan = resolve_thinking(provider, None, 0)
        assert plan.effective_level == "default"
        assert plan.supported is True
        assert plan.budget_tokens == 0
        assert plan.litellm_kwargs == {}
        assert plan.sdk_thinking is None
        assert plan.display == "default (provider default)"


def test_claude_code_off_sends_disabled_not_nothing() -> None:
    """'off' is distinct from 'default': claude_code must send an explicit disable."""
    plan = resolve_thinking("claude_code", "off", 0)
    assert plan.effective_level == "off"
    assert plan.sdk_thinking == {"type": "disabled"}
    assert plan.litellm_kwargs == {}
    assert plan.display == "off"


@pytest.mark.parametrize(
    ("level", "budget"),
    [("low", 2048), ("medium", 8192), ("high", 24576)],
)
def test_claude_code_levels_map_to_sdk_enabled_budget(level: str, budget: int) -> None:
    plan = resolve_thinking("claude_code", level, 0)
    assert plan.budget_tokens == budget == LEVEL_BUDGET[level]
    assert plan.sdk_thinking == {"type": "enabled", "budget_tokens": budget}
    assert plan.supported is True


def test_anthropic_uses_native_thinking_kwarg() -> None:
    assert resolve_thinking("anthropic", "high", 0).litellm_kwargs == {
        "thinking": {"type": "enabled", "budget_tokens": 24576}
    }
    # 'off' omits the kwarg entirely (anthropic default is thinking-off).
    assert resolve_thinking("anthropic", "off", 0).litellm_kwargs == {}
    assert resolve_thinking("anthropic", "high", 0).sdk_thinking is None


@pytest.mark.parametrize("provider", ["openai", "codex", "lm_studio", "ollama", "argonne"])
def test_effort_providers_map_level_to_reasoning_effort(provider: str) -> None:
    assert resolve_thinking(provider, "medium", 0).litellm_kwargs == {"reasoning_effort": "medium"}
    assert resolve_thinking(provider, "off", 0).litellm_kwargs == {}
    assert resolve_thinking(provider, "low", 0).sdk_thinking is None


def test_explicit_budget_override_wins_for_budget_providers() -> None:
    """A level + explicit budget: the explicit budget replaces the level default."""
    plan = resolve_thinking("claude_code", "high", 5000)
    assert plan.sdk_thinking == {"type": "enabled", "budget_tokens": 5000}
    plan_a = resolve_thinking("anthropic", "low", 5000)
    assert plan_a.litellm_kwargs == {"thinking": {"type": "enabled", "budget_tokens": 5000}}


def test_budget_only_no_level_infers_level_and_applies_backcompat() -> None:
    """Pre-#895 behavior: a positive budget with no level still enables thinking."""
    # claude_code budget-only → enabled at that budget, level bucketed for display.
    plan = resolve_thinking("claude_code", None, 4000)
    assert plan.sdk_thinking == {"type": "enabled", "budget_tokens": 4000}
    assert plan.effective_level == "medium"  # bucket of 4000
    # effort provider budget-only → bucketed reasoning_effort.
    assert resolve_thinking("openai", None, 1000).litellm_kwargs == {"reasoning_effort": "low"}
    assert resolve_thinking("openai", None, 9000).litellm_kwargs == {"reasoning_effort": "high"}


def test_unsupported_provider_is_typed_not_silent() -> None:
    """A requested level on a provider with no mapping → typed unsupported."""
    plan = resolve_thinking("mystery", "high", 0)
    assert plan.supported is False
    assert plan.effective_level == "unsupported"
    assert plan.unsupported_reason is not None
    assert "mystery" in plan.unsupported_reason
    assert plan.litellm_kwargs == {}
    assert plan.sdk_thinking is None
    assert plan.display.startswith("unsupported")


def test_unsupported_default_request_is_still_supported() -> None:
    """An unknown provider that requests nothing is a clean no-op, not unsupported."""
    plan = resolve_thinking("mystery", None, 0)
    assert plan.supported is True
    assert plan.effective_level == "default"


def test_invalid_level_raises() -> None:
    with pytest.raises(ValueError, match="off|low|medium|high"):
        resolve_thinking("claude_code", "extreme", 0)


def test_level_is_case_insensitive_and_trimmed() -> None:
    assert resolve_thinking("claude_code", "  HIGH ", 0).effective_level == "high"
    assert resolve_thinking("claude_code", "", 0).effective_level == "default"


def test_log_unsupported_emits_structured_warning(caplog) -> None:
    plan = resolve_thinking("mystery", "high", 0)
    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.thinking"):
        log_unsupported_thinking(plan)
    assert any("thinking_unsupported" in r.message for r in caplog.records)
    # A supported plan logs nothing.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.thinking"):
        log_unsupported_thinking(resolve_thinking("claude_code", "low", 0))
    assert not caplog.records


def test_plan_is_frozen() -> None:
    plan = resolve_thinking("claude_code", "low", 0)
    assert isinstance(plan, ThinkingPlan)
    with pytest.raises(AttributeError):
        plan.budget_tokens = 1  # type: ignore[misc]


def test_haiku_claude_code_ships_thinking_level_low_by_default() -> None:
    """#895 acceptance outcome: haiku via claude_code defaults to level 'low'
    (lowest level passing the 2-turn EarthScope probe). Explicit settings win."""
    from clio_agent.config import LMProviderConfig

    cfg = LMProviderConfig(provider="claude_code", model="haiku")
    assert cfg.thinking_level == "low"
    # explicit level wins
    cfg2 = LMProviderConfig(provider="claude_code", model="haiku", thinking_level="high")
    assert cfg2.thinking_level == "high"
    # explicit budget suppresses the shipped default (budget path governs)
    cfg3 = LMProviderConfig(provider="claude_code", model="haiku", thinking_budget=9000)
    assert cfg3.thinking_level is None
    # other models/providers keep None (SDK/provider default governs)
    cfg4 = LMProviderConfig(provider="claude_code", model="sonnet")
    assert cfg4.thinking_level is None
