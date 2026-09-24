"""Provider catalog freshness: last-good lists, invalidation, targeted refresh (K).

ALCF models vanished after every restart until "validate provider": the catalog
froze its first read, an empty passive probe was served as "no models", and
neither sign-in nor "Check provider" retired the snapshot. These tests pin the
fixed contract on the real objects (the overlay file, the served payload).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.provider_catalog import discover_provider
from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers import model_discovery
from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)

METIS_BASE = "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"


def _metis() -> LMProviderPreset:
    return LMProviderPreset(
        id="argonne_metis",
        label="ALCF Metis",
        provider="argonne",
        api_base=METIS_BASE,
        suggested_model="openai/gpt-oss-120b",
    )


def _live_report() -> HandshakeReport:
    return HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models_source="live",
        generated_at="2026-09-22T10:00:00+00:00",
        evidence_generated_at="2026-09-22T10:00:00+00:00",
        models=(
            ModelProfile(
                id="openai/gpt-oss-120b",
                context_window=131_072,
                is_reasoning=True,
                reasoning_param="openai_gptoss",
                native_tool_calling=True,
            ),
        ),
    )


def _skipped_report() -> HandshakeReport:
    return HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.SKIPPED,
        auth=AuthState.DEFERRED,
        error="argonne_stored_token_unusable: a Globus sign-in is stored but ...",
        models_source="unavailable",
        generated_at="2026-09-23T08:00:00+00:00",
    )


def test_live_answer_is_persisted_and_served_stale_when_the_probe_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [_live_report(), _skipped_report()]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return reports.pop(0)

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    live = asyncio.run(discover_provider(_metis()))
    assert [row["model_id"] for row in live["models"]] == ["openai/gpt-oss-120b"]
    assert live["models"][0]["availability"] == "available"
    # The real store: the overlay file now holds the live list under the exact id.
    stored = json.loads(model_discovery.overlay_path().read_text(encoding="utf-8"))
    assert stored["argonne_metis"]["source"] == model_discovery.HTTP_SOURCE
    assert stored["argonne_metis"]["models"][0]["context_window"] == 131_072

    restarted = asyncio.run(discover_provider(_metis()))
    assert [row["model_id"] for row in restarted["models"]] == ["openai/gpt-oss-120b"]
    row = restarted["models"][0]
    # Served, but never as current evidence.
    assert row["availability"] == "candidate"
    assert row["evidence"]["source"] == "last_good"
    assert row["evidence"]["live"] is False
    assert row["evidence"]["generated_at"] == "2026-09-22T10:00:00+00:00"
    assert row["context_window"] == 131_072
    freshness = restarted["freshness"]
    assert freshness["source"] == "last_good"
    assert freshness["generated_at"] == "2026-09-22T10:00:00+00:00"
    assert freshness["staleness"]["reason"] == "last_good_catalog_served"
    assert freshness["staleness"]["live_failure"].startswith("argonne_stored_token_unusable")
    assert restarted["failure"].startswith("argonne_stored_token_unusable")
    assert restarted["health"] == "unavailable"


def test_no_last_good_list_means_no_models(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return _skipped_report()

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(_metis()))
    assert provider["models"] == []
    assert "staleness" not in provider["freshness"]


def test_last_good_lookup_is_exact_provider_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ALCF clusters share a kind, never a model list."""
    assert model_discovery.persist_live_catalog("argonne_metis", _live_report())
    assert model_discovery.last_good_catalog("argonne_sophia") is None
    assert model_discovery.last_good_catalog("argonne") is None
    catalog = model_discovery.last_good_catalog("argonne_metis")
    assert catalog is not None
    assert catalog.profiles[0].reasoning_param == "openai_gptoss"


def test_only_live_answers_are_persisted() -> None:
    static = HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        models_source="static",
        models=(ModelProfile(id="guess"),),
    )
    assert model_discovery.persist_live_catalog("argonne_metis", static) is False
    assert model_discovery.persist_live_catalog("argonne_metis", _skipped_report()) is False
    assert model_discovery.last_good_catalog("argonne_metis") is None


def test_invalidate_provider_drops_every_api_base_variant() -> None:
    handshake_cache.put_cached(("argonne_metis", "a"), _live_report())
    handshake_cache.put_cached(("argonne_metis", "b"), _live_report())
    handshake_cache.put_cached(("argonne_sophia", "a"), _live_report())

    assert handshake_cache.invalidate_provider("argonne_metis") == 2
    assert handshake_cache.get_cached(("argonne_metis", "a")) is None
    assert handshake_cache.get_cached(("argonne_sophia", "a")) is not None
    handshake_cache.invalidate()


def _record(provider_id: str, *, source: str = "live") -> dict[str, Any]:
    return {
        "id": provider_id,
        "name": provider_id,
        "kind": "argonne",
        "endpoint": "",
        "configuration_url": f"/settings/providers?provider={provider_id}",
        "connectivity": "ok",
        "auth": "ok",
        "health": "ready",
        "freshness": {"generated_at": "2026-09-23T00:00:00Z", "source": source},
        "failure": "",
        "models": [],
    }


def _snapshot(*ids: str) -> dict[str, Any]:
    return {
        "catalog_id": "active",
        "providers": [_record(provider_id) for provider_id in ids],
        "authoritative": "live_handshake",
    }


