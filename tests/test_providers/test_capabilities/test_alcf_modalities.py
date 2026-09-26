"""ALCF models: modalities and model type are evidenced, three-valued, end to end.

Defect this locks: every ALCF/Argonne model was presented as ``["text"]``.
The gateway's ``/models`` rows (``fixtures/handshake/alcf_sophia_models.json``,
a recorded Sophia response) carry NO modality fields, the argonne adapter set
none, and the catalog row then emitted ``["text"]`` for the live-evidenced row
-- which the image gate read as proof and refused images for every ALCF model,
Llama-3.2-90B-Vision included. models.dev ``modalities`` and LiteLLM's
``supports_*``/``mode`` were never read, and the Hugging Face layer was wired
into nothing.

The real :class:`ArgonneHandshake` loop runs here over the recorded listing,
with recorded models.dev and LiteLLM snippets (``fixtures/catalogs/``) standing
in for the fetched catalogs and recorded Hugging Face responses
(``tests/fixtures/capabilities/hf_repo/alcf_*``: the public metadata call and,
where the repo is not gated, ``config.json``/``processor_config.json``/
``params.json`` at the pinned commit) served for huggingface.co. A URL with no
recording answers 404, and the gated repos' files answer 401 exactly as the
Hub does anonymously. The model overlay (``catalogs/models/*.yaml``, the
compiled bundled copy) is live: it outranks the Hugging Face layer and the
community catalogs (brief 5.1), so every model it lists is decided by it.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clio_agent.gact.modality_evidence import image_input_capability
from clio_agent.gact.model_selection import SURROGATE_NOT_CHAT, surrogate_selection_error
from clio_agent.gact.provider_catalog import model_catalog_row
from clio_agent.gact.types import LMProviderPreset, ModelRef
from clio_agent.providers import fetched_catalog
from clio_agent.providers.capabilities import accessor, hf_repo, invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.handshake.argonne import ArgonneHandshake
from clio_agent.providers.handshake.base import ConnectivityResult, HandshakeContext
from clio_agent.providers.handshake.model import AuthState, ConnectivityState, HandshakeReport
from clio_agent.providers.handshake.sources import db, litellm_catalog, models_dev

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HF_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "capabilities" / "hf_repo"
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

#: repo id -> recorded fixture stem (``alcf_<stem>_meta.json`` and
#: ``alcf_<stem>__<file>`` for each non-gated file recorded at the pinned sha).
HF_RECORDED = {
    "meta-llama/Llama-3.2-90B-Vision-Instruct": "meta-llama_Llama-3.2-90B-Vision-Instruct",
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct": "meta-llama_Llama-4-Maverick-17B-128E-Instruct",
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": "meta-llama_Llama-4-Scout-17B-16E-Instruct",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama_Llama-3.3-70B-Instruct",
    "mistralai/Mistral-Large-3-675B-Instruct-2512": "mistralai_Mistral-Large-3-675B-Instruct-2512",
    "google/gemma-4-E4B-it": "google_gemma-4-E4B-it",
    "google/gemma-3-27b-it": "google_gemma-3-27b-it",
    "Salesforce/SFR-Embedding-Mistral": "Salesforce_SFR-Embedding-Mistral",
}
#: Gated repos: the Hub answers their files 401 without a token (recorded).
HF_GATED = {repo for repo in HF_RECORDED if repo.startswith(("meta-llama/", "google/gemma-3"))}

#: All eight ALCF vision models: expected effective modalities + which layer decided.
#: Each has an overlay entry (Llama-4's corrected to vision: true), which outranks
#: the Hugging Face layer and the community catalogs.
VISION = {
    "google/gemma-4-31B-it": ({"text", "image"}, "overlay"),
    "google/gemma-4-26B-A4B-it": ({"text", "image"}, "overlay"),
    "google/gemma-4-E4B-it": ({"text", "image", "audio"}, "overlay"),
    "google/gemma-3-27b-it": ({"text", "image"}, "overlay"),
    "meta-llama/Llama-3.2-90B-Vision-Instruct": ({"text", "image"}, "overlay"),
    "meta-llama/Llama-4-Maverick-17B-128E-Instruct": ({"text", "image"}, "overlay"),
    "meta-llama/Llama-4-Scout-17B-16E-Instruct": ({"text", "image"}, "overlay"),
    "mistralai/Mistral-Large-3-675B-Instruct-2512": ({"text", "image"}, "overlay"),
}


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
        return _FakeResponse(None, status_code=404)

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


class _HubRouter:
    """Serves the recorded Hub responses for ``httpx.get`` (as FetchedCatalog calls it)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requested: list[str] = []
        for repo, stem in HF_RECORDED.items():
            meta_bytes = (HF_FIXTURES / f"alcf_{stem}_meta.json").read_bytes()
            meta = json.loads(meta_bytes)
            self.routes[f"https://huggingface.co/api/models/{repo}"] = (200, meta_bytes)
            for sibling in meta.get("siblings", []):
                name = sibling["rfilename"]
                url = f"https://huggingface.co/{repo}/raw/{meta['sha']}/{name}"
                recorded = HF_FIXTURES / f"alcf_{stem}__{name}"
                if repo in HF_GATED:
                    self.routes[url] = (401, b"Access to model is restricted.")
                elif recorded.exists():
                    self.routes[url] = (200, recorded.read_bytes())

    def __call__(self, url: str, **_: object) -> httpx.Response:
        self.requested.append(url)
        status, content = self.routes.get(url, (404, b'{"error":"not found"}'))
        return httpx.Response(status, content=content, request=httpx.Request("GET", url))


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> _HubRouter:
    router = _HubRouter()
    monkeypatch.setattr(fetched_catalog.httpx, "get", router)
    return router


