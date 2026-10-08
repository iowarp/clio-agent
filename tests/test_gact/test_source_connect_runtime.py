"""Source attachment approval and source/account status use the real storage service."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.storage import connect_tool, setup_tool
from clio_agent.gact.storage.service import StorageService


@pytest.fixture
def bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[StorageService, Any, Path]]:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = build_app(sessions_path=tmp_path / "sessions.json")
    configured = app.state.workspaces.update("ws_default", root_path=str(workspace))
    assert configured is not None
    configured.config["granted_write_roots"] = [str(tmp_path / "second")]
    sid = app.state.sessions.create(workspace_id="ws_default", title="owner").id
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "measurements.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    with TestClient(app):
        app.state.connected_storage = service
        app.state.pending_permission_gate = lambda *args: "allow"
        monkeypatch.setattr(connect_tool.context, "active_app", lambda: app)
        monkeypatch.setattr(connect_tool.context, "active_session_id", lambda: sid)
        yield service, app, upstream


def test_permission_denial_leaves_no_source(bound: tuple[StorageService, Any, Path]) -> None:
    service, app, upstream = bound
    app.state.pending_permission_gate = lambda *args: "deny"
    with pytest.raises(PermissionError, match="not approved"):
        connect_tool.connected_data_connect("local", str(upstream), "Measurements")
    assert service.sources("ws_default") == []


@pytest.mark.parametrize("mode,decision", [("ask", "deny"), ("ask", "allow"), ("bypass", "allow")])
def test_connection_uses_live_permission_gate(
    tmp_path: Path, mode: str, decision: str, request: pytest.FixtureRequest
) -> None:
    """Prompted and bypass connections share the actual audited permission gate."""
    import threading
    import time

    from clio_agent.gact.app import _gact_app_context, _tool_session_context
    from tests.test_gact.test_a2ui_v3 import HEADERS, _session_client

    client, sid, _ = _session_client(tmp_path)
    client.__enter__()
    request.addfinalizer(lambda: client.__exit__(None, None, None))
    app = client.app
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "readme.txt").write_text("input", encoding="utf-8")
    response = client.patch(f"/v1/sessions/{sid}", headers=HEADERS, json={"approval_mode": mode})
    assert response.status_code == 200
    outcomes: list[object] = []

    def connect() -> None:
        with _gact_app_context(app), _tool_session_context(sid):
            token = connect_tool.context.set_session_id(sid)
            try:
                outcomes.append(
                    connect_tool.connected_data_connect("local", str(upstream), "Input")
                )
            except PermissionError as exc:
                outcomes.append(exc)
            finally:
                connect_tool.context.reset(token)

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    if mode == "ask":
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not app.state.permissions:
            time.sleep(0.01)
        assert app.state.permissions
        pending = next(iter(app.state.permissions.values()))
        assert pending["status"] == "pending"
        assert pending["tool_call"]["tool_name"] == "connected_data_connect"
        replied = client.post(
            f"/v1/permissions/{pending['id']}", headers=HEADERS, json={"action": decision}
        )
        assert replied.status_code == 204
    worker.join(timeout=5)
    assert not worker.is_alive()
    session = app.state.sessions.get(sid)
    assert session is not None
    records = app.state.connected_storage.sources(session.workspace_id)
    if decision == "deny":
        assert isinstance(outcomes[0], PermissionError) and records == []
    else:
        assert isinstance(outcomes[0], dict) and len(records) == 1
    if mode == "bypass":
        row = next(iter(app.state.permissions.values()))
        assert row["status"] == "auto_approved" and row["reason"] == "approval_mode_bypass"


def test_connection_is_real_and_reused_without_a_second_approval(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, app, upstream = bound
    requests: list[tuple[Any, ...]] = []

    def allow(*args: Any) -> str:
        requests.append(args)
        return "allow"

    app.state.pending_permission_gate = allow
    first = connect_tool.connected_data_connect("local", str(upstream), "Measurements")
    from clio_agent.gact.task_controls import wait_tasks

    settled = wait_tasks(first["handle"])
    assert settled["results"][0]["status"] == "completed"
    assert first["connection"]["state"] == "indexing"
    second = connect_tool.connected_data_connect("local", str(upstream), "Measurements")
    assert second["connection"]["source_id"] == first["connection"]["source_id"]
    assert second["connection"]["state"] == "linked"
    assert len(requests) == 1 and requests[0][0] == "connected_data_connect"
    opened = setup_tool.connected_data_open(first["connection"]["source_id"])
    assert [row["path"] for row in opened["entries"]] == ["measurements.csv"]
    assert len(service.sources("ws_default")) == 1


def test_status_keeps_workspace_folders_when_remote_sources_are_empty(
    bound: tuple[StorageService, Any, Path],
) -> None:
    _, _, _ = bound
    result = setup_tool.connected_data_status()
    assert result["sources"] == []
    assert [folder["role"] for folder in result["workspace_folders"]] == ["primary", "additional"]
    assert not any(
        block["type"] == "link"
        for block in setup_tool.setup_presentation({}, result, None)["blocks"]
    )


def test_oauth_connection_waits_for_private_sign_in(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, _, _ = bound
    result = connect_tool.connected_data_connect("google_drive", "drive-folder", "Dataset")
    assert result["connection"]["state"] == "sign_in_required"
    assert service.sources("ws_default")[0].linked_manifest_id is None
    assert (
        next(row for row in result["accounts"] if row["provider"] == "google_drive")[
            "login_action"
        ]["name"]
        == "data_source/login/google_drive"
    )


def test_source_configuration_cannot_smuggle_a_credential(
    bound: tuple[StorageService, Any, Path],
) -> None:
    service, _, _ = bound
    with pytest.raises(ValueError):
        connect_tool.connected_data_connect(
            "github", "iowarp/clio-agent", "Repo", configuration={"token": "must-not-accept"}
        )
    assert service.sources("ws_default") == []
