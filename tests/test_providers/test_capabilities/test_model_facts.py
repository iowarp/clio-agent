"""Descriptive model facts: description, release date (+ recent), pricing and size.

Evidence only: every value names its source, and a fact no source states stays
unknown -- a size is never read off a model's name. Fixtures:

* ``tests/fixtures/capabilities/openrouter/api_v1_models_all.json`` -- the
  recorded OpenRouter listing (628 models) with ``description``, ``created``
  and ``hugging_face_id`` kept.
* ``tests/fixtures/capabilities/hf_repo/facts_Qwen_*`` -- recorded Hugging Face
  ``/api/models/<repo>`` responses (slimmed to ``sha``/``createdAt``/
  ``safetensors``/``config``) and, for the MoE repo, ``config.json`` at the
  pinned commit (``text_config.num_experts`` / ``num_experts_per_tok``).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from clio_agent.providers import fetched_catalog
from clio_agent.providers.capabilities import accessor, hf_repo, invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.catalog_facts import descriptive_catalog_facts
from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.dialects import cloud, llama_cpp, lm_studio, ollama
from clio_agent.providers.capabilities.dialects import openrouter as openrouter_dialect
from clio_agent.providers.capabilities.facts_wire import model_facts, months_before, plain_text
from clio_agent.providers.capabilities.hf_repo import HfRepoCatalogSource
from clio_agent.providers.capabilities.model_facts import (
    SUBSCRIPTION,
    ParameterCount,
    Price,
    ReleaseDate,
    TokenPricing,
    parameters_from_size_field,
    price_from_per_token,
    pricing_from_per_token,
    release_from_text,
    release_from_unix,
)
from clio_agent.providers.capabilities.model_overlay import (
    OverlayEntry,
    entry_to_model_capabilities,
)
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
)
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import DiscoveredModel, DiscoveredModelFacts
from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake
from clio_agent.providers.handshake.sources import db, litellm_catalog, models_dev

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "capabilities"
OPENROUTER = FIXTURES / "openrouter" / "api_v1_models_all.json"
HF = FIXTURES / "hf_repo"
API_BASE = "https://openrouter.ai/api/v1"
AS_OF = date(2026, 9, 26)

#: repo -> recorded fixture stem.
HF_RECORDED = {
    "Qwen/Qwen3.6-27B": "facts_Qwen_Qwen3.6-27B",
    "Qwen/Qwen3.6-35B-A3B": "facts_Qwen_Qwen3.6-35B-A3B",
}


def _or_row(model_id: str) -> dict[str, Any]:
    rows = json.loads(OPENROUTER.read_text(encoding="utf-8"))["data"]
    return next(row for row in rows if row["id"] == model_id)


class _Hub:
    """Serves the recorded Hub responses for ``httpx.get`` (as FetchedCatalog calls it)."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.requested: list[str] = []
        for repo, stem in HF_RECORDED.items():
            meta_bytes = (HF / f"{stem}_meta.json").read_bytes()
            meta = json.loads(meta_bytes)
            self.routes[f"https://huggingface.co/api/models/{repo}"] = meta_bytes
            for sibling in meta["siblings"]:
                recorded = HF / f"{stem}__{sibling['rfilename']}"
                if recorded.exists():
                    url = f"https://huggingface.co/{repo}/raw/{meta['sha']}/{sibling['rfilename']}"
                    self.routes[url] = recorded.read_bytes()

    def __call__(self, url: str, **_: object) -> httpx.Response:
        self.requested.append(url)
        content = self.routes.get(url)
        status = 200 if content is not None else 404
        return httpx.Response(
            status, content=content or b'{"error":"not found"}', request=httpx.Request("GET", url)
        )


