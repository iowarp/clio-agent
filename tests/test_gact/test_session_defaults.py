"""Session-default persistence and create-time precedence tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

GACT_V3_HEADERS = {"x-gact-version": "0.3"}


def _client(path: Path) -> TestClient:
    """Build a test client with all default state persisted beside ``path``."""

    return TestClient(build_app(sessions_path=path, agent=None))


def test_session_defaults_persist_and_apply_only_when_fields_are_omitted(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    client = _client(sessions_path)

    initial = client.get("/v1/session-defaults")
    assert initial.status_code == 200
    assert initial.json() == {
        "provider_id": "",
        "model_id": "",
        "effort": "medium",
        "mode": "edit",
        "edit_mode": "diff",
        "routing_mode": "auto",
        "approval_mode": "ask",
        "blueprint_id": "",
    }

    updated = client.patch(
        "/v1/session-defaults",
        json={
            "provider_id": "codex",
            "model_id": "gpt-5.6-luna",
            "effort": "medium",
            "mode": "architect",
            "edit_mode": "patch",
            "routing_mode": "experts",
            "approval_mode": "ai-review",
            "blueprint_id": "earthscope-review",
        },
    )
    assert updated.status_code == 200

    inherited = client.post(
        "/v1/sessions",
        headers=GACT_V3_HEADERS,
        json={"workspace_id": "ws_default", "title": "Inherited defaults"},
    )
    assert inherited.status_code == 201
    inherited_expected = {
        "provider_id": "codex",
        "model_id": "gpt-5.6-luna",
        "effort": "medium",
        "mode": "architect",
        "edit_mode": "patch",
        "routing_mode": "experts",
        "approval_mode": "ai-review",
        "active_blueprint_id": "earthscope-review",
    }
    assert {key: inherited.json().get(key) for key in inherited_expected} == inherited_expected

    explicit = client.post(
        "/v1/sessions",
        headers=GACT_V3_HEADERS,
        json={
            "workspace_id": "ws_default",
            "title": "Explicit overrides",
            "model": None,
            "mode": "edit",
            "edit_mode": "whole",
            "routing_mode": "chat",
            "approval_mode": "ask",
            "metadata": {"effort": "high", "active_agent_blueprint_id": "manual"},
        },
    )
    assert explicit.status_code == 201
    payload = explicit.json()
    assert "provider_id" not in payload
    assert "model_id" not in payload
    explicit_expected = {
        "effort": "high",
        "mode": "edit",
        "edit_mode": "whole",
        "routing_mode": "chat",
        "approval_mode": "ask",
        "active_blueprint_id": "manual",
    }
    assert {key: payload.get(key) for key in explicit_expected} == explicit_expected

    rebuilt = _client(sessions_path)
    assert rebuilt.get("/v1/session-defaults").json() == updated.json()


def test_session_defaults_reject_unknown_and_invalid_values(tmp_path: Path) -> None:
    client = _client(tmp_path / "sessions.json")

    assert client.patch("/v1/session-defaults", json={"unknown": True}).status_code == 422
    assert client.patch("/v1/session-defaults", json={"mode": "chat"}).status_code == 422
