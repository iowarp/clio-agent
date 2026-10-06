"""A normal A2UI Button requests private sign-in without starting an agent turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from .test_a2ui_actions import _event, _stub_spawn
from .test_a2ui_v3 import HEADERS, _create_message, _session_client


@pytest.mark.parametrize("provider", ["github", "google_drive", "globus"])
def test_private_login_is_audited_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    created = client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    assert created.status_code == 200, created.text
    spawned = _stub_spawn(app, monkeypatch)
    service = app.state.connected_storage
    session = app.state.sessions.get(sid)
    assert session is not None

    def no_auth(*args: object) -> None:
        raise AssertionError("An A2UI click must not obtain or create a credential")

    monkeypatch.setattr(service.auth, "token", no_auth)
    action = _event(
        f"data_source/login/{provider}",
        "surface_1",
        {"clio_id": service.store.clio_id, "workspace_id": session.workspace_id},
        "2026-10-05T00:00:00Z",
    )
    first = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert first.status_code == 200, first.text
    assert first.json()["destination"] == "client"
    assert first.json()["delivery"] == "client_ui"
    assert first.json()["state"] == "consumed"
    second = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert second.json()["action_id"] == first.json()["action_id"]
    assert spawned == []


def test_login_for_another_owner_is_rejected(tmp_path: Path) -> None:
    client, sid, _ = _session_client(tmp_path)
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    action = _event(
        "data_source/login/github",
        "surface_1",
        {"clio_id": "wrong", "workspace_id": "foreign"},
        "2026-10-05T00:00:00Z",
    )
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert response.status_code == 422
    assert "a2ui_source_login_out_of_scope" in response.text


def test_login_with_extra_parameters_is_rejected(tmp_path: Path) -> None:
    """The server and client accept the same owner-only action context."""
    client, sid, _ = _session_client(tmp_path)
    app = client.app
    client.post(
        f"/v1/sessions/{sid}/a2ui/messages", headers=HEADERS, json={"messages": [_create_message()]}
    )
    session = app.state.sessions.get(sid)
    assert session is not None
    action = _event(
        "data_source/login/github",
        "surface_1",
        {
            "clio_id": app.state.connected_storage.store.clio_id,
            "workspace_id": session.workspace_id,
            "redirect": "https://example.invalid",
        },
        "2026-10-05T00:00:00Z",
    )
    response = client.post(
        f"/v1/sessions/{sid}/a2ui/actions", headers=HEADERS, json={"message": action}
    )
    assert response.status_code == 422
