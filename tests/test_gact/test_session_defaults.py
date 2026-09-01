"""Session-default persistence and create-time precedence tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import _clear_session_model_refs, build_app
from clio_agent.gact.session_defaults import SessionDefaultsStore

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


def test_session_defaults_reject_half_filled_model_reference(tmp_path: Path) -> None:
    client = _client(tmp_path / "sessions.json")

    provider_only = client.patch("/v1/session-defaults", json={"provider_id": "codex"})
    model_only = client.patch("/v1/session-defaults", json={"model_id": "gpt-5.6-luna"})
    mismatched_empty = client.patch(
        "/v1/session-defaults",
        json={"provider_id": "codex", "model_id": ""},
    )

    for response in (provider_only, model_only, mismatched_empty):
        assert response.status_code == 422
        assert "provider_id and model_id" in response.text


def test_corrupt_session_defaults_are_quarantined_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "session-defaults.json"
    original = '{"provider_id": "codex", broken}'
    path.write_text(original, encoding="utf-8")

    store = SessionDefaultsStore(path)

    assert store.get().provider_id == ""
    assert not path.exists()
    quarantined = list(tmp_path.glob("session-defaults.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == original
    assert store.load_degradation == {
        "reason": "session_defaults_corrupt",
        "source_path": str(path),
        "quarantine_path": str(quarantined[0]),
    }


def test_provider_swap_clears_persisted_session_default_model_reference(tmp_path: Path) -> None:
    sessions_path = tmp_path / "sessions.json"
    client = _client(sessions_path)
    updated = client.patch(
        "/v1/session-defaults",
        json={"provider_id": "codex", "model_id": "gpt-5.6-luna"},
    )
    assert updated.status_code == 200

    _clear_session_model_refs(client.app)

    assert client.get("/v1/session-defaults").json()["provider_id"] == ""
    assert client.get("/v1/session-defaults").json()["model_id"] == ""
    rebuilt = _client(sessions_path)
    assert rebuilt.get("/v1/session-defaults").json()["provider_id"] == ""
    assert rebuilt.get("/v1/session-defaults").json()["model_id"] == ""
