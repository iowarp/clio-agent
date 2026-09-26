"""Cache and refresh semantics for the normalized provider catalog route."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def test_provider_catalog_serves_the_in_process_snapshot_without_reprobing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary reads must stay cheap once the background discovery completed."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    cached = {
        "catalog_id": "active",
        "providers": [{"id": "claude_code"}],
        "authoritative": "live_handshake",
    }
    app.state.provider_catalog = cached
    discover = AsyncMock(side_effect=AssertionError("cached reads must not probe providers"))
    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", discover)

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog")

    assert response.status_code == 200
    # The response carries the live "checking" overlay (#1446 follow-up: a
    # background probe running for a provider right now, never persisted
    # into the snapshot itself) -- none is in flight here, so it reads False.
    assert response.json() == {
        **cached,
        "providers": [{**provider, "checking": False} for provider in cached["providers"]],
    }
    discover.assert_not_awaited()


def test_provider_catalog_refresh_bypasses_the_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit refresh query remains the opt-in provider re-probe."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = {
        "catalog_id": "active",
        "providers": [{"id": "stale"}],
        "authoritative": "live_handshake",
    }
    discover = AsyncMock(
        return_value={
            "id": "fresh",
            "name": "Fresh",
            "kind": "test",
            "endpoint": "",
            "configuration_url": "/settings/providers?provider=fresh",
            "connectivity": "ok",
            "auth": "not_required",
            "health": "ready",
            "freshness": {"generated_at": "2026-09-20T00:00:00Z", "source": "live"},
            "failure": "",
            "models": [],
        }
    )
    monkeypatch.setattr("clio_agent.gact.provider_catalog_snapshot.discover_provider", discover)

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog?refresh=true")

    assert response.status_code == 200
    assert response.json()["providers"]
    assert all(provider["id"] == "fresh" for provider in response.json()["providers"])
    assert discover.await_count > 0


def test_provider_catalog_marks_a_provider_being_reprobed_as_checking(
    tmp_path: Path,
) -> None:
    """A client reading the catalog while the background reprobe loop is
    mid-handshake for a provider sees ``checking: true`` for it -- never a
    stale failure that later silently flips to ready with nothing shown in
    between (#1446 follow-up)."""
    from clio_agent.gact import provider_catalog_snapshot as snapshot

    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.provider_catalog = {
        "catalog_id": "active",
        "providers": [{"id": "claude_code"}, {"id": "argonne_metis"}],
        "authoritative": "live_handshake",
    }
    snapshot.mark_checking(app, ["argonne_metis"])

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog")

    by_id = {provider["id"]: provider for provider in response.json()["providers"]}
    assert by_id["argonne_metis"]["checking"] is True
    assert by_id["claude_code"]["checking"] is False
