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

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests.turn_signals import wait_for_terminal_status

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
        cursor = app.state.bus.latest_event_id(sid)
        ack = c.post(
            f"/v1/sessions/{sid}/messages",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
        assert ack.status_code == 202, ack.text
        assert ack.json()["task_handle"]

        assert wait_for_terminal_status(app.state.bus, sid, after_event_id=cursor) == "idle"
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
    assert "workspace.lease" not in phase_names
    assert "agent.resolve" in phase_names

    # Session ownership replaces the removed whole-workspace execution lease.
    # Blueprint resolution is finite preparation at the outer level.
    resolve_row = next(r for r in phase_rows if r["phase"] == "agent.resolve")
    # No removed workspace lease may reappear as an enclosing phase.
    assert resolve_row["depth"] == 0
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


def test_every_turn_is_timed_up_to_its_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every turn brings its agent up again, so every turn gets its own summary,
    numbered per session, with the resolution and the module build as phases.
    SABOTAGE: open turns with ``timer_for_session`` instead of ``begin_turn`` and
    turn 2 gets the null timer, so it has no summary."""

    audits = _capture_audits(monkeypatch)
    agent = FakeClioAgent(answer="first")
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)

    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"title": "bringup2"}).json()["id"]

        def _send_and_wait(text: str) -> None:
            cursor = app.state.bus.latest_event_id(sid)
            ack = c.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": text}]},
            )
            assert ack.status_code == 202, ack.text
            assert ack.json()["task_handle"]
            assert wait_for_terminal_status(app.state.bus, sid, after_event_id=cursor) == "idle"

        _send_and_wait("first turn")
        _send_and_wait("second turn")

    summaries = [
        f for stage, f in audits if stage == "bringup.summary" and f.get("session_id") == sid
    ]
    assert [s["turn"] for s in summaries] == [1, 2]
    first, second = ({p["name"] for p in s["phases"]} for s in summaries)
    assert "session.create" in first and "session.create" not in second
    assert {"turn.accept_gap", "agent.resolve", "agent.resolution", "agent.build"} <= second
    assert all(s["unclosed_phase_names"] == [] for s in summaries)
    phase_turns = {
        f["turn"] for stage, f in audits if stage == "bringup.phase" and f.get("session_id") == sid
    }
    assert phase_turns == {1, 2}


def test_a_turn_that_never_settled_is_closed_by_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed turn leaves its timer open; the next turn settles it, naming the
    open phase, and times itself separately. SABOTAGE: drop the stale-timer
    branch in ``begin_turn`` and turn 2 nests inside turn 1's window."""
    from clio_agent.gact.runtime.bringup_timing import BringupTimerRegistry

    audits = _capture_audits(monkeypatch)
    registry = BringupTimerRegistry()
    registry.begin_turn("s").start_phase("agent.resolve")
    second = registry.begin_turn("s")
    assert second.turn == 2
    summaries = [f for stage, f in audits if stage == "bringup.summary"]
    assert [(s["turn"], s["unclosed_phase_names"]) for s in summaries] == [(1, ["agent.resolve"])]
    second.start_phase("agent.build")
    second.end_phase("agent.build")
    assert registry.finish("s").turn == 2
