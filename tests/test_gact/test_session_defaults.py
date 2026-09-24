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
        # No fixed level: new sessions start on the selected model's own default.
        "effort": None,
        "effort_source": None,
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


def test_session_default_effort_can_be_reset_to_the_model_default(tmp_path: Path) -> None:
    client = _client(tmp_path / "sessions.json")
    assert client.patch("/v1/session-defaults", json={"effort": "max"}).json()["effort"] == "max"
    assert client.patch("/v1/session-defaults", json={"mode": "plan"}).json()["effort"] == "max"
    reset = client.patch("/v1/session-defaults", json={"effort": None})
    assert reset.json()["effort"] is None
    created = client.post(
        "/v1/sessions",
        headers=GACT_V3_HEADERS,
        json={"workspace_id": "ws_default", "title": "Model default"},
    )
    assert created.status_code == 201
    assert created.json().get("effort") is None


def test_legacy_forced_medium_is_migrated_to_unset(tmp_path: Path) -> None:
    """Old builds force-wrote effort=medium with no source; it must not take effect."""
    import json

    path = tmp_path / "session_defaults.json"
    path.write_text(json.dumps({"effort": "medium", "mode": "plan"}), encoding="utf-8")

    store = SessionDefaultsStore(path)

    assert store.get().effort is None
    assert store.get().mode == "plan"
    # One-time migration: the file itself no longer carries the legacy level.
    assert json.loads(path.read_text(encoding="utf-8"))["effort"] is None
    assert SessionDefaultsStore(path).get().effort is None


def test_user_picked_default_effort_survives_reload(tmp_path: Path) -> None:
    from clio_agent.gact.session_defaults import UpdateSessionDefaultsRequest

    path = tmp_path / "session_defaults.json"
    SessionDefaultsStore(path).update(UpdateSessionDefaultsRequest(effort="max"))
    reloaded = SessionDefaultsStore(path).get()
    assert reloaded.effort == "max"
    assert reloaded.effort_source == "user"


def test_legacy_session_metadata_effort_is_read_as_unset() -> None:
    from types import SimpleNamespace

    from clio_agent.gact.protocol.v3.session import session_to_v3

    def _session(metadata: dict[str, object]) -> SimpleNamespace:
        return SimpleNamespace(
            id="sess_legacy",
            title="t",
            workspace_id="ws",
            status="idle",
            metadata=metadata,
            model=None,
            parent_session_id="",
        )

    legacy = session_to_v3(_session({"effort": "medium"}))
    assert "effort" not in legacy
    chosen = session_to_v3(_session({"effort": "max", "effort_source": "user"}))
    assert chosen["effort"] == "max"
