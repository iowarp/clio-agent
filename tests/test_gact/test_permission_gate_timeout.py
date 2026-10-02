"""A permission request nobody answers expires loudly, never as a silent deny (#1577 4).

The REAL gate on a REAL app: the request registers, nobody answers within the (shortened)
window, and the outcome is a resolved row with the typed timeout reason, a
``permission.resolved`` event carrying that reason, a WARNING log, and a deny whose
model-facing message says the request timed out (not an ordinary refusal).
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


def test_an_unanswered_permission_request_expires_typed_and_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SABOTAGE: restore ``row["status"] = "timeout"; return "deny"`` -> no reason, no
    event, no log, a plain "deny" without a message -> red."""
    monkeypatch.setattr(permission_timeout, "PERMISSION_REQUEST_TIMEOUT_S", 0.3)
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
        gate = _make_permission_gate(app)
        with caplog.at_level(logging.WARNING):
            decision = gate("shell.exec", {"cmd": "rm -rf /tmp/x"})

        assert decision == "deny"
        message = getattr(decision, "deny_message", "")
        assert "timed out" in message and "shell.exec" in message

        (row,) = app.state.permissions.values()
        assert row["status"] == "timeout"
        assert row["action"] == "deny"
        assert row["reason"] == permission_timeout.REASON_PERMISSION_TIMEOUT
        assert row["id"] not in app.state.permission_events

        resolved = [
            e
            for e in app.state.bus.session_events_since(sid)
            if e.type == "permission.resolved" and e.payload.get("permission_id") == row["id"]
        ]
        assert resolved, "no permission.resolved event for the expired request"
        assert resolved[0].payload["reason"] == permission_timeout.REASON_PERMISSION_TIMEOUT
        assert resolved[0].payload["action"] == "deny"

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
