"""The three identity keys (model-capabilities brief Part 3).

``provider_kind`` chooses a dialect, never an identity. These tests pin the
key builders every cache / snapshot / handshake-cache entry must use instead,
and the exact scenario the brief calls out: two presets that share a kind but
have different ``api_base``s must never collide on the same key, and a
changed ``api_base`` for one provider_id must produce a different key from
its old one.
"""

from __future__ import annotations

from clio_agent.providers.identity import deployment_key, endpoint_key


def test_endpoint_key_pairs_provider_id_with_normalized_api_base() -> None:
    assert endpoint_key("llama_cpp", "http://127.0.0.1:8088/v1/") == (
        "llama_cpp",
        "http://127.0.0.1:8088/v1",
    )


def test_endpoint_key_of_two_same_kind_presets_never_collide() -> None:
    """openrouter and llama_cpp share the catalog kind "openai" but must key apart."""
    openrouter = endpoint_key("openrouter", "https://openrouter.ai/api/v1")
    llama_cpp = endpoint_key("llama_cpp", "http://127.0.0.1:8088/v1")
    assert openrouter != llama_cpp
    assert openrouter[0] == "openrouter"
    assert llama_cpp[0] == "llama_cpp"


def test_endpoint_key_of_two_argonne_presets_never_collide() -> None:
    """Sophia and Metis share the catalog kind "argonne" but are different endpoints."""
    sophia = endpoint_key(
        "argonne_sophia", "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1"
    )
    metis = endpoint_key(
        "argonne_metis", "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"
    )
    assert sophia != metis


def test_endpoint_key_changes_with_a_changed_api_base() -> None:
    """Moving a provider's configured endpoint must never key as the old one."""
    before = endpoint_key("llama_cpp", "http://127.0.0.1:8088/v1")
    after = endpoint_key("llama_cpp", "http://127.0.0.1:9000/v1")
    assert before != after


def test_endpoint_key_is_stable_across_equivalent_spellings() -> None:
    assert endpoint_key("ollama", "http://127.0.0.1:11434/") == endpoint_key(
        "ollama", "http://127.0.0.1:11434"
    )


def test_deployment_key_adds_the_wire_model_id() -> None:
    assert deployment_key("llama_cpp", "http://127.0.0.1:8088/v1/", "qwen3-8b") == (
        "llama_cpp",
        "http://127.0.0.1:8088/v1",
        "qwen3-8b",
    )


def test_deployment_key_differs_by_model_id_on_the_same_endpoint() -> None:
    base = "http://127.0.0.1:8088/v1"
    assert deployment_key("llama_cpp", base, "model-a") != deployment_key(
        "llama_cpp", base, "model-b"
    )
