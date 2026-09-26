"""Unit tests for :mod:`clio_agent.providers.capabilities.records` (brief Part 4)."""

from __future__ import annotations

from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
    ThinkingSpec,
    modalities_from_capabilities,
    no_restriction,
    unknown,
)


def test_fact_known_reflects_value_presence() -> None:
    assert Fact(value=42, source="server_report", observed_at="t").known is True
    assert Fact(value=None, source="unknown", observed_at="").known is False


def test_unknown_fact_has_no_value_and_unknown_source() -> None:
    fact = unknown()
    assert fact.value is None
    assert fact.source == "unknown"
    assert fact.known is False


def test_no_restriction_is_true_with_source_dialect() -> None:
    """Brief 4.2: "no restriction" is recorded as True with source=dialect, never left unknown."""
    fact = no_restriction(True, observed_at="t", detail="cloud API: no projector to lack")
    assert fact.value is True
    assert fact.source == "dialect"
    assert fact.known is True


def test_modalities_from_capabilities_always_includes_text() -> None:
    assert modalities_from_capabilities([]) == frozenset({"text"})
    assert modalities_from_capabilities(None) == frozenset({"text"})


def test_modalities_from_capabilities_normalizes_known_aliases() -> None:
    result = modalities_from_capabilities(["vision", "PDF_input", "Audio", "unknown-thing"])
    assert result == frozenset({"text", "image", "pdf", "audio"})
    # An unrecognized capability string is ignored, not guessed at.
    assert "unknown-thing" not in result


def test_modalities_from_capabilities_dash_and_case_insensitive() -> None:
    assert "image" in modalities_from_capabilities(["Image-Input"])
    assert "video" in modalities_from_capabilities(["VIDEO"])


def test_model_capabilities_defaults_are_unknown() -> None:
    caps = ModelCapabilities(model_key="qwen3-8b")
    assert not caps.context_max.known
    assert not caps.tools.known
    assert not caps.thinking.known
    assert caps.model_key == "qwen3-8b"


def test_endpoint_capabilities_defaults_are_unknown_and_fingerprint_empty() -> None:
    endpoint = EndpointCapabilities(provider_id="ollama", api_base="http://localhost:11434")
    assert not endpoint.accepted_params.known
    assert endpoint.fingerprint == ""
    assert endpoint.multi_model is False


def test_deployment_capabilities_defaults_are_unknown() -> None:
    deployment = DeploymentCapabilities(
        provider_id="ollama", api_base="http://localhost:11434", model_id="qwen3:8b"
    )
    assert not deployment.model_key.known
    assert not deployment.context_served.known
    assert not deployment.tools_enabled.known


def test_thinking_spec_defaults_to_none_mechanism() -> None:
    spec = ThinkingSpec(mechanism="effort_levels", levels=("low", "medium", "high"))
    assert spec.mechanism == "effort_levels"
    assert spec.levels == ("low", "medium", "high")
    assert spec.template_kwarg is None
