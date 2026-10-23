"""iowarp/clio-agent — plan_mode + edit_modes + the Undo write boundary.

Covers:
  - Session.mode + edit_mode in create + patch responses.
  - capabilities.plan_mode + edit_modes advertised.
  - PATCH /v1/sessions/{sid} flips mode/edit_mode + publishes
    session.updated.
  - Permission gate auto-denies destructive tools when mode is
    plan or architect (no user prompt; reason logged).
  - Undo (``revert_file_change``) writes through the shared policy writer
    under the workspace root, and refuses outside the workspace root, in
    plan mode, and under a deny policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import (
    _make_permission_gate,
    build_app,
)
from clio_agent.gact.file_change_revert import FileChangeRefusal, revert_file_change

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake (a ``dspy.Module`` host — e.g. the real
# ``ClioAgent`` with mocked planner internals — is streamified unchanged).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def __init__(self, pred):
        self._pred = pred

    def forward(self, *args, **kwargs):
        return self._pred


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(_Pred())))


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
    patched = client.patch(f"/v1/sessions/{sid}", json={"mode": "edit", "edit_mode": "whole"})
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
        sid = c.post("/v1/sessions", json={"title": "ro", "mode": "plan"}).json()["id"]
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


def _recorded_change(app: Any, sess: Any, target: Path, previous: str, now: str) -> str:
    """Put an agent write of ``target`` (``previous`` -> ``now``) on the change ledger."""

    target.write_text(now, encoding="utf-8")
    app.state.file_changes.setdefault(sess.id, []).append(
        {
            "id": "chg_1",
            "path": str(target),
            "status": "applied",
            "created": False,
            "previous_content": previous,
            "sha256_after": hashlib.sha256(now.encode()).hexdigest(),
        }
    )
    return "chg_1"


def test_undo_uses_shared_policy_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo must not maintain a separate raw disk-write path."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    calls: list[tuple[str, str]] = []

    def spy_writer(filepath: str, new_content: str) -> dict[str, object]:
        calls.append((filepath, new_content))
        target = Path(filepath)
        target.write_text(new_content, encoding="utf-8")
        return {"path": str(target), "size_bytes": target.stat().st_size, "ok": True}

    monkeypatch.setattr("clio_agent.gact.file_change_revert.write_text_with_policy", spy_writer)

    target = workspace / "x.txt"
    change = _recorded_change(app, sess, target, "shared\n", "agent\n")
    result = revert_file_change(app, sess, change)

    assert calls == [(str(target.resolve()), "shared\n")]
    assert result["status"] == "reverted"
    assert target.read_text(encoding="utf-8") == "shared\n"


def test_undo_refuses_outside_workspace(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    sess = app.state.sessions.create(workspace_id="ws_default", title="t")
    change = _recorded_change(app, sess, tmp_path / "outside.txt", "a", "b")
    with pytest.raises(FileChangeRefusal) as refused:
        revert_file_change(app, sess, change)
    assert refused.value.code == "path_outside_workspace"
    assert refused.value.status_code == 403


def test_undo_refuses_in_plan_mode(tmp_path: Path) -> None:
    """Undo is a write: in plan mode the unified resolver (the built-in plan_acl
    @40 deny) refuses it, typed ``policy_denied``."""

    app = build_app(sessions_path=tmp_path / "s.json")
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="plan")
    target = tmp_path / "ws" / "x.txt"
    change = _recorded_change(app, sess, target, "a", "b")
    with pytest.raises(FileChangeRefusal) as refused:
        revert_file_change(app, sess, change)
    assert refused.value.code == "policy_denied"
    assert target.read_text(encoding="utf-8") == "b"


def test_undo_refuses_policy_denied_write(tmp_path: Path) -> None:
    """A deny policy on fs_write_file blocks Undo before disk mutation."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    app.state.permission_policies = [
        {
            "scope": "session",
            "scope_id": sess.id,
            "tool_name_pattern": "fs_write_file",
            "path_pattern": str(workspace / "*.txt"),
            "action": "deny",
        }
    ]

    target = workspace / "x.txt"
    change = _recorded_change(app, sess, target, "before", "after")
    with pytest.raises(FileChangeRefusal) as refused:
        revert_file_change(app, sess, change)

    assert refused.value.code == "policy_denied"
    assert target.read_text(encoding="utf-8") == "after"
    rows = list(app.state.permissions.values())
    assert len(rows) == 1
    assert rows[0]["status"] == "auto_denied"
    assert rows[0]["reason"] == "policy_deny"


def test_undo_inside_the_workspace_survives_a_narrower_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1334 review round: ``tool_workspace_context`` binds the session's OWN workspace
    as an always-writable file_policy root, so a configured ``allowed_roots`` narrower
    than the workspace does not refuse an Undo INSIDE it; the workspace containment
    check remains the boundary for a path outside the workspace."""

    workspace = tmp_path / "ws"
    allowed = tmp_path / "allowed"
    workspace.mkdir()
    allowed.mkdir()
    from tests._config_layer import set_config

    set_config("tools.file_policy.allowed_roots", [str(allowed)])

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(workspace_id="ws_default", title="t", mode="edit")
    target = workspace / "x.txt"
    change = _recorded_change(app, sess, target, "x", "y")
    revert_file_change(app, sess, change)
    assert target.read_text(encoding="utf-8") == "x"
