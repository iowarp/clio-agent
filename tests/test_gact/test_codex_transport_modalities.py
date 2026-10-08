"""Codex direct modalities: the Direct transport claims PDF input.

The Direct transport delivers PDFs as Responses ``input_file`` parts, so a Codex
selection's catalog rows and bound handshake report both answer "this model takes
a PDF".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.modality_evidence import catalog_model_rows, live_model_modalities
from clio_agent.gact.types import ModelRef
from clio_agent.providers.capabilities import invalidation
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


def _row(modalities: list[str]) -> dict[str, Any]:
    return {
        "model_id": _MODEL,
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
                    _row(["image", "pdf", "text"]),
                ],
            }
        ]
    }
    return SimpleNamespace(
        state=SimpleNamespace(provider_catalog=catalog, lm_handshake_report=report)
    )


def test_catalog_rows_answer_for_a_codex_selection() -> None:
    app = _app()

    rows = catalog_model_rows(app, ModelRef(provider_id="codex", model_id=_MODEL))

    assert [row["model_id"] for row in rows] == [_MODEL]
    assert "transport" not in rows[0]
    modalities = live_model_modalities(app, ModelRef(provider_id="codex", model_id=_MODEL))
    assert modalities.modalities == frozenset({"text", "image", "pdf"})


def test_a_codex_selection_reads_the_direct_handshake_report() -> None:
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

    direct = live_model_modalities(_app(report), ModelRef(provider_id="codex", model_id=_MODEL))

    assert direct.evidence == "discovery_overlay"
    assert "pdf" in (direct.modalities or frozenset())


def test_codex_picker_offers_off_only_when_the_model_lists_it() -> None:
    """The backend refuses an unlisted effort (live: gpt-6-astra rejects 'none')."""

    from clio_agent.gact.provider_catalog import _reasoning_wire_block
    from clio_agent.providers.capabilities.records import ThinkingSpec

    def _block(levels: tuple[str, ...], *, dialect: str) -> list[str]:
        spec = ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level={})
        thinking = SimpleNamespace(
            spec=spec, control="reasoning_effort", known=True, decided_by="model"
        )
        block = _reasoning_wire_block(thinking, DiscoveredModel(id=_MODEL), dialect=dialect)
        return list(block["levels"])

    assert _block(("low", "high"), dialect="codex") == ["low", "high"]
    assert _block(("off", "low"), dialect="codex") == ["off", "low"]
    # A transport whose "off" holds for every model keeps it (claude_code's disabled option).
    assert _block(("low", "high"), dialect="claude_code") == ["off", "low", "high"]


def test_a_model_whose_template_has_no_thinking_is_not_offered_reasoning() -> None:
    """Live on ares: Qwen2.5-0.5B-Instruct on llama.cpp read ``supported: true``.

    Its template scan says mechanism "none" -- a known spec -- and ``known`` was
    taken to mean "supports reasoning".
    """

    from clio_agent.gact.provider_catalog import _reasoning_wire_block
    from clio_agent.providers.capabilities.records import ThinkingSpec

    def _supported(mechanism: str) -> bool:
        thinking = SimpleNamespace(
            spec=ThinkingSpec(mechanism=mechanism),  # type: ignore[arg-type]
            control=None,
            known=True,
            decided_by="model",
        )
        return bool(_reasoning_wire_block(thinking, DiscoveredModel(id=_MODEL))["supported"])

    assert _supported("none") is False
    assert _supported("always_on") is True


def test_an_on_off_model_is_offered_a_switch_not_an_effort_ladder() -> None:
    """Live c14: Ollama qwen3:4b (think on/off) was offered off/low/medium/high (F039)."""

    from clio_agent.gact.provider_catalog import ON_OFF_LEVEL, _reasoning_wire_block
    from clio_agent.providers.capabilities.records import ThinkingSpec

    thinking = SimpleNamespace(
        spec=ThinkingSpec(mechanism="on_off"), control="think", known=True, decided_by="model"
    )
    block = _reasoning_wire_block(thinking, DiscoveredModel(id=_MODEL), dialect="ollama")
    assert [x for x in block["levels"] if x != "off"] == [ON_OFF_LEVEL]
    assert block["control"] == "toggle"
    assert block["supported"] is True
