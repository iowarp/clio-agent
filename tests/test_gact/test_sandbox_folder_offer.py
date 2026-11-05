"""A confirmed sandbox block raises the folder-access card; a grant elsewhere settles it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.sandbox_folder_offer import (
    ORIGIN_SANDBOX_BOUNDARY,
    offer_blocked_folders,
)
from clio_agent.runtime.sandbox_roots import clear_write_root_grants


@pytest.fixture(autouse=True)
def clean_grants() -> Any:
    clear_write_root_grants()
    yield
    clear_write_root_grants()


@pytest.fixture
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, TestClient, str, str, Path]:
    root, outside = tmp_path / "Workspace", tmp_path / "Outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(root))
    monkeypatch.setitem(conf.store().data["tools"]["file_policy"], "allowed_roots", [str(root)])
    app = build_app(sessions_path=tmp_path / "sessions.json")
    client = TestClient(app)
    wid = client.post("/v1/workspaces", json={"name": "Copy", "root_path": str(root)}).json()["id"]
    sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
    return app, client, wid, sid, outside


def _shell_result(outside: Path, **overrides: Any) -> dict[str, Any]:
    """A settled shell task envelope whose reply carries the diagnosis (the real shape)."""
    boundary = {
        "kind": "write_denied",
        "confidence": "confirmed",
        "mechanism": "landlock",
        "paths": [str(outside / "notes.txt")],
        "request_roots": [str(outside)],
        "message": "CLIO's sandbox blocked this write.",
        **overrides,
    }
    reply = {"structuredContent": {"exit_code": 1, "boundary": boundary}, "isError": False}
    return {
        "structuredContent": {
            "handle": "task_shell",
            "pending": False,
            "result": {"result": reply},
        }
    }


def _offer(app: Any, sid: str, wid: str, result: Any) -> dict[str, Any] | None:
    return offer_blocked_folders(
        app,
        sid,
        tool_name="shell_bash",
        args={"command": "cp notes.txt /elsewhere"},
        call_id="call_1",
        result=result,
        workspace_id=wid,
    )


def test_a_confirmed_block_raises_one_folder_card(world: Any) -> None:
    """SABOTAGE: drop the envelope lookup in ``_boundary`` -> no card -> red."""
    app, client, wid, sid, outside = world
    row = _offer(app, sid, wid, _shell_result(outside))
    assert row is not None and row["status"] == "pending"
    assert row["kind"] == "folder_access" and row["origin"] == ORIGIN_SANDBOX_BOUNDARY
    assert row["access"]["folders"] == [str(outside)]
    assert row["access"]["command"] == "cp notes.txt /elsewhere"
    assert row["actions"] == ["deny", "allow_workspace"]
    # Shown to the person as a pending permission (the attention the UI notifies on).
    listed = client.get(f"/v1/permissions?session_id={sid}&status=pending").json()
    assert [p["id"] for p in listed["permissions"]] == [row["id"]]
    # The same block again does not ask twice.
    assert _offer(app, sid, wid, _shell_result(outside)) is None


@pytest.mark.parametrize(
    "overrides",
    [{"confidence": "possible"}, {"kind": "elevation_required"}, {"request_roots": []}],
)
def test_no_card_without_a_confirmed_folder_to_ask_for(world: Any, overrides: Any) -> None:
    app, _client, wid, sid, outside = world
    assert _offer(app, sid, wid, _shell_result(outside, **overrides)) is None
    assert not [r for r in app.state.permissions.values() if r.get("status") == "pending"]


def test_allowing_the_card_grants_the_folder(world: Any) -> None:
    app, client, wid, sid, outside = world
    row = _offer(app, sid, wid, _shell_result(outside))
    assert row is not None
    answer = client.post(f"/v1/permissions/{row['id']}", json={"action": "allow_workspace"})
    assert answer.status_code == 204, answer.text
    assert app.state.permissions[row["id"]]["status"] == "resolved"
    granted = app.state.workspaces.get(wid).config.get("granted_write_roots") or []
    assert any(Path(g) == outside.resolve() for g in granted)


def test_a_grant_from_the_notice_settles_the_card(world: Any) -> None:
    """SABOTAGE: drop the settle call in the grants route -> the card stays pending -> red."""
    app, client, wid, sid, outside = world
    row = _offer(app, sid, wid, _shell_result(outside))
    assert row is not None
    granted = client.post(
        f"/v1/workspaces/{wid}/grants", json={"kind": "fs_root", "pattern": str(outside)}
    )
    assert granted.status_code == 200, granted.text
    settled = app.state.permissions[row["id"]]
    assert settled["status"] == "resolved" and settled["action"] == "allow_workspace"
    assert settled["settled_by"] == "workspace_grant"
    assert (
        client.get(f"/v1/permissions?session_id={sid}&status=pending").json()["permissions"] == []
    )
