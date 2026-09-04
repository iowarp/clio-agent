"""Tests for ``CliCatalogHandshake`` (iowarp/clio-agent#1211 review D4).

Zero tests existed on this path before this change (flagged explicitly in
review). Covers: overlay-first ``discover_models`` (falling back to the static
registry catalog when absent/malformed, logging the malformed case — review
R5), and the D4 fix itself — an overlay-sourced model's context/output limit is
pre-filled onto its ``ModelProfile`` in ``discover_model_config`` and
``enrich_capabilities`` SKIPS the models.dev/litellm/local-DB cascade entirely
for it (whether the persisted value is a real number or a confirmed miss),
while a static-catalog-sourced row (no overlay yet) still runs the cascade
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.cli_catalog import CliCatalogHandshake
from clio_agent.providers.model_discovery import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
    record_refresh,
)


def _ctx(provider_id: str = "codex", provider_kind: str = "codex") -> HandshakeContext:
    return HandshakeContext(
        provider_id=provider_id,
        provider_kind=provider_kind,
        api_base="codex://sdk",
        allow_external_sources=True,
    )


# --------------------------------------------------------------------------- #
# discover_models: overlay-first, static fallback, malformed-overlay degrade.
# --------------------------------------------------------------------------- #


def test_discover_models_absent_overlay_falls_back_to_static_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    handshake = CliCatalogHandshake(provider=None)
    rows = asyncio.run(handshake.discover_models(client=None, ctx=_ctx()))
    # The static registry catalog for codex (3 candidate ids); none carry the
    # overlay marker.
    assert rows
    assert all("_overlay_context_checked" not in r for r in rows)


def test_discover_models_present_overlay_served_with_context_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    record_refresh(
        ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {
                    "id": "gpt-5.6-sol",
                    "name": "GPT-5.6-Sol",
                    "description": "",
                    "context_window": 272000,
                    "context_source": "models.dev",
                    "output_limit": 64000,
                    "capabilities": ["text", "image"],
                }
            ],
            source=CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )
    handshake = CliCatalogHandshake(provider=None)
    rows = asyncio.run(handshake.discover_models(client=None, ctx=_ctx()))
    assert [r["id"] for r in rows] == ["gpt-5.6-sol"]
    assert rows[0]["context_window"] == 272000
    assert rows[0]["output_limit"] == 64000
    assert rows[0]["capabilities"] == ["text", "image"]
    assert rows[0]["_overlay_context_checked"] is True


def test_discover_models_malformed_overlay_degrades_to_static_and_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The passive/ambient path must never crash on a corrupt overlay (RULE 2);
    it degrades to static -- but the degrade is LOGGED (#1211 review R5), never
    silent."""
    overlay_file = tmp_path / "overlay.json"
    overlay_file.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(overlay_file))
    handshake = CliCatalogHandshake(provider=None)
    with caplog.at_level(logging.WARNING):
        rows = asyncio.run(handshake.discover_models(client=None, ctx=_ctx()))
    assert rows  # degraded to the static catalog, not an empty/crashed result
    assert all("_overlay_context_checked" not in r for r in rows)
    assert any("overlay malformed" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# discover_model_config + enrich_capabilities: the D4 cascade-skip.
# --------------------------------------------------------------------------- #


def test_discover_model_config_prefills_from_overlay_row() -> None:
    handshake = CliCatalogHandshake(provider=None)
    raw = {
        "id": "gpt-5.6-sol",
        "name": "GPT-5.6-Sol",
        "description": "",
        "context_window": 272000,
        "output_limit": 64000,
        "context_source": "models.dev",
        "capabilities": ["text", "image"],
        "_overlay_context_checked": True,
    }
    profile = asyncio.run(handshake.discover_model_config(client=None, ctx=_ctx(), raw=raw))
    assert profile.id == "gpt-5.6-sol"
    assert profile.context_window == 272000
    assert profile.output_limit == 64000
    assert profile.capabilities == ("text", "image")
    assert profile.context_source == "models.dev"


def test_discover_model_config_prefills_a_confirmed_miss_as_none() -> None:
    """A model that was CHECKED at refresh time but had no context/output limit
    anywhere in the cascade still carries the "checked" marker -- the miss is
    itself the persisted, authoritative answer."""
    handshake = CliCatalogHandshake(provider=None)
    raw = {
        "id": "some-obscure-model",
        "context_window": None,
        "output_limit": None,
        "context_source": "",
        "_overlay_context_checked": True,
    }
    profile = asyncio.run(handshake.discover_model_config(client=None, ctx=_ctx(), raw=raw))
    assert profile.context_window is None
    assert profile.output_limit is None
    assert profile.raw.get("_overlay_context_checked") is True


def test_discover_model_config_static_row_falls_through_to_noop_base() -> None:
    """A row with NO overlay marker (the static-catalog fallback) is untouched --
    same behavior as before #1211, the cascade still runs for it downstream."""
    handshake = CliCatalogHandshake(provider=None)
    raw = {"id": "gpt-5.5", "name": "GPT-5.5", "description": "candidate"}
    profile = asyncio.run(handshake.discover_model_config(client=None, ctx=_ctx(), raw=raw))
    assert profile.id == "gpt-5.5"
    assert profile.context_window is None  # unresolved -- NoOpHandshake's base behavior


def test_enrich_capabilities_skips_the_cascade_for_an_overlay_checked_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1211 D4 -- the real fix: enrich_capabilities must NEVER call
    resolve_context/resolve_output_limit for an overlay-checked profile, even
    when its context_window is a confirmed-miss None (never re-attempt the
    cascade)."""
    from clio_agent.providers.handshake.model import ModelProfile

    calls: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_context",
        lambda model_id, kind: (calls.append("resolve_context"), (None, ""))[1],
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_output_limit",
        lambda model_id, kind: (calls.append("resolve_output_limit"), None)[1],
    )

    handshake = CliCatalogHandshake(provider=None)
    checked_profile = ModelProfile(
        id="some-obscure-model",
        context_window=None,
        output_limit=None,
        raw={"_overlay_context_checked": True},
    )
    out = asyncio.run(handshake.enrich_capabilities(checked_profile, _ctx()))
    assert out is checked_profile  # unchanged, cascade never touched
    assert calls == []


def test_enrich_capabilities_still_runs_the_cascade_for_a_non_overlay_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A static-catalog-sourced profile (no overlay yet) keeps the PRE-#1211
    behavior: the cascade still runs to try to resolve its context window."""
    from clio_agent.providers.handshake.model import ModelProfile

    calls: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_context",
        lambda model_id, kind: (calls.append("resolve_context"), (128000, "models.dev"))[1],
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_output_limit",
        lambda model_id, kind: (calls.append("resolve_output_limit"), None)[1],
    )

    handshake = CliCatalogHandshake(provider=None)
    unchecked_profile = ModelProfile(id="gpt-5.5", context_window=None, output_limit=None, raw={})
    out = asyncio.run(handshake.enrich_capabilities(unchecked_profile, _ctx()))
    assert out.context_window == 128000
    assert "resolve_context" in calls


