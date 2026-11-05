"""A ``run_code`` script's approval covers its nested calls by approval mode (TD16)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.app import _make_permission_gate, _tool_session_context, build_app
from clio_agent.gact.code_mode import grants
from tests.turn_signals import TURN_SIGNAL_BACKSTOP_S

pytestmark = pytest.mark.usefixtures("host_agent_executor")

#: A non-read call no catalog tag classifies: every mode but bypass asks for it.
_NON_READ = "shell.exec"


def _session(client: Any, mode: str) -> str:
    return client.post("/v1/sessions", json={"title": "t", "approval_mode": mode}).json()["id"]


def _pending(app: Any) -> dict[str, Any]:
    deadline = time.time() + TURN_SIGNAL_BACKSTOP_S
    while time.time() < deadline:
        rows = [r for r in app.state.permissions.values() if r["status"] == "pending"]
        if rows:
            return rows[0]
        time.sleep(0.02)
    raise AssertionError("no permission question appeared")


# spotter-ai and spotter-watcher need SPOTTER set up; `covers()` is unit-tested for them.
@pytest.mark.parametrize("mode", ["auto-edits", "ai-review"])
def test_the_script_approval_covers_nested_calls(tmp_path: Path, mode: str) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, mode)
        gate = _make_permission_gate(app)
        token = grants.bind_grant(grants.ScriptGrant("call_1", "await tools.shell.exec()"))
        try:
            with _tool_session_context(sid):
                assert gate(_NON_READ, {"cmd": "ls"}) == "allow"
        finally:
            grants.reset_grant(token)
        (row,) = app.state.permissions.values()
        assert (row["status"], row["reason"]) == ("auto_approved", grants.REASON_SCRIPT_GRANT)


def test_without_a_script_the_same_call_asks(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "auto-edits")
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}

        def fire() -> None:
            with _tool_session_context(sid):
                result["decision"] = gate(_NON_READ, {"cmd": "ls"})

        thread = threading.Thread(target=fire)
        thread.start()
        row = _pending(app)
        assert "script" not in row
        assert (
            client.post(f"/v1/permissions/{row['id']}", json={"action": "deny"}).status_code == 204
        )
        thread.join(timeout=5)
        assert result["decision"] == "deny"


def test_ask_mode_asks_with_the_script_and_allow_script_covers_the_rest(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "ask")
        gate = _make_permission_gate(app)
        grant = grants.ScriptGrant("call_9", "for f in files:\n    await tools.shell.exec(f)")
        result: dict[str, str] = {}

        def fire() -> None:
            token = grants.bind_grant(grant)
            try:
                with _tool_session_context(sid):
                    result["first"] = gate(_NON_READ, {"cmd": "a"})
                    result["second"] = gate(_NON_READ, {"cmd": "b"})
            finally:
                grants.reset_grant(token)

        thread = threading.Thread(target=fire)
        thread.start()
        row = _pending(app)
        assert row["script"] == {"call_id": "call_9", "code": grant.code}
        assert row["actions"][-1] == grants.ALLOW_SCRIPT
        assert grant.waiting
        answer = client.post(f"/v1/permissions/{row['id']}", json={"action": grants.ALLOW_SCRIPT})
        assert answer.status_code == 204
        thread.join(timeout=5)
        assert result == {"first": "allow", "second": "allow"}
        assert grant.covered and not grant.waiting
        reasons = [r.get("reason") for r in app.state.permissions.values()]
        assert grants.REASON_SCRIPT_GRANT in reasons


def test_allow_script_is_refused_on_a_question_that_does_not_offer_it(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "ask")
        gate = _make_permission_gate(app)

        def fire() -> None:
            with _tool_session_context(sid):
                gate(_NON_READ, {"cmd": "ls"})

        thread = threading.Thread(target=fire)
        thread.start()
        row = _pending(app)
        refused = client.post(f"/v1/permissions/{row['id']}", json={"action": grants.ALLOW_SCRIPT})
        assert refused.status_code == 422
        client.post(f"/v1/permissions/{row['id']}", json={"action": "deny"})
        thread.join(timeout=5)


def _background_question(app: Any, sid: str, stop: threading.Event) -> tuple[dict, Any]:
    """Ask the gate as a background script's nested call would; the row and the thread."""
    from clio_agent.gact import native_code

    result: dict[str, Any] = {}

    def fire() -> None:
        token = native_code._BACKGROUND_TASK.set(native_code.BackgroundTask("task_bg", stop))
        try:
            with _tool_session_context(sid):
                result["decision"] = _make_permission_gate(app)(_NON_READ, {"cmd": "ls"})
        finally:
            native_code._BACKGROUND_TASK.reset(token)

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    return result, thread


def test_a_background_scripts_question_is_its_tasks_and_outlives_an_unrelated_stop(
    tmp_path: Path,
) -> None:
    """CT8: the question is tagged with the script's task; Stop on whatever turn the
    session runs then does not deny it; cancelling the task does.

    SABOTAGE: take the cancel token from the session's turn again -> the unrelated Stop
    denies the question -> red."""
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "auto-edits")
        stop = threading.Event()
        result, thread = _background_question(app, sid, stop)
        row = _pending(app)
        assert row["task_handle"] == "task_bg"
        unrelated = threading.Event()
        app.state.cancel_events[sid] = unrelated
        unrelated.set()  # the person stops a later turn of the same conversation
        time.sleep(0.5)
        assert thread.is_alive() and row["status"] == "pending"
        stop.set()  # the background task is cancelled
        thread.join(timeout=5)
        assert result["decision"] == "deny"


def test_a_background_scripts_question_resumes_on_the_answer_with_no_turn(
    tmp_path: Path,
) -> None:
    """With no turn running, the person's answer reaches the waiting script."""
    from fastapi.testclient import TestClient

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = _session(client, "auto-edits")
        result, thread = _background_question(app, sid, threading.Event())
        row = _pending(app)
        assert (
            client.post(f"/v1/permissions/{row['id']}", json={"action": "allow"}).status_code == 204
        )
        thread.join(timeout=5)
        assert result["decision"] == "allow"
