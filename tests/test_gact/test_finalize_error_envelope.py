"""#756: an exception in the turn finalize region must settle the turn.

Everything after ``_run_turn_in_background``'s forward except-chain (answer
grounding, part assembly, diff indexing, publishes, persistence) runs inside a
fire-and-forget task. Before the fix, an exception there died silently: no
``message.completed``, no ``session.status_changed``, session wedged in
``running`` forever. The finalize region is now wrapped in the turn's error
envelope, so an injected finalize exception must yield a visible error turn
and a terminal session status.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to the
# ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")

from .test_post_messages import FakeClioAgent


def test_finalize_exception_settles_turn_with_error_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalize-only helper raising must not wedge the session in running."""

    def _boom(app: Any, sid: str, error_info: Any) -> Any:
        raise RuntimeError("simulated finalize failure")

    # _enrich_cancellation_error_info runs unconditionally in the finalize
    # region (after the forward except-chain), so raising here simulates any
    # finalize crash: grounding, Part construction, Pydantic validation, ...
    monkeypatch.setattr("clio_agent.gact.app._enrich_cancellation_error_info", _boom)

    app = build_app(sessions_path=tmp_path / "s.json", agent=FakeClioAgent(answer="ok"))
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text
        user_id = ack.json()["message_id"]

        # Poll until the session leaves 'running' (or time out — the pre-fix
        # symptom: the background task dies silently and the status never
        # changes, so this loop exhausts and the assert below reports it).
        deadline = time.monotonic() + 5.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)

        history = app.state.bus._history.get(sid, [])
        completed = [ev for ev in history if ev.type == "message.completed"]

        assert status == "error", (
            f"finalize exception must settle the session to a terminal status; "
            f"session stayed {status!r} with "
            f"{len(completed)} message.completed event(s)"
        )

        # The failure is a visible error turn on the bus...
        assert completed, "finalize exception must still publish message.completed"
        payload = completed[-1].payload
        assert payload["turn_id"] == user_id
        assert payload["stop_reason"] == "error"
        assert payload["error_info"]["error"] == "finalize_error"
        assert "simulated finalize failure" in payload["error_info"]["message"]
        assert payload["error_info"]["details"]["reason"] == "turn_finalize_error"

        status_events = [ev for ev in history if ev.type == "session.status_changed"]
        assert status_events and status_events[-1].payload["status"] == "error"

        # ...and in the persisted transcript.
        msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        error_turns = [m for m in msgs if m["role"] == "assistant" and m.get("turn_id") == user_id]
        assert error_turns, "error turn must land in the persisted transcript"
        assert error_turns[0]["stop_reason"] == "error"
        assert error_turns[0]["error_info"]["error"] == "finalize_error"
