"""Unit tests for :mod:`clio_agent.providers.capabilities.endpoint` (brief 5.2)."""

from __future__ import annotations

from clio_agent.providers.capabilities.endpoint import (
    build_endpoint_capabilities,
    dialect_for_provider,
    resolve_accepted_params,
)


def test_resolve_accepted_params_uses_litellm_when_mapped() -> None:
    fact = resolve_accepted_params("ollama", "qwen3:8b", custom_llm_provider="ollama_chat")
    assert fact.known
    assert fact.source == "litellm"
    assert "temperature" in fact.value


def test_resolve_accepted_params_llama_cpp_supplement_fills_the_litellm_gap() -> None:
    """llama.cpp has no dedicated LiteLLM provider -- the supplement table is the only source."""
    fact = resolve_accepted_params("llama_cpp", "x", custom_llm_provider="does-not-exist-provider")
    assert fact.known
    assert fact.value == frozenset(
        {"top_k", "min_p", "repeat_penalty", "reasoning_effort", "chat_template_kwargs"}
    )
    assert fact.source == "dialect"


def test_resolve_accepted_params_vllm_supplement_adds_to_litellm() -> None:
    fact = resolve_accepted_params("vllm", "x", custom_llm_provider="hosted_vllm")
    assert fact.known
    # LiteLLM's own hosted_vllm list plus the supplement's extras.
    assert {"top_k", "min_p", "repetition_penalty", "chat_template_kwargs"} <= fact.value
    assert "temperature" in fact.value  # still carries LiteLLM's own mapped params


def test_resolve_accepted_params_lm_studio_never_gets_chat_template_kwargs() -> None:
    """Brief 5.2: LM Studio must NEVER receive chat_template_kwargs."""
    fact = resolve_accepted_params("lm_studio", "x", custom_llm_provider="openai")
    assert fact.known
    assert "chat_template_kwargs" not in fact.value


def test_resolve_accepted_params_total_miss_is_unknown() -> None:
    fact = resolve_accepted_params("some_unknown_dialect", "x", custom_llm_provider="nope")
    assert not fact.known


def test_dialect_for_provider_kind_shortcuts() -> None:
    assert dialect_for_provider("ollama", "ollama_chat", "ollama") == "ollama"
    assert dialect_for_provider("lm_studio", "openai", "lm_studio") == "lm_studio"
    assert dialect_for_provider("argonne", "openai", "argonne_sophia") == "vllm"


def test_dialect_for_provider_hosted_vllm_prefix() -> None:
    assert dialect_for_provider("openai", "hosted_vllm", "vllm_local") == "vllm"


def test_dialect_for_provider_llama_cpp_from_provider_id() -> None:
    assert dialect_for_provider("openai", "openai", "llama_cpp") == "llama_cpp"
    assert dialect_for_provider("openai", "openai", "my-llama.cpp-box") == "llama_cpp"


def test_dialect_for_provider_falls_back_to_litellm_prefix() -> None:
    assert dialect_for_provider("openai", "openrouter", "openrouter") == "openrouter"


def test_build_endpoint_capabilities_is_pure_and_populates_dialect_tables() -> None:
    endpoint = build_endpoint_capabilities(
        "ollama-local",
        "http://127.0.0.1:11434",
        "ollama",
        "qwen3:8b",
        custom_llm_provider="ollama_chat",
    )
    assert endpoint.provider_id == "ollama-local"
    assert endpoint.dialect == "ollama"
    assert endpoint.thinking_controls.value == frozenset({"think"})
    assert not endpoint.server_version.known  # nothing supplied this run


def test_build_endpoint_capabilities_with_no_thinking_table_entry_is_unknown() -> None:
    endpoint = build_endpoint_capabilities(
        "mystery", "http://x", "totally-unknown-dialect", "m", custom_llm_provider="nope"
    )
    assert not endpoint.thinking_controls.known
    assert not endpoint.structured_output_modes.known
