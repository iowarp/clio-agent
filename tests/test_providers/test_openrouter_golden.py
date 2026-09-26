"""OpenRouter is a golden provider: its own ``/api/v1/models`` row decides its facts.

Fixture: ``tests/fixtures/capabilities/openrouter/api_v1_models_all.json`` is a
recorded live response (628 models, fetched with ``?output_modalities=all``),
slimmed to the fields the adapter reads (``id``, ``name``, ``context_length``,
``architecture``, ``pricing``, ``top_provider``, ``supported_parameters``);
model descriptions are dropped.

The real :class:`OpenAICompatHandshake` runs over it for the ``openrouter``
preset (a fake client serves the recording), so the listing URL, the
no-name-based-drop rule, the authoritative precedence and the catalog wire
are all exercised end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.model_selection import SURROGATE_NOT_CHAT, surrogate_selection_error
from clio_agent.gact.provider_catalog import model_catalog_row
from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers.capabilities import accessor, invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.dialects import openrouter
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import HandshakeReport
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "capabilities"
    / "openrouter"
    / "api_v1_models_all.json"
)
API_BASE = "https://openrouter.ai/api/v1"


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _row(model_id: str) -> dict[str, Any]:
    return next(r for r in _payload()["data"] if r["id"] == model_id)


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class _Client:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _Response:
        self.urls.append(url)
        if url.endswith("/models?output_modalities=all"):
            return _Response(_payload())
        if url.endswith("/key"):
            return _Response({"data": {}})
        return _Response(None, status_code=404)

    async def aclose(self) -> None:
        return None


class _RecordedOpenRouter(OpenAICompatHandshake):
    def __init__(self, client: _Client) -> None:
        super().__init__(provider=get_provider("openrouter"))
        self._client = client

    async def _open_client(self, ctx: HandshakeContext) -> Any:
        return self._client


@pytest.fixture(autouse=True)
def _clean_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-fixture")
    invalidation.clear_all()
    accessor.clear_cache()
    yield
    invalidation.clear_all()
    accessor.clear_cache()


async def _handshake() -> tuple[HandshakeReport, _Client]:
    client = _Client()
    ctx = HandshakeContext(
        provider_id="openrouter",
        provider_kind="openai",
        api_base=API_BASE,
        api_key="sk-or-fixture",
        auth_mode="active",
        allow_external_sources=False,
    )
    report = await _RecordedOpenRouter(client).handshake(ctx)
    assert report.ok, report.error
    return report, client


def _rows(report: HandshakeReport) -> dict[str, dict[str, Any]]:
    preset = LMProviderPreset(
        id="openrouter",
        label="OpenRouter",
        provider="openai",
        api_base=API_BASE,
        suggested_model="",
    )
    return {m.id: model_catalog_row(preset, report, m) for m in report.models}


def _app_with(rows: dict[str, dict[str, Any]], provider_id: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        state=SimpleNamespace(
            provider_catalog={
                "providers": [{"id": provider_id, "health": "ready", "models": list(rows.values())}]
            },
            lm_handshake_report=None,
        )
    )


# --------------------------------------------------------------------------- dialect unit


def test_jev_router_gets_all_five_input_modalities() -> None:
    model, _deployment = openrouter.parse_model_row(
        _row("typesafe/jev-router"), provider_id="openrouter", api_base=API_BASE
    )
    # file -> pdf (a document attachment)
    assert model.input_modalities.value == frozenset({"text", "image", "pdf", "audio", "video"})
    assert model.input_modalities.source == "openrouter"


def test_perceptron_gets_its_four_modalities_and_parameters() -> None:
    model, deployment = openrouter.parse_model_row(
        _row("perceptron/perceptron-mk1.5"), provider_id="openrouter", api_base=API_BASE
    )
    assert model.input_modalities.value == frozenset({"text", "image", "audio", "video"})
    assert model.tools.value is True
    assert model.structured_output.value is True
    assert model.thinking.value is not None and model.thinking.value.mechanism == "effort_levels"
    assert model.context_max.value == 36864
    assert deployment.output_max.value == 8192
    assert "tools" in (deployment.route_params.value or ())
    assert deployment.pricing.value == {"prompt": "0.00000015", "completion": "0.0000015"}
    assert deployment.free.value is False
    assert deployment.router.value is False


def test_openrouter_free_is_free_and_a_router() -> None:
    _model, deployment = openrouter.parse_model_row(
        _row("openrouter/free"), provider_id="openrouter", api_base=API_BASE
    )
    assert deployment.free.value is True
    assert deployment.router.value is True


def test_a_minus_one_price_is_variable_never_zero() -> None:
    _model, deployment = openrouter.parse_model_row(
        _row("openrouter/auto"), provider_id="openrouter", api_base=API_BASE
    )
    assert deployment.pricing.value == {"prompt": "variable", "completion": "variable"}
    assert deployment.free.value is False
    assert deployment.router.value is True


def test_free_comes_from_pricing_not_the_suffix() -> None:
    rows = _payload()["data"]
    free = []
    for row in rows:
        _model, deployment = openrouter.parse_model_row(
            row, provider_id="openrouter", api_base=API_BASE
        )
        if deployment.free.value:
            free.append(row["id"])
    assert len(free) == 101
    assert sum(1 for model_id in free if model_id.endswith(":free")) < len(free)


def test_decisions_output_is_text_classification() -> None:
    model, _deployment = openrouter.parse_model_row(
        _row("~typesafe/jev-latest"), provider_id="openrouter", api_base=API_BASE
    )
    assert model.task.value == "text-classification"


def test_an_empty_parameter_list_states_nothing() -> None:
    model, _deployment = openrouter.parse_model_row(
        _row("typesafe/jev-router"), provider_id="openrouter", api_base=API_BASE
    )
    assert not model.tools.known
    assert not model.thinking.known


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (["text"], "text-generation"),
        (["image", "text"], "text-generation"),
        (["decisions"], "text-classification"),
        (["image"], "text-to-image"),
        (["video"], "text-to-video"),
        (["embeddings"], "feature-extraction"),
        (["speech"], "text-to-speech"),
        (["transcription"], "automatic-speech-recognition"),
        (["rerank"], "text-ranking"),
        (None, None),
    ],
)
def test_output_modalities_decide_the_task(output: Any, expected: str | None) -> None:
    assert openrouter.task_from_output_modalities(output) == expected


# --------------------------------------------------------------------------- handshake + wire


@pytest.mark.asyncio
async def test_listing_asks_for_every_output_modality_and_drops_nothing_by_name() -> None:
    report, client = await _handshake()
    assert any(url.endswith("/models?output_modalities=all") for url in client.urls)
    assert len(report.models) == 628  # embedding/rerank rows are listed, not dropped


@pytest.mark.asyncio
async def test_the_wire_carries_the_openrouter_facts() -> None:
    report, _client = await _handshake()
    rows = _rows(report)

    jev = rows["typesafe/jev-router"]
    assert jev["modalities"] == ["audio", "image", "pdf", "text", "video"]
    assert jev["pricing"] == {"prompt": "variable", "completion": "variable"}

    free = rows["openrouter/free"]
    assert free["free"] is True and free["router"] is True
    assert free["task"] == "text-generation" and free["role"] == "general"
    assert free["availability"] == "available"

    perceptron = rows["perceptron/perceptron-mk1.5"]
    assert perceptron["modalities"] == ["audio", "image", "text", "video"]
    assert perceptron["native_tool_calling"] is True
    assert perceptron["structured_output"] is True
    assert perceptron["free"] is False
    assert perceptron["output_modalities"] == ["text"]


@pytest.mark.asyncio
async def test_surrogates_are_listed_and_refused_only_as_the_chat_model() -> None:
    report, _client = await _handshake()
    rows = _rows(report)
    jev = rows["~typesafe/jev-latest"]
    assert jev["task"] == "text-classification"
    assert jev["role"] == "surrogate"
    assert jev["chat_selectable"] is False
    assert jev["availability"] == "available"  # listed: surrogates are first-class
    image_gen = next(row for row in rows.values() if row["output_modalities"] == ["image"])
    assert image_gen["task"] == "text-to-image"
    assert image_gen["role"] == "surrogate"
    # Only selecting a surrogate as the chat model is refused, with a typed reason.
    app = _app_with(rows, "openrouter")
    refused = surrogate_selection_error(app, "openrouter", "~typesafe/jev-latest")
    assert refused is not None and refused.error.error == SURROGATE_NOT_CHAT
    assert refused.error.details["task"] == "text-classification"
    assert surrogate_selection_error(app, "openrouter", "openrouter/free") is None


@pytest.mark.asyncio
async def test_openrouter_facts_outrank_the_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """A golden provider's own report ranks above the overlay (a curated guess)."""
    from clio_agent.providers.capabilities import model_sources
    from clio_agent.providers.capabilities.records import Fact, ModelCapabilities

    class _WrongOverlay:
        def facts(self, model_key: str) -> ModelCapabilities:
            return ModelCapabilities(
                model_key=model_key,
                input_modalities=Fact(frozenset({"text"}), "overlay", "", "overlay says text only"),
            )

    monkeypatch.setattr(model_sources, "EmptyOverlaySource", _WrongOverlay)
    report, client = await _handshake()
    del client
    # allow_external_sources=False skips enrichment; run it explicitly.
    handshake = _RecordedOpenRouter(_Client())
    ctx = HandshakeContext(
        provider_id="openrouter",
        provider_kind="openai",
        api_base=API_BASE,
        api_key="sk-or-fixture",
        allow_external_sources=True,
    )
    from clio_agent.providers.handshake.model import DiscoveredModel, DiscoveredModelFacts

    model, deployment = openrouter.parse_model_row(
        _row("perceptron/perceptron-mk1.5"), provider_id="openrouter", api_base=API_BASE
    )
    facts = DiscoveredModelFacts(
        discovered=DiscoveredModel(id="perceptron/perceptron-mk1.5"),
        model=model,
        deployment=deployment,
    )
    enriched = await handshake.enrich_capabilities(facts, ctx)
    assert enriched.model.input_modalities.value == frozenset({"text", "image", "audio", "video"})
    assert report.models


def test_effective_view_exposes_the_deployment_facts() -> None:
    model, deployment = openrouter.parse_model_row(
        _row("openrouter/free"), provider_id="openrouter", api_base=API_BASE
    )
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(deployment)
    effective = get_effective_capabilities("openrouter", API_BASE, "openrouter/free")
    assert effective.free.value is True
    assert effective.router.value is True
    assert effective.pricing.value == {"prompt": "0", "completion": "0"}
