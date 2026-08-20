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
    assert plan.sdk_thinking == {
        "type": "enabled",
        "budget_tokens": budget,
        "display": "summarized",
    }
    assert plan.supported is True


def test_claude_code_enabled_config_requests_summarized_display() -> None:
    """The enabled SDK config carries ``display: "summarized"`` (CoT un-redaction).

    claude CLI >= 2.1.x defaults the thinking display to "omitted": thinking_delta
    events arrive with empty text plus an estimated_tokens count and the
    ThinkingBlock is signature-only, so WITHOUT this key no provider CoT text ever
    reaches clio (probed live 2026-08-05, CLI 2.1.222 / claude-agent-sdk 0.2.128).
    """
    for level in ("low", "medium", "high"):
        assert resolve_thinking("claude_code", level, 0).sdk_thinking["display"] == "summarized"
    # The budget-only back-compat path enables with the same display request.
    assert resolve_thinking("claude_code", None, 4000).sdk_thinking["display"] == "summarized"
    # 'off' sends the bare disabled config — the SDK disabled TypedDict has no
    # display member, and adding one would be sent to the CLI as junk.
    assert resolve_thinking("claude_code", "off", 0).sdk_thinking == {"type": "disabled"}
    # display is an SDK-transport knob ONLY: the native anthropic LiteLLM kwarg
    # must not grow it (the Messages API thinking param has no such key).
    anthropic_thinking = resolve_thinking("anthropic", "low", 0).litellm_kwargs["thinking"]
    assert "display" not in anthropic_thinking


def test_anthropic_uses_native_thinking_kwarg() -> None:
    assert resolve_thinking("anthropic", "high", 0).litellm_kwargs == {
        "thinking": {"type": "enabled", "budget_tokens": 24576}
    }
    # 'off' omits the kwarg entirely (anthropic default is thinking-off).
    assert resolve_thinking("anthropic", "off", 0).litellm_kwargs == {}
    assert resolve_thinking("anthropic", "high", 0).sdk_thinking is None


@pytest.mark.parametrize("provider", ["openai", "lm_studio", "ollama", "argonne"])
def test_effort_providers_map_level_to_reasoning_effort(provider: str) -> None:
    assert resolve_thinking(provider, "medium", 0).litellm_kwargs == {"reasoning_effort": "medium"}
    assert resolve_thinking(provider, "off", 0).litellm_kwargs == {}
    assert resolve_thinking(provider, "low", 0).sdk_thinking is None


def test_codex_maps_level_to_codex_reasoning_effort() -> None:
    """Codex has a dedicated key + an explicit ``none`` for off (#896).

    ``codex_reasoning_effort`` (not ``reasoning_effort``) so the codex CustomLLM
    reads it and pins it on ``turn/start`` (LiteLLM drops ``reasoning_effort`` on
    the CustomLLM path — the old silent no-op). ``off`` → ``"none"`` (disable), NOT
    an omitted kwarg (which would inherit the ambient ``config.toml`` effort).
    """
    assert resolve_thinking("codex", "medium", 0).litellm_kwargs == {
        "codex_reasoning_effort": "medium"
    }
    assert resolve_thinking("codex", "low", 0).litellm_kwargs == {"codex_reasoning_effort": "low"}
    assert resolve_thinking("codex", "high", 0).litellm_kwargs == {"codex_reasoning_effort": "high"}
    # off maps to codex's explicit disable, never omit-and-inherit-ambient.
    assert resolve_thinking("codex", "off", 0).litellm_kwargs == {"codex_reasoning_effort": "none"}
    # unset → nothing pinned (codex's own default governs).
    assert resolve_thinking("codex", None, 0).litellm_kwargs == {}
    assert resolve_thinking("codex", "high", 0).supported is True
    assert resolve_thinking("codex", "high", 0).sdk_thinking is None


def test_explicit_budget_override_wins_for_budget_providers() -> None:
    """A level + explicit budget: the explicit budget replaces the level default."""
    plan = resolve_thinking("claude_code", "high", 5000)
    assert plan.sdk_thinking == {
        "type": "enabled",
        "budget_tokens": 5000,
        "display": "summarized",
    }
    plan_a = resolve_thinking("anthropic", "low", 5000)
    assert plan_a.litellm_kwargs == {"thinking": {"type": "enabled", "budget_tokens": 5000}}


def test_budget_only_no_level_infers_level_and_applies_backcompat() -> None:
    """Pre-#895 behavior: a positive budget with no level still enables thinking."""
    # claude_code budget-only → enabled at that budget, level bucketed for display.
    plan = resolve_thinking("claude_code", None, 4000)
    assert plan.sdk_thinking == {
        "type": "enabled",
        "budget_tokens": 4000,
        "display": "summarized",
    }
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
    # models outside the shipped set keep None (SDK/provider default governs)
    cfg4 = LMProviderConfig(provider="claude_code", model="opus")
    assert cfg4.thinking_level is None


def test_sonnet_claude_code_ships_thinking_level_low_by_default() -> None:
    """sonnet via claude_code also ships 'low' (probed 2026-08-05, CLI 2.1.222):
    with no thinking config the CLI runs sonnet thinking-OFF, so the provider
    default model ('sonnet') and a blueprint-pinned sonnet main had no
    provider-CoT lane at all. Explicit settings still win."""
    from clio_agent.config import LMProviderConfig

    cfg = LMProviderConfig(provider="claude_code", model="sonnet")
    assert cfg.thinking_level == "low"
    # full model ids match by substring, same as haiku's shipped default
    cfg2 = LMProviderConfig(provider="claude_code", model="claude-sonnet-4-5")
    assert cfg2.thinking_level == "low"
    # explicit level wins
    cfg3 = LMProviderConfig(provider="claude_code", model="sonnet", thinking_level="off")
    assert cfg3.thinking_level == "off"
    # explicit budget suppresses the shipped default (budget path governs)
    cfg4 = LMProviderConfig(provider="claude_code", model="sonnet", thinking_budget=9000)
    assert cfg4.thinking_level is None
    # the shipped default is claude_code-scoped: sonnet on another provider keeps None
    cfg5 = LMProviderConfig(provider="anthropic", model="claude-sonnet-4-5", api_key="k")
    assert cfg5.thinking_level is None
