"""Offline unit tests for :class:`LMStudioHandshake`.

These tests never touch the network: the real captured ``/api/v0/models``
payload is loaded from ``fixtures/handshake/lmstudio_v0_models.json`` and served
through a tiny in-memory fake of an :class:`httpx.AsyncClient`. They exercise the
field mapping (``max_context_length`` -> ceiling, ``loaded_context_length`` ->
runtime window, ``tool_use`` -> native tool calling, quantization passthrough)
and the no-auth connectivity probe with its OpenAI-compatible fallback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.lmstudio import LMStudioHandshake
from clio_agent.providers.handshake.model import AuthState, ConnectivityState

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "handshake"
API_BASE = "http://localhost:1234/v1"
ROOT = "http://localhost:1234"


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a captured JSON fixture from the handshake fixtures directory."""
    return json.loads((FIXTURE_DIR / name).read_text())


@dataclass
class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` (status + JSON body)."""

    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """In-memory fake of ``httpx.AsyncClient`` driven by a URL->response map."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self._routes = routes
        self.requested: list[str] = []

    async def get(self, url: str) -> _FakeResponse:
        self.requested.append(url)
        if url in self._routes:
            return self._routes[url]
        raise ConnectionError(f"no route for {url}")


def _ctx() -> HandshakeContext:
    return HandshakeContext(
        provider_id="lmstudio-local",
        provider_kind="lmstudio",
        api_base=API_BASE,
        # keep enrich's external-source fallback off the network in case a
        # window were ever missing — the fixture always supplies one anyway.
        allow_external_sources=False,
    )


@pytest.mark.asyncio
async def test_discover_model_config_maps_qwen_fields() -> None:
    """qwopus3.5-9b-v3 maps ceiling, loaded window, tool_use and quant correctly."""
    payload = _load_fixture("lmstudio_v0_models.json")
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient({f"{ROOT}/api/v0/models": _FakeResponse(200, payload)})
    ctx = _ctx()

    raw_rows = await handshake.discover_models(client, ctx)
    by_id = {row["id"]: row for row in raw_rows}
    assert "qwopus3.5-9b-v3" in by_id

    profile = await handshake.discover_model_config(client, ctx, by_id["qwopus3.5-9b-v3"])

    assert profile.id == "qwopus3.5-9b-v3"
    assert profile.context_window == 262144
    assert profile.loaded_context_window == 65536
    assert profile.loaded_context_window is not None
    assert profile.native_tool_calling is True
    assert profile.quantization == "Q4_K_M"
    assert profile.arch == "qwen35"
    assert profile.is_loaded is True
    assert profile.context_source == "live"
    # effective window is the loaded (runtime) one, not the ceiling
    assert profile.effective_context_window == 65536


@pytest.mark.asyncio
async def test_non_tool_model_has_no_native_tool_calling() -> None:
    """An embeddings row without a capabilities list is not flagged tool-capable."""
    payload = _load_fixture("lmstudio_v0_models.json")
    handshake = LMStudioHandshake(provider=None)
    ctx = _ctx()
    by_id = {row["id"]: row for row in payload["data"]}

    embed = await handshake.discover_model_config(
        _FakeAsyncClient({}), ctx, by_id["text-embedding-nomic-embed-text-v1.5"]
    )
    assert embed.native_tool_calling is False
    assert embed.capabilities == ()
    assert embed.is_loaded is False
    assert embed.loaded_context_window is None


@pytest.mark.asyncio
async def test_connectivity_ok_no_auth_required() -> None:
    """A reachable native endpoint yields OK + NOT_REQUIRED auth."""
    payload = _load_fixture("lmstudio_v0_models.json")
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient({f"{ROOT}/api/v0/models": _FakeResponse(200, payload)})

    result = await handshake.check_connectivity(client, _ctx())
    assert result.connectivity is ConnectivityState.OK
    assert result.auth is AuthState.NOT_REQUIRED


@pytest.mark.asyncio
async def test_connectivity_falls_back_to_openai_models() -> None:
    """When ``/api/v0/models`` is absent, the OpenAI ``/models`` route still passes."""
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient({f"{API_BASE}/models": _FakeResponse(200, {"data": []})})

    result = await handshake.check_connectivity(client, _ctx())
    assert result.connectivity is ConnectivityState.OK
    assert result.auth is AuthState.NOT_REQUIRED
    # native endpoint was tried first, then the fallback
    assert client.requested == [f"{ROOT}/api/v0/models", f"{API_BASE}/models"]


@pytest.mark.asyncio
async def test_connectivity_unreachable() -> None:
    """No route answering marks the backend UNREACHABLE (auth still NOT_REQUIRED)."""
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient({})  # every GET raises ConnectionError

    result = await handshake.check_connectivity(client, _ctx())
    assert result.connectivity is ConnectivityState.UNREACHABLE
    assert result.auth is AuthState.NOT_REQUIRED
    assert result.error is not None


@pytest.mark.asyncio
async def test_root_strips_trailing_v1() -> None:
    """``_root`` strips a trailing ``/v1`` (with or without a trailing slash)."""
    assert LMStudioHandshake._root("http://h:1234/v1") == "http://h:1234"
    assert LMStudioHandshake._root("http://h:1234/v1/") == "http://h:1234"
    assert LMStudioHandshake._root("http://h:1234") == "http://h:1234"
