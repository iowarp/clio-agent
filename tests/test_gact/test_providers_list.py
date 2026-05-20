"""iowarp/clio-agent#15: /v1/providers catalogs LM presets."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_providers_list_includes_known_presets(client: TestClient) -> None:
    body = client.get("/v1/providers").json()
    ids = {p["id"] for p in body["providers"]}
    assert {"openai", "openrouter", "lm_studio", "ollama", "anthropic", "codex"} <= ids
    for row in body["providers"]:
        assert row["name"]
        assert row["api_base"]


def test_providers_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["providers"] is True


def test_unknown_provider_models_returns_structured_404(client: TestClient) -> None:
    resp = client.get("/v1/providers/not-a-provider/models")

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "not-a-provider" in body["error"]["message"]
    assert "lm_studio" in body["error"]["details"]["available"]
