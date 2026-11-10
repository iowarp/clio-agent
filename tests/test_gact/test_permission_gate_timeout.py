"""A permission question waits for the person; only an explicit deadline expires it.

The REAL gate on a REAL app. With no deadline (every question, CT13) the gate keeps
waiting and the wait counts as turn progress. With one (the hook defer), the expiry is
typed, published and logged (#1577 4), and an answer that lands first always wins.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import permission_timeout
from clio_agent.gact.app import _make_permission_gate, build_app

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_an_unanswered_question_waits_for_the_person_and_counts_as_progress(
    tmp_path: Path,
) -> None:
    """Nothing expires a question (CT13): the gate still waits well past any old deadline,
    the wait counts as the session's turn progress, and the person's answer then decides.

    SABOTAGE: restore a short default deadline in ``wait_for_permission`` -> the call is
    denied before the person answers -> red; drop ``track`` -> no progress -> red."""
    from clio_agent.runtime.commitment_activity import commitment_wait_in_flight

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        gate = _make_permission_gate(app)
        result: dict[str, object] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("d", gate("shell.exec", {"cmd": "rm -rf /tmp/x"})),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not app.state.permissions and time.monotonic() < deadline:
            time.sleep(0.02)
        (row,) = app.state.permissions.values()
        time.sleep(1.0)  # a person who has stepped away
        assert thread.is_alive() and row["status"] == "pending"
        assert commitment_wait_in_flight(row["session_id"] or sid)
        assert c.post(f"/v1/permissions/{row['id']}", json={"action": "allow"}).status_code == 204
        thread.join(timeout=10)
        assert result["d"] == "allow"
        assert not commitment_wait_in_flight(row["session_id"] or sid)


def test_an_explicit_deadline_still_expires_typed_and_loud(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller that passes a deadline (the hook defer) gets the typed, published, logged
    expiry, never a silent deny.

    SABOTAGE: restore ``row["status"] = "timeout"; return "deny"`` -> no reason, no
    event, no log, a plain "deny" without a message -> red."""
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        row = {"id": "perm_x", "session_id": sid, "status": "pending", "tool": "shell.exec"}
        app.state.permissions["perm_x"] = row
        app.state.permission_events["perm_x"] = event = threading.Event()
        with caplog.at_level(logging.WARNING):
            decision = permission_timeout.wait_for_permission(
                app, "perm_x", row, event, tool_name="shell.exec", cancelled=None, timeout_s=0.3
            )
        assert decision == "deny"
        message = getattr(decision, "deny_message", "")
        assert "timed out" in message and "shell.exec" in message
        assert row["status"] == "timeout" and row["action"] == "deny"
        assert row["reason"] == permission_timeout.REASON_PERMISSION_TIMEOUT
        assert row["id"] not in app.state.permission_events
        resolved = [
            e
            for e in app.state.bus.session_events_since(sid)
            if e.type == "permission.resolved" and e.payload.get("permission_id") == row["id"]
        ]
        assert (
            resolved
            and resolved[0].payload["reason"] == permission_timeout.REASON_PERMISSION_TIMEOUT
        )
    warned = [r for r in caplog.records if "reason=permission_request_timeout" in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING


def test_an_answer_racing_the_expiry_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A row the user resolved is never overwritten as a timeout.

    SABOTAGE: drop the ``status == "pending"`` check in ``expire_permission`` -> the
    resolved allow is rewritten to a timeout deny -> red.
    """
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        c.post("/v1/sessions", json={"title": "t"})
        gate = _make_permission_gate(app)
        result: dict[str, str] = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("d", gate("shell.exec", {"cmd": "touch x"}))
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not app.state.permissions and time.monotonic() < deadline:
            time.sleep(0.02)
        (row,) = app.state.permissions.values()
        assert c.post(f"/v1/permissions/{row['id']}", json={"action": "allow"}).status_code == 204
        thread.join(timeout=10)
        # The expiry path, reached late (the answer already landed), must not rewrite it.
        assert permission_timeout.expire_permission(app, row["id"], row, tool_name="x") is None
        assert row["status"] == "resolved" and row["action"] == "allow"
        assert result["d"] == "allow"
