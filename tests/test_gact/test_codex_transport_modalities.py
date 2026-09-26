"""Per-transport Codex modalities: only the Direct transport claims PDF input.

Both transports list the same model ids. The Direct transport delivers PDFs as
Responses ``input_file`` parts; the SDK transport cannot carry files. These
tests pin that a selection's own transport answers "can this model take a
PDF?" -- never the other transport's rows or records.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.modality_evidence import catalog_model_rows, live_model_modalities
from clio_agent.gact.provider_catalog import _record_sdk_model
from clio_agent.gact.types import ModelRef
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.accessor import get_effective_capabilities
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    modalities_from_capabilities,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)

_NOW = "2026-09-26T12:00:00+00:00"
_MODEL = "gpt-6-sol"


@pytest.fixture(autouse=True)
def _clean_capability_store() -> Any:
    invalidation.clear_all()
    yield
    invalidation.clear_all()


def _seed_direct_model_record() -> None:
    """What the Direct handshake records from its overlay row (live list + PDF fact)."""

    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key=_MODEL,
            context_max=Fact(value=272000, source="server_report", observed_at=_NOW),
            input_modalities=Fact(
                value=modalities_from_capabilities(("text", "image", "pdf")),
                source="server_report",
                observed_at=_NOW,
            ),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="codex",
            api_base="codex://direct",
            model_id=_MODEL,
            model_key=Fact(value=_MODEL, source="server_report", observed_at=_NOW),
        )
    )


def test_each_transport_keeps_its_own_facts_for_a_shared_model_id() -> None:
    """Owner ruling: each half is its own model list -- no fact crosses over."""

    _seed_direct_model_record()

    _record_sdk_model(
        "codex",
        {
            "id": _MODEL,
            "capabilities": ["text", "image"],
            "supported_reasoning_efforts": ["low", "medium", "high"],
            "default_reasoning_effort": "medium",
        },
        observed_at=_NOW,
    )

    sdk = get_effective_capabilities("codex", "codex://sdk", _MODEL)
    direct = get_effective_capabilities("codex", "codex://direct", _MODEL)
    assert sdk.input_modalities.value == frozenset({"text", "image"})
    assert direct.input_modalities.value == frozenset({"text", "image", "pdf"})
    # The Direct list's context window is NOT copied onto the SDK row (the SDK's
    # model/list does not report one), and the SDK row did not touch Direct's.
    assert sdk.context.value is None
    assert direct.context.value == 272000
    assert sdk.thinking.known
    assert not direct.thinking.known


def test_sdk_rows_alone_report_what_the_sdk_list_says() -> None:
    _record_sdk_model("codex", {"id": _MODEL, "capabilities": ["text", "image"]}, observed_at=_NOW)

    sdk = get_effective_capabilities("codex", "codex://sdk", _MODEL)
    assert sdk.input_modalities.value == frozenset({"text", "image"})
    assert (
        get_effective_capabilities("codex", "codex://direct", _MODEL).input_modalities.value is None
    )


def _row(transport: str, modalities: list[str]) -> dict[str, Any]:
    return {
        "model_id": _MODEL,
        "transport": transport,
        "modalities": modalities,
        "availability": "available",
        "evidence": {
            "source": "overlay",
            "evidenced": True,
            "modality_evidenced": True,
            "generated_at": _NOW,
        },
    }


def _app(report: HandshakeReport | None = None) -> Any:
    catalog = {
        "providers": [
            {
                "id": "codex",
                "health": "ready",
                "models": [
                    _row("sdk", ["image", "text"]),
                    _row("direct", ["image", "pdf", "text"]),
                ],
            }
        ]
    }
    return SimpleNamespace(
        state=SimpleNamespace(provider_catalog=catalog, lm_handshake_report=report)
    )


def test_catalog_rows_answer_for_the_selections_own_transport() -> None:
    app = _app()

    sdk_rows = catalog_model_rows(
        app, ModelRef(provider_id="codex", model_id=_MODEL, variant="sdk")
    )
    direct_rows = catalog_model_rows(app, ModelRef(provider_id="codex", model_id=_MODEL))

    assert [row["transport"] for row in sdk_rows] == ["sdk"]
    # An unset variant is the Direct transport (LMProviderConfig's normalization).
    assert [row["transport"] for row in direct_rows] == ["direct"]
    sdk = live_model_modalities(app, ModelRef(provider_id="codex", model_id=_MODEL, variant="sdk"))
    direct = live_model_modalities(
        app, ModelRef(provider_id="codex", model_id=_MODEL, variant="direct")
    )
    assert sdk.modalities == frozenset({"text", "image"})
    assert direct.modalities == frozenset({"text", "image", "pdf"})


def test_an_sdk_selection_never_reads_the_direct_handshake_report() -> None:
    """The bound codex handshake report is the Direct transport (codex://direct)."""

    _seed_direct_model_record()
    report = HandshakeReport(
        provider_id="codex",
        provider_kind="codex",
        api_base="codex://direct",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models_source="overlay",
        generated_at=_NOW,
        models=(DiscoveredModel(id=_MODEL),),
    )
    app = _app(report)

    direct = live_model_modalities(app, ModelRef(provider_id="codex", model_id=_MODEL))
    sdk = live_model_modalities(app, ModelRef(provider_id="codex", model_id=_MODEL, variant="sdk"))

    assert direct.evidence == "discovery_overlay"
    assert "pdf" in (direct.modalities or frozenset())
    assert sdk.modalities == frozenset({"text", "image"})


def test_codex_picker_offers_off_only_when_the_model_lists_it() -> None:
    """The backend refuses an unlisted effort (live: gpt-6-astra rejects 'none')."""

    from clio_agent.gact.provider_catalog import _reasoning_wire_block
    from clio_agent.providers.capabilities.records import ThinkingSpec

    def _block(levels: tuple[str, ...], *, codex: bool) -> list[str]:
        spec = ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level={})
        thinking = SimpleNamespace(
            spec=spec, control="reasoning_effort", known=True, decided_by="model"
        )
        block = _reasoning_wire_block(thinking, DiscoveredModel(id=_MODEL), listed_off_only=codex)
        return list(block["levels"])

    assert _block(("low", "high"), codex=True) == ["low", "high"]
    assert _block(("off", "low"), codex=True) == ["off", "low"]
    # Other providers keep their generic "off" (omitting the directive is off there).
    assert _block(("low", "high"), codex=False) == ["off", "low", "high"]
