"""Offline tests for ``OpenAICompatHandshake`` and ``NoOpHandshake``.

No network is touched: a fake ``httpx``-shaped async client feeds canned ``/models``
and ``/api/tags`` payloads, and assertions confirm the no-op handshake never calls
the client at all. ``allow_external_sources=False`` is used so the base
``enrich_capabilities`` step does not consult the (separately owned) context-source
factory — these tests cover only the code in ``openai_compat.py`` / ``noop.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.catalog_types import ModelEntry, Provider
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
)
from clio_agent.providers.handshake.noop import NoOpHandshake
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

FIXTURES = Path(__file__).parent / "fixtures" / "handshake"

#: model-capabilities brief 9.1: no REAL provider in the registry carries a
#: populated ``model_catalog`` any more (claude_code's own former exception
#: moved to the maintained catalog document, cli_catalog.py's
#: ``ClaudeCodeCatalogHandshake._fallback_models``). ``NoOpHandshake``'s
#: generic documented-modality-preservation behavior is still real, intended
#: functionality for a FUTURE no-HTTP-surface CLI provider though (its own
#: docstring says so), so these tests exercise it against a synthetic
#: provider row instead of leaning on a real one that no longer has the data.
_SYNTHETIC_CLI_PROVIDER = Provider(
    id="a_future_cli_provider",
    label="A Future CLI Provider",
    description="test-only synthetic provider for NoOpHandshake's generic contract",
    provider_kind="claude_code",
    litellm_prefix="claude_code",
    api_base="a-future-cli-provider://sdk",
    suggested_model="",
    requires_api_key=False,
    model_catalog=(
        ModelEntry("fable", "Fable", "", ("text", "image")),
        ModelEntry("sonnet", "Sonnet", "", ("text", "image")),
        ModelEntry("opus", "Opus", "", ("text", "image")),
        ModelEntry("haiku", "Haiku", "", ("text", "image")),
    ),
)


def _patch_synthetic_cli_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "clio_agent.providers.catalog.get_provider",
        lambda provider_id: _SYNTHETIC_CLI_PROVIDER
        if provider_id == _SYNTHETIC_CLI_PROVIDER.id
        else None,
    )


def _load(name: str) -> Any:
    """Load a captured JSON fixture from disk (offline)."""
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    """A minimal stand-in for ``httpx.Response`` (status + JSON body)."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeAsyncClient:
    """A fake ``httpx.AsyncClient`` that serves canned responses per-URL.

    ``routes`` maps a URL to a :class:`FakeResponse`. ``raise_for`` maps a URL to an
    exception instance to raise (simulating a transport failure). Every ``get`` is
    recorded in ``calls`` so tests can assert on (or assert the absence of) traffic.
    """

    def __init__(
        self,
        routes: dict[str, FakeResponse] | None = None,
        raise_for: dict[str, Exception] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.raise_for = raise_for or {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
        self.calls.append((url, dict(headers or {})))
        if url in self.raise_for:
            raise self.raise_for[url]
        if url in self.routes:
            return self.routes[url]
        return FakeResponse(404, {"error": "not found"})

    async def aclose(self) -> None:  # pragma: no cover - not exercised here
        return None


def _ctx(
    *,
    provider_kind: str = "openai",
    api_key: str = "sk-test",
    api_base: str = "https://api.example.com/v1",
) -> HandshakeContext:
    """Build a passive context with external sources disabled (offline-safe)."""
    return HandshakeContext(
        provider_id=f"{provider_kind}-prov",
        provider_kind=provider_kind,
        api_base=api_base,
        api_key=api_key,
        allow_external_sources=False,
    )


# --------------------------------------------------------------------------- OpenAI


@pytest.mark.asyncio
async def test_openai_models_data_list_yields_profiles_with_no_context() -> None:
    """A ``{"data": [...]}`` listing -> profiles whose ``context_window`` is None."""
    ctx = _ctx()
    payload = {
        "object": "list",
        "data": [
            {"id": "gpt-4o", "object": "model"},
            {"id": "gpt-4o-mini", "object": "model"},
        ],
    }
    client = FakeAsyncClient(
        routes={"https://api.example.com/v1/models": FakeResponse(200, payload)}
    )
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.OK
    assert conn.auth is AuthState.OK

    raw_models = await handshake.discover_models(client, ctx)
    assert [r["id"] for r in raw_models] == ["gpt-4o", "gpt-4o-mini"]

    profiles = [await handshake.discover_model_config(client, ctx, raw) for raw in raw_models]
    assert [p.discovered.id for p in profiles] == ["gpt-4o", "gpt-4o-mini"]
    assert all(not p.model.context_max.known for p in profiles)
    # Base enrich is a no-op when external sources are disabled -> still unknown.
    enriched = [await handshake.enrich_capabilities(p, ctx) for p in profiles]
    assert all(not p.model.context_max.known for p in enriched)


@pytest.mark.asyncio
async def test_full_handshake_no_external_sources() -> None:
    """End-to-end ``handshake()`` over the fake client stays offline and OK."""
    ctx = _ctx()
    payload = {"object": "list", "data": [{"id": "gpt-4o", "object": "model"}]}
    client = FakeAsyncClient(
        routes={"https://api.example.com/v1/models": FakeResponse(200, payload)}
    )
    handshake = OpenAICompatHandshake(provider=object())
    # Inject the fake client so no real httpx.AsyncClient is opened.
    handshake._open_client = _const_client(client)  # type: ignore[method-assign]

    report = await handshake.handshake(ctx)
    assert report.ok
    assert report.connectivity is ConnectivityState.OK
    assert [m.id for m in report.models] == ["gpt-4o"]
    from clio_agent.providers.capabilities.accessor import get_effective_capabilities

    for m in report.models:
        effective = get_effective_capabilities(report.provider_id, report.api_base, m.id)
        assert not effective.context.known


@pytest.mark.asyncio
async def test_missing_required_key_is_skipped_missing() -> None:
    """A cloud provider with no key -> SKIPPED/MISSING and no network call."""
    ctx = _ctx(api_key="")
    client = FakeAsyncClient()
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.SKIPPED
    assert conn.auth is AuthState.MISSING
    assert client.calls == []  # never probed the network


@pytest.mark.asyncio
async def test_http_401_is_reachable_but_rejected() -> None:
    """A 401 from ``/models`` -> reachable (OK) but auth REJECTED."""
    ctx = _ctx(api_key="sk-bad")
    client = FakeAsyncClient(
        routes={"https://api.example.com/v1/models": FakeResponse(401, {"error": "x"})}
    )
    handshake = OpenAICompatHandshake(provider=object())
    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.OK
    assert conn.auth is AuthState.REJECTED
    assert conn.error_code == "api_key_rejected"


def _openrouter_ctx(api_key: str) -> HandshakeContext:
    return HandshakeContext(
        provider_id="openrouter",
        provider_kind="openai",
        api_base="https://openrouter.ai/api/v1",
        api_key=api_key,
        allow_external_sources=False,
    )


_OPENROUTER_MODELS = {"data": [{"id": "openai/gpt-oss-120b:free"}]}


@pytest.mark.asyncio
async def test_a_public_model_listing_never_proves_a_fake_key() -> None:
    """OpenRouter lists its models to anyone, so a fake key would sail through a
    /models probe. The registry's ``key_check_path`` makes the handshake ask an
    endpoint that answers only a valid key -- and a 401 there is a rejected key."""
    client = FakeAsyncClient(
        routes={
            "https://openrouter.ai/api/v1/models": FakeResponse(200, _OPENROUTER_MODELS),
            "https://openrouter.ai/api/v1/key": FakeResponse(401, {"error": "No auth"}),
        }
    )
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, _openrouter_ctx("sk-or-v1-fake"))

    assert conn.auth is AuthState.REJECTED
    assert conn.error_code == "api_key_rejected"
    assert ("https://openrouter.ai/api/v1/key", {"Authorization": "Bearer sk-or-v1-fake"}) in (
        client.calls
    )


@pytest.mark.asyncio
async def test_a_key_the_key_check_accepts_is_ok() -> None:
    client = FakeAsyncClient(
        routes={
            "https://openrouter.ai/api/v1/models": FakeResponse(200, _OPENROUTER_MODELS),
            "https://openrouter.ai/api/v1/key": FakeResponse(200, {"data": {"label": "k"}}),
        }
    )
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, _openrouter_ctx("sk-or-v1-real"))

    assert conn.auth is AuthState.OK