@pytest.fixture
def hub(monkeypatch: pytest.MonkeyPatch) -> _Hub:
    router = _Hub()
    monkeypatch.setattr(fetched_catalog.httpx, "get", router)
    return router


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No community catalog states anything unless a test says so; clean record store."""
    invalidation.clear_all()
    accessor.clear_cache()
    hf_repo.clear_miss_cache()
    hf_repo._metadata_catalog.cache_clear()
    hf_repo._file_catalog.cache_clear()
    monkeypatch.setattr(models_dev, "_load_models_dev", lambda *a, **k: {})
    monkeypatch.setattr(litellm_catalog, "_cost_map", lambda *, allow_fetch=True: {})
    monkeypatch.setattr(db, "lookup_context", lambda model_id: None)
    monkeypatch.setattr(db, "lookup_output", lambda model_id: None)
    yield
    invalidation.clear_all()
    accessor.clear_cache()
    hf_repo.clear_miss_cache()


class _NoOverlay:
    def facts(self, model_key: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.providers.capabilities import model_overlay

    monkeypatch.setattr(model_overlay, "default_overlay_source", lambda: _NoOverlay())


async def _enriched_openrouter(model_id: str) -> tuple[ModelCapabilities, DeploymentCapabilities]:
    """Parse one recorded row and run the real enrich step (Hub layer included)."""
    from clio_agent.providers.catalog import get_provider

    model, deployment = openrouter_dialect.parse_model_row(
        _or_row(model_id), provider_id="openrouter", api_base=API_BASE
    )
    handshake = OpenAICompatHandshake(provider=get_provider("openrouter"))
    ctx = HandshakeContext(
        provider_id="openrouter",
        provider_kind="openai",
        api_base=API_BASE,
        api_key="sk-or-fixture",
        allow_external_sources=True,
    )
    facts = DiscoveredModelFacts(
        discovered=DiscoveredModel(id=model_id), model=model, deployment=deployment
    )
    enriched = await handshake.enrich_capabilities(facts, ctx)
    return enriched.model, enriched.deployment


def _wire(
    model: ModelCapabilities, deployment: DeploymentCapabilities | None = None
) -> dict[str, Any]:
    effective = combine_capabilities(model, None, deployment)
    return model_facts(effective, model_key=model.model_key, as_of=AS_OF)


# --------------------------------------------------------------------------- description


def test_pareto_code_router_carries_its_description_with_links_as_data() -> None:
    model, deployment = openrouter_dialect.parse_model_row(
        _or_row("openrouter/pareto-code"), provider_id="openrouter", api_base=API_BASE
    )
    facts = _wire(model, deployment)
    description = facts["description"]
    assert description["value"]["text"].startswith("The Pareto Router maintains")
    assert "[Artificial Analysis](https://artificialanalysis.ai/)" in description["value"]["text"]
    assert "](" not in description["value"]["plain"]
    assert "ranked by Artificial Analysis coding percentiles" in description["value"]["plain"]
    assert {"text": "Artificial Analysis", "url": "https://artificialanalysis.ai/"} in (
        description["value"]["links"]
    )
    assert description["evidence"][0]["source"] == "openrouter"
    assert description["evidence"][0]["detail"] == "openrouter description"


def test_bodybuilder_router_has_a_description_too() -> None:
    model, deployment = openrouter_dialect.parse_model_row(
        _or_row("openrouter/bodybuilder"), provider_id="openrouter", api_base=API_BASE
    )
    assert _wire(model, deployment)["description"]["value"]["text"]


def test_plain_text_keeps_text_without_links_unchanged() -> None:
    assert plain_text("no links here") == ("no links here", [])
    assert plain_text("see [docs](/docs/x) now") == (
        "see docs now",
        [{"text": "docs", "url": "/docs/x"}],
    )


# --------------------------------------------------------------------------- released_at / recent


def test_a_model_created_over_six_months_ago_is_not_recent_and_a_new_one_is() -> None:
    old_model, old_dep = openrouter_dialect.parse_model_row(
        _or_row("openrouter/auto"), provider_id="openrouter", api_base=API_BASE
    )
    new_model, new_dep = openrouter_dialect.parse_model_row(
        _or_row("respan/span-01"), provider_id="openrouter", api_base=API_BASE
    )
    old = _wire(old_model, old_dep)
    new = _wire(new_model, new_dep)
    assert old["released_at"]["value"] == {"date": "2023-11-08", "precision": "day"}
    assert old["released_at"]["evidence"][0]["detail"] == "openrouter created=1699401600"
    assert old["recent"]["value"] is False
    assert new["released_at"]["value"]["date"] == "2026-09-26"
    assert new["recent"] == {
        "value": True,
        "window_months": 6,
        "as_of": "2026-09-26",
        "evidence": new["released_at"]["evidence"],
    }


def test_recent_is_computed_at_serve_time_not_stored() -> None:
    model = ModelCapabilities(
        model_key="m", released_at=Fact(ReleaseDate("2026-04-01", "day"), "models.dev", "", "x")
    )
    effective = combine_capabilities(model, None, None)
    assert model_facts(effective, model_key="m", as_of=date(2026, 9, 26))["recent"]["value"] is True
    assert (
        model_facts(effective, model_key="m", as_of=date(2026, 10, 2))["recent"]["value"] is False
    )
    assert not hasattr(model, "recent")


def test_release_date_spellings_keep_their_precision() -> None:
    assert release_from_unix(1699401600) == ReleaseDate("2023-11-08", "day")
    assert release_from_text("2025-04-27T03:43:05.000Z") == ReleaseDate("2025-04-27", "day")
    assert release_from_text("2026-02") == ReleaseDate("2026-02", "month")
    assert release_from_text("not a date") is None
    assert release_from_unix(True) is None
    assert months_before(date(2026, 8, 31), 6) == date(2026, 2, 28)


def test_models_dev_release_outranks_the_hub_created_at(
    monkeypatch: pytest.MonkeyPatch, hub: _Hub
) -> None:
    """A curated catalog release_date beats a Hub repo's createdAt; the Hub still gives size."""
    from clio_agent.providers.capabilities.model_sources import resolve_model_capabilities

    catalog = {
        "alibaba/qwen3.6-27b": {
            "id": "qwen3.6-27b",
            "description": "Dense Qwen 3.6",
            "release_date": "2026-04",
            "weights": [
                {"label": "Hugging Face", "url": "https://huggingface.co/Qwen/Qwen3.6-27B"}
            ],
        }
    }
    monkeypatch.setattr(models_dev, "_load_models_dev", lambda *a, **k: catalog)
    resolved = resolve_model_capabilities("qwen3.6-27b", overlay=_NoOverlay())
    assert resolved.released_at.value == ReleaseDate("2026-04", "month")
    assert resolved.released_at.source == "models.dev"
    # The models.dev weights link is the evidence the Hub layer reads size through.
    assert resolved.hf_repo.value == "Qwen/Qwen3.6-27B"
    assert resolved.parameters.value == ParameterCount(total=27_781_427_952)
    assert resolved.parameters.source == "hf_repo"
    assert resolved.description.value == "Dense Qwen 3.6"


