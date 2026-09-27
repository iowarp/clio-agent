"""accepted_parameters: only the settings a model accepts, with evidence, ranges and defaults.

Records are seeded through :mod:`clio_agent.providers.capabilities.invalidation`
(no network). Every returned row is a validated ``clio_schemas.AcceptedParameter``
(the projection builds each through the record), so these tests assert WHICH
settings appear and what they say.
"""

from __future__ import annotations

import pytest
from clio_schemas import AcceptedParameter

from clio_agent.providers.capabilities import accepted_parameters as ap
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache, get_effective_capabilities
from clio_agent.providers.capabilities.dialects.openrouter import parse_model_row
from clio_agent.providers.capabilities.endpoint import build_endpoint_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
)

_NOW = "2026-09-26T00:00:00+00:00"
_OPENROUTER = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _reset() -> None:
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def _rows(
    provider_id: str, api_base: str, model_id: str, *, dialect: str, prefix: str
) -> dict[str, dict]:
    rows = ap.accepted_parameters(
        provider_id,
        api_base,
        model_id,
        dialect=dialect,
        litellm_prefix=prefix,
        effective=get_effective_capabilities(provider_id, api_base, model_id),
    )
    for row in rows:
        AcceptedParameter.model_validate(row)
    return {row["name"]: row for row in rows}


def _seed_openrouter(model_id: str, supported: list[str]) -> None:
    invalidation.record_endpoint_capabilities(
        build_endpoint_capabilities(
            "openrouter", _OPENROUTER, "openrouter", model_id, custom_llm_provider="openrouter"
        )
    )
    row = {
        "id": model_id,
        "context_length": 131072,
        "top_provider": {"context_length": 131072, "max_completion_tokens": 16384},
        "supported_parameters": supported,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    }
    model, deployment = parse_model_row(row, provider_id="openrouter", api_base=_OPENROUTER)
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(deployment)


@pytest.mark.parametrize(
    ("provider_id", "api_base", "dialect", "prefix"),
    [
        ("claude_code", "claude-code://sdk", "claude_code", "claude_code"),
        ("codex", "codex://direct", "codex", "codex_direct"),
        ("codex", "codex://sdk", "codex", "codex_sdk"),
    ],
)
def test_sdk_transports_accept_no_response_settings(
    provider_id: str, api_base: str, dialect: str, prefix: str
) -> None:
    # The SDK options carry no sampling or output-cap field at all, and LiteLLM
    # has no provider for these transports: unknown is not offered.
    assert _rows(provider_id, api_base, "m", dialect=dialect, prefix=prefix) == {}