@pytest.mark.asyncio
async def test_an_unavailable_key_check_is_a_typed_deferred_never_a_pass() -> None:
    client = FakeAsyncClient(
        routes={
            "https://openrouter.ai/api/v1/models": FakeResponse(200, _OPENROUTER_MODELS),
            "https://openrouter.ai/api/v1/key": FakeResponse(503, {}),
        }
    )
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, _openrouter_ctx("sk-or-v1-real"))

    assert conn.auth is AuthState.DEFERRED
    assert conn.error.startswith("key_check_unavailable")


@pytest.mark.asyncio
async def test_a_provider_without_a_key_check_path_makes_no_extra_call() -> None:
    client = FakeAsyncClient(
        routes={"https://api.example.com/v1/models": FakeResponse(200, {"data": [{"id": "m"}]})}
    )
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, _ctx())

    assert conn.auth is AuthState.OK
    assert [url for url, _ in client.calls] == ["https://api.example.com/v1/models"]


@pytest.mark.asyncio
async def test_unreachable_on_transport_error() -> None:
    """A transport exception -> UNREACHABLE."""
    ctx = _ctx()
    url = "https://api.example.com/v1/models"
    client = FakeAsyncClient(raise_for={url: ConnectionError("refused")})
    handshake = OpenAICompatHandshake(provider=object())
    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.UNREACHABLE


