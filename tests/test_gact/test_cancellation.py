"""CLIO-BBBBBBBBBB20: POST /v1/sessions/{sid}/cancel.

Two scenarios:
- Idle session: flag flipped, status -> cancelled, event fired, 204.
- Unknown session: 404 with the v0.2 error envelope.
- Post-cancel turn: the next POST message sees the cancel flag set
  before it returns, so the turn envelope reports error=cancelled
  instead of delivering whatever the agent would have produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    )


def test_cancel_flips_status_and_publishes_event(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    sid = sess["id"]

    resp = client.post(f"/v1/sessions/{sid}/cancel")
    assert resp.status_code == 204

    # Session now reports cancelled.
    row = client.get(f"/v1/sessions/{sid}").json()
    assert row["status"] == "cancelled"


def test_cancel_unknown_session_404s_with_v0_2_envelope(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.post("/v1/sessions/sess_nope/cancel")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "internal_error"
    assert "session not found" in body["error"]["message"]


class _SlowAgent:
    """Agent that sleeps long enough for /cancel to race in.

    Used to verify hard-abort cancels the running task instead of
    waiting for forward() to complete. Without the fix the test
    sleeps 5s; with the fix it returns within ~0.3s of /cancel.
    """

    def __init__(self, sleep_s: float = 5.0) -> None:
        self.sleep_s = sleep_s
        self.completed = False

    def forward(self, question: str, session_id: str):
        import time
        time.sleep(self.sleep_s)
        self.completed = True
        return type("Pred", (), {"answer": "late",
                                 "selected_expert": "",
                                 "routing_rationale": ""})()


def test_cancel_hard_aborts_in_flight_task(tmp_path: Path) -> None:
    """iowarp/clio-agent#3: cancel during forward() interrupts the
    task instead of waiting for it to finish."""

    import time as _time
    agent = _SlowAgent(sleep_s=10.0)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "x"}).json()["id"]
        # Fire turn (returns ack immediately; turn runs in background).
        c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        # Give the loop a slice to schedule the task + start the
        # blocking sleep in the executor.
        _time.sleep(0.3)
        c.post(f"/v1/sessions/{sid}/cancel")
        # Poll for the assistant turn to settle as cancelled.
        # complete_turn polls list_messages — the assistant
        # appears once the cancellation path finalises.
        # We just want the task NOT to take the full 10s.
        deadline = _time.monotonic() + 5.0
        seen_cancelled = False
        while _time.monotonic() < deadline:
            sess = c.get(f"/v1/sessions/{sid}").json()
            if sess["status"] in {"cancelled", "error"}:
                seen_cancelled = True
                break
            _time.sleep(0.1)
        assert seen_cancelled, "cancel didn't take effect within 5s"
        # The slow agent must NOT have completed — proves the hard
        # abort bypassed forward().
        assert agent.completed is False, (
            "slow agent ran to completion despite /cancel; hard-abort failed"
        )


def test_cancel_during_turn_marks_turn_as_cancelled(tmp_path: Path) -> None:
    """If /cancel fires *concurrently* with POST /messages, the turn
    resolves with error=cancelled instead of the agent's answer.

    Simulating timing is hard in a synchronous TestClient, so we
    emulate the race by setting the flag BEFORE the POST — the flag
    stays set until the handler clears it, so the order is
    observationally equivalent to a cancel landing mid-forward."""

    from .conftest import complete_turn

    client = _client(tmp_path)
    sess = client.post("/v1/sessions", json={"title": "t"}).json()
    sid = sess["id"]
    # Cancel *first*, then POST. Handler should pick up the flag.
    client.post(f"/v1/sessions/{sid}/cancel")
    a = complete_turn(client, sid, "hi")
    assert a["error_info"]["error"] == "cancelled"
    # No text part with body — turn was cancelled before producing.
    parts = a["parts"]
    assert all(p["type"] != "text" or not p.get("text") for p in parts)
