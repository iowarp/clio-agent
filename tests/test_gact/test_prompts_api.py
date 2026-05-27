from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json"))


def test_capabilities_advertise_prompt_registry(client: TestClient) -> None:
    caps = client.get("/v1/capabilities").json()["capabilities"]

    assert caps["x_clio_prompt_registry"] is True


def test_builtin_prompts_are_listed_and_resolvable(client: TestClient) -> None:
    listed = client.get("/v1/prompts")

    assert listed.status_code == 200
    prompts = {row["id"]: row for row in listed.json()["prompts"]}
    assert "clio.chat" in prompts
    assert "default" in prompts["clio.chat"]["profiles"]

    resolved = client.get("/v1/prompts/clio.chat").json()["prompt"]
    assert resolved["id"] == "clio.chat"
    assert resolved["profile"] == "default"
    assert "CLIO" in resolved["text"]
    assert resolved["scope"] == "builtin"
    assert resolved["checksum"]


def test_put_prompt_saves_external_profile_and_resolution_uses_it(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={
            "profile": "heavy",
            "title": "Heavy chat",
            "description": "More explicit behavior",
            "text": "Use detailed but grounded CLIO behavior.",
            "provider": "openai",
            "model": "gpt-5.1",
            "metadata": {"edited_by": "test"},
        },
    )

    assert resp.status_code == 200, resp.text
    saved = resp.json()["prompt"]
    assert saved["profiles"]["heavy"]["text"] == "Use detailed but grounded CLIO behavior."

    resolved = client.get("/v1/prompts/clio.chat?profile=heavy").json()["prompt"]
    assert resolved["text"] == "Use detailed but grounded CLIO behavior."
    assert resolved["title"] == "Heavy chat"
    assert resolved["scope"] == "global"
    assert resolved["provider"] == "openai"
    assert resolved["model"] == "gpt-5.1"
    assert resolved["metadata"]["edited_by"] == "test"


def test_put_prompt_rejects_invalid_profile(client: TestClient) -> None:
    resp = client.put(
        "/v1/prompts/clio.chat",
        json={"profile": "../bad", "text": "bad"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"

