"""Provider catalog freshness: last-good lists, invalidation, targeted refresh (K).

ALCF models vanished after every restart until "validate provider": the catalog
froze its first read, an empty passive probe was served as "no models", and
neither sign-in nor "Check provider" retired the snapshot. These tests pin the
fixed contract on the real objects (the overlay file, the served payload).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.provider_catalog import discover_provider
from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers import model_discovery
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
)
from clio_agent.providers.handshake import cache as handshake_cache
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
)

METIS_BASE = "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"
_METIS_MODEL = "openai/gpt-oss-120b"
_NOW = "2026-09-22T10:00:00+00:00"


@pytest.fixture(autouse=True)
def _fresh_confirmations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with no in-process last-good confirmations."""
    from clio_agent.providers.model_discovery import last_good

    monkeypatch.setattr(last_good, "_CONFIRMED_IN_PROCESS", {}, raising=False)
    invalidation.clear_all()
    yield
    invalidation.clear_all()


def _seed_metis_capabilities() -> None:
    """Seed the capability store the way a real Argonne handshake would.

    ``_live_report()`` only carries bare identity now (brief Part 4); the
    context/tool facts ``model_catalog_row`` and the overlay's persisted
    capability snapshot both read come from here.
    """
    invalidation.record_model_capabilities(ModelCapabilities(model_key=_METIS_MODEL))
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="argonne_metis",
            api_base=METIS_BASE,
            model_id=_METIS_MODEL,
            model_key=Fact(value=_METIS_MODEL, source="server_report", observed_at=_NOW),
            context_served=Fact(value=131_072, source="server_report", observed_at=_NOW),
            tools_enabled=Fact(value=True, source="server_report", observed_at=_NOW),
            reasoning_enabled=Fact(value=True, source="server_report", observed_at=_NOW),
        )
    )


def _metis() -> LMProviderPreset:
    return LMProviderPreset(
        id="argonne_metis",
        label="ALCF Metis",
        provider="argonne",
        api_base=METIS_BASE,
        suggested_model="openai/gpt-oss-120b",
    )


def _live_report() -> HandshakeReport:
    _seed_metis_capabilities()
    return HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base=METIS_BASE,
        models_source="live",
        generated_at="2026-09-22T10:00:00+00:00",
        evidence_generated_at="2026-09-22T10:00:00+00:00",
        models=(DiscoveredModel(id=_METIS_MODEL),),
    )


def _skipped_report() -> HandshakeReport:
    return HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.SKIPPED,
        auth=AuthState.DEFERRED,
        error="argonne_stored_token_unusable: a Globus sign-in is stored but ...",
        models_source="unavailable",
        api_base=METIS_BASE,
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
    assert stored["argonne_metis"]["models"][0]["capability_snapshot"]["context_served"] == 131_072

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
    catalog = model_discovery.last_good_catalog("argonne_metis", api_base=METIS_BASE)
    assert catalog is not None
    assert catalog.models[0].id == _METIS_MODEL


def test_only_live_answers_are_persisted() -> None:
    static = HandshakeReport(
        provider_id="argonne_metis",
        provider_kind="argonne",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base=METIS_BASE,
        models_source="static",
        models=(DiscoveredModel(id="guess"),),
    )
    assert model_discovery.persist_live_catalog("argonne_metis", static) is False
    assert model_discovery.persist_live_catalog("argonne_metis", _skipped_report()) is False
    assert model_discovery.last_good_catalog("argonne_metis") is None


def test_invalidate_provider_drops_every_api_base_variant() -> None:
    # The cache is process-global; count only the entries this test puts.
    handshake_cache.invalidate()
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
    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog_reprobe.REPROBE_BACKOFF_S", (0.0, 0.0, 0.0)
    )

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


class _ScriptedHandshake:
    """A provider handshake returning scripted reports (the cache stays REAL)."""

    def __init__(self, reports: list[HandshakeReport]) -> None:
        self.reports = reports
        self.calls = 0

    async def handshake(self, ctx: object) -> HandshakeReport:
        self.calls += 1
        return self.reports[min(self.calls - 1, len(self.reports) - 1)]


