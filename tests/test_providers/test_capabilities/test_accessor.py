"""Unit tests for :mod:`clio_agent.providers.capabilities.accessor`.

Covers the one-accessor contract: it combines whatever is in the store, caches
the result, and recomputes when any contributing record changes (brief 5.5's
"nobody edits its result by hand" plus the cache-invalidation requirement).
"""

from __future__ import annotations

import pytest

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import clear_cache, get_effective_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
)

_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_everything():
    invalidation.clear_all()
    clear_cache()
    yield
    invalidation.clear_all()
    clear_cache()


def test_cold_identity_degrades_gracefully_never_raises() -> None:
    effective = get_effective_capabilities("nope", "http://nowhere", "no-model")
    assert not effective.context.known
    assert effective.model_key is None


def test_joins_model_and_deployment_through_the_link() -> None:
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="qwen3-8b",
            context_max=Fact(value=128_000, source="litellm", observed_at=_NOW),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="ollama-local",
            api_base="http://127.0.0.1:11434",
            model_id="qwen3:8b",
            model_key=Fact(value="qwen3-8b", source="server_report", observed_at=_NOW),
            context_served=Fact(value=32_768, source="server_report", observed_at=_NOW),
        )
    )
    effective = get_effective_capabilities("ollama-local", "http://127.0.0.1:11434", "qwen3:8b")
    assert effective.context.value == 32_768  # the smaller of the two
    assert effective.model_key == "qwen3-8b"


def test_result_is_cached_across_identical_calls() -> None:
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m")
    )
    first = get_effective_capabilities("p", "a", "m")
    second = get_effective_capabilities("p", "a", "m")
    assert first is second


def test_cache_recomputes_when_the_deployment_record_changes() -> None:
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="p",
            api_base="a",
            model_id="m",
            tools_enabled=Fact(value=False, source="server_report", observed_at=_NOW),
        )
    )
    before = get_effective_capabilities("p", "a", "m")
    assert before.tools.value is False

    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="p",
            api_base="a",
            model_id="m",
            tools_enabled=Fact(value=True, source="server_report", observed_at=_NOW),
        )
    )
    after = get_effective_capabilities("p", "a", "m")
    assert after.tools.value is True
    assert after is not before


def test_cache_recomputes_when_the_model_record_changes() -> None:
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="m-key", context_max=Fact(value=1000, source="litellm", observed_at=_NOW)
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="p",
            api_base="a",
            model_id="m",
            model_key=Fact(value="m-key", source="server_report", observed_at=_NOW),
        )
    )
    before = get_effective_capabilities("p", "a", "m")
    assert before.context.value == 1000

    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key="m-key", context_max=Fact(value=2000, source="litellm", observed_at=_NOW)
        )
    )
    after = get_effective_capabilities("p", "a", "m")
    assert after.context.value == 2000


def test_endpoint_key_isolation_two_api_bases_never_cross_talk() -> None:
    """brief 9.2: api_base-keyed isolation, through the accessor end to end."""
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="llama_cpp",
            api_base="http://127.0.0.1:8088",
            model_id="m",
            tools_enabled=Fact(value=True, source="server_report", observed_at=_NOW),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="llama_cpp",
            api_base="http://127.0.0.1:9000",
            model_id="m",
            tools_enabled=Fact(value=False, source="server_report", observed_at=_NOW),
        )
    )
    first = get_effective_capabilities("llama_cpp", "http://127.0.0.1:8088", "m")
    second = get_effective_capabilities("llama_cpp", "http://127.0.0.1:9000", "m")
    assert first.tools.value is True
    assert second.tools.value is False


def test_clear_cache_forces_a_fresh_combine() -> None:
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(provider_id="p", api_base="a", model_id="m")
    )
    first = get_effective_capabilities("p", "a", "m")
    clear_cache()
    second = get_effective_capabilities("p", "a", "m")
    assert first is not second
    assert first == second
