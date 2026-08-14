"""iowarp/clio-agent#1211: overlay-first ``GET /v1/providers/{id}/models`` +
``POST /v1/providers/models/refresh``.

``GET`` serving tests write directly to the overlay file (via
``CLIO_MODEL_CATALOG``) rather than going through a real refresh, so they pin the
serving CONTRACT independent of any discovery mechanism. ``POST`` wiring is
tested by mocking :func:`clio_agent.providers.model_discovery.refresh_all` — the
discovery mechanisms themselves are covered in
``tests/test_providers/test_model_discovery.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "model_catalog.json"))
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def _write_overlay(tmp_path: Path, data: dict[str, Any]) -> None:
    (tmp_path / "model_catalog.json").write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# GET /v1/providers/{id}/models -- overlay-first / static-fallback / malformed.
# --------------------------------------------------------------------------- #


def test_get_models_overlay_absent_falls_back_to_static(client: TestClient) -> None:
    body = client.get("/v1/providers/codex/models").json()
    assert body["source"] == "static_catalog"
    ids = {m["id"] for m in body["models"]}
    # The frozen static candidate list (#1184's stale pins), unaffected by
    # this change per the #1211 non-goal (static catalog stays as fallback).
    assert {"gpt-5.5", "gpt-5.5-codex", "gpt-5.1"} <= ids


def test_get_models_overlay_present_is_served_verbatim(
    client: TestClient, tmp_path: Path
) -> None:
    """The core overlay-first contract: a refreshed list overrides the static one."""
    _write_overlay(
        tmp_path,
        {
            "codex": {
                "models": [
                    {"id": "gpt-5.6-sol", "name": "GPT-5.6-Sol", "description": "live"},
                    {"id": "gpt-5.6-terra", "name": "GPT-5.6-Terra", "description": "live"},
                ],
                "source": "codex_app_server",
                "default_model": "gpt-5.6-sol",
                "generated_at": "2026-08-14T00:00:00+00:00",
            }
        },
    )
    body = client.get("/v1/providers/codex/models").json()
    assert body["source"] == "codex_app_server"
    ids = {m["id"] for m in body["models"]}
    assert ids == {"gpt-5.6-sol", "gpt-5.6-terra"}
    # SABOTAGE-sensitive: none of the STALE static ids leak through once an
    # overlay exists -- if overlay-first serving were reverted to static-first,
    # this would fail (gpt-5.5 et al would appear instead).
    assert "gpt-5.5" not in ids
    assert body["default_model"] == "gpt-5.6-sol"


def test_get_models_malformed_overlay_is_typed_500_not_silent_fallback(
    client: TestClient, tmp_path: Path
) -> None:
    (tmp_path / "model_catalog.json").write_text("{not valid json", encoding="utf-8")
    resp = client.get("/v1/providers/codex/models")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["error"] == "overlay_malformed"


def test_get_models_overlay_serves_for_claude_code_too(
    client: TestClient, tmp_path: Path
) -> None:
    _write_overlay(
        tmp_path,
        {
            "claude_code": {
                "models": [{"id": "fable", "name": "Claude Fable", "description": "d"}],
                "source": "claude_code_alias_probe",
                "default_model": "fable",
            }
        },
    )
    body = client.get("/v1/providers/claude_code/models").json()
    assert body["source"] == "claude_code_alias_probe"
    assert body["models"] == [{"id": "fable", "name": "Claude Fable", "description": "d"}]


def test_get_models_overlay_never_downgrades_a_configured_http_provider(
    client: TestClient, tmp_path: Path
) -> None:
    """An overlay entry for an HTTP-backed provider (e.g. from a prior refresh)
    is also served overlay-first, ahead of a fresh live handshake attempt."""
    _write_overlay(
        tmp_path,
        {"openai": {"models": [{"id": "gpt-5", "name": "gpt-5", "description": ""}], "source": "live_handshake"}},
    )
    body = client.get("/v1/providers/openai/models").json()
    assert body["source"] == "live_handshake"
    assert body["models"] == [{"id": "gpt-5", "name": "gpt-5", "description": ""}]


# --------------------------------------------------------------------------- #
# POST /v1/providers/models/refresh -- thin wiring over model_discovery.refresh_all.
# --------------------------------------------------------------------------- #


def test_post_refresh_returns_the_discovery_results_verbatim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_results = [
        {
            "provider": "codex",
            "discovered": [{"id": "gpt-5.6-sol", "name": "Sol", "description": ""}],
            "source": "codex_app_server",
            "default_model": "gpt-5.6-sol",
            "generated_at": "2026-08-14T00:00:00+00:00",
            "added": ["gpt-5.6-sol"],
            "removed": [],
            "unchanged": [],
        },
        {
            "provider": "claude_code",
            "discovered": [],
            "source": "claude_code_alias_probe",
            "default_model": "",
            "generated_at": "2026-08-14T00:00:00+00:00",
            "added": [],
            "removed": [],
            "unchanged": [],
            "failed_reason": "claude CLI not found on PATH",
        },
    ]
    mock_refresh = AsyncMock(return_value=fake_results)
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.refresh_all", mock_refresh
    )
    resp = client.post("/v1/providers/models/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"results": fake_results}
    mock_refresh.assert_awaited_once()
