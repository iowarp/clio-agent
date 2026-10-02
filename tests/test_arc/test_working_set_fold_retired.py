"""The folded working set says which atoms are retired, and when.

``list_segments(include_tombstoned=True)`` is the history view (replay, provenance,
undo). A retired atom there must read as retired -- ``status="tombstoned"`` at the
clock of the op that retired it -- as the plain SegmentStore reports it, or a caller
cannot tell what compaction or a delete removed.
"""

from __future__ import annotations

from typing import Any

from clio_agent.arc.memory import ARCMemory

SID, SCOPE = "s1", "agentA"


def _history(arc: ARCMemory) -> list[tuple[str, str, bool]]:
    return [
        (s.content.get("text", ""), s.status, s.tombstoned_at > 0)
        for s in arc.list_segments(SID, SCOPE, include_tombstoned=True)
    ]


def test_a_summarized_atom_reads_as_retired(tmp_path: Any) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    ids = [arc.append_segment(SID, SCOPE, "thought", {"text": t}, step=0).id for t in "ab"]
    arc.append_segment(SID, SCOPE, "thought", {"text": "c"}, step=0)
    summary = arc.summarize_segments(SID, SCOPE, ids, {"text": "S"})

    assert sorted(_history(arc)) == [
        ("S", "live", False),
        ("a", "tombstoned", True),
        ("b", "tombstoned", True),
        ("c", "live", False),
    ]
    retired = [s for s in arc.list_segments(SID, SCOPE, include_tombstoned=True) if s.id in ids]
    assert {s.tombstoned_at for s in retired} == {summary.logical_time}


def test_a_deleted_atom_reads_as_retired(tmp_path: Any) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    gone = arc.append_segment(SID, SCOPE, "observation", {"text": "x"}, step=0)
    arc.append_segment(SID, SCOPE, "observation", {"text": "y"}, step=0)
    arc.delete_segments(SID, SCOPE, [gone.id])

    assert _history(arc) == [("x", "tombstoned", True), ("y", "live", False)]
    assert [s.content["text"] for s in arc.render_segments(SID, SCOPE)] == ["y"]
