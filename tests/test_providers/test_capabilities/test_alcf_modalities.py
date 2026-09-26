"""ALCF models: modalities and model type are evidenced, three-valued, end to end.

Defect this locks: every ALCF/Argonne model was presented as ``["text"]``.
The gateway's ``/models`` rows (``fixtures/handshake/alcf_sophia_models.json``,
a recorded Sophia response) carry NO modality fields, the argonne adapter set
none, and the catalog row then emitted ``["text"]`` for the live-evidenced row
-- which the image gate read as proof and refused images for every ALCF model,
Llama-3.2-90B-Vision included. models.dev ``modalities`` and LiteLLM's
``supports_*``/``mode`` were never read at all.

The real :class:`ArgonneHandshake` loop runs here over the recorded listing,
with recorded models.dev and LiteLLM snippets
(``fixtures/catalogs/*_snippet.json``) standing in for the fetched catalogs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.modality_evidence import image_input_capability
from clio_agent.gact.provider_catalog import NOT_CHAT_AVAILABILITY, model_catalog_row
from clio_agent.gact.types import LMProviderPreset, ModelRef
from clio_agent.providers.capabilities import accessor, invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.handshake.argonne import ArgonneHandshake
from clio_agent.providers.handshake.base import ConnectivityResult, HandshakeContext
from clio_agent.providers.handshake.model import AuthState, ConnectivityState, HandshakeReport
from clio_agent.providers.handshake.sources import db, litellm_catalog, models_dev

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SOPHIA = "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1"
PROVIDER_ID = "argonne_sophia"

#: Not in the recorded Sophia listing; the same row shape the gateway reports,
#: so the eighth vision model the ALCF fleet serves is covered too.
MISTRAL_LARGE_3_ROW = {
    "id": "mistralai/Mistral-Large-3-675B-Instruct-2512",
    "object": "model",
    "cluster": "sophia",
    "framework": "vllm",
}

#: Vision models whose modalities the community catalogs state (models.dev).
CATALOG_VISION = {
    "google/gemma-4-31B-it": {"text", "image"},
    "google/gemma-4-26B-A4B-it": {"text", "image"},
    "google/gemma-4-E4B-it": {"text", "image", "audio"},
    "google/gemma-3-27b-it": {"text", "image"},
}

#: Vision models no community catalog resolves by their ALCF id. They must be
#: UNKNOWN (never "text") until a deeper source establishes them.
UNRESOLVED_VISION = (
    "meta-llama/Llama-3.2-90B-Vision-Instruct",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct",
    "mistralai/Mistral-Large-3-675B-Instruct-2512",
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        if url.endswith("/sophia/models"):
            return _FakeResponse(self._rows)
        if url.endswith("/sophia/jobs"):
            return _FakeResponse({"running": []})
        raise AssertionError(f"unexpected URL requested: {url}")

    async def aclose(self) -> None:
        return None


class _RecordedArgonne(ArgonneHandshake):
    """The real handshake loop over the recorded listing (no Globus, no network)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(provider=None)
        self._rows = rows

    async def _open_client(self, ctx: HandshakeContext) -> Any:
        return _FakeClient(self._rows)

    async def check_connectivity(self, client: Any, ctx: HandshakeContext) -> ConnectivityResult:
        return ConnectivityResult(
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            auth_header={"Authorization": "Bearer fixture-token"},
        )


@pytest.fixture(autouse=True)
def _recorded_catalogs(monkeypatch: pytest.MonkeyPatch) -> None:
    invalidation.clear_all()
    accessor.clear_cache()
    md = _load(FIXTURES / "catalogs" / "models_dev_snippet.json")
    ll = _load(FIXTURES / "catalogs" / "litellm_snippet.json")
    monkeypatch.setattr(models_dev, "_load_models_dev", lambda *a, **k: md)
    monkeypatch.setattr(litellm_catalog, "_cost_map", lambda *, allow_fetch=True: ll)
    monkeypatch.setattr(db, "lookup_context", lambda model_id: None)
    monkeypatch.setattr(db, "lookup_output", lambda model_id: None)
    yield
    invalidation.clear_all()
    accessor.clear_cache()


async def _handshake() -> HandshakeReport:
    rows = _load(FIXTURES / "handshake" / "alcf_sophia_models.json") + [MISTRAL_LARGE_3_ROW]
    ctx = HandshakeContext(
        provider_id=PROVIDER_ID,
        provider_kind="argonne",
        api_base=SOPHIA,
        auth_mode="active",
        allow_external_sources=True,
    )
    report = await _RecordedArgonne(rows).handshake(ctx)
    assert report.ok, report.error
    return report


def _preset() -> LMProviderPreset:
    return LMProviderPreset(
        id=PROVIDER_ID,
        label="ALCF Sophia",
        provider="argonne",
        api_base=SOPHIA,
        suggested_model="openai/gpt-oss-120b",
    )


def _rows(report: HandshakeReport) -> dict[str, dict[str, Any]]:
    return {m.id: model_catalog_row(_preset(), report, m) for m in report.models}