@pytest.mark.asyncio
async def test_anthropic_uses_x_api_key_and_version_header() -> None:
    """Anthropic kind uses ``x-api-key`` + ``anthropic-version``, not Bearer."""
    ctx = _ctx(provider_kind="anthropic", api_key="sk-ant", api_base="https://api.anthropic.com/v1")
    payload = {"data": [{"id": "claude-sonnet-4", "type": "model"}]}
    url = "https://api.anthropic.com/v1/models"
    client = FakeAsyncClient(routes={url: FakeResponse(200, payload)})
    handshake = OpenAICompatHandshake(provider=object())

    await handshake.check_connectivity(client, ctx)
    _, headers = client.calls[-1]
    assert headers.get("x-api-key") == "sk-ant"
    assert headers.get("anthropic-version") == "2023-06-01"
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_bearer_header_for_openai() -> None:
    """Non-anthropic cloud kinds use ``Authorization: Bearer``."""
    ctx = _ctx()
    url = "https://api.example.com/v1/models"
    client = FakeAsyncClient(routes={url: FakeResponse(200, {"data": []})})
    handshake = OpenAICompatHandshake(provider=object())
    await handshake.check_connectivity(client, ctx)
    _, headers = client.calls[-1]
    assert headers.get("Authorization") == "Bearer sk-test"


@pytest.mark.asyncio
async def test_embedding_models_are_skipped() -> None:
    """Embedding/reranker rows are dropped from discovery."""
    ctx = _ctx()
    payload = {
        "data": [
            {"id": "gpt-4o", "object": "model"},
            {"id": "text-embedding-3-large", "object": "model"},
            {"id": "some-reranker", "type": "rerank"},
        ]
    }
    url = "https://api.example.com/v1/models"
    client = FakeAsyncClient(routes={url: FakeResponse(200, payload)})
    handshake = OpenAICompatHandshake(provider=object())
    raw_models = await handshake.discover_models(client, ctx)
    assert [r["id"] for r in raw_models] == ["gpt-4o"]


# NOTE: the Ollama /api/tags fallback that used to live here (in the GENERIC
# OpenAICompatHandshake) was removed in the #1447 consolidation review --
# provider_kind == "ollama" always dispatches to OllamaHandshake
# (handshake/__init__.py's _BY_KIND), so that branch was dead in production
# and duplicated the real reader in dialects/ollama.py. Equivalent coverage
# lives on the real class: test_handshake_ollama.py::
# test_discover_models_lists_tags_and_drops_the_embedding_row.


@pytest.mark.asyncio
async def test_bare_list_payload_is_parsed() -> None:
    """A server returning a bare JSON list (no ``data`` wrapper) still parses."""
    ctx = _ctx(provider_kind="vllm", api_key="", api_base="http://localhost:8000/v1")
    # Reuse a real captured vLLM-shaped fixture (bare list of model rows).
    payload = _load("alcf_sophia_models.json")
    url = "http://localhost:8000/v1/models"
    client = FakeAsyncClient(routes={url: FakeResponse(200, payload)})
    handshake = OpenAICompatHandshake(provider=object())
    raw_models = await handshake.discover_models(client, ctx)
    # Every row parsed, minus the embedding models that get filtered out.
    embed_count = sum(1 for r in payload if handshake._is_embedding(r))
    assert embed_count > 0  # fixture really does contain embedding rows
    assert len(raw_models) == len(payload) - embed_count
    assert all(not handshake._is_embedding(r) for r in raw_models)
    facts = await handshake.discover_model_config(client, ctx, raw_models[0])
    assert not facts.model.context_max.known