# --------------------------------------------------------------------------- pricing


def test_pricing_converts_per_token_strings_to_per_million_numbers() -> None:
    assert price_from_per_token("0.00000015") == Price("usd", Decimal("0.15"))
    assert price_from_per_token("0.0000015") == Price("usd", Decimal("1.5"))
    assert price_from_per_token(3e-06) == Price("usd", Decimal(3))
    assert price_from_per_token("0") == Price("usd", Decimal(0))
    assert price_from_per_token("-1") == Price("variable")
    assert price_from_per_token("-0.5") is None
    assert price_from_per_token("abc") is None
    assert pricing_from_per_token("0.1", None) is None


def test_variable_price_is_typed_and_never_a_number_or_free() -> None:
    model, deployment = openrouter_dialect.parse_model_row(
        _or_row("openrouter/pareto-code"), provider_id="openrouter", api_base=API_BASE
    )
    pricing = _wire(model, deployment)["pricing"]
    assert pricing["value"] == {
        "unit": "usd_per_1m_tokens",
        "input": {"kind": "variable", "per_1m": None},
        "output": {"kind": "variable", "per_1m": None},
    }
    assert pricing["evidence"][0]["source"] == "server_report"
    assert deployment.free.value is False


def test_metered_price_is_a_slider_number() -> None:
    model, deployment = openrouter_dialect.parse_model_row(
        _or_row("perceptron/perceptron-mk1.5"), provider_id="openrouter", api_base=API_BASE
    )
    value = _wire(model, deployment)["pricing"]["value"]
    assert value["input"] == {"kind": "usd", "per_1m": 0.15}
    assert value["output"] == {"kind": "usd", "per_1m": 1.5}


