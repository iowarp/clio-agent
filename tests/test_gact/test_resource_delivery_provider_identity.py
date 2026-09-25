"""Regression: modality evidence must never be matched by provider KIND (#1418).

Nine provider presets share the wire kind ``"openai"`` (bedrock, llama_cpp,
azure_openai, ...). ``_live_modalities`` used to accept a model ref whose
``provider_id`` merely matched the ACTIVE handshake report's ``provider_kind``
-- so a message routed to ``llama_cpp`` could read Bedrock's live vision
evidence just because both dialects speak the ``openai`` kind. These tests
drive that resolver directly against a fake ``app``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.resource_delivery import live_model_modalities
from clio_agent.gact.types import ModelRef
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


def _app(*, report: HandshakeReport | None, catalog: Any = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(lm_handshake_report=report, provider_catalog=catalog)
    )


def _bedrock_report() -> HandshakeReport:
    """A live handshake for Bedrock -- provider_kind "openai", id "bedrock"."""

    return HandshakeReport(
        provider_id="bedrock",
        provider_kind="openai",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models_source="live",
        generated_at="2026-09-24T00:00:00+00:00",
        models=(
            ModelProfile(
                id="anthropic.claude-3-5-sonnet-20240620-v1:0",
                capabilities=("vision",),
            ),
        ),
    )


def test_matching_provider_id_reads_its_own_live_evidence() -> None:
    """The report's OWN provider_id still matches (sanity check)."""

    app = _app(report=_bedrock_report())
    model = ModelRef(provider_id="bedrock", model_id="anthropic.claude-3-5-sonnet-20240620-v1:0")
    modalities, evidence, _generated_at = live_model_modalities(app, model)
    assert "image" in modalities
    assert evidence == "live_handshake"


def test_bare_kind_provider_id_does_not_borrow_another_providers_evidence() -> None:
    """A model ref naming the KIND ("openai") must not match Bedrock's report.

    Before the fix, ``model.provider_id == "openai"`` matched
    ``report.provider_kind == "openai"`` even though the live report is for
    the DISTINCT ``bedrock`` provider id -- exactly how a llama.cpp message
    could read Bedrock's (or any other same-kind provider's) capabilities.
    """

    app = _app(report=_bedrock_report())
    model = ModelRef(provider_id="openai", model_id="anthropic.claude-3-5-sonnet-20240620-v1:0")
    modalities, evidence, _generated_at = live_model_modalities(app, model)
    assert modalities == {"text"}
    assert evidence == "unavailable"


def test_a_different_same_kind_provider_id_does_not_borrow_the_evidence() -> None:
    """``llama_cpp`` (also kind "openai") must not read Bedrock's live report."""

    app = _app(report=_bedrock_report())
    model = ModelRef(provider_id="llama_cpp", model_id="anthropic.claude-3-5-sonnet-20240620-v1:0")
    modalities, evidence, _generated_at = live_model_modalities(app, model)
    assert modalities == {"text"}
    assert evidence == "unavailable"