@pytest.fixture(autouse=True)
def _recorded_catalogs(monkeypatch: pytest.MonkeyPatch, hub: _HubRouter) -> None:
    invalidation.clear_all()
    accessor.clear_cache()
    hf_repo.clear_miss_cache()
    hf_repo._metadata_catalog.cache_clear()
    hf_repo._file_catalog.cache_clear()
    md = _load(FIXTURES / "catalogs" / "models_dev_snippet.json")
    ll = _load(FIXTURES / "catalogs" / "litellm_snippet.json")
    monkeypatch.setattr(models_dev, "_load_models_dev", lambda *a, **k: md)
    monkeypatch.setattr(litellm_catalog, "_cost_map", lambda *, allow_fetch=True: ll)
    monkeypatch.setattr(db, "lookup_context", lambda model_id: None)
    monkeypatch.setattr(db, "lookup_output", lambda model_id: None)
    yield
    invalidation.clear_all()
    accessor.clear_cache()
    hf_repo.clear_miss_cache()


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


@pytest.mark.asyncio
async def test_all_eight_vision_models_get_their_real_modalities() -> None:
    report = await _handshake()
    rows = _rows(report)
    for model_id, (expected, source) in VISION.items():
        effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, model_id)
        assert effective.input_modalities.value == frozenset(expected), model_id
        assert effective.input_modalities.source == source, model_id
        assert rows[model_id]["modalities"] == sorted(expected), model_id
        assert rows[model_id]["evidence"]["modality_evidenced"] is True, model_id
        assert rows[model_id]["role"] in {"general", None}, model_id
        assert rows[model_id]["chat_selectable"] is True, model_id


@pytest.mark.asyncio
async def test_overlay_decides_and_names_the_matched_family() -> None:
    await _handshake()
    scout = get_effective_capabilities(
        PROVIDER_ID, SOPHIA, "meta-llama/Llama-4-Scout-17B-16E-Instruct"
    )
    assert scout.model_key == "meta-llama-4-scout"  # link rule 3 (overlay matchPatterns)
    assert scout.input_modalities.source == "overlay"
    e4b = get_effective_capabilities(PROVIDER_ID, SOPHIA, "google/gemma-4-E4B-it")
    assert e4b.model_key == "gemma-4-e4b"


@pytest.mark.asyncio
async def test_hf_still_resolves_models_the_overlay_does_not_list() -> None:
    """An overlay-less repo still resolves through the Hugging Face layer."""

    await _handshake()
    llama33 = get_effective_capabilities(PROVIDER_ID, SOPHIA, "meta-llama/Llama-3.3-70B-Instruct")
    assert llama33.input_modalities.value == frozenset({"text"})
    assert llama33.input_modalities.source == "hf_repo"


@pytest.mark.asyncio
async def test_known_text_only_models_are_text_only_from_evidence() -> None:
    report = await _handshake()
    rows = _rows(report)
    gpt_oss = get_effective_capabilities(PROVIDER_ID, SOPHIA, "openai/gpt-oss-120b")
    assert gpt_oss.input_modalities.value == frozenset({"text"})  # overlay: vision false
    llama33 = get_effective_capabilities(PROVIDER_ID, SOPHIA, "meta-llama/Llama-3.3-70B-Instruct")
    assert llama33.input_modalities.value == frozenset({"text"})  # LlamaForCausalLM, no processor
    assert llama33.input_modalities.source == "hf_repo"
    assert rows["meta-llama/Llama-3.3-70B-Instruct"]["modalities"] == ["text"]