# --------------------------------------------------------------------------- #
# End-to-end: the full handshake() flow never touches the cascade once an
# overlay exists, whatever the persisted context outcome was.
# --------------------------------------------------------------------------- #


def test_full_handshake_never_hits_the_cascade_once_overlay_populated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    record_refresh(
        ProviderDiscoveryResult(
            provider="codex",
            discovered=[
                {
                    "id": "gpt-5.6-sol",
                    "name": "GPT-5.6-Sol",
                    "description": "",
                    "context_window": 272000,
                    "context_source": "models.dev",
                    "output_limit": 64000,
                }
            ],
            source=CODEX_SOURCE,
            default_model="gpt-5.6-sol",
        )
    )

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the cascade must never be reached for an overlay-checked model")

    monkeypatch.setattr("clio_agent.providers.handshake.sources.resolve_context", _boom)
    monkeypatch.setattr("clio_agent.providers.handshake.sources.resolve_output_limit", _boom)

    handshake = CliCatalogHandshake(provider=None)
    report = asyncio.run(handshake.handshake(_ctx()))
    assert [m.id for m in report.models] == ["gpt-5.6-sol"]
    assert report.models[0].context_window == 272000
    assert report.models[0].output_limit == 64000


# --------------------------------------------------------------------------- #
# Catalog provenance: a zero-network read must not claim a live probe, and
# cached evidence must not date itself to the moment it was read (AF-IMG F3).
# --------------------------------------------------------------------------- #


def test_overlay_backed_handshake_reports_overlay_not_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The overlay is real evidence -- but it is not THIS run's evidence.

    ``models_source`` was hardcoded to ``"live"`` whenever any model existed, so
    this handshake (which makes zero network calls) claimed a live probe it never
    performed, and stamped the read's wall clock over evidence that could be
    arbitrarily old.
    """

    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    record_refresh(
        ProviderDiscoveryResult(
            provider="codex",
            discovered=[{"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "description": ""}],
            source=CODEX_SOURCE,
            default_model="gpt-5.6-sol",
            generated_at="2026-01-02T03:04:05+00:00",
        )
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_context", lambda model_id, kind: (None, "")
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_output_limit",
        lambda model_id, kind: None,
    )

    report = asyncio.run(CliCatalogHandshake(provider=None).handshake(_ctx()))

    assert report.models_source == "overlay"
    assert report.evidence_generated_at == "2026-01-02T03:04:05+00:00"
    assert report.models[0].evidence_generated_at == "2026-01-02T03:04:05+00:00"
    # The read's own clock is still reported, separately and honestly.
    assert report.generated_at != report.evidence_generated_at


def test_static_catalog_handshake_reports_static_not_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no overlay the rows ARE the compiled-in candidates -- never evidence."""

    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_context", lambda model_id, kind: (None, "")
    )
    monkeypatch.setattr(
        "clio_agent.providers.handshake.sources.resolve_output_limit",
        lambda model_id, kind: None,
    )

    report = asyncio.run(CliCatalogHandshake(provider=None).handshake(_ctx()))

    assert report.models  # the static registry candidates are still surfaced
    assert report.models_source == "static"
    # A static row carries no capability evidence, so no modality can be claimed.
    assert all(profile.capabilities == () for profile in report.models)


def test_http_handshake_still_reports_live() -> None:
    """The fix must not flip a REAL probe to a weaker provenance."""

    from clio_agent.providers.handshake.openai_compat import OpenAICompatHandshake

    handshake = OpenAICompatHandshake(provider=None)
    assert handshake.models_provenance(_ctx("openai", "openai")) == ("live", "")