def test_refresh_for_one_provider_probes_only_that_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = _snapshot("codex", "argonne_metis")
    probed: list[tuple[str, bool]] = []

    async def _discover(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
        probed.append((preset.id, refresh))
        record = _record(preset.id)
        record["name"] = "fresh"
        return record

    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", _discover)

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog?refresh=true&provider=argonne_metis")

    assert response.status_code == 200
    assert probed == [("argonne_metis", True)]
    names = {row["id"]: row["name"] for row in response.json()["providers"]}
    assert names == {"codex": "codex", "argonne_metis": "fresh"}
    assert app.state.provider_catalog == response.json()


def test_unknown_provider_refresh_is_not_found(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = _snapshot("codex")
    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog?refresh=true&provider=nope")
    assert response.status_code == 404


def test_sign_in_completion_retires_every_alcf_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After sign-in the very next plain read re-discovers ALCF providers only."""
    from clio_agent.providers import argonne_auth

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = _snapshot("codex", "argonne_metis", "argonne_sophia")
    handshake_cache.put_cached(("argonne_metis", METIS_BASE), _skipped_report())
    probed: list[str] = []

    async def _discover(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
        probed.append(preset.id)
        return _record(preset.id)

    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", _discover)
    monkeypatch.setattr(
        "clio_agent.gact.routes.provider_catalog_routes.ensure_argonne_support", lambda: False
    )
    monkeypatch.setattr(argonne_auth, "complete_authentication", lambda *_args: None)

    with TestClient(app) as client:
        done = client.post(
            "/v1/providers/argonne_metis/auth",
            json={"action": "complete", "flow_id": "f", "authorization_code": "c"},
        )
        assert done.status_code == 200
        assert handshake_cache.get_cached(("argonne_metis", METIS_BASE)) is None
        catalog = client.get("/v1/provider-catalog")
        again = client.get("/v1/provider-catalog")

    assert catalog.status_code == 200
    assert sorted(probed) == ["argonne_metis", "argonne_sophia"]
    assert again.json() == catalog.json()


def test_explicit_check_retires_that_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = _snapshot("codex", "argonne_metis")
    probed: list[str] = []

    async def _discover(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
        probed.append(preset.id)
        return _record(preset.id)

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return _live_report()

    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", _discover)
    monkeypatch.setattr("clio_agent.providers.handshake.run_handshake", _handshake)

    with TestClient(app) as client:
        checked = client.get("/v1/providers/argonne_metis/handshake?refresh=true")
        assert checked.status_code == 200
        client.get("/v1/provider-catalog")

    assert probed == ["argonne_metis"]


def test_last_good_entry_is_reprobed_and_replaced_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.provider_catalog_snapshot import read_catalog

    app = build_app(sessions_path=tmp_path / "sessions.json")
    snapshot = _snapshot("codex")
    snapshot["providers"].append(_record("argonne_metis", source="last_good"))
    app.state.provider_catalog = snapshot
    published: list[Any] = []
    monkeypatch.setattr(app.state.bus, "publish", published.append)
    monkeypatch.setattr(app.state.sessions, "list", lambda: [type("S", (), {"id": "s1"})()])

    async def _discover(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
        assert refresh is False  # bounded by the handshake TTL cache
        return _record(preset.id, source="live")

    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", _discover)

    async def _run() -> dict[str, Any]:
        served = await read_catalog(app)
        await app.state.provider_catalog_reprobe_task
        return served

    served = asyncio.run(_run())
    # The read itself returned the snapshot immediately...
    assert served["providers"][1]["freshness"]["source"] == "last_good"
    # ...and the background re-probe replaced it and told every session.
    current = app.state.provider_catalog["providers"][1]
    assert current["freshness"]["source"] == "live"
    assert [event.type for event in published] == ["provider_catalog.refreshed"]


def test_catalog_names_are_canonical_and_sign_in_is_separate_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One label source: the catalog ``name`` is clean; the sign-in service is detail (J)."""
    from clio_agent.providers.catalog import as_lm_presets

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return _skipped_report()

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)
    presets = {preset.id: preset for preset in as_lm_presets()}
    for provider_id, name in (("argonne_metis", "ALCF Metis"), ("argonne_sophia", "ALCF Sophia")):
        record = asyncio.run(discover_provider(presets[provider_id]))
        assert record["name"] == name
        assert record["auth_method"] == "oauth"
        assert record["auth_label"] == "Globus Auth"
        assert record["configuration_url"] == f"/settings/providers?provider={provider_id}"
    assert all(
        "(" not in preset.label for preset in presets.values() if preset.provider == "argonne"
    )


def test_targeted_refresh_without_a_snapshot_forces_only_that_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model refresh retires the snapshot; a targeted re-read must not re-probe everyone."""
    from clio_agent.providers.catalog import as_lm_presets

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = None
    forced: list[str] = []

    async def _discover(preset: LMProviderPreset, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            forced.append(preset.id)
        return _record(preset.id)

    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", _discover)

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog?refresh=true&provider=argonne_metis")

    assert response.status_code == 200
    assert forced == ["argonne_metis"]
    ids = [row["id"] for row in response.json()["providers"]]
    assert ids == [preset.id for preset in as_lm_presets()]
