"""S5 seam-wiring test (iowarp/clio-agent#1215): drives a REAL first turn
end-to-end through a fake host agent (no real LM) and asserts the wired
bring-up phases actually fire on the stream_audit sink, with sane percentages.

Wired this slice: session.create (routes/sessions.py), turn.accept_gap
(turn.py), enrichment (gact/enrichment.py's enrich_turn_context), and
workspace.lease/blueprint.resolve (turn_forward.py — blueprint.resolve
NESTS inside workspace.lease, see bringup_timing.py's SEAM WIRING docstring
for why that pair specifically cannot be flat). fleet.mount is deliberately
NOT wired this slice (a genuine architecture-layering blocker: the
SyncMCPToolExecutor cache in agent.py is keyed by workspace root, not
session, and tools/execution.py cannot import gact.context without
violating the tools/<->gact layering — see the coordinator report for the
full reasoning). #1215 stays open; this is not a cold/warm live capture
(that needs a running stack, a later step).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

from .test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _capture_audits(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    audits: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.gact.runtime.bringup_timing.stream_audit",
        lambda stage, **fields: audits.append((stage, fields)),
    )
    return audits


def _drive_one_turn(app: Any, tmp_path: Path) -> str:
    """POST a session + one message through a real TestClient and wait for
    the turn to settle. Returns the session id."""

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "bringup"}).json()["id"]
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 200, ack.text

        deadline = time.monotonic() + 10.0
        status = "running"
        while time.monotonic() < deadline:
            status = c.get(f"/v1/sessions/{sid}").json()["status"]
            if status != "running":
                break
            time.sleep(0.05)
        assert status != "running", "turn never settled"
    return sid


def test_first_turn_bringup_phases_reach_stream_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audits = _capture_audits(monkeypatch)
    agent = FakeClioAgent(answer="bring-up probe")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    sid = _drive_one_turn(app, tmp_path)

    phase_rows = [
        f for stage, f in audits if stage == "bringup.phase" and f.get("session_id") == sid
    ]
    summary_rows = [
        f for stage, f in audits if stage == "bringup.summary" and f.get("session_id") == sid
    ]
    phase_names = [r["phase"] for r in phase_rows]

    # Sabotage: comment out any ONE seam's start_phase/end_phase pair -> its name
    # is missing from phase_names -> the matching assertion below goes red.
    assert "session.create" in phase_names
    assert "turn.accept_gap" in phase_names
    assert "enrichment" in phase_names
    assert "workspace.lease" in phase_names
    assert "blueprint.resolve" in phase_names

    # workspace.lease is the depth-0 OUTER phase; blueprint.resolve nests inside
    # it (depth 1) -- the deliberate nesting the module docstring documents.
    lease_row = next(r for r in phase_rows if r["phase"] == "workspace.lease")
    resolve_row = next(r for r in phase_rows if r["phase"] == "blueprint.resolve")
    # Sabotage: flatten blueprint.resolve to depth 0 (start it OUTSIDE
    # workspace.lease's phase() block) -> this assertion goes red AND the
    # attribution contract below would double-count wall time.
    assert lease_row["depth"] == 0
    assert resolve_row["depth"] == 1
    assert lease_row["forced_close"] is False
    assert resolve_row["forced_close"] is False

    # Exactly one settled bringup.summary for this session (finish_bringup is
    # called once, right after forward_turn() returns in turn.py).
    assert len(summary_rows) == 1
    summary = summary_rows[0]
    assert summary["session_id"] == sid

    # Sane flat percentages -- the #891 attribution contract holds and the
    # D3 over-attribution guard never tripped on this ordinary, successful turn.
    assert 0.0 <= summary["attributed_pct"] <= 100.0
    assert 0.0 <= summary["unattributed_pct"] <= 100.0
    assert summary["attributed_pct"] + summary["unattributed_pct"] == pytest.approx(100.0, abs=0.5)
    assert summary["overattributed_ms"] == 0.0
    assert summary["unclosed_phase_names"] == []


def test_second_turn_does_not_reopen_bringup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bring-up is a first-turn-only concept: a SECOND turn on the same
    session must not emit any new bringup.phase/bringup.summary rows."""

    audits = _capture_audits(monkeypatch)
    agent = FakeClioAgent(answer="first")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "bringup2"}).json()["id"]

        def _send_and_wait(text: str) -> None:
            ack = c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": text}]},
            )
            assert ack.status_code == 200, ack.text
            deadline = time.monotonic() + 10.0
            status = "running"
            while time.monotonic() < deadline:
                status = c.get(f"/v1/sessions/{sid}").json()["status"]
                if status != "running":
                    break
                time.sleep(0.05)
            assert status != "running", "turn never settled"

        _send_and_wait("first turn")
        first_turn_count = len(
            [f for stage, f in audits if stage == "bringup.summary" and f.get("session_id") == sid]
        )
        assert first_turn_count == 1

        _send_and_wait("second turn")

    summaries_after_second = [
        f for stage, f in audits if stage == "bringup.summary" and f.get("session_id") == sid
    ]
    # Sabotage: drop the _settled gate in BringupTimerRegistry (or key it by
    # something other than "already finished once") -> turn 2 starts a fresh
    # timer and finishes it too -> len becomes 2 -> this assertion goes red.
    assert len(summaries_after_second) == 1
