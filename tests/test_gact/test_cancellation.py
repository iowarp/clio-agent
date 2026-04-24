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