def test_endpoint_price_wins_and_the_catalog_list_price_is_an_alternative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cost_map = {
        "anthropic/claude-sonnet-4-5": {
            "input_cost_per_token": 3e-06,
            "output_cost_per_token": 1.5e-05,
        }
    }
    monkeypatch.setattr(litellm_catalog, "_cost_map", lambda *, allow_fetch=True: cost_map)
    catalog = descriptive_catalog_facts("claude-sonnet-4-5")
    assert catalog is not None
    model = ModelCapabilities(
        model_key="claude-sonnet-4-5", catalog_pricing=catalog.catalog_pricing
    )
    subscription = DeploymentCapabilities(
        provider_id="claude_code",
        api_base="claude-code://",
        model_id="claude-sonnet-4-5",
        pricing=Fact(SUBSCRIPTION, "dialect", "", "claude_code: subscription plan"),
    )
    pricing = _wire(model, subscription)["pricing"]
    assert pricing["value"]["input"] == {"kind": "subscription", "per_1m": None}
    assert pricing["evidence"][0]["source"] == "dialect"
    assert pricing["alternatives"] == [
        {
            "value": {
                "unit": "usd_per_1m_tokens",
                "input": {"kind": "usd", "per_1m": 3.0},
                "output": {"kind": "usd", "per_1m": 15.0},
            },
            "evidence": [
                {
                    "source": "litellm",
                    "detail": "litellm anthropic/claude-sonnet-4-5 input_cost_per_token=3e-06 "
                    "output_cost_per_token=1.5e-05",
                    "observed_at": pricing["alternatives"][0]["evidence"][0]["observed_at"],
                }
            ],
        }
    ]
    # With no endpoint price (a cloud API key), the catalog list price is the value.
    assert _wire(model)["pricing"]["value"]["output"] == {"kind": "usd", "per_1m": 15.0}


def test_subscription_is_not_free() -> None:
    assert SUBSCRIPTION.free is False
    assert TokenPricing(Price("usd", Decimal(0)), Price("usd", Decimal(0))).free is True


# --------------------------------------------------------------------------- parameters


@pytest.mark.asyncio
async def test_hf_linked_openrouter_model_gets_its_size_from_safetensors(hub: _Hub) -> None:
    model, deployment = await _enriched_openrouter("qwen/qwen3.6-27b")
    assert model.hf_repo.value == "Qwen/Qwen3.6-27B"
    assert model.parameters.value == ParameterCount(total=27_781_427_952)
    assert model.parameters.source == "hf_repo"
    assert "6a9e13bd6fc8" in model.parameters.detail  # pinned to the commit it read
    assert "safetensors.total=27781427952" in model.parameters.detail
    # OpenRouter's own created outranks the Hub's createdAt (golden provider).
    assert model.released_at.source == "openrouter"
    # Only the metadata call: an endpoint that states everything else reads no files.
    assert hub.requested == ["https://huggingface.co/api/models/Qwen/Qwen3.6-27B"]
    facts = _wire(model, deployment)
    assert facts["parameters"]["value"] == {
        "total": 27_781_427_952,
        "active": None,
        "experts_total": None,
        "experts_active": None,
        "precision": "exact",
    }
    assert facts["parameters"]["evidence"][0]["source"] == "hf_repo"


@pytest.mark.asyncio
async def test_an_unlinked_model_has_unknown_size_never_guessed_from_its_name(hub: _Hub) -> None:
    row = _or_row("nvidia/nemotron-3-ultra-550b-a55b")  # "550b" in the id, but ...
    row = {**row, "hugging_face_id": None}  # ... no source links it to any weights
    model, _ = openrouter_dialect.parse_model_row(row, provider_id="openrouter", api_base=API_BASE)
    assert not model.hf_repo.known
    assert not model.parameters.known
    enriched, deployment = await _enriched_openrouter("openrouter/auto")
    assert not enriched.parameters.known
    assert _wire(enriched, deployment)["parameters"] is None
    assert hub.requested == []  # nothing linked, nothing asked


