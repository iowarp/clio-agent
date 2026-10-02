"""Releasing a session drops only its in-memory copy; clio-core keeps everything.

The reserved ``_events`` family holds the session's semantic event log and the
transcript atoms the UI reloads from. Release used to ERASE that family from clio-core
whenever a durable trace was configured -- the default -- trusting the trace to hold
the history. clio-core
is the one context store: release never erases it, whatever the trace backend.
"""

from __future__ import annotations

from pathlib import Path

from clio_agent.arc.memory import EVENTS_SCOPE, ARCMemory
from clio_agent.gact.semantic_events import SemanticEvent


def _event(sid: str) -> SemanticEvent:
    return SemanticEvent(
        event_type="turn.started",
        session_id=sid,
        trace_id="trace_t1",
        turn_id="t1",
        occurred_at="2026-09-30T00:00:00+00:00",
        payload={"input": "the needle is 42"},
    )


def _log(arc: ARCMemory, sid: str) -> list[str]:
    return [s.content["payload"]["input"] for s in arc.render_segments(sid, EVENTS_SCOPE)]


def test_release_keeps_the_session_event_log_in_clio_core(tmp_path: Path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.record_semantic_event(_event("s1"))
    assert _log(arc, "s1") == ["the needle is 42"]

    arc.release_session("s1")

    assert _log(ARCMemory(data_dir=str(tmp_path / "arc")), "s1") == ["the needle is 42"]


def test_flush_and_release_keeps_every_session_event_log(tmp_path: Path) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.record_semantic_event(_event("s1"))

    arc.flush_and_release()

    assert _log(ARCMemory(data_dir=str(tmp_path / "arc")), "s1") == ["the needle is 42"]
