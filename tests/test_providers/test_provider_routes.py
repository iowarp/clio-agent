"""Focused wiring tests for catalog-selected LiteLLM providers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.factory import _connection_kwargs, _provider_lm_kwargs, _resolve_model_name
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
        # No /v1: LiteLLM's native ollama_chat provider appends its own
        # /api/chat to the base (#1413).
        ("ollama", "qwen3", "ollama_chat/", "http://127.0.0.1:11434", {}),
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


def test_ollama_connection_strips_saved_v1_suffix() -> None:
    """A config saved before #1413 (api_base still carrying /v1) is repaired.

    ``_connection_kwargs`` -- not just the catalog default -- must strip a
    trailing ``/v1`` for the resolved ``ollama_chat`` prefix, or an existing
    saved config keeps 404ing after the fix ships.
    """
    config = LMProviderConfig(
        provider="ollama",  # type: ignore[arg-type]
        provider_id="ollama",
        model="qwen3",
        api_base="http://127.0.0.1:11434/v1",
        api_key="ollama",
    )
    assert _connection_kwargs(config) == {"api_base": "http://127.0.0.1:11434"}


def test_ollama_connection_leaves_v1_suffix_for_non_ollama_dialects() -> None:
    """Only the resolved ``ollama_chat`` prefix gets the /v1 strip."""
    config = LMProviderConfig(
        provider="llama_cpp",  # type: ignore[arg-type]
        provider_id="llama_cpp",
        model="local-model",
        api_base="http://127.0.0.1:8088/v1",
        api_key="llama-cpp",
    )
    assert _connection_kwargs(config) == {"api_base": "http://127.0.0.1:8088/v1"}


def test_ollama_connection_combined_url_matches_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection this factory builds must combine into the REAL Ollama URL.

    Regression for #1413: the previous tests checked the prefix and the base
    URL separately, which is exactly how a base that still doubled into
    ``/v1/api/chat`` slipped through. This calls LiteLLM's own
    ``OllamaChatConfig.get_complete_url`` (litellm 1.91.3) on what the factory
    actually produces.
    """
    from litellm.llms.ollama.chat.transformation import OllamaChatConfig

    config = LMProviderConfig(provider="ollama", model="qwen3", api_key="ollama")  # type: ignore[arg-type]
    connection = _connection_kwargs(config)
    model_name = _resolve_model_name(config)
    assert model_name == "ollama_chat/qwen3"

    url = OllamaChatConfig().get_complete_url(
        api_base=connection.get("api_base"),
        api_key=config.api_key,
        model=model_name,
        optional_params={},
        litellm_params={},
    )
    assert url == "http://127.0.0.1:11434/api/chat"


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


@pytest.mark.parametrize(
    "provider_id",
    [
        "openai",
        "azure_openai",
        "anthropic",
        "gemini",
        "vertex_ai",
        "bedrock",
        "openrouter",
        "nvidia_nim",
        "vllm",
        "argonne_sophia",
        "argonne_metis",
        "llama_cpp",
    ],
)
def test_litellm_transport_routes_accept_image_parts(provider_id: str) -> None:
    provider = get_provider(provider_id)
    assert provider is not None
    assert provider.supports_vision is True