# --------------------------------------------------------------------------- dialect dispatch (#1447 consolidation)
#
# discover_model_config no longer reads/parses any dialect-specific field
# itself -- it resolves the dialect and calls that dialect adapter. These
# tests confirm the DISPATCH wiring; the field-mapping details themselves are
# covered by each dialect module's own contract tests
# (test_dialect_vllm.py / test_dialect_openrouter.py / test_dialect_llama_cpp.py
# / test_dialect_cloud.py).


@pytest.mark.asyncio
async def test_discover_model_config_routes_vllm_dialect_to_the_vllm_adapter() -> None:
    ctx = _ctx(provider_kind="vllm", api_key="", api_base="http://localhost:8000/v1")
    row = {"id": "Qwen/Qwen3-8B", "root": "Qwen/Qwen3-8B", "max_model_len": 40960}
    handshake = OpenAICompatHandshake(provider=object())

    facts = await handshake.discover_model_config(client=None, ctx=ctx, raw=row)

    assert facts.deployment.context_served.value == 40960
    assert facts.deployment.context_served.detail == "vllm /v1/models max_model_len"
    assert facts.model.model_key == "Qwen/Qwen3-8B"


@pytest.mark.asyncio
async def test_discover_model_config_routes_openrouter_dialect_to_the_openrouter_adapter() -> None:
    ctx = _ctx(provider_kind="openrouter", api_base="https://openrouter.ai/api/v1")
    row = {
        "id": "openai/gpt-4o-mini",
        "context_length": 128000,
        "architecture": {"input_modalities": ["text", "image"]},
        "top_provider": {"context_length": 128000, "max_completion_tokens": 16384},
        "supported_parameters": ["temperature", "tools"],
    }
    handshake = OpenAICompatHandshake(provider=object())

    facts = await handshake.discover_model_config(client=None, ctx=ctx, raw=row)

    assert facts.model.context_max.value == 128000
    assert facts.model.context_max.source == "openrouter"
    assert facts.deployment.route_params.value == frozenset({"temperature", "tools"})


@pytest.mark.asyncio
async def test_discover_model_config_routes_llama_cpp_dialect_to_the_llama_cpp_adapter() -> None:
    """``provider_kind`` alone can't name llama.cpp (it shares ``"openai"``); the
    ``provider_id`` substring sniff in ``dialect_for_provider`` is what routes it."""
    ctx = HandshakeContext(
        provider_id="llama_cpp",
        provider_kind="openai",
        api_base="http://127.0.0.1:9088/v1",
        api_key="",
        allow_external_sources=False,
    )
    props_payload = {
        "default_generation_settings": {"n_ctx": 8192},
        "total_slots": 2,
        "build_info": "1234 (abc)",
    }
    client = FakeAsyncClient(
        routes={"http://127.0.0.1:9088/props": FakeResponse(200, props_payload)}
    )
    handshake = OpenAICompatHandshake(provider=object())

    facts = await handshake.discover_model_config(client, ctx, {"id": "local-model"})

    assert facts.deployment.context_served.value == 8192
    assert facts.deployment.slots.value == 2


@pytest.mark.asyncio
async def test_discover_model_config_routes_a_cloud_dialect_to_no_restriction_defaults() -> None:
    """A recognized cloud dialect (``openai``, the default ``_ctx()`` kind) gets the
    brief-4.2 "no restriction" deployment defaults, not an unknown/guessed one."""
    ctx = _ctx()  # provider_kind="openai" -> dialect "openai", a CLOUD_DIALECTS member
    handshake = OpenAICompatHandshake(provider=object())

    facts = await handshake.discover_model_config(client=None, ctx=ctx, raw={"id": "gpt-4o"})

    assert facts.deployment.modalities_enabled.known
    assert facts.deployment.modalities_enabled.source == "dialect"
    assert facts.deployment.tools_enabled.value is True


# ----------------------------------------------------------------------------- NoOp


