"""Focused wiring tests for catalog-selected LiteLLM providers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.factory import _provider_lm_kwargs, _resolve_model_name
from clio_agent.providers import credentials
from clio_agent.providers.catalog import get_provider


@pytest.mark.parametrize(
    ("provider_id", "model", "prefix", "endpoint", "options"),
    [
        ("openai", "gpt-4o-mini", "openai/", "https://api.openai.com/v1", {}),
        (
            "azure_openai",
            "deployment",
            "azure/",
            "https://YOUR-RESOURCE.openai.azure.com/",
            {"api_version": "2024-10-21"},
        ),
        ("anthropic", "claude-sonnet-4", "anthropic/", "https://api.anthropic.com/v1", {}),
        (
            "gemini",
            "gemini-2.5-flash",
            "gemini/",
            "https://generativelanguage.googleapis.com/v1beta",
            {},
        ),
        (
            "vertex_ai",
            "gemini-2.5-flash",
            "vertex_ai/",
            "https://aiplatform.googleapis.com",
            {"vertex_project": "science", "vertex_location": "us-central1"},
        ),
        (
            "bedrock",
            "anthropic.claude-v2",
            "bedrock/",
            "https://bedrock-runtime.us-east-1.amazonaws.com",
            {"aws_region_name": "us-east-1"},
        ),
        (
            "openrouter",
            "anthropic/claude-sonnet-4",
            "openrouter/",
            "https://openrouter.ai/api/v1",
            {},
        ),
        (
            "nvidia_nim",
            "meta/llama",
            "nvidia_nim/",
            "https://integrate.api.nvidia.com/v1",
            {},
        ),
        ("vllm", "Qwen/Qwen3-8B", "hosted_vllm/", "http://127.0.0.1:8000/v1", {}),
        (
            "argonne_sophia",
            "openai/gpt-oss-120b",
            "hosted_vllm/",
            "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1",
            {},
        ),
        ("ollama", "qwen3", "ollama_chat/", "http://127.0.0.1:11434/v1", {}),
        ("lm_studio", "local", "openai/", "http://127.0.0.1:1234/v1", {}),
        ("llama_cpp", "local-model", "openai/", "http://127.0.0.1:8088/v1", {}),
    ],
)
def test_catalog_provider_routes_to_exact_litellm_prefix(
    provider_id: str,
    model: str,
    prefix: str,
    endpoint: str,
    options: dict[str, str],
) -> None:
    config = LMProviderConfig(
        provider=provider_id,  # type: ignore[arg-type]
        provider_id=provider_id,
        model=model,
        api_key="test",
        provider_options=options,
    )

    assert config.provider_id == provider_id
    kwargs = _provider_lm_kwargs(config)
    assert _resolve_model_name(config).startswith(prefix)
    assert config.api_base == endpoint
    for key, value in options.items():
        assert kwargs[key] == value


def test_legacy_catalog_id_recovers_identity_before_runtime_kind() -> None:
    config = LMProviderConfig(provider="openrouter", model="openai/gpt-oss-120b", api_key="test")  # type: ignore[arg-type]

    assert config.provider_id == "openrouter"
    assert config.provider == "openai"
    assert _resolve_model_name(config) == "openrouter/openai/gpt-oss-120b"
    assert get_provider("argonne_local_vllm") == get_provider("vllm")


@pytest.mark.parametrize(
    ("provider_id", "env_name"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("azure_openai", "AZURE_API_KEY"),
        ("gemini", "GOOGLE_API_KEY"),
        ("nvidia_nim", "NVIDIA_NIM_API_KEY"),
    ],
)
def test_credentials_resolve_by_stable_provider_id(provider_id: str, env_name: str) -> None:
    with patch.dict("os.environ", {env_name: "secret"}, clear=True):
        assert credentials.resolve(provider_id) == "secret"


def test_provider_options_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unsupported options"):
        LMProviderConfig(
            provider="azure_openai",  # type: ignore[arg-type]
            provider_id="azure_openai",
            model="deployment",
            api_key="test",
            provider_options={"shell": "no"},
        )
