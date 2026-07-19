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
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from clio_agent.agent import ClioAgent
from clio_agent.gact.app import (
    _apply_edit_to_disk,
    _make_permission_gate,
    build_app,
)
from clio_agent.tools.file_policy import FilePolicyError

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake (a ``dspy.Module`` host — e.g. the real
# ``ClioAgent`` with mocked planner internals — is streamified unchanged).
pytestmark = pytest.mark.usefixtures("host_agent_executor")


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


def test_diffs_apply_writes_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sample_diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    workspace = tmp_path / "ws"
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    pred = _Pred(
        file_diffs=[
            {
                "path": str(workspace / "x.py"),
                "unified_diff": sample_diff,
                "new_content": "print('hello new')\n",
            }
        ]
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(pred))
    # Pin ws_default's root to tmp_path/ws so the write passes
    # the workspace boundary check.
    workspace.mkdir()
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    with TestClient(app) as c:
        from .conftest import complete_turn

        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        complete_turn(c, sid, "make the edit")

        target = workspace / "x.py"
        assert not target.exists(), "should not have been written before /apply"

        resp = c.post(f"/v1/sessions/{sid}/diffs/apply", json={}).json()
        assert resp["applied"] == [str(target)]
        assert "write_errors" not in resp
        assert target.read_text() == "print('hello new')\n"


def test_real_agent_propose_edit_trace_becomes_applicable_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "real.py"
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    proposed = {
        "path": str(target),
        "unified_diff": "--- a/real.py\n+++ b/real.py\n@@\n-old\n+new\n",
        "new_content": "print('from promoted diff')\n",
        "lines_added": 1,
        "lines_removed": 1,
    }
    agent = ClioAgent(data_dir=str(tmp_path / "clio"), verbose=False)
    agent._plan_next_action = MagicMock(
        side_effect=[
            {
                "action": "tool",
                "tool": "fs_propose_edit",
                "args": {
                    "filepath": str(target),
                    "new_content": proposed["new_content"],
                },
                "reason": "propose edit",
            },
            {"action": "answer", "answer": "Review the proposed diff.", "reason": "done"},
        ]
    )
    agent._selected_expert_for_tool = MagicMock(return_value="data")

    def execute(tool_name: str, raw_args: dict, trace) -> dict:
        trace.record_tool(
            tool=tool_name,
            params=raw_args,
            result=proposed,
            duration_ms=1.0,
            ok=True,
        )
        return proposed

    agent._execute_tool_action = MagicMock(side_effect=execute)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    app.state.workspaces.update("ws_default", root_path=str(workspace))

    try:
        with TestClient(app) as c:
            from .conftest import complete_turn

            sid = c.post(
                "/v1/sessions",
                json={"title": "t", "edit_mode": "patch"},
            ).json()["id"]
            msg = complete_turn(c, sid, "propose the edit")
            diff_parts = [part for part in msg["parts"] if part["type"] == "file_diff"]
            # #731: parts serialize via the slim ``to_wire`` projection (omitempty —
            # only fields the part set survive, plus the id/type/agent_id triple and
            # the monotonic ``sequence`` ordering key), identical to the live SSE
            # shape so a reloaded conversation matches what streamed.
            assert diff_parts == [
                {
                    "id": diff_parts[0]["id"],
                    "type": "file_diff",
                    "agent_id": "data",
                    "sequence": diff_parts[0]["sequence"],
                    "path": str(target),
                    "unified_diff": proposed["unified_diff"],
                    "new_content": proposed["new_content"],
                    "status": "pending",
                    "edit_mode": "patch",
                    "lines_added": 1,
                    "lines_removed": 1,
                }
            ]
            assert not target.exists()

            resp = c.post(f"/v1/sessions/{sid}/diffs/apply", json={}).json()
            assert resp["applied"] == [str(target)]
            assert "write_errors" not in resp
            assert target.read_text(encoding="utf-8") == proposed["new_content"]
    finally:
        agent.shutdown()


def test_apply_edit_uses_shared_policy_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GACT diff apply must not maintain a separate raw disk-write path."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(
        workspace_id="ws_default",
        title="t",
        mode="edit",
    )
    calls: list[tuple[str, str]] = []

    def spy_writer(filepath: str, new_content: str) -> dict[str, object]:
        calls.append((filepath, new_content))
        target = Path(filepath)
        target.write_text(new_content, encoding="utf-8")
        return {"path": str(target), "size_bytes": target.stat().st_size, "ok": True}

    monkeypatch.setattr("clio_agent.gact.enrichment.write_text_with_policy", spy_writer)

    target = workspace / "x.txt"
    result = _apply_edit_to_disk(
        path=str(target),
        new_content="shared\n",
        session=sess,
        app=app,
    )

    assert calls == [(str(target.resolve()), "shared\n")]
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "shared\n"


def test_apply_edit_refuses_outside_workspace(tmp_path: Path) -> None:
    """_apply_edit_to_disk raises PermissionError when target is
    outside the workspace root."""

    app = build_app(sessions_path=tmp_path / "s.json")
    (tmp_path / "ws").mkdir()
    app.state.workspaces.update("ws_default", root_path=str(tmp_path / "ws"))
    sess = app.state.sessions.create(
        workspace_id="ws_default",
        title="t",
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
        workspace_id="ws_default",
        title="t",
        mode="plan",
    )
    with pytest.raises(PermissionError, match="session.mode"):
        _apply_edit_to_disk(
            path=str(tmp_path / "ws" / "x.txt"),
            new_content="x",
            session=sess,
            app=app,
        )


def test_apply_edit_refuses_policy_denied_write(tmp_path: Path) -> None:
    """A deny policy must block /diffs/apply before disk mutation."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(
        workspace_id="ws_default",
        title="t",
        mode="edit",
    )
    app.state.permission_policies = [
        {
            "scope": "session",
            "scope_id": sess.id,
            "tool_name_pattern": "fs_apply_edit_write",
            "path_pattern": str(workspace / "*.txt"),
            "action": "deny",
        }
    ]

    target = workspace / "x.txt"
    with pytest.raises(PermissionError, match="permission policy denied"):
        _apply_edit_to_disk(
            path=str(target),
            new_content="x",
            session=sess,
            app=app,
        )

    assert not target.exists()
    rows = list(app.state.permissions.values())
    assert len(rows) == 1
    assert rows[0]["status"] == "auto_denied"
    assert rows[0]["reason"] == "policy_deny"


def test_apply_edit_refuses_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace scope is not enough; writes must also pass file_policy."""

    workspace = tmp_path / "ws"
    allowed = tmp_path / "allowed"
    workspace.mkdir()
    allowed.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(allowed))

    app = build_app(sessions_path=tmp_path / "s.json")
    app.state.workspaces.update("ws_default", root_path=str(workspace))
    sess = app.state.sessions.create(
        workspace_id="ws_default",
        title="t",
        mode="edit",
    )
    with pytest.raises(FilePolicyError) as exc:
        _apply_edit_to_disk(
            path=str(workspace / "x.txt"),
            new_content="x",
            session=sess,
            app=app,
        )
    assert exc.value.code == "outside_allowed_roots"