@pytest.mark.asyncio
async def test_noop_makes_zero_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """NoOpHandshake never touches the client across every phase.

    Connectivity is now ``OK`` (a local CLI is always reachable) and discovery
    returns the provider's registry-declared candidate models — but still with
    zero network traffic on the probe client.
    """
    _patch_synthetic_cli_provider(monkeypatch)
    ctx = HandshakeContext(
        provider_id=_SYNTHETIC_CLI_PROVIDER.id,
        provider_kind="claude_code",
        api_base=_SYNTHETIC_CLI_PROVIDER.api_base,
        allow_external_sources=False,
    )
    client = FakeAsyncClient()
    handshake = NoOpHandshake(provider=object())

    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.OK
    assert conn.auth is AuthState.NOT_REQUIRED

    models = await handshake.discover_models(client, ctx)
    assert {m["id"] for m in models} == {"fable", "sonnet", "opus", "haiku"}

    facts = await handshake.discover_model_config(client, ctx, {"id": "x"})
    assert facts.discovered.id == "x"

    assert client.calls == []  # the contract: zero network traffic


@pytest.mark.asyncio
async def test_noop_discover_models_unknown_provider_is_empty() -> None:
    """An unregistered provider id yields no models (no crash)."""
    ctx = HandshakeContext(
        provider_id="not-a-real-provider",
        provider_kind="codex",
        api_base="",
        allow_external_sources=False,
    )
    handshake = NoOpHandshake(provider=object())
    assert await handshake.discover_models(FakeAsyncClient(), ctx) == []


@pytest.mark.asyncio
async def test_noop_preserves_documented_claude_image_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Static registry aliases retain documented image input without claiming availability."""

    _patch_synthetic_cli_provider(monkeypatch)
    ctx = HandshakeContext(
        provider_id=_SYNTHETIC_CLI_PROVIDER.id,
        provider_kind="claude_code",
        api_base=_SYNTHETIC_CLI_PROVIDER.api_base,
        allow_external_sources=False,
    )
    handshake = NoOpHandshake(provider=object())

    models = await handshake.discover_models(FakeAsyncClient(), ctx)
    sonnet = next(model for model in models if model["id"] == "sonnet")
    facts = await handshake.discover_model_config(FakeAsyncClient(), ctx, sonnet)

    assert facts.model.input_modalities.value == frozenset({"image", "text"})
    assert facts.discovered.raw["capability_evidence"]["source"] == "provider_documentation"
    assert facts.discovered.raw["capability_evidence"]["reason"] == "modality_documented"


@pytest.mark.asyncio
async def test_noop_full_handshake_lists_static_candidates_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic no-auth CLI handshake lists candidates without claiming liveness."""

    _patch_synthetic_cli_provider(monkeypatch)

    class _NoNetClient(FakeAsyncClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
            raise AssertionError("NoOpHandshake must not make HTTP calls")

    handshake = NoOpHandshake(provider=object())
    handshake._open_client = _const_client(_NoNetClient())  # type: ignore[method-assign]

    ctx = HandshakeContext(
        provider_id=_SYNTHETIC_CLI_PROVIDER.id,
        provider_kind="claude_code",
        api_base=_SYNTHETIC_CLI_PROVIDER.api_base,
        allow_external_sources=True,
    )
    report = await handshake.handshake(ctx)
    assert report.connectivity is ConnectivityState.OK
    assert report.auth is AuthState.NOT_REQUIRED
    by_id = {m.id: m for m in report.models}
    assert {"fable", "sonnet", "opus", "haiku"} == set(by_id)
    assert report.models_source == "static"


def _const_client(client: Any) -> Any:
    """Return an ``_open_client``-shaped coroutine that yields ``client``."""

    async def _open(_ctx: HandshakeContext) -> Any:
        return client

    return _open


@pytest.mark.asyncio
async def test_a_rejected_key_ends_the_handshake_before_model_discovery() -> None:
    """A refused key must not go on to list (and enrich) a public catalog of
    hundreds of models: one /models probe, one key check, then the verdict."""
    client = FakeAsyncClient(
        routes={
            "https://openrouter.ai/api/v1/models": FakeResponse(200, _OPENROUTER_MODELS),
            "https://openrouter.ai/api/v1/key": FakeResponse(401, {"error": "No auth"}),
        }
    )
    handshake = OpenAICompatHandshake(provider=object())

    async def _client(_ctx: HandshakeContext) -> FakeAsyncClient:
        return client

    handshake._open_client = _client  # type: ignore[method-assign]
    report = await handshake.handshake(_openrouter_ctx("sk-or-v1-fake"))

    assert report.auth is AuthState.REJECTED
    assert report.error_code == "api_key_rejected"
    assert report.models == ()
    assert [url for url, _ in client.calls] == [
        "https://openrouter.ai/api/v1/models",
        "https://openrouter.ai/api/v1/key",
    ]