@pytest.mark.parametrize(
    ("provider_id", "api_base", "dialect", "prefix"),
    [
        ("claude_code", "claude-code://sdk", "claude_code", "claude_code"),
        ("codex", "codex://direct", "codex", "codex_direct"),
        ("codex", "codex://sdk", "codex", "codex_sdk"),
    ],
)
def test_sdk_transports_stay_empty_after_their_handler_ran_in_this_process(
    provider_id: str, api_base: str, dialect: str, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CI-only failure of the test above (4 xdist workers).

    clio registers these transports as LiteLLM CUSTOM handlers. Once LiteLLM has set
    custom handlers up (any completion in the process does it),
    ``get_supported_openai_params`` answers the generic OpenAI list for each of them,
    so a worker that had already run a codex/claude turn offered frequency_penalty,
    max_tokens, ... for a transport that takes none of them. A handler clio itself
    registered is not LiteLLM evidence about the transport.
    """
    import litellm  # noqa: PLC0415
    from litellm.utils import custom_llm_setup  # noqa: PLC0415

    monkeypatch.setattr(
        litellm, "custom_provider_map", [{"provider": prefix, "custom_handler": object()}]
    )
    monkeypatch.setattr(litellm, "provider_list", list(litellm.provider_list))
    monkeypatch.setattr(litellm, "_custom_providers", list(litellm._custom_providers))
    custom_llm_setup()  # what the first completion after registration does
    assert litellm.get_supported_openai_params(model="m", custom_llm_provider=prefix)
    assert _rows(provider_id, api_base, "m", dialect=dialect, prefix=prefix) == {}


def test_openrouter_offers_exactly_the_routes_supported_parameters() -> None:
    _seed_openrouter(
        "qwen/qwen3-235b",
        ["temperature", "top_p", "top_k", "min_p", "repetition_penalty", "seed", "max_tokens"],
    )
    rows = _rows(
        "openrouter", _OPENROUTER, "qwen/qwen3-235b", dialect="openrouter", prefix="openrouter"
    )
    assert list(rows) == [
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
        "max_tokens",
        "seed",
    ]
    details = [e["detail"] for e in rows["top_k"]["evidence"]]
    assert any("openrouter supported_parameters" in d for d in details)
    # max_tokens is capped at the route's output limit, with that evidence.
    assert rows["max_tokens"]["maximum"] == 16384


def test_openrouter_model_without_sampling_offers_none_of_it() -> None:
    _seed_openrouter("openai/o3", ["max_tokens", "tools", "reasoning", "seed"])
    rows = _rows("openrouter", _OPENROUTER, "openai/o3", dialect="openrouter", prefix="openrouter")
    assert set(rows) == {"max_tokens", "seed"}


def test_ollama_offers_its_own_options_and_not_frequency_penalty() -> None:
    rows = _rows(
        "ollama", "http://127.0.0.1:11434", "qwen3:8b", dialect="ollama", prefix="ollama_chat"
    )
    assert {"temperature", "top_p", "top_k", "min_p", "repetition_penalty", "seed"} <= set(rows)
    assert rows["context_length"]["group"] == "length"
    assert "frequency_penalty" not in rows  # litellm maps it onto repeat_penalty
    assert "presence_penalty" not in rows  # ollama_chat drops it
    assert "parallel" not in rows


def test_lm_studio_offers_its_load_settings_with_dialect_evidence() -> None:
    rows = _rows(
        "lm_studio", "http://127.0.0.1:1234/v1", "qwen3-8b", dialect="lm_studio", prefix="openai"
    )
    assert {"context_length", "parallel", "top_k", "repetition_penalty"} <= set(rows)
    assert "min_p" not in rows
    assert rows["parallel"]["evidence"][0]["source"] == "dialect"
    assert "models/load" in rows["parallel"]["evidence"][0]["detail"]


def test_vllm_has_no_context_setting_because_it_is_fixed_at_launch() -> None:
    rows = _rows("vllm", "http://127.0.0.1:8000/v1", "m", dialect="vllm", prefix="hosted_vllm")
    assert {"top_k", "min_p", "repetition_penalty", "frequency_penalty"} <= set(rows)
    assert "context_length" not in rows


def test_anthropic_temperature_range_is_zero_to_one() -> None:
    rows = _rows(
        "anthropic",
        "https://api.anthropic.com/v1",
        "claude-sonnet-4-5",
        dialect="anthropic",
        prefix="anthropic",
    )
    assert rows["temperature"]["maximum"] == 1.0
    assert "top_k" not in rows


def test_context_size_is_capped_at_the_models_window_not_the_loaded_one() -> None:
    base = "http://127.0.0.1:1234/v1"
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="qwen3-8b",
            context_max=Fact(40960, "server_report", _NOW, "lm studio max_context_length"),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="lm_studio",
            api_base=base,
            model_id="qwen3-8b",
            model_key=Fact("qwen3-8b", "server_report", _NOW),
            context_served=Fact(4096, "server_report", _NOW),
        )
    )
    rows = _rows("lm_studio", base, "qwen3-8b", dialect="lm_studio", prefix="openai")
    assert rows["context_length"]["maximum"] == 40960


def test_default_is_the_recommended_value_clio_actually_sends() -> None:
    base = "http://127.0.0.1:8088/v1"
    invalidation.record_endpoint_capabilities(
        build_endpoint_capabilities(
            "llama_cpp", base, "llama_cpp", "q", custom_llm_provider="openai"
        )
    )
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="q",
            thinking=Fact(ThinkingSpec(mechanism="always_on"), "overlay", _NOW),
            sampling_thinking=Fact({"temperature": 0.6, "top_k": 20.0}, "overlay", _NOW),
            sampling_instruct=Fact({"temperature": 0.7}, "overlay", _NOW),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="llama_cpp",
            api_base=base,
            model_id="q",
            model_key=Fact("q", "server_report", _NOW),
        )
    )
    rows = _rows("llama_cpp", base, "q", dialect="llama_cpp", prefix="openai")
    assert rows["temperature"]["default"] == 0.6  # always-on thinking -> thinking mode
    assert rows["top_k"]["default"] == 20
    assert rows["top_p"]["default"] is None  # nothing sent: the provider's own default
    assert rows["temperature"]["evidence"][-1]["source"] == "overlay"


def test_validate_settings_checks_accepted_ranges_only() -> None:
    rows = list(
        _rows(
            "ollama", "http://127.0.0.1:11434", "m", dialect="ollama", prefix="ollama_chat"
        ).values()
    )
    assert ap.validate_settings(
        {"temperature": 3.0, "top_k": 2.5, "frequency_penalty": 9.0}, rows
    ) == [
        "temperature=3.0 is above 2",
        "top_k=2.5 is not a whole number",
    ]
    assert ap.validate_settings({"temperature": None, "top_p": 0.9}, rows) == []


def test_every_tunable_is_a_real_config_and_request_field() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.lm_provider_types import LMProviderInfo, LMProviderRequest

    for name in ap.TUNABLE_NAMES:
        assert name in LMProviderRequest.model_fields
        assert name in LMProviderInfo.model_fields
        if name != "parallel":  # a load-only setting
            assert name in LMProviderConfig.__dataclass_fields__
