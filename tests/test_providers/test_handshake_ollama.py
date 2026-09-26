"""Contract tests for :class:`OllamaHandshake` against RECORDED server responses.

Fixtures are real-shaped Ollama ``/api/tags`` and ``/api/show`` payloads
(``tests/test_providers/fixtures/handshake/ollama_api_tags.json`` /
``ollama_api_show_qwen3.json``), served through a tiny in-memory fake client.
No network is touched. Covers the field mapping the brief calls out for
Ollama (Part 6): ``model_info.<arch>.context_length`` -> the model's own
ceiling, ``capabilities`` -> tools/thinking/input-modalities, all as
``source="server_report"`` facts -- never a flat ``ModelProfile`` field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import ConnectivityState
from clio_agent.providers.handshake.ollama import OllamaHandshake

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "handshake"
API_BASE = "http://127.0.0.1:11434"


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text())


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    """In-memory fake serving the recorded Ollama fixtures by URL/method."""

    def __init__(self, *, tags: Any, show_by_model: dict[str, Any]) -> None:
        self._tags = tags
        self._show_by_model = show_by_model
        self.requested: list[str] = []

    async def get(self, url: str, **_: object) -> _FakeResponse:
        self.requested.append(url)
        if url.endswith("/api/tags"):
            return _FakeResponse(200, self._tags)
        if url.endswith("/models"):
            # The OpenAI-compat connectivity probe OllamaHandshake inherits;
            # Ollama's /v1/models shim reports nothing useful, just reachability.
            return _FakeResponse(200, {"data": []})
        raise ConnectionError(f"no route for GET {url}")

    async def post(self, url: str, json: dict[str, Any], **_: object) -> _FakeResponse:
        self.requested.append(url)
        model_id = json.get("model")
        payload = self._show_by_model.get(model_id)
        if payload is None:
            return _FakeResponse(404, {"error": "model not found"})
        return _FakeResponse(200, payload)


def _client() -> _FakeAsyncClient:
    return _FakeAsyncClient(
        tags=_load("ollama_api_tags.json"),
        show_by_model={"qwen3:8b": _load("ollama_api_show_qwen3.json")},
    )


def _ctx() -> HandshakeContext:
    return HandshakeContext(
        provider_id="ollama",
        provider_kind="ollama",
        api_base=API_BASE,
        allow_external_sources=False,
    )


@pytest.mark.asyncio
async def test_discover_models_lists_tags_and_drops_the_embedding_row() -> None:
    handshake = OllamaHandshake(provider=None)
    rows = await handshake.discover_models(_client(), _ctx())
    ids = [row.get("id") or row.get("model") for row in rows]
    assert ids == ["qwen3:8b"]  # nomic-embed-text is an embedding model, filtered out


@pytest.mark.asyncio
async def test_discover_model_config_maps_context_tools_and_thinking() -> None:
    handshake = OllamaHandshake(provider=None)
    client = _client()
    rows = await handshake.discover_models(client, _ctx())
    facts = await handshake.discover_model_config(client, _ctx(), rows[0])

    assert facts.discovered.id == "qwen3:8b"
    # model_info.qwen3.context_length -> the model's own ceiling, self-reported.
    assert facts.model.context_max.value == 40960
    assert facts.model.context_max.source == "server_report"
    assert "model_info" in facts.model.context_max.detail
    # capabilities: ["completion", "tools", "thinking"] -> tools True, thinking on.
    assert facts.model.tools.value is True
    assert facts.model.thinking.known
    assert facts.model.thinking.value.mechanism == "on_off"
    # No "vision" in capabilities -> only text.
    assert facts.model.input_modalities.value == frozenset({"text"})
    # Deployment record exists but this adapter reports no serving-specific facts.
    assert facts.deployment.provider_id == "ollama"
    assert facts.deployment.model_id == "qwen3:8b"


@pytest.mark.asyncio
async def test_discover_model_config_links_hf_co_pulled_names() -> None:
    """brief 5.4: an Ollama hf.co/<repo> pull name links to its Hugging Face repo."""
    handshake = OllamaHandshake(provider=None)
    client = _FakeAsyncClient(
        tags={
            "models": [{"name": "hf.co/Qwen/Qwen3-8B-GGUF", "model": "hf.co/Qwen/Qwen3-8B-GGUF"}]
        },
        show_by_model={},
    )
    ctx = _ctx()
    rows = await handshake.discover_models(client, ctx)
    facts = await handshake.discover_model_config(client, ctx, rows[0])
    assert facts.model.model_key == "Qwen/Qwen3-8B-GGUF"
    assert facts.deployment.model_key.value == "Qwen/Qwen3-8B-GGUF"


@pytest.mark.asyncio
async def test_show_failure_is_best_effort_and_leaves_facts_unknown() -> None:
    """A model with no matching /api/show fixture (a 404) degrades to unknown, not a crash."""
    handshake = OllamaHandshake(provider=None)
    client = _client()
    raw = {"id": "unknown-model", "model": "unknown-model"}
    facts = await handshake.discover_model_config(client, _ctx(), raw)
    assert not facts.model.context_max.known
    assert not facts.model.tools.known


@pytest.mark.asyncio
async def test_full_handshake_reports_live_provenance() -> None:
    handshake = OllamaHandshake(provider=None)

    async def _open_client(ctx: HandshakeContext) -> _FakeAsyncClient:
        return _client()

    handshake._open_client = _open_client  # type: ignore[method-assign]
    report = await handshake.handshake(_ctx())
    assert report.connectivity is ConnectivityState.OK
    assert report.models_source == "live"
    assert [m.id for m in report.models] == ["qwen3:8b"]