def _reprobe_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: _ScriptedHandshake):
    from clio_agent.gact import provider_catalog_reprobe, provider_catalog_snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(provider_catalog_snapshot, "as_lm_presets", lambda: [_metis()])
    monkeypatch.setattr(
        "clio_agent.providers.handshake.get_handshake_for", lambda *_a, **_k: scripted
    )
    monkeypatch.setattr(provider_catalog_reprobe, "REPROBE_BACKOFF_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(provider_catalog_reprobe, "REPROBE_STEADY_S", 0.0)
    handshake_cache.invalidate()
    return app


def test_reprobe_bypasses_the_cached_failure_and_stops_when_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The 30s handshake cache caches failures; the re-probe must really re-check."""
    import logging

    from clio_agent.gact.provider_catalog_snapshot import read_catalog

    assert model_discovery.persist_live_catalog("argonne_metis", _live_report())
    scripted = _ScriptedHandshake([_skipped_report(), _skipped_report(), _live_report()])
    app = _reprobe_app(tmp_path, monkeypatch, scripted)
    caplog.set_level(logging.INFO, logger="clio_agent.gact.provider_catalog_reprobe")

    async def _run() -> dict[str, object]:
        served = await read_catalog(app)
        await app.state.provider_catalog_reprobe_task
        return served

    served = asyncio.run(_run())

    assert served["providers"][0]["freshness"]["source"] == "last_good"
    # boot probe + one failed re-probe + the live one: every attempt hit the provider.
    assert scripted.calls == 3
    assert app.state.provider_catalog["providers"][0]["freshness"]["source"] == "live"
    assert app.state.provider_catalog_reprobe_task.done()
    attempts = [
        r.getMessage()
        for r in caplog.records
        if "provider_catalog_reprobe_attempt" in r.getMessage()
    ]
    assert "outcome=still_stale" in attempts[0]
    assert "argonne_stored_token_unusable" in attempts[0]
    assert "outcome=live" in attempts[-1]
    handshake_cache.invalidate()


def test_reprobe_backoff_schedule() -> None:
    from clio_agent.gact import provider_catalog_reprobe as reprobe

    assert [reprobe._delay(n) for n in range(5)] == [5.0, 30.0, 120.0, 600.0, 600.0]


def test_older_reprobe_never_merges_over_a_newer_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import provider_catalog_reprobe, provider_catalog_snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    stale = _record("argonne_metis", source="last_good")
    provider_catalog_snapshot.commit(
        app, {"catalog_id": "active", "providers": [stale]}, ["argonne_metis"]
    )
    newer = _record("argonne_metis", source="live")
    newer["name"] = "newer refresh"

    async def _discover(_app: Any, ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
        # A refresh lands while this re-probe attempt is in flight.
        provider_catalog_snapshot.commit(
            app, {"catalog_id": "active", "providers": [newer]}, ["argonne_metis"]
        )
        return [_record("argonne_metis", source="live")]

    monkeypatch.setattr(provider_catalog_snapshot, "discover", _discover)
    asyncio.run(provider_catalog_reprobe._attempt(app, 1, ["argonne_metis"]))

    assert app.state.provider_catalog["providers"][0]["name"] == "newer refresh"


def test_invalidation_during_a_read_is_kept_for_the_next_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import provider_catalog_snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(provider_catalog_snapshot, "as_lm_presets", lambda: [_metis()])

    async def _discover(_app: Any, ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
        provider_catalog_snapshot.invalidate_provider(app, "argonne_metis")
        return [_record(pid) for pid in ids]

    monkeypatch.setattr(provider_catalog_snapshot, "discover", _discover)
    asyncio.run(provider_catalog_snapshot.read_catalog(app))

    assert "argonne_metis" in app.state.provider_catalog_invalidated


def test_lifespan_shutdown_cancels_the_reprobe_task(tmp_path: Path) -> None:
    """Shutdown itself cancels the task -- checked on the SAME loop, before it closes.

    (A TestClient portal cancels leftover tasks when its loop closes, which would
    hide a missing cancel; driving the lifespan directly does not.)
    """
    app = build_app(sessions_path=tmp_path / "sessions.json")

    async def _run() -> asyncio.Task:
        async with app.router.lifespan_context(app):
            task = asyncio.create_task(asyncio.sleep(3600))
            app.state.provider_catalog_reprobe_task = task
            await asyncio.sleep(0)
        # Lifespan exited; this loop is still running and owns the task.
        assert task.done(), "lifespan shutdown left the re-probe task running"
        return task

    assert asyncio.run(_run()).cancelled()


def test_unchanged_last_good_list_is_not_rewritten() -> None:
    assert model_discovery.persist_live_catalog("argonne_metis", _live_report()) is True
    path = model_discovery.overlay_path()
    before = path.stat().st_mtime_ns
    assert model_discovery.persist_live_catalog("argonne_metis", _live_report()) is False
    assert path.stat().st_mtime_ns == before


def test_a_changed_api_base_stamps_a_different_freshness_key(tmp_path: Path) -> None:
    """A provider whose configured endpoint changes must never read as fresh
    evidence for its OLD endpoint (model-capabilities brief Part 3): the
    freshness ledger is keyed by (provider_id, normalized api_base), not just
    provider_id."""
    from clio_agent.gact import provider_catalog_snapshot as snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    old_base = "http://127.0.0.1:8088/v1"
    new_base = "http://127.0.0.1:9000/v1"

    snapshot.commit(
        app,
        {"catalog_id": "active", "providers": [{**_record("llama_cpp"), "endpoint": old_base}]},
        ["llama_cpp"],
    )
    before = snapshot.provider_seq(app, "llama_cpp", old_base)
    assert before != 0
    # The new endpoint has never been stamped -- its own freshness key is 0,
    # entirely independent of the old endpoint's.
    assert snapshot.provider_seq(app, "llama_cpp", new_base) == 0

    snapshot.commit(
        app,
        {"catalog_id": "active", "providers": [{**_record("llama_cpp"), "endpoint": new_base}]},
        ["llama_cpp"],
    )
    # The old endpoint's freshness entry is untouched by the new commit.
    assert snapshot.provider_seq(app, "llama_cpp", old_base) == before
    assert snapshot.provider_seq(app, "llama_cpp", new_base) != 0
    assert snapshot.provider_seq(app, "llama_cpp", new_base) != before


def test_last_confirmed_tracks_every_live_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged list is not rewritten, but its confirmation time moves forward."""
    from clio_agent.providers.model_discovery import last_good

    first = _live_report()
    assert model_discovery.persist_live_catalog("argonne_metis", first)
    later = replace(first, generated_at="2026-09-22T18:30:00+00:00")
    assert model_discovery.persist_live_catalog("argonne_metis", later) is False  # list unchanged

    catalog = model_discovery.last_good_catalog("argonne_metis")
    assert catalog is not None
    assert catalog.generated_at == "2026-09-22T10:00:00+00:00"  # first discovery
    assert catalog.confirmed_at == "2026-09-22T18:30:00+00:00"  # exact in this process
    # Persisted too (the stored value was > 1h old), so a restart still knows it.
    stored = json.loads(model_discovery.overlay_path().read_text(encoding="utf-8"))
    assert stored["argonne_metis"]["confirmed_at"] == "2026-09-22T18:30:00+00:00"

    # Within the hour: exact in memory, no disk write.
    path = model_discovery.overlay_path()
    before = path.stat().st_mtime_ns
    soon = replace(first, generated_at="2026-09-22T18:50:00+00:00")
    model_discovery.persist_live_catalog("argonne_metis", soon)
    assert path.stat().st_mtime_ns == before
    assert model_discovery.last_good_catalog("argonne_metis").confirmed_at == (
        "2026-09-22T18:50:00+00:00"
    )
    # After a restart (in-memory value gone) the persisted hour-accurate value is used.
    monkeypatch.setattr(last_good, "_CONFIRMED_IN_PROCESS", {}, raising=False)
    assert model_discovery.last_good_catalog("argonne_metis").confirmed_at == (
        "2026-09-22T18:30:00+00:00"
    )


def test_served_last_good_carries_confirmed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    reports = [_live_report(), _skipped_report()]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return reports.pop(0)

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)
    asyncio.run(discover_provider(_metis()))
    served = asyncio.run(discover_provider(_metis()))
    assert served["freshness"]["staleness"]["confirmed_at"] == "2026-09-22T10:00:00+00:00"


