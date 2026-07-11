"""Transport-level wiring of the provider-generic thinking knob (#895).

Proves the level travels config → factory kwargs → optional_params → the SDK
call: the LM-factory mapping, the claude_code provider pass-through into
``_run_sdk``, the ``ClaudeAgentOptions.thinking`` placement, and the session-pool
re-key on distinct thinking configs. The env/config plumbing and typed-unsupported
surfacing round it out.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from clio_agent.config import LMProviderConfig, load_config_from_env
from clio_agent.lm.factory import _thinking_kwargs
from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_litellm import ClaudeCodeLLM, _SdkSessionPool
from clio_agent.providers.claude_code_options import build_sdk_options, thinking_key


# --------------------------------------------------------------------------- #
# LM-factory mapping: config.thinking_level → provider-specific kwargs.
# --------------------------------------------------------------------------- #
def test_factory_maps_claude_code_level_to_optional_params() -> None:
    """claude_code carries the SDK thinking config under ``claude_code_thinking``."""
    off = _thinking_kwargs(
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="off")
    )
    assert off == {"claude_code_thinking": {"type": "disabled"}}

    low = _thinking_kwargs(
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="low")
    )
    assert low == {"claude_code_thinking": {"type": "enabled", "budget_tokens": 2048}}

    # Unset → nothing (byte-for-byte pre-#895 behavior), never reasoning_effort.
    default = _thinking_kwargs(LMProviderConfig(provider="claude_code", model="haiku"))
    assert default == {}


def test_factory_maps_anthropic_and_effort_providers() -> None:
    anth = _thinking_kwargs(
        LMProviderConfig(provider="anthropic", model="x", api_key="k", thinking_level="high")
    )
    assert anth == {"thinking": {"type": "enabled", "budget_tokens": 24576}}
    # openai-compatible → reasoning_effort; no claude_code_thinking key leaks.
    oai = _thinking_kwargs(
        LMProviderConfig(provider="lm_studio", model="m", thinking_level="medium")
    )
    assert oai == {"reasoning_effort": "medium"}
    assert "claude_code_thinking" not in oai


def test_provider_lm_kwargs_carries_transport_and_thinking() -> None:
    """The full provider kwargs surface carries BOTH transport and thinking."""
    from clio_agent.lm.factory import _provider_lm_kwargs

    extras = _provider_lm_kwargs(
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="off")
    )
    assert extras["claude_code_transport"] == "sdk"
    assert extras["claude_code_thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- #
# The option reaches the real SDK options object.
# --------------------------------------------------------------------------- #
def test_build_sdk_options_places_thinking_on_real_sdk_options() -> None:
    pytest.importorskip("claude_agent_sdk")
    opts = build_sdk_options(model="haiku", cwd=None, stream=True, thinking={"type": "disabled"})
    assert opts.thinking == {"type": "disabled"}
    assert opts.include_partial_messages is True

    enabled = {"type": "enabled", "budget_tokens": 2048}
    assert (
        build_sdk_options(model="haiku", cwd=None, stream=False, thinking=enabled).thinking
        == enabled
    )

    # Unset → the SDK field stays None (provider/CLI default governs).
    assert build_sdk_options(model="haiku", cwd=None, stream=False, thinking=None).thinking is None


# --------------------------------------------------------------------------- #
# The claude_code provider passes optional_params thinking through to the SDK path.
# (Fake ``_run_sdk`` asserts it received the option — the sabotage target: drop
# ``thinking=thinking`` in ClaudeCodeLLM.completion and this goes red.)
# --------------------------------------------------------------------------- #
def test_completion_passes_thinking_from_optional_params_to_run_sdk(monkeypatch) -> None:
    seen: dict = {}

    def fake_sdk(*, prompt, model, timeout, cwd, thinking=None):
        seen["thinking"] = thinking
        return "ok", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", fake_sdk)
    ClaudeCodeLLM().completion(
        model="claude_code/cc-haiku",
        messages=[{"role": "user", "content": "hi"}],
        api_base="",
        custom_prompt_dict={},
        model_response=MagicMock(),
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={
            "claude_code_transport": "sdk",
            "claude_code_thinking": {"type": "disabled"},
        },
    )
    assert seen["thinking"] == {"type": "disabled"}


# --------------------------------------------------------------------------- #
# Session pool re-keys on distinct thinking configs.
# --------------------------------------------------------------------------- #
def test_thinking_key_is_stable_and_distinct() -> None:
    assert thinking_key(None) is None
    assert thinking_key({}) is None
    a = thinking_key({"type": "enabled", "budget_tokens": 2048})
    b = thinking_key({"budget_tokens": 2048, "type": "enabled"})  # key order irrelevant
    assert a == b
    assert a != thinking_key({"type": "disabled"})


def test_pool_rekeys_on_distinct_thinking() -> None:
    pool = _SdkSessionPool()
    disabled = pool._session_for("haiku", "/w", thinking_key({"type": "disabled"}))
    low = pool._session_for("haiku", "/w", thinking_key({"type": "enabled", "budget_tokens": 2048}))
    disabled_again = pool._session_for("haiku", "/w", thinking_key({"type": "disabled"}))
    assert disabled is disabled_again
    assert disabled is not low


# --------------------------------------------------------------------------- #
# Config + env plumbing.
# --------------------------------------------------------------------------- #
def test_config_validates_thinking_level() -> None:
    assert (
        LMProviderConfig(
            provider="claude_code", model="haiku", thinking_level="HIGH"
        ).thinking_level
        == "high"
    )
    with pytest.raises(ValueError, match="thinking_level must be"):
        LMProviderConfig(provider="claude_code", model="haiku", thinking_level="extreme")


def test_thinking_level_and_budget_from_env() -> None:
    env = {
        "CLIO_LM_PROVIDER": "claude_code",
        "CLIO_LM_THINKING_LEVEL": "low",
        "CLIO_LM_THINKING_BUDGET": "3000",
    }
    with patch.dict("os.environ", env, clear=True):
        config = load_config_from_env()
    assert config.thinking_level == "low"
    assert config.thinking_budget == 3000


def test_unsupported_level_logs_and_returns_empty(caplog) -> None:
    """A level on a provider with no mapping is a typed warning, not a silent no-op."""
    cfg = LMProviderConfig(provider="argonne", model="m")
    # Force a genuinely-unmapped provider (all real providers have a mapping).
    cfg.provider = "mystery"  # type: ignore[assignment]
    cfg.thinking_level = "high"
    with caplog.at_level(logging.WARNING, logger="clio_agent.providers.thinking"):
        extras = _thinking_kwargs(cfg)
    assert extras == {}
    assert any("thinking_unsupported" in r.message for r in caplog.records)
