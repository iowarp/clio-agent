"""A highway event's ``arc.op`` names it; a context segment's ``arc.op`` copies it whole.

A ``semantic_event`` segment holds a semantic event the trace already stores as its own
event, and clio-core stores the segment (the highway's source of truth). Copying it into
the ``arc.op`` too made the copies 21 of 31.6 MB of one session's trace. Context
segments stay full dicts, because replay rebuilds the live context from them alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.segments import SegmentStore
from tests.test_arc.test_segment_store import _clio_core

SID = "s1"


def _logging_store(tmp_path: Path) -> tuple[SegmentStore, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    def log(op: str, session_id: str, scope: str, **payload: Any) -> dict[str, Any]:
        event = {
            "event_id": f"evt_{len(events)}",
            "event_type": "arc.op",
            "session_id": session_id,
            "payload": {"op": op, "scope": scope, **payload},
        }
        events.append(event)
        return event

    return SegmentStore(_clio_core(str(tmp_path)), op_logger=log), events


def test_an_event_append_logs_a_reference_not_the_event(tmp_path: Path) -> None:
    """SABOTAGE: log ``msgspec.to_builtins`` for every kind and the body appears."""
    store, events = _logging_store(tmp_path)
    body = {"event_id": "sev_1", "payload": {"record": "x" * 2000}}
    seg = store.append(SID, "_events", "semantic_event", body)

    [written] = events[-1]["payload"]["segments_written"]
    assert written == {
        "id": seg.id,
        "kind": "semantic_event",
        "scope": "_events",
        "logical_time": seg.logical_time,
        "event_id": "sev_1",
        "stored_in": "clio-core",
    }
    # clio-core still holds the whole event.
    assert store.render(SID, "_events")[0].content == body


def test_context_segments_stay_full_and_replay_skips_references(tmp_path: Path) -> None:
    store, events = _logging_store(tmp_path)
    store.append(SID, "_events", "semantic_event", {"event_id": "sev_1"})
    user = store.append(SID, "agentA", "user", {"text": "q"})

    [written] = events[-1]["payload"]["segments_written"]
    assert written["content"] == {"text": "q"}
    replayed = reconstruct_arc_segments(events)
    assert [s.id for s in replayed] == [user.id]