def test_moe_total_and_experts_from_the_config_fixture(hub: _Hub) -> None:
    source = HfRepoCatalogSource(repo_id="Qwen/Qwen3.6-35B-A3B")
    facts = source.facts("Qwen/Qwen3.6-35B-A3B")
    assert facts is not None
    count = facts.parameters.value
    assert count == ParameterCount(
        total=35_951_822_704, experts_total=256, experts_active=8, active=None
    )
    assert "config.json text_config.num_experts=256" in facts.parameters.detail
    assert "text_config.num_experts_per_tok=8" in facts.parameters.detail
    assert facts.released_at.value == ReleaseDate("2026-04-15", "day")
    assert facts.released_at.source == "hf_repo"
    wire = _wire(facts)["parameters"]["value"]
    assert wire["total"] == 35_951_822_704  # the slider value
    assert (wire["experts_total"], wire["experts_active"]) == (256, 8)


def test_overlay_parameters_state_total_and_active() -> None:
    entry = OverlayEntry(
        family="qwen3.6-35b-a3b",
        match_patterns=("qwen3.6-35b-a3b",),
        capabilities={
            "parameters": {"total": 35_951_822_704, "active": 3_000_000_000},
            "released": "2026-04-15",
            "description": "Qwen 3.6 MoE",
        },
        quirks={},
        root="project",
    )
    model = entry_to_model_capabilities("qwen3.6-35b-a3b", entry, "qwen3.6-35b-a3b")
    assert model.parameters.value == ParameterCount(total=35_951_822_704, active=3_000_000_000)
    assert model.parameters.source == "overlay"
    assert model.released_at.value == ReleaseDate("2026-04-15", "day")
    assert model.description.value == "Qwen 3.6 MoE"
    malformed = OverlayEntry("x", ("x",), {"parameters": "70b"}, {}, "project")
    assert not entry_to_model_capabilities("x", malformed, "x").parameters.known


def test_ollama_show_states_exact_count_and_experts() -> None:
    show = {
        "capabilities": ["completion", "tools"],
        "details": {"parameter_size": "30.5B"},
        "model_info": {
            "general.architecture": "qwen3moe",
            "general.parameter_count": 30_532_122_624,
            "qwen3moe.expert_count": 128,
            "qwen3moe.expert_used_count": 8,
        },
    }
    model = ollama.parse_show(show, model_key="qwen3:30b-a3b")
    assert model.parameters.value == ParameterCount(
        total=30_532_122_624, experts_total=128, experts_active=8
    )
    assert "general.parameter_count=30532122624" in model.parameters.detail
    rounded = ollama.parse_show({"details": {"parameter_size": "7.6B"}}, model_key="m")
    assert rounded.parameters.value == ParameterCount(total=7_600_000_000, precision="rounded")
    assert not ollama.parse_show({"details": {}}, model_key="llama3:70b").parameters.known


def test_llama_cpp_and_lm_studio_state_their_own_size_fields() -> None:
    payload = {
        "data": [{"id": "m.gguf", "meta": {"n_ctx_train": 40960, "n_params": 8_190_735_360}}]
    }
    model = llama_cpp.build_model_capabilities("m", payload, "m.gguf")
    assert model.parameters.value == ParameterCount(total=8_190_735_360)
    no_meta = llama_cpp.build_model_capabilities(
        "m", {"data": [{"id": "qwen3-8b.gguf"}]}, "qwen3-8b.gguf"
    )
    assert not no_meta.parameters.known
    lms, _ = lm_studio.parse_v1_row(
        {"id": "qwen/qwen3-8b", "params_string": "8B"}, provider_id="lm", api_base="http://x"
    )
    assert lms.parameters.value == ParameterCount(total=8_000_000_000, precision="rounded")
    assert parameters_from_size_field("qwen3-8b") is None


