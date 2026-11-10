"""Highway appends log no ``arc.op``; context appends log the full segment.

``arc.op`` replays the live context. A highway lane (the ``_events`` chunks, the task
lanes, message parts) is not context: clio-core holds it as the source of truth and
the trace holds each semantic event, so its ``arc.op`` was a further copy, about 11
trace events per MCP call (P7, 2026-11-02). Older traces may still carry reference
entries for highway events; replay skips them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.replay import logs_context_op, reconstruct_arc_segments
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


def test_a_highway_append_logs_no_arc_op(tmp_path: Path) -> None:
    """SABOTAGE: drop ``logs_context_op`` from ``_finish_write`` and the event is logged."""
    store, events = _logging_store(tmp_path)
    body = {"event_id": "sev_1", "payload": {"record": "x" * 2000}}
    store.append(SID, "_events", "semantic_event", body)
    assert events == []
    assert store.render(SID, "_events")[0].content == body  # clio-core holds it


def test_a_context_append_is_logged_whole_and_replays(tmp_path: Path) -> None:
    store, events = _logging_store(tmp_path)
    user = store.append(SID, "agentA", "user", {"text": "q"})
    [written] = events[-1]["payload"]["segments_written"]
    assert written["content"] == {"text": "q"}
    assert [s.id for s in reconstruct_arc_segments(events)] == [user.id]


def test_replay_skips_highway_references_from_older_traces(tmp_path: Path) -> None:
    store, events = _logging_store(tmp_path)
    user = store.append(SID, "agentA", "user", {"text": "q"})
    reference = {"id": "seg_x", "kind": "semantic_event", "stored_in": "clio-core"}
    events.append(
        {
            "event_id": "evt_old",
            "event_type": "arc.op",
            "session_id": SID,
            "payload": {"op": "append", "scope": "_events", "segments_written": [reference]},
        }
    )
    assert [s.id for s in reconstruct_arc_segments(events)] == [user.id]


@pytest.mark.parametrize(
    ("scope", "logged"),
    [
        ("_events", False),
        ("_events/12", False),
        ("_events/t/log", False),
        ("_events/t/content/abc", False),
        ("_events/m/3", False),
        ("_events/w/span_1", True),
        ("agentA", True),
    ],
)
def test_which_scopes_are_context(scope: str, logged: bool) -> None:
    assert logs_context_op(scope) is logged
