"""``drop_params`` safety net + the proactive dropped-param warning (model-capabilities
plan, Part 2.4).

LiteLLM exposes no after-the-fact hook when ``drop_params=True`` actually drops a
param (verified against litellm 1.102.1's ``utils.py`` -- it just pops the key).
So clio checks PROACTIVELY at LM construction: would this optional kwarg survive
this model/dialect's own ``get_supported_openai_params()``? A drop means one of
clio's own capability records is wrong, so it is logged at WARNING as a bug
signal, not silently absorbed.
"""

from __future__ import annotations

import logging

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.factory import _warn_dropped_params, create_lm
from clio_agent.lm.request_builder import build_request_kwargs


def test_drop_params_is_on_by_default() -> None:
    extras = build_request_kwargs(LMProviderConfig(provider="openai", model="gpt-5", api_key="k"))
    assert extras["drop_params"] is True


def test_explicit_provider_option_overrides_the_default() -> None:
    # `provider_options` is schema-validated at construction against each
    # provider's declared configuration fields, and `drop_params` is not one of
    # them (it is an internal safety net, not a user-facing setting) -- so the
    # override is set post-construction here, exercising exactly the same
    # `dict(config.provider_options)` -> `setdefault` code path.
    config = LMProviderConfig(provider="openai", model="gpt-5", api_key="k")
    config.provider_options = {"drop_params": False}
    extras = build_request_kwargs(config)
    assert extras["drop_params"] is False


def test_create_lm_carries_drop_params_through_to_the_real_lm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = LMProviderConfig(provider="openai", model="gpt-5", api_key="sk-test")
    lm = create_lm(config)
    assert lm.kwargs["drop_params"] is True


def test_warns_on_a_param_the_dialect_does_not_support(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """anthropic's dialect does not accept ``presence_penalty`` -- this WOULD be dropped."""
    with caplog.at_level(logging.WARNING):
        _warn_dropped_params(
            model="anthropic/claude-x",
            kwargs={"presence_penalty": 0.5, "temperature": 0.7},
        )
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "presence_penalty" in warnings[0].message
    assert "temperature" not in warnings[0].message  # temperature IS supported -- not flagged
    assert "anthropic/claude-x" in warnings[0].message


def test_no_warning_when_every_param_is_supported(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_dropped_params(model="openai/gpt-4o", kwargs={"temperature": 0.5, "top_p": 0.9})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_no_warning_when_no_checked_params_are_present(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_dropped_params(model="anthropic/claude-x", kwargs={"api_key": "k", "cache": False})
    assert not caplog.records


@pytest.mark.parametrize("model", ["codex/cdx-gpt-5.5", "claude_code/cc-opus"])
def test_custom_cli_transports_are_never_checked(
    model: str, caplog: pytest.LogCaptureFixture
) -> None:
    """codex/claude_code are not LiteLLM dialects -- get_supported_openai_params
    does not apply, and their own optional_params contract is separate."""
    with caplog.at_level(logging.WARNING):
        _warn_dropped_params(
            model=model, kwargs={"presence_penalty": 0.5, "codex_reasoning_effort": "low"}
        )
    assert not caplog.records


def test_never_raises_for_an_unmapped_model(caplog: pytest.LogCaptureFixture) -> None:
    """A model litellm cannot classify must degrade to a debug log, never raise."""
    with caplog.at_level(logging.DEBUG):
        _warn_dropped_params(model="totally-unknown-format", kwargs={"temperature": 0.5})
    # No exception escaped (pytest would have failed the test); nothing at WARNING.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
