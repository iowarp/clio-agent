"""Unit tests for :mod:`clio_agent.providers.capabilities.link` (brief 5.4)."""

from __future__ import annotations

from clio_agent.providers.capabilities.link import deployment_model_key_fact, link_model


def test_empty_wire_id_is_no_link() -> None:
    result = link_model("")
    assert result.model_key is None
    assert result.rule == "no_link"


def test_exact_cloud_model_id_known_to_catalogs_wins_first() -> None:
    result = link_model("gpt-4o", known_cloud_id=lambda wire_id: wire_id == "gpt-4o")
    assert result.model_key == "gpt-4o"
    assert result.rule == "cloud_catalog"


def test_known_cloud_id_predicate_never_called_with_a_guess() -> None:
    """The predicate is only ever asked about the EXACT wire id, never a substring."""
    seen: list[str] = []

    def _predicate(wire_id: str) -> bool:
        seen.append(wire_id)
        return False

    link_model("some/local-model:Q4", known_cloud_id=_predicate)
    assert seen == ["some/local-model:Q4"]


def test_a_broken_known_cloud_id_predicate_degrades_to_the_next_rule() -> None:
    def _boom(_wire_id: str) -> bool:
        raise RuntimeError("catalog offline")

    result = link_model("hf.co/Qwen/Qwen3-8B-GGUF", known_cloud_id=_boom)
    assert result.rule == "hf_ollama_pull"  # fell through to the next rule, not a crash


def test_vllm_root_is_trusted_directly() -> None:
    result = link_model("served-alias", vllm_root="meta-llama/Llama-3.1-8B-Instruct")
    assert result.model_key == "meta-llama/Llama-3.1-8B-Instruct"
    assert result.rule == "hf_vllm_root"


def test_ollama_hf_co_pull_name_extracts_the_repo() -> None:
    result = link_model("hf.co/Qwen/Qwen3-8B-GGUF:Q4_K_M")
    assert result.model_key == "Qwen/Qwen3-8B-GGUF"
    assert result.rule == "hf_ollama_pull"


def test_llama_cpp_router_id_extracts_the_repo_from_the_wire_id() -> None:
    result = link_model("unsloth/Qwen3-32B-GGUF:Q4_K_M")
    assert result.model_key == "unsloth/Qwen3-32B"
    assert result.rule == "hf_llama_cpp_router"


def test_llama_cpp_router_id_extracts_the_repo_from_the_gguf_filename() -> None:
    """A wire id that doesn't itself look like a router id, but the GGUF filename does."""
    result = link_model("local-model", gguf_filename="unsloth/Qwen3-32B-GGUF:Q4_K_M")
    assert result.model_key == "unsloth/Qwen3-32B"
    assert result.rule == "hf_llama_cpp_router"


def test_overlay_match_is_consulted_last_when_provided() -> None:
    result = link_model(
        "Weird-Custom-Name", overlay_match=lambda wire_id: "qwen3.8" if wire_id else None
    )
    assert result.model_key == "qwen3.8"
    assert result.rule == "overlay_match"


def test_overlay_match_omitted_is_not_the_same_as_overlay_found_nothing() -> None:
    """No overlay wired (P6 not landed yet) -> no_link; never silently treated as evidence."""
    result = link_model("Weird-Custom-Name")
    assert result.rule == "no_link"


def test_a_broken_overlay_match_degrades_to_no_link() -> None:
    def _boom(_wire_id: str) -> str | None:
        raise RuntimeError("overlay corrupt")

    result = link_model("Weird-Custom-Name", overlay_match=_boom)
    assert result.rule == "no_link"
    assert result.model_key is None


def test_never_guesses_from_a_partial_name() -> None:
    """A wire id that merely CONTAINS a slash-shaped substring is not linked without a real rule match."""
    result = link_model("local-model-not-a-real-repo-name")
    assert result.rule == "no_link"
    assert result.model_key is None


def test_rule_order_cloud_catalog_beats_hf_shapes() -> None:
    """When multiple rules COULD match, the brief's own order wins -- cloud catalog first."""
    result = link_model(
        "hf.co/some/repo",
        known_cloud_id=lambda wire_id: True,  # pretend a catalog claims this exact id
    )
    assert result.rule == "cloud_catalog"


def test_deployment_model_key_fact_uses_the_real_link_when_one_matches() -> None:
    fact = deployment_model_key_fact("hf.co/Qwen/Qwen3-8B-GGUF", observed_at="t")
    assert fact.value == "Qwen/Qwen3-8B-GGUF"
    assert fact.source == "server_report"
    assert "link rule=hf_ollama_pull" in fact.detail


def test_deployment_model_key_fact_falls_back_to_the_wire_id_with_no_link() -> None:
    fact = deployment_model_key_fact("local-model", observed_at="t")
    assert fact.value == "local-model"
    assert "no link rule matched" in fact.detail
