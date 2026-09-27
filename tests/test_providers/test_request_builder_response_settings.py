"""Response settings on the wire: sent only when accepted, spelled per dialect, drops logged.

The request builder gates every saved setting on the SAME accepted set the
catalog's ``accepted_parameters`` shows (``accepted_param_set``), so a setting a
person is not offered is never sent -- and a saved one the current model does
not accept is logged (``response_setting_not_sent``) instead of vanishing.
"""

from __future__ import annotations

import logging

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.request_builder import build_request_kwargs
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache
from clio_agent.providers.capabilities.records import DeploymentCapabilities, Fact

_NOW = "2026-09-26T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset() -> None:
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _cfg(provider_id: str, model: str, **kwargs: object) -> LMProviderConfig:
    return LMProviderConfig(provider_id=provider_id, model=model, **kwargs)  # type: ignore[arg-type]


def _not_sent(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "response_setting_not_sent" in r.getMessage()]


@pytest.mark.parametrize(
    ("provider_id", "model"), [("codex", "gpt-5.5"), ("claude_code", "sonnet")]
)
def test_sdk_transports_never_receive_sampling_and_the_drop_is_logged(
    provider_id: str, model: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.lm.request_builder")
    extras = build_request_kwargs(_cfg(provider_id, model, temperature=0.4, top_p=0.9, seed=7))
    for field in ("temperature", "top_p", "seed", "extra_body"):
        assert field not in extras
    messages = _not_sent(caplog)
    assert any("field=temperature" in m for m in messages)
    assert any("field=top_p" in m and "reason=not_accepted_by_model" in m for m in messages)


def test_ollama_options_go_top_level_so_litellm_puts_them_in_options() -> None:
    extras = build_request_kwargs(
        _cfg(
            "ollama",
            "qwen3:8b",
            top_k=20,
            min_p=0.05,
            repetition_penalty=1.1,
            context_length=16384,
            seed=3,
        )
    )
    assert extras["top_k"] == 20
    assert extras["min_p"] == 0.05
    assert extras["repeat_penalty"] == 1.1
    assert extras["num_ctx"] == 16384
    assert extras["seed"] == 3
    assert "extra_body" not in extras


def test_ollama_frequency_penalty_is_not_sent(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.lm.request_builder")
    extras = build_request_kwargs(_cfg("ollama", "qwen3:8b", frequency_penalty=0.5))
    assert "frequency_penalty" not in extras
    assert any("field=frequency_penalty" in m for m in _not_sent(caplog))


def test_zero_context_length_leaves_the_server_alone() -> None:
    assert "num_ctx" not in build_request_kwargs(_cfg("ollama", "qwen3:8b", context_length=0))


def test_lm_studio_spells_repeat_penalty_and_never_sends_context_on_the_request() -> None:
    extras = build_request_kwargs(
        _cfg("lm_studio", "qwen3-8b", top_k=40, repetition_penalty=1.2, context_length=8192)
    )
    assert extras["extra_body"] == {"top_k": 40, "repeat_penalty": 1.2}
    assert "num_ctx" not in extras and "context_length" not in extras


def test_vllm_standard_fields_top_level_extras_in_body() -> None:
    extras = build_request_kwargs(
        _cfg("vllm", "m", frequency_penalty=0.2, seed=11, repetition_penalty=1.05)
    )
    assert extras["frequency_penalty"] == 0.2
    assert extras["seed"] == 11
    assert extras["extra_body"]["repetition_penalty"] == 1.05


def test_openrouter_before_a_handshake_sends_top_k_with_require_parameters() -> None:
    extras = build_request_kwargs(_cfg("openrouter", "qwen/qwen3-235b", top_k=20))
    assert extras["extra_body"]["top_k"] == 20
    assert extras["extra_body"]["provider"] == {"require_parameters": True}


def test_openrouter_route_without_top_k_withholds_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.lm.request_builder")
    from clio_agent.providers.capabilities.endpoint import build_endpoint_capabilities

    base = "https://openrouter.ai/api/v1"
    invalidation.record_endpoint_capabilities(
        build_endpoint_capabilities(
            "openrouter", base, "openrouter", "openai/o3", custom_llm_provider="openrouter"
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="openrouter",
            api_base=base,
            model_id="openai/o3",
            model_key=Fact("openai/o3", "server_report", _NOW),
            route_params=Fact(frozenset({"max_tokens", "seed"}), "server_report", _NOW),
        )
    )
    extras = build_request_kwargs(
        _cfg("openrouter", "openai/o3", api_base=base, top_k=20, temperature=0.3, seed=5)
    )
    assert extras["seed"] == 5
    assert "temperature" not in extras
    assert "top_k" not in (extras.get("extra_body") or {})
    fields = " ".join(_not_sent(caplog))
    assert "field=temperature" in fields and "field=top_k" in fields


def test_unknown_sampling_withholds_settings_while_unknown_levels_pass_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Two different "unknown" rules, kept apart on purpose: no evidence that
    # Codex accepts sampling means none is offered or sent, while a requested
    # effort for a model whose reasoning levels are not known yet still rides
    # through (reasoning_levels_unknown) -- reasoning is not an accepted
    # parameter; it has its own control.
    caplog.set_level(logging.INFO, logger="clio_agent.lm.request_builder")
    extras = build_request_kwargs(
        _cfg("codex", "gpt-unlisted", temperature=0.4, thinking_level="high")
    )
    assert extras["codex_reasoning_effort"] == "high"
    assert "temperature" not in extras
    messages = [r.getMessage() for r in caplog.records]
    assert any("reason=reasoning_levels_unknown" in m for m in messages)
    assert any("response_setting_not_sent" in m and "field=temperature" in m for m in messages)
