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

    facts = await handshake.discover_model_config(client, ctx, by_id["qwopus3.5-9b-v3"])

    assert facts.discovered.id == "qwopus3.5-9b-v3"
    assert facts.model.context_max.value == 262144
    assert facts.model.context_max.source == "server_report"
    assert facts.deployment.context_served.value == 65536
    assert facts.model.tools.value is True
    assert facts.discovered.raw["quantization"] == "Q4_K_M"
    assert facts.discovered.raw["arch"] == "qwen35"
    assert facts.discovered.is_loaded is True


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
    assert not embed.model.tools.known  # no capabilities list reported: unknown, not False
    assert embed.discovered.is_loaded is False
    assert not embed.deployment.context_served.known


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
    """When neither native endpoint answers, the OpenAI ``/models`` route still passes."""
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient({f"{API_BASE}/models": _FakeResponse(200, {"data": []})})

    result = await handshake.check_connectivity(client, _ctx())
    assert result.connectivity is ConnectivityState.OK
    assert result.auth is AuthState.NOT_REQUIRED
    # v1, then v0, then the OpenAI-compatible fallback (brief Part 6: v1 first).
    assert client.requested == [f"{ROOT}/api/v1/models", f"{ROOT}/api/v0/models", f"{API_BASE}/models"]


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
async def test_discover_models_prefers_v1_over_v0() -> None:
    """P4b: when ``/api/v1/models`` answers, it wins over the v0 fallback (brief Part 6)."""
    capability_fixtures = Path(__file__).parent.parent / "fixtures" / "capabilities" / "lm_studio"
    v1_payload = json.loads((capability_fixtures / "api_v1_models.json").read_text(encoding="utf-8"))
    v0_payload = _load_fixture("lmstudio_v0_models.json")
    handshake = LMStudioHandshake(provider=None)
    client = _FakeAsyncClient(
        {
            f"{ROOT}/api/v1/models": _FakeResponse(200, v1_payload),
            f"{ROOT}/api/v0/models": _FakeResponse(200, v0_payload),
        }
    )
    ctx = _ctx()

    raw_rows = await handshake.discover_models(client, ctx)

    assert client.requested == [f"{ROOT}/api/v1/models"]  # v0 never even queried
    assert [row["id"] for row in raw_rows] == ["qwen/qwen3-8b"]

    facts = await handshake.discover_model_config(client, ctx, raw_rows[0])

    assert facts.discovered.id == "qwen/qwen3-8b"
    assert facts.model.context_max.value == 40960
    assert facts.model.tools.value is True
    assert facts.deployment.context_served.value == 8192
    assert facts.deployment.template_caps.value == {
        "reasoning_allowed_options": ["low", "medium", "high"]
    }