def test_reprobe_logs_quiet_down_after_three_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from clio_agent.gact import provider_catalog_reprobe, provider_catalog_snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    provider_catalog_snapshot.commit(
        app,
        {"catalog_id": "active", "providers": [_record("argonne_metis", source="last_good")]},
        ["argonne_metis"],
    )
    answers = ["last_good"] * 5 + ["live"]

    async def _discover(_app: Any, ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
        return [_record("argonne_metis", source=answers.pop(0))]

    monkeypatch.setattr(provider_catalog_snapshot, "discover", _discover)
    caplog.set_level(logging.DEBUG, logger="clio_agent.gact.provider_catalog_reprobe")
    failures: dict[str, int] = {}
    for attempt in range(1, 7):
        asyncio.run(provider_catalog_reprobe._attempt(app, attempt, ["argonne_metis"], failures))

    levels = [
        (r.levelno, "outcome=live" in r.getMessage())
        for r in caplog.records
        if "provider_catalog_reprobe_attempt" in r.getMessage()
    ]
    assert [lvl for lvl, _ in levels] == [logging.INFO] * 3 + [logging.DEBUG] * 2 + [logging.INFO]
    assert levels[-1][1] is True


def test_full_refresh_never_overwrites_a_newer_reprobe_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import provider_catalog_snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    monkeypatch.setattr(provider_catalog_snapshot, "as_lm_presets", lambda: [_metis()])
    provider_catalog_snapshot.commit(
        app,
        {"catalog_id": "active", "providers": [_record("argonne_metis", source="last_good")]},
        ["argonne_metis"],
    )
    newer = _record("argonne_metis", source="live")
    newer["name"] = "re-probe answer"

    async def _discover(_app: Any, ids: list[str], *, refresh: bool) -> list[dict[str, Any]]:
        # A background re-probe answers while this (older) refresh is in flight.
        provider_catalog_snapshot.commit(
            app, {"catalog_id": "active", "providers": [newer]}, ["argonne_metis"]
        )
        return [_record("argonne_metis", source="last_good")]

    monkeypatch.setattr(provider_catalog_snapshot, "discover", _discover)
    asyncio.run(provider_catalog_snapshot.read_catalog(app, refresh=True))

    assert app.state.provider_catalog["providers"][0]["name"] == "re-probe answer"