@pytest.mark.asyncio
async def test_models_no_source_resolves_stay_unknown_never_text() -> None:
    report = await _handshake()
    rows = _rows(report)
    tulu = rows["allenai/Llama-3.1-Tulu-3-405B"]
    assert tulu["modalities"] == []
    assert tulu["evidence"]["modality_evidenced"] is False
    assert tulu["capabilities_provenance"]["modalities"]["decided_by"] == "unknown"
    # No row anywhere in the fleet is a fabricated text-only placeholder.
    for model_id, row in rows.items():
        if row["modalities"] == ["text"]:
            assert get_effective_capabilities(PROVIDER_ID, SOPHIA, model_id).input_modalities.known


@pytest.mark.asyncio
async def test_image_gate_allows_vision_refuses_known_text_and_passes_unknown() -> None:
    report = await _handshake()
    app = _app(report)

    def gate(model_id: str) -> tuple[bool, str]:
        return image_input_capability(app, ModelRef(provider_id=PROVIDER_ID, model_id=model_id))

    for model_id in VISION:
        assert gate(model_id) == (True, "live_modality_evidence"), model_id
    assert gate("openai/gpt-oss-120b") == (False, "live_modality_evidence")
    assert gate("allenai/Llama-3.1-Tulu-3-405B") == (True, "modality_unknown")


@pytest.mark.asyncio
async def test_surrogates_are_listed_and_refused_only_as_the_chat_model() -> None:
    report = await _handshake()
    rows = _rows(report)

    for embed_id in (
        "Salesforce/SFR-Embedding-Mistral",
        "mistralai/Mistral-7B-Instruct-v0.3-embed",
    ):
        row = rows[embed_id]
        assert row["task"] == "feature-extraction", embed_id  # overlay embeddings: true
        assert row["role"] == "surrogate", embed_id
        assert row["chat_selectable"] is False, embed_id
        assert row["availability"] == "available", embed_id  # listed, first-class
        effective = get_effective_capabilities(PROVIDER_ID, SOPHIA, embed_id)
        assert effective.task.source == "overlay", embed_id

    sam3 = rows["sam3"]
    assert sam3["task"] == "mask-generation"  # ALCF framework=sam3service
    assert sam3["role"] == "surrogate"
    assert sam3["availability"] == "available"
    # Only SELECTING a surrogate as the chat model is refused, with a typed reason.
    app = _app_with(rows, PROVIDER_ID)
    refused = surrogate_selection_error(app, PROVIDER_ID, "sam3")
    assert refused is not None and refused.error.error == SURROGATE_NOT_CHAT
    assert surrogate_selection_error(app, PROVIDER_ID, "google/gemma-3-27b-it") is None
    assert get_effective_capabilities(PROVIDER_ID, SOPHIA, "sam3").task.source == ("server_report")


@pytest.mark.asyncio
async def test_a_second_handshake_does_not_re_ask_the_hub_about_misses(hub: _HubRouter) -> None:
    """Gated files and unknown repos are remembered as misses (no per-handshake re-fetch)."""

    await _handshake()
    first = len(hub.requested)
    assert first > 0
    await _handshake()
    assert len(hub.requested) == first


def test_litellm_embedding_mode_is_feature_extraction() -> None:
    from clio_agent.providers.capabilities.model_sources import community_catalog_facts

    facts = community_catalog_facts("mistral/mistral-embed")
    assert facts is not None
    assert facts.task.value == "feature-extraction"
    assert facts.task.source == "litellm"


def test_litellm_modalities_need_an_explicit_vision_flag() -> None:
    """A LiteLLM row with no ``supports_vision`` key is no evidence of text-only."""

    assert litellm_catalog.modalities_from_info({"mode": "chat"}) is None
    assert litellm_catalog.modalities_from_info({"supports_vision": False}) == frozenset({"text"})
    assert litellm_catalog.modalities_from_info(
        {"supports_vision": True, "supports_audio_input": True, "supports_pdf_input": True}
    ) == frozenset({"text", "image", "audio", "pdf"})


def test_models_dev_output_decides_a_task_only_without_text() -> None:
    assert models_dev.task_from_output(frozenset({"text"})) is None
    assert models_dev.task_from_output(frozenset({"image"})) == "text-to-image"
    assert models_dev.task_from_output(frozenset({"audio"})) == "text-to-speech"
    assert models_dev.task_from_output(None) is None