# --------------------------------------------------------------------------- cloud rows


def test_cloud_rows_state_release_dates_only_through_their_own_fields() -> None:
    anthropic = cloud.model_row_facts(
        "anthropic",
        {"id": "claude-sonnet-4-5", "created_at": "2025-09-29T00:00:00Z"},
        observed_at="",
    )
    assert anthropic["released_at"].value == ReleaseDate("2025-09-29", "day")
    openai = cloud.model_row_facts(
        "openai", {"id": "gpt-4o", "created": 1715367049}, observed_at=""
    )
    assert openai["released_at"].value == ReleaseDate("2024-05-10", "day")
    # A vLLM/NIM ``created`` is the server's own clock, not a release.
    assert cloud.model_row_facts("nvidia_nim", {"created": 1715367049}, observed_at="") == {}
    gemini = cloud.model_row_facts(
        "gemini", {"id": "gemini-2.5-pro", "description": "Stable release"}, observed_at=""
    )
    assert gemini["description"].value == "Stable release"


def test_catalog_row_serves_model_facts_next_to_capability_tags() -> None:
    from clio_agent.gact.provider_catalog import model_catalog_row
    from clio_agent.gact.types import LMProviderPreset
    from clio_agent.providers.handshake.model import AuthState, ConnectivityState, HandshakeReport

    model, deployment = openrouter_dialect.parse_model_row(
        _or_row("openrouter/pareto-code"), provider_id="openrouter", api_base=API_BASE
    )
    invalidation.record_model_capabilities(model)
    invalidation.record_deployment_capabilities(deployment)
    preset = LMProviderPreset(
        id="openrouter",
        label="OpenRouter",
        provider="openai",
        api_base=API_BASE,
        suggested_model="",
    )
    report = HandshakeReport(
        provider_id="openrouter",
        provider_kind="openai",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base=API_BASE,
    )
    row = model_catalog_row(preset, report, DiscoveredModel(id="openrouter/pareto-code"))
    assert "pricing" not in row  # cut: the per-token strings live only in evidence now
    facts = row["model_facts"]
    assert set(facts) == {
        "model_key",
        "description",
        "released_at",
        "recent",
        "pricing",
        "parameters",
    }
    assert facts["description"]["value"]["plain"].startswith("The Pareto Router")
    assert facts["parameters"] is None
    effective = get_effective_capabilities("openrouter", API_BASE, "openrouter/pareto-code")
    assert effective.router.value is True


# --------------------------------------------------------------------------- CLI subscriptions


def test_cli_rows_are_priced_as_subscription_with_cached_catalog_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from clio_agent.providers.handshake.cli_catalog import CliCatalogHandshake

    fetches: list[bool] = []

    def _catalog(*_a: Any, allow_fetch: bool = True, **_k: Any) -> dict[str, Any]:
        fetches.append(allow_fetch)
        return {"openai/gpt-5.6-sol": {"id": "gpt-5.6-sol", "release_date": "2026-08-14"}}

    monkeypatch.setattr(models_dev, "_load_models_dev", _catalog)
    ctx = HandshakeContext(
        provider_id="codex",
        provider_kind="codex",
        api_base="codex://direct",
        allow_external_sources=True,
    )
    raw = {
        "id": "gpt-5.6-sol",
        "description": "Frontier agentic coding model.",
        "context_window": 272000,
        "_overlay_context_checked": True,
    }
    facts = asyncio.run(CliCatalogHandshake(provider=None).discover_model_config(None, ctx, raw))
    assert facts.deployment.pricing.value == SUBSCRIPTION
    assert facts.deployment.pricing.source == "dialect"
    assert not facts.deployment.free.known  # a plan is neither $0 nor "free"
    assert facts.model.description.value == "Frontier agentic coding model."
    assert facts.model.released_at.value == ReleaseDate("2026-08-14", "day")
    assert fetches and not any(fetches)  # the passive CLI read never touches the network
    wire = _wire(facts.model, facts.deployment)["pricing"]["value"]
    assert wire["input"] == {"kind": "subscription", "per_1m": None}