def _app(report: HandshakeReport) -> Any:
    catalog = {
        "providers": [
            {"id": PROVIDER_ID, "health": "ready", "models": list(_rows(report).values())}
        ]
    }
    return SimpleNamespace(
        state=SimpleNamespace(provider_catalog=catalog, lm_handshake_report=None)
    )


@pytest.mark.asyncio
async def test_catalog_stated_vision_models_get_their_real_modalities() -> None:
    report = await _handshake()
    for model_id, expected in CATALOG_VISION.items():
        effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, model_id)
        assert effective.input_modalities.value == frozenset(expected), model_id
        assert effective.input_modalities.source == "models.dev", model_id
    rows = _rows(report)
    assert rows["google/gemma-4-E4B-it"]["modalities"] == ["audio", "image", "text"]
    assert rows["google/gemma-4-E4B-it"]["evidence"]["modality_evidenced"] is True


@pytest.mark.asyncio
async def test_unresolved_models_are_unknown_never_text() -> None:
    report = await _handshake()
    rows = _rows(report)
    for model_id in UNRESOLVED_VISION:
        effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, model_id)
        assert not effective.input_modalities.known, model_id
        assert rows[model_id]["modalities"] == [], model_id
        assert rows[model_id]["evidence"]["modality_evidenced"] is False, model_id
        assert rows[model_id]["capabilities_provenance"]["modalities"]["decided_by"] == "unknown"
    # No row anywhere in the fleet is a fabricated text-only placeholder.
    for model_id, row in rows.items():
        if row["modalities"] == ["text"]:
            assert get_effective_capabilities(PROVIDER_ID, SOPHIA, model_id).input_modalities.known


@pytest.mark.asyncio
async def test_known_text_only_model_is_text_only_from_evidence() -> None:
    report = await _handshake()
    effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, "openai/gpt-oss-120b")
    assert effective.input_modalities.value == frozenset({"text"})
    assert _rows(report)["openai/gpt-oss-120b"]["modalities"] == ["text"]


@pytest.mark.asyncio
async def test_image_gate_allows_vision_refuses_known_text_and_passes_unknown() -> None:
    report = await _handshake()
    app = _app(report)
    for model_id in CATALOG_VISION:
        assert image_input_capability(
            app, ModelRef(provider_id=PROVIDER_ID, model_id=model_id)
        ) == (
            True,
            "live_modality_evidence",
        )
    for model_id in UNRESOLVED_VISION:
        assert image_input_capability(
            app, ModelRef(provider_id=PROVIDER_ID, model_id=model_id)
        ) == (
            True,
            "modality_unknown",
        )
    assert image_input_capability(
        app, ModelRef(provider_id=PROVIDER_ID, model_id="openai/gpt-oss-120b")
    ) == (False, "live_modality_evidence")


@pytest.mark.asyncio
async def test_sam3_is_a_segmentation_model_not_offered_for_chat() -> None:
    report = await _handshake()
    effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, "sam3")
    assert effective.model_type.value == "segmentation"
    assert effective.model_type.source == "server_report"
    row = _rows(report)["sam3"]
    assert row["model_type"] == "segmentation"
    assert row["chat_selectable"] is False
    assert row["availability"] == NOT_CHAT_AVAILABILITY
    assert row["capabilities_provenance"]["model_type"]["decided_by"] == "model"


@pytest.mark.asyncio
async def test_chat_models_carry_their_type_and_stay_selectable() -> None:
    report = await _handshake()
    rows = _rows(report)
    gemma = rows["google/gemma-3-27b-it"]
    assert gemma["model_type"] == "chat"  # LiteLLM mode="chat"
    assert gemma["chat_selectable"] is True
    assert gemma["availability"] == "available"
    # A model whose type no source states is unknown -- and still selectable.
    tulu = rows["allenai/Llama-3.1-Tulu-3-405B"]
    assert tulu["model_type"] is None
    assert tulu["chat_selectable"] is True
    assert tulu["availability"] == "available"


def test_litellm_embedding_mode_is_an_embedding_type() -> None:
    from clio_agent.providers.capabilities.model_sources import community_catalog_facts

    facts = community_catalog_facts("mistral/mistral-embed")
    assert facts is not None
    assert facts.model_type.value == "embedding"
    assert facts.model_type.source == "litellm"


def test_litellm_modalities_need_an_explicit_vision_flag() -> None:
    """A LiteLLM row with no ``supports_vision`` key is no evidence of text-only."""

    assert litellm_catalog.modalities_from_info({"mode": "chat"}) is None
    assert litellm_catalog.modalities_from_info({"supports_vision": False}) == frozenset({"text"})
    assert litellm_catalog.modalities_from_info(
        {"supports_vision": True, "supports_audio_input": True, "supports_pdf_input": True}
    ) == frozenset({"text", "image", "audio", "pdf"})


def test_models_dev_output_decides_a_type_only_without_text() -> None:
    assert models_dev.model_type_from_output(frozenset({"text"})) is None
    assert models_dev.model_type_from_output(frozenset({"image"})) == "image_generation"
    assert models_dev.model_type_from_output(frozenset({"audio"})) == "audio_speech"
    assert models_dev.model_type_from_output(None) is None
