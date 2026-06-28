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

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
)
from clio_agent.providers.handshake.noop import NoOpHandshake
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

FIXTURES = Path(__file__).parent / "fixtures" / "handshake"


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
    assert [p.id for p in profiles] == ["gpt-4o", "gpt-4o-mini"]
    assert all(p.context_window is None for p in profiles)
    # Base enrich is a no-op when external sources are disabled -> still None.
    enriched = [await handshake.enrich_capabilities(p, ctx) for p in profiles]
    assert all(p.context_window is None for p in enriched)
    assert all(p.context_source == "live" for p in enriched)


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
    assert all(m.context_window is None for m in report.models)


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


@pytest.mark.asyncio
async def test_ollama_no_auth_and_tags_fallback() -> None:
    """Ollama needs no key; ``/v1/models`` 404 -> ``/api/tags`` fallback parses names."""
    ctx = _ctx(provider_kind="ollama", api_key="", api_base="http://127.0.0.1:11434/v1")
    tags_payload = {
        "models": [
            {"name": "llama3.2:latest", "model": "llama3.2:latest"},
            {"name": "nomic-embed-text:latest", "model": "nomic-embed-text:latest"},
        ]
    }
    routes = {
        # /v1/models intentionally absent -> FakeAsyncClient returns a 404.
        "http://127.0.0.1:11434/api/tags": FakeResponse(200, tags_payload),
    }
    client = FakeAsyncClient(routes=routes)
    handshake = OpenAICompatHandshake(provider=object())

    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.OK
    assert conn.auth is AuthState.NOT_REQUIRED

    raw_models = await handshake.discover_models(client, ctx)
    # Embedding model filtered out -> only the chat model remains.
    assert [r["id"] for r in raw_models] == ["llama3.2:latest"]


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
    profile = await handshake.discover_model_config(client, ctx, raw_models[0])
    assert profile.context_window is None


# ----------------------------------------------------------------------------- NoOp


@pytest.mark.asyncio
async def test_noop_makes_zero_network_calls() -> None:
    """NoOpHandshake never touches the client across every phase.

    Connectivity is now ``OK`` (a local CLI is always reachable) and discovery
    returns the provider's registry-declared candidate models — but still with
    zero network traffic on the probe client.
    """
    ctx = HandshakeContext(
        provider_id="codex",
        provider_kind="codex",
        api_base="",
        allow_external_sources=False,
    )
    client = FakeAsyncClient()
    handshake = NoOpHandshake(provider=object())

    conn = await handshake.check_connectivity(client, ctx)
    assert conn.connectivity is ConnectivityState.OK
    assert conn.auth is AuthState.NOT_REQUIRED

    models = await handshake.discover_models(client, ctx)
    # The codex registry catalog declares candidate model ids.
    assert {m["id"] for m in models} >= {"gpt-5.5", "gpt-5.1"}

    profile = await handshake.discover_model_config(client, ctx, {"id": "x"})
    assert profile.id == "x"

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
async def test_noop_full_handshake_ok_with_enriched_context() -> None:
    """A full ``handshake()`` over NoOp is OK, lists registry models, and the base
    enrichment fills each model's context window from the offline source cascade —
    all without a single HTTP call to the provider (iowarp/clio-agent#740)."""

    class _NoNetClient(FakeAsyncClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> FakeResponse:
            raise AssertionError("NoOpHandshake must not make HTTP calls")

    handshake = NoOpHandshake(provider=object())
    handshake._open_client = _const_client(_NoNetClient())  # type: ignore[method-assign]

    ctx = HandshakeContext(
        provider_id="codex",
        provider_kind="codex",
        api_base="",
        allow_external_sources=True,
    )
    report = await handshake.handshake(ctx)
    assert report.connectivity is ConnectivityState.OK
    assert report.auth is AuthState.NOT_REQUIRED
    by_id = {m.id: m for m in report.models}
    assert {"gpt-5.5", "gpt-5.5-codex", "gpt-5.1"} <= set(by_id)
    # Every candidate model carries a resolved context window (no None leaks).
    for model in report.models:
        assert model.context_window and model.context_window > 0


def _const_client(client: Any) -> Any:
    """Return an ``_open_client``-shaped coroutine that yields ``client``."""

    async def _open(_ctx: HandshakeContext) -> Any:
        return client

    return _open
