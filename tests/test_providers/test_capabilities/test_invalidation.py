"""Unit tests for :mod:`clio_agent.providers.capabilities.invalidation` (brief 5.6).

Covers separate invalidation per record and ``api_base``-keyed isolation
(brief 9.2).
"""

from __future__ import annotations

import pytest

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    EndpointCapabilities,
    Fact,
    ModelCapabilities,
)

_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_store():
    invalidation.clear_all()
    yield
    invalidation.clear_all()


def test_record_and_get_model_capabilities_roundtrips() -> None:
    caps = ModelCapabilities(model_key="qwen3-8b")
    invalidation.record_model_capabilities(caps)
    assert invalidation.get_model_capabilities("qwen3-8b") is caps


def test_get_model_capabilities_with_no_key_returns_none() -> None:
    assert invalidation.get_model_capabilities(None) is None
    assert invalidation.get_model_capabilities("") is None


def test_record_and_get_endpoint_capabilities_roundtrips() -> None:
    caps = EndpointCapabilities(provider_id="ollama", api_base="http://localhost:11434")
    invalidation.record_endpoint_capabilities(caps)
    assert invalidation.get_endpoint_capabilities(("ollama", "http://localhost:11434")) is caps


def test_api_base_keyed_isolation_for_endpoints() -> None:
    """Two api_base values for the SAME provider_id never collide (brief Part 3)."""
    base_a = EndpointCapabilities(provider_id="llama_cpp", api_base="http://127.0.0.1:8088")
    base_b = EndpointCapabilities(provider_id="llama_cpp", api_base="http://127.0.0.1:9000")
    invalidation.record_endpoint_capabilities(base_a)
    invalidation.record_endpoint_capabilities(base_b)

    assert invalidation.get_endpoint_capabilities(("llama_cpp", "http://127.0.0.1:8088")) is base_a
    assert invalidation.get_endpoint_capabilities(("llama_cpp", "http://127.0.0.1:9000")) is base_b


def test_api_base_keyed_isolation_for_deployments() -> None:
    dep_a = DeploymentCapabilities(
        provider_id="llama_cpp", api_base="http://127.0.0.1:8088", model_id="m"
    )
    dep_b = DeploymentCapabilities(
        provider_id="llama_cpp", api_base="http://127.0.0.1:9000", model_id="m"
    )
    invalidation.record_deployment_capabilities(dep_a)
    invalidation.record_deployment_capabilities(dep_b)

    assert (
        invalidation.get_deployment_capabilities(("llama_cpp", "http://127.0.0.1:8088", "m"))
        is dep_a
    )
    assert (
        invalidation.get_deployment_capabilities(("llama_cpp", "http://127.0.0.1:9000", "m"))
        is dep_b
    )


def test_invalidate_model_drops_only_that_model() -> None:
    invalidation.record_model_capabilities(ModelCapabilities(model_key="a"))
    invalidation.record_model_capabilities(ModelCapabilities(model_key="b"))
    invalidation.invalidate_model("a")
    assert invalidation.get_model_capabilities("a") is None
    assert invalidation.get_model_capabilities("b") is not None


def test_invalidate_deployment_drops_only_that_deployment() -> None:
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m1")
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m2")
    )
    invalidation.invalidate_deployment(("p", "a", "m1"))
    assert invalidation.get_deployment_capabilities(("p", "a", "m1")) is None
    assert invalidation.get_deployment_capabilities(("p", "a", "m2")) is not None


def test_invalidate_endpoint_drops_every_deployment_under_it() -> None:
    """brief 5.6: an endpoint fingerprint change (server restart) invalidates its deployments too."""
    invalidation.record_endpoint_capabilities(EndpointCapabilities(provider_id="p", api_base="a"))
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m1")
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m2")
    )
    # A DIFFERENT api_base's deployment must survive.
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="b", model_id="m1")
    )

    invalidation.invalidate_endpoint(("p", "a"))

    assert invalidation.get_endpoint_capabilities(("p", "a")) is None
    assert invalidation.get_deployment_capabilities(("p", "a", "m1")) is None
    assert invalidation.get_deployment_capabilities(("p", "a", "m2")) is None
    assert invalidation.get_deployment_capabilities(("p", "b", "m1")) is not None


def test_invalidate_endpoint_does_not_touch_model_records() -> None:
    """Model records are not owned by any one endpoint -- untouched by an endpoint invalidation."""
    invalidation.record_model_capabilities(ModelCapabilities(model_key="shared-model"))
    invalidation.record_endpoint_capabilities(EndpointCapabilities(provider_id="p", api_base="a"))
    invalidation.invalidate_endpoint(("p", "a"))
    assert invalidation.get_model_capabilities("shared-model") is not None


def test_invalidate_provider_drops_every_api_base_variant() -> None:
    invalidation.record_endpoint_capabilities(EndpointCapabilities(provider_id="p", api_base="a"))
    invalidation.record_endpoint_capabilities(EndpointCapabilities(provider_id="p", api_base="b"))
    invalidation.record_endpoint_capabilities(
        EndpointCapabilities(provider_id="other", api_base="a")
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m")
    )

    invalidation.invalidate_provider("p")

    assert invalidation.get_endpoint_capabilities(("p", "a")) is None
    assert invalidation.get_endpoint_capabilities(("p", "b")) is None
    assert invalidation.get_deployment_capabilities(("p", "a", "m")) is None
    assert invalidation.get_endpoint_capabilities(("other", "a")) is not None


def test_clear_all_wipes_every_record_type() -> None:
    invalidation.record_model_capabilities(ModelCapabilities(model_key="m"))
    invalidation.record_endpoint_capabilities(EndpointCapabilities(provider_id="p", api_base="a"))
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m")
    )
    invalidation.clear_all()
    assert invalidation.get_model_capabilities("m") is None
    assert invalidation.get_endpoint_capabilities(("p", "a")) is None
    assert invalidation.get_deployment_capabilities(("p", "a", "m")) is None


def test_recording_the_same_key_overwrites_not_duplicates() -> None:
    first = ModelCapabilities(
        model_key="m", tools=Fact(value=True, source="server_report", observed_at=_NOW)
    )
    second = ModelCapabilities(
        model_key="m", tools=Fact(value=False, source="server_report", observed_at=_NOW)
    )
    invalidation.record_model_capabilities(first)
    invalidation.record_model_capabilities(second)
    assert invalidation.get_model_capabilities("m") is second
