"""Regression: provider catalog routes resolve a preset by id, never by kind (#1418).

``"argonne"`` is the one wire kind in the catalog with NO provider preset
sharing its id (only ``argonne_sophia`` / ``argonne_metis`` exist), which
makes it the cleanest probe for the kind-fallback these routes used to carry:
``next((p for p in _LM_PRESETS if p.provider == provider_id), None)`` silently
resolved ``"argonne"`` to whichever argonne preset came first in catalog
order, instead of naming an unknown provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_models_route_no_longer_aliases_a_bare_kind_to_a_preset(client: TestClient) -> None:
    """``argonne`` (a kind, not a preset id) never live-resolves to argonne_sophia.

    It still answers from the static catalog fallback (a real path for any
    provider id ``_PROVIDER_MODELS`` recognizes) rather than 404ing outright,
    but it must not take the live per-model handshake path meant for a real,
    specific preset id.
    """
    resp = client.get("/v1/providers/argonne/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "static_catalog"


def test_handshake_route_rejects_a_bare_kind_as_unknown_provider(client: TestClient) -> None:
    """``GET /v1/providers/argonne/handshake`` 404s -- "argonne" names no preset."""

    resp = client.get("/v1/providers/argonne/handshake")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "argonne" in body["error"]["message"]


def test_handshake_route_still_resolves_the_real_preset_id(client: TestClient) -> None:
    """The real preset id (never the kind) still resolves -- sanity check.

    Uses ``llama_cpp`` (unreachable in this test env) so the assertion is only
    about identity resolution reaching the handshake attempt (a 200 carrying
    the handshake's own report shape), not live network behavior.
    """
    resp = client.get("/v1/providers/llama_cpp/handshake")
    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert "source" in body
