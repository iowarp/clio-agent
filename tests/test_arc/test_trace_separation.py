"""Acceptance: ARC/Trace separation (GOAL.md "Definition of done" #4).

Every applied context op is logged to the durable Trace as a self-describing
``arc.op`` event; the live segment set is reconstructable from those events alone
(``arc.replay``); and after a compaction the Trace still holds the originals plus
the summary's provenance. The capturing op_logger here mirrors the event shape
``gact.app._emit_arc_op`` produces.
"""

from __future__ import annotations

from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.schema import segment_text
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import LocalFSStore

SID, SCOPE = "s1", "agentA/expertB"


def _proj(segs):
    """A precise, id-bearing projection of an ordered segment list (render order)."""
    return [(s.id, s.kind, s.content) for s in segs]


def _text(segs) -> str:
    """Segments flattened exactly as ``SegmentStore.render_text`` does."""
    return "\n".join(segment_text(s) for s in segs)


def _make_logger(events_out: list[dict]):
    """An op_logger that records arc.op events in _emit_arc_op's shape and returns
    each (with a stable event_id) so SegmentStore can stamp trace_ref."""
    counter = {"n": 0}

    def op_logger(
        op,
        session_id,
        scope,
        *,
        logical_time,
        step=None,
        position=None,
        segments_written=None,
        segments_tombstoned=None,
        derived_from=None,
    ):
        counter["n"] += 1
        ev = {
            "event_type": "arc.op",
            "event_id": f"sem_{counter['n']:04d}",
            "session_id": session_id,
            "payload": {
                "op": op,
                "scope": scope,
                "logical_time": logical_time,
                "step": step,
                "position": position,
                "segments_written": segments_written or [],
                "segments_tombstoned": segments_tombstoned or [],
                "derived_from": derived_from or [],
            },
        }
        events_out.append(ev)
        return ev

    return op_logger


def _store(tmp_path):
    events: list[dict] = []
    ss = SegmentStore(LocalFSStore(str(tmp_path)), op_logger=_make_logger(events))
    return ss, events


def _iteration(ss, step, thought, tool, obs):
    ss.append(SID, SCOPE, "thought", {"text": thought}, step=step)
    ss.append(SID, SCOPE, "tool_call", {"name": tool, "args": {}}, step=step)
    ss.append(SID, SCOPE, "observation", {"text": obs}, step=step)


def test_every_op_emits_an_event(tmp_path):
    ss, events = _store(tmp_path)
    _iteration(ss, 0, "t0", "a", "o0")
    assert len(events) == 3
    assert [e["payload"]["op"] for e in events] == ["append", "append", "append"]
    seg = ss.append(SID, SCOPE, "thought", {"text": "x"})
    assert events[-1]["payload"]["op"] == "append"
    assert seg.trace_ref == events[-1]["event_id"]  # back-link stamped


def test_replay_reconstructs_the_live_view(tmp_path):
    ss, events = _store(tmp_path)
    _iteration(ss, 0, "keep0", "a", "obs0")
    _iteration(ss, 1, "drop1", "b", "obs1")
    # edit: delete obs0, summarize iteration 1
    obs0 = next(s for s in ss.render(SID, SCOPE) if s.content.get("text") == "obs0")
    ss.delete(SID, SCOPE, [obs0.id])
    iter1 = [s.id for s in ss.render(SID, SCOPE) if s.step == 1]
    ss.summarize(SID, SCOPE, iter1, {"text": "SUMMARY1"})

    live = ss.render(SID, SCOPE)
    replayed = reconstruct_arc_segments(events)
    # The replayed render is byte-identical to the live render.
    assert _proj(replayed) == _proj(live)
    assert "SUMMARY1" in _text(replayed)
    assert "obs0" not in _text(replayed)


def test_replay_trace_ref_matches_live(tmp_path):
    ss, events = _store(tmp_path)
    _iteration(ss, 0, "t0", "a", "o0")
    live = {s.id: s.trace_ref for s in ss.render(SID, SCOPE)}
    replayed = {s.id: s.trace_ref for s in reconstruct_arc_segments(events)}
    assert replayed == live  # reconstructed trace_ref == live trace_ref (audit-grade)


def test_trace_retains_originals_after_compaction(tmp_path):
    ss, events = _store(tmp_path)
    _iteration(ss, 0, "ORIG_T", "a", "ORIG_O")
    live_ids = [s.id for s in ss.render(SID, SCOPE)]
    ss.summarize(SID, SCOPE, live_ids, {"text": "COMPACTED"})
    # Live view is just the summary...
    assert [(s.kind, s.content) for s in ss.render(SID, SCOPE)] == [
        ("summary", {"text": "COMPACTED"})
    ]
    # ...but the Trace still carries the originals (replay before the summarize lt)
    summarize_ev = next(e for e in events if e["payload"]["op"] == "summarize")
    before_lt = summarize_ev["payload"]["logical_time"] - 1
    pre = reconstruct_arc_segments(events, as_of_logical_time=before_lt)
    assert "ORIG_T" in _text(pre) and "ORIG_O" in _text(pre)
    # And the summary records provenance.
    assert set(summarize_ev["payload"]["derived_from"]) == set(live_ids)


def test_replay_scope_filter(tmp_path):
    ss, events = _store(tmp_path)
    ss.append(SID, "agentA/x", "thought", {"text": "in-x"})
    ss.append(SID, "agentB/y", "thought", {"text": "in-y"})
    agent_a = reconstruct_arc_segments(events, scope_filter="agentA/")
    assert "in-x" in _text(agent_a)
    assert "in-y" not in _text(agent_a)
