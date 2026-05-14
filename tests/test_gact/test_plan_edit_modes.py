"""iowarp/clio-agent — plan_mode + edit_modes + #4 file_diff write.

Covers:
  - Session.mode + edit_mode in create + patch responses.
  - capabilities.plan_mode + edit_modes advertised.
  - PATCH /v1/sessions/{sid} flips mode/edit_mode + publishes
    session.updated.
  - Permission gate auto-denies destructive tools when mode is
    plan or architect (no user prompt; reason logged).
  - /diffs/apply actually writes new_content to disk under the
    workspace root.
  - Disk write refuses when path is outside the workspace root.
  - Disk write refuses when session.mode is plan or architect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _apply_edit_to_disk,
    _make_permission_gate,
    build_app,
)


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""
    file_diffs: list = field(default_factory=list)


class _Agent:
    def __init__(self, pred):
        self._pred = pred

    def forward(self, *args, **kwargs):
        return self._pred


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_Agent(_Pred()))
    )


def test_capabilities_advertise_plan_and_edit_modes(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["plan_mode"] is True
    assert body["capabilities"]["edit_modes"] is True


def test_create_session_with_explicit_mode(client: TestClient) -> None:
    sess = client.post(
        "/v1/sessions",
        json={"title": "explore", "mode": "plan", "edit_mode": "patch"},
    ).json()
    assert sess["mode"] == "plan"
    assert sess["edit_mode"] == "patch"


def test_patch_session_flips_mode(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    patched = client.patch(
        f"/v1/sessions/{sid}", json={"mode": "edit", "edit_mode": "whole"}
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["mode"] == "edit"
    assert body["edit_mode"] == "whole"
    fresh = client.get(f"/v1/sessions/{sid}").json()
    assert fresh["mode"] == "edit"


def test_patch_session_unknown_session_404s(client: TestClient) -> None:
    resp = client.patch("/v1/sessions/sess_nope", json={"mode": "plan"})
    assert resp.status_code == 404


def test_plan_mode_auto_denies_destructive_tools(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post(
            "/v1/sessions", json={"title": "ro", "mode": "plan"}
        ).json()["id"]
        gate = _make_permission_gate(app)
        # Most-recently-active session is the one we just created
        # with mode=plan; destructive call must be auto-denied
        # without a permission prompt.
        decision = gate("shell.exec", {"cmd": "rm -rf /"})
        assert decision == "deny"
        # And a permission row should record auto_denied for audit.
        assert any(
            r["session_id"] == sid and r["status"] == "auto_denied"
            for r in app.state.permissions.values()
        )


def test_diffs_apply_writes_to_disk(tmp_path: Path) -> None:
    sample_diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    pred = _Pred(file_diffs=[{
        "path": str(tmp_path / "ws" / "x.py"),
        "unified_diff": sample_diff,
        "new_content": "print('hello new')\n",
    }])
    app = build_app(
        sessions_path=tmp_path / "s.json", agent=_Agent(pred)
    )
    # Pin ws_default's root to tmp_path/ws so the write passes
    # the workspace boundary check.
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    with TestClient(app) as c:
        from .conftest import complete_turn

        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(c, sid, "make the edit")

        target = tmp_path / "ws" / "x.py"
        assert not target.exists(), "should not have been written before /apply"

        resp = c.post(
            f"/v1/sessions/{sid}/diffs/apply", json={}
        ).json()
        assert resp["applied"] == [str(target)]
        assert "write_errors" not in resp
        assert target.read_text() == "print('hello new')\n"


def test_apply_edit_refuses_outside_workspace(tmp_path: Path) -> None:
    """_apply_edit_to_disk raises PermissionError when target is
    outside the workspace root."""

    app = build_app(sessions_path=tmp_path / "s.json")
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    sess = app.state.sessions.create(
        workspace_id="ws_default", title="t",
    )
    with pytest.raises(PermissionError, match="outside workspace root"):
        _apply_edit_to_disk(
            path=str(tmp_path / "outside.txt"),
            new_content="x",
            session=sess,
            app=app,
        )


def test_apply_edit_refuses_in_plan_mode(tmp_path: Path) -> None:
    """_apply_edit_to_disk refuses to write when session.mode
    is plan or architect."""

    app = build_app(sessions_path=tmp_path / "s.json")
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    sess = app.state.sessions.create(
        workspace_id="ws_default", title="t", mode="plan",
    )
    with pytest.raises(PermissionError, match="session.mode"):
        _apply_edit_to_disk(
            path=str(tmp_path / "ws" / "x.txt"),
            new_content="x",
            session=sess,
            app=app,
        )
