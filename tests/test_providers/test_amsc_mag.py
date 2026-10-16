"""Offline MAG preset, credential, routing and authenticated discovery regressions."""

from __future__ import annotations

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.lm.factory import _resolve_model_name
from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.catalog_types import Provider
from clio_agent.providers.handshake import get_handshake_for
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import AuthState, ConnectivityState
from tests.test_providers.test_handshake_openai_compat import FakeAsyncClient, FakeResponse


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_id", "context", "output"),
    [
        ("claude-opus-5-5-high@amazon", 1_000_000, 128_000),
        ("claude-opus-5-5-compact@amazon", 1_000_000, 128_000),
        ("gpt-6-luna@openai", 922_000, 128_000),
        ("llama-4-scout@amazon", 10_000_000, 8192),
        ("opaque-deployment", 128, 64),
    ],
)
async def test_mag_reports_independent_deployment_limits(
    mag_provider: Provider, model_id: str, context: int, output: int
) -> None:
    """The actual MAG handshake feeds reported limits into the shared capability view."""
    raw = {"id": model_id, "max_input_tokens": context, "max_output_tokens": output}
    ctx = HandshakeContext(
        provider_id=mag_provider.id,
        provider_kind=mag_provider.provider_kind,
        api_base=mag_provider.api_base,
        api_key="unit-mag-pat",
        allow_external_sources=False,
    )
    handshake = get_handshake_for(mag_provider.provider_kind, mag_provider)
    client = FakeAsyncClient(routes={})
    facts = await handshake.discover_model_config(client, ctx, raw)
    effective = combine_capabilities(facts.model, None, facts.deployment)
    assert facts.discovered.id == model_id
    assert facts.discovered.raw == raw
    assert effective.context.value == context
    assert effective.output_max.value == output
    assert effective.context.source == effective.output_max.source == "server_report"
    assert effective.context.decided_by == effective.output_max.decided_by == "deployment"
    assert "max_input_tokens" in facts.deployment.context_served.detail
    assert "max_output_tokens" in facts.deployment.output_max.detail
    assert not client.calls  # /models already supplied the limits; no per-model requests.


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, True, False, 0, -1, "1000000", 1.5])
async def test_mag_unknown_limits_are_independent(mag_provider: Provider, invalid: object) -> None:
    """Bad/missing input limits never borrow an output limit or invent a context tag."""
    ctx = HandshakeContext(
        provider_id=mag_provider.id,
        provider_kind=mag_provider.provider_kind,
        api_base=mag_provider.api_base,
        allow_external_sources=False,
    )
    handshake = get_handshake_for(mag_provider.provider_kind, mag_provider)
    for input_value, output_value in [(invalid, 128_000), (1_000_000, invalid)]:
        facts = await handshake.discover_model_config(
            FakeAsyncClient(routes={}),
            ctx,
            {
                "id": "unknown-model",
                "max_input_tokens": input_value,
                "max_output_tokens": output_value,
            },
        )
        assert facts.deployment.context_served.value == (
            1_000_000 if input_value == 1_000_000 else None
        )
        assert facts.deployment.output_max.value == (128_000 if output_value == 128_000 else None)


@pytest.fixture
def mag_provider() -> Provider:
    """Return the production MAG preset without contacting the service."""
    provider = get_provider("amsc_mag")
    assert provider is not None
    return provider


@pytest.mark.parametrize("model", ["gpt-6-luna:high:flex@openai", "claude-sonnet@amazon"])
def test_mag_config_uses_own_token_and_preserves_model_modifiers(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """The generic OpenAI transport must retain MAG identity and its exact model id."""
    monkeypatch.setenv("AMSC_TOKEN", "unit-mag-pat")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    config = LMProviderConfig(provider_id="amsc_mag", model=model)
    assert config.provider_id == "amsc_mag"
    assert config.provider == "openai"
    assert config.api_base == "https://mag.genesis.americansciencecloud.org/v1"
    assert config.api_key == "unit-mag-pat"
    assert _resolve_model_name(config) == f"openai/{model}"


def test_mag_does_not_reuse_an_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing MAG credential cannot silently spend an unrelated provider's key."""
    monkeypatch.delenv("AMSC_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-key")
    config = LMProviderConfig(provider_id="amsc_mag", model="claude-sonnet@amazon")
    assert config.api_key == ""


@pytest.mark.asyncio
async def test_mag_discovers_project_models_with_bearer_pat(mag_provider: Provider) -> None:
    """The authenticated project listing is authoritative, including modifiers."""
    assert mag_provider.suggested_model == ""
    assert mag_provider.supports_live_catalog
    assert mag_provider.key_check_path is None
    model_ids = ["gpt-6-luna:high@openai", "claude-sonnet@amazon"]
    url = f"{mag_provider.api_base}/models"
    client = FakeAsyncClient(
        routes={url: FakeResponse(200, {"data": [{"id": value} for value in model_ids]})}
    )
    context = HandshakeContext(
        provider_id=mag_provider.id,
        provider_kind=mag_provider.provider_kind,
        api_base=mag_provider.api_base,
        api_key="unit-mag-pat",
        allow_external_sources=False,
    )
    handshake = get_handshake_for(mag_provider.provider_kind, mag_provider)
    connectivity = await handshake.check_connectivity(client, context)
    assert connectivity.connectivity is ConnectivityState.OK
    assert connectivity.auth is AuthState.OK
    models = await handshake.discover_models(client, context)
    assert [model["id"] for model in models] == model_ids
    assert client.calls
    assert all(
        path == url and headers == {"Authorization": "Bearer unit-mag-pat"}
        for path, headers in client.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_mag_rejected_pat_is_not_reported_as_authenticated(
    mag_provider: Provider, status: int
) -> None:
    """An expired PAT or refused allocation stays a typed authentication rejection."""
    url = f"{mag_provider.api_base}/models"
    client = FakeAsyncClient(routes={url: FakeResponse(status, {"error": "refused"})})
    context = HandshakeContext(
        provider_id=mag_provider.id,
        provider_kind=mag_provider.provider_kind,
        api_base=mag_provider.api_base,
        api_key="unit-invalid-pat",
        allow_external_sources=False,
    )
    handshake = get_handshake_for(mag_provider.provider_kind, mag_provider)
    connectivity = await handshake.check_connectivity(client, context)
    assert connectivity.connectivity is ConnectivityState.OK
    assert connectivity.auth is AuthState.REJECTED
    assert client.calls == [(url, {"Authorization": "Bearer unit-invalid-pat"})]
