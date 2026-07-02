"""Unit tests for the ARC live-context SegmentStore.

Covers the four ops (append/insert/delete/summarize), the dspy-shaped render,
as-of-T reads, persistence/reload, scope isolation, op-logging, and token
attribution. The byte-equality / mutation-propagation acceptance tests against a
real dspy render live in test_live_plane_byte_equality.py.
"""

from __future__ import annotations

import msgspec
import pytest

from clio_agent.arc.schema import Segment, decode_segment, decode_segments, encode_segments
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import ARC_KINDS, LocalFSStore

SID = "sess-1"
SCOPE = "agentA/expertB"


def _store(tmp_path):
    """A SegmentStore over a fresh LocalFSStore, recording every logged op."""
    logged: list[dict] = []

    def op_logger(op, session_id, scope, **kw):
        ev = {"event_id": f"ev{len(logged) + 1}", "op": op, "scope": scope, **kw}
        logged.append(ev)
        return ev

    return SegmentStore(LocalFSStore(str(tmp_path)), op_logger=op_logger), logged


def _iteration(ss, sid, scope, step, thought, tool, args, obs):
    """Write one full ReAct iteration's worth of segments."""
    ss.append(sid, scope, "thought", {"text": thought}, step=step)
    ss.append(sid, scope, "tool_call", {"name": tool, "args": args}, step=step)
    ss.append(sid, scope, "observation", {"text": obs}, step=step)


def test_segments_kind_in_arc_kinds():
    assert "segments" in ARC_KINDS


def test_append_renders_gapless_dspy_dict(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "think0", "grep", {"q": "x"}, "obs0")
    _iteration(ss, SID, SCOPE, 1, "think1", "finish", {}, "done")
    keys = ss.render_keys(SID, SCOPE)
    assert list(keys.keys()) == [
        "thought_0",
        "tool_name_0",
        "tool_args_0",
        "observation_0",
        "thought_1",
        "tool_name_1",
        "tool_args_1",
        "observation_1",
    ]
    assert keys["tool_args_0"] == {"q": "x"}  # dict preserved, not re-parsed


def test_append_orders_monotonically(tmp_path):
    ss, _ = _store(tmp_path)
    a = ss.append(SID, SCOPE, "thought", {"text": "a"})
    b = ss.append(SID, SCOPE, "thought", {"text": "b"})
    assert b.order > a.order
    assert b.logical_time > a.logical_time


def test_delete_tombstones_absent_from_render_present_in_store(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "think0", "grep", {}, "obs0")
    o0 = [s for s in ss.list_segments(SID, SCOPE) if s.kind == "observation"][0]
    assert ss.delete(SID, SCOPE, [o0.id]) == 1
    assert "obs0" not in str(ss.render_keys(SID, SCOPE))  # gone from the prompt
    tombstoned = ss.list_segments(SID, SCOPE, include_tombstoned=True)
    assert any(s.id == o0.id and s.status == "tombstoned" for s in tombstoned)  # kept for replay


def test_delete_renumbers_gaplessly(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "t0", "a", {}, "o0")
    _iteration(ss, SID, SCOPE, 1, "t1", "b", {}, "o1")
    # delete the whole first iteration
    first = [s for s in ss.list_segments(SID, SCOPE) if s.step == 0]
    ss.delete(SID, SCOPE, [s.id for s in first])
    keys = ss.render_keys(SID, SCOPE)
    # surviving iteration renumbers to idx 0 — no gap
    assert list(keys.keys()) == ["thought_0", "tool_name_0", "tool_args_0", "observation_0"]
    assert keys["thought_0"] == "t1"


def test_summarize_replaces_range_with_summary(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "keep", "a", {}, "keepobs")
    _iteration(ss, SID, SCOPE, 1, "drop", "b", {}, "dropobs")
    drop = [s for s in ss.list_segments(SID, SCOPE) if s.step == 1]
    summary = ss.summarize(SID, SCOPE, [s.id for s in drop], {"text": "SUMMARY"})
    rendered = str(ss.render_keys(SID, SCOPE))
    assert "SUMMARY" in rendered
    assert "drop" not in rendered and "dropobs" not in rendered  # originals gone from prompt
    assert "keep" in rendered  # untouched range stays
    assert set(summary.derived_from) == {s.id for s in drop}  # provenance recorded


def test_summarize_all_is_context_compaction(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "t0", "a", {}, "o0")
    _iteration(ss, SID, SCOPE, 1, "t1", "b", {}, "o1")
    live_ids = [s.id for s in ss.render(SID, SCOPE)]
    ss.summarize(SID, SCOPE, live_ids, {"text": "EVERYTHING"})
    assert ss.render_keys(SID, SCOPE) == {"observation_0": "EVERYTHING"}


def test_insert_at_position(tmp_path):
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "first"})
    ss.append(SID, SCOPE, "thought", {"text": "third"})
    ss.insert(SID, SCOPE, 1, "thought", {"text": "second"})
    texts = list(ss.render_keys(SID, SCOPE).values())
    assert texts == ["first", "second", "third"]


def test_insert_does_not_renumber_order(tmp_path):
    ss, _ = _store(tmp_path)
    a = ss.append(SID, SCOPE, "thought", {"text": "a"})
    c = ss.append(SID, SCOPE, "thought", {"text": "c"})
    b = ss.insert(SID, SCOPE, 1, "thought", {"text": "b"})
    assert a.order < b.order < c.order  # gap allocation, neighbours untouched


def test_as_of_returns_pre_edit_view(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "t0", "a", {}, "o0")
    snapshot = max(s.logical_time for s in ss.list_segments(SID, SCOPE))
    obs = [s for s in ss.list_segments(SID, SCOPE) if s.kind == "observation"][0]
    ss.delete(SID, SCOPE, [obs.id])
    assert "o0" not in str(ss.render_keys(SID, SCOPE))  # current: gone
    assert "o0" in str(ss.render_keys(SID, SCOPE, as_of=snapshot))  # as-of-T: present


def test_persistence_reload(tmp_path):
    ss, _ = _store(tmp_path)
    _iteration(ss, SID, SCOPE, 0, "t0", "a", {"k": 1}, "o0")
    before = ss.render_keys(SID, SCOPE)
    reloaded = SegmentStore(LocalFSStore(str(tmp_path)))  # cold store, same backend dir
    assert reloaded.render_keys(SID, SCOPE) == before


def test_logical_time_recovered_after_reload(tmp_path):
    ss, _ = _store(tmp_path)
    last = ss.append(SID, SCOPE, "thought", {"text": "x"})
    reloaded = SegmentStore(LocalFSStore(str(tmp_path)))
    nxt = reloaded.append(SID, SCOPE, "thought", {"text": "y"})
    assert nxt.logical_time > last.logical_time  # clock not reset to 1


def test_scope_isolation(tmp_path):
    ss, _ = _store(tmp_path)
    ss.append(SID, "agentA/x", "thought", {"text": "in-x"})
    ss.append(SID, "agentA/y", "thought", {"text": "in-y"})
    assert "in-y" not in str(ss.render_keys(SID, "agentA/x"))
    assert sorted(ss.scan_scopes(SID)) == ["agentA/x", "agentA/y"]
    assert ss.scan_scopes(SID, "agentA/") == ["agentA/x", "agentA/y"]
    ss.append("other-sess", "agentA/x", "thought", {"text": "other"})
    assert ss.scan_scopes(SID) == ["agentA/x", "agentA/y"]  # session-scoped


def test_op_logging_and_trace_ref(tmp_path):
    ss, logged = _store(tmp_path)
    seg = ss.append(SID, SCOPE, "thought", {"text": "x"})
    assert len(logged) == 1
    assert logged[0]["op"] == "append"
    assert seg.trace_ref == logged[0]["event_id"]  # back-link stamped from the op event


def test_tokens_by_kind(tmp_path):
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "t"}, token_count=10)
    ss.append(SID, SCOPE, "tool_call", {"name": "a", "args": {}}, token_count=5)
    ss.append(SID, SCOPE, "observation", {"text": "o"}, token_count=20)
    assert ss.tokens_by_kind(SID, SCOPE) == {"thought": 10, "tool_call": 5, "observation": 20}


def test_apply_dispatch_and_unknown_op(tmp_path):
    ss, _ = _store(tmp_path)
    ss.apply("append", SID, SCOPE, kind="thought", content={"text": "via-apply"})
    assert "via-apply" in str(ss.render_keys(SID, SCOPE))
    with pytest.raises(ValueError, match="unknown segment op"):
        ss.apply("frobnicate", SID, SCOPE)


def test_release_drops_memory_keeps_store(tmp_path):
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "persisted"})
    assert ss.release(SID) == 1
    # reload from the same backend: data survived the in-memory release
    assert "persisted" in str(ss.render_keys(SID, SCOPE))


# ---- schema extension (additive, msgspec back-compatible) ------------------


def test_new_kinds_round_trip():
    """The richer ARC-as-source kinds (answer / semantic_event) encode and
    decode through the locked Segment schema."""
    for kind in ("answer", "semantic_event"):
        seg = Segment(
            scope="agentA/exp",
            kind=kind,
            content={"text": f"{kind}-payload"},
            session_id=SID,
            step=0,
            order=1.0,
            logical_time=1,
        )
        back = decode_segments(encode_segments([seg]))[0]
        assert back.kind == kind
        assert back.content == {"text": f"{kind}-payload"}


def test_new_span_fields_round_trip_and_default():
    """turn_id / expert_span_id / run_span_id round-trip when set, default to ''."""
    seg = Segment(
        scope="agentA/exp",
        kind="thought",
        content={"text": "x"},
        session_id=SID,
        step=0,
        order=1.0,
        logical_time=1,
        turn_id="turn-1",
        expert_span_id="espan-1",
        run_span_id="run-1",
    )
    back = decode_segments(encode_segments([seg]))[0]
    assert (back.turn_id, back.expert_span_id, back.run_span_id) == (
        "turn-1",
        "espan-1",
        "run-1",
    )
    # An unstamped segment defaults the span ids to "".
    bare = Segment(
        scope="s", kind="thought", content={}, session_id=SID, step=0, order=1.0, logical_time=2
    )
    assert (bare.turn_id, bare.expert_span_id, bare.run_span_id) == ("", "", "")


def test_old_record_decodes_under_extended_schema():
    """A record encoded WITHOUT the new fields (the pre-extension on-disk shape) must
    still decode under the extended Segment — back-compat is the whole point of the
    additive, default-bearing extension. We simulate an old record by encoding a
    msgspec.Struct that lacks the new fields, then decoding it as a Segment."""

    class _OldSegment(msgspec.Struct):
        scope: str
        kind: str
        content: dict
        session_id: str
        step: int
        order: float
        logical_time: int
        id: str = "old-id"
        token_count: int = 0
        derived_from: list = msgspec.field(default_factory=list)
        status: str = "live"
        tombstoned_at: int = 0
        trace_ref: str = ""
        created_at: float = 123.0

    old = _OldSegment(
        scope="agentA/exp",
        kind="thought",
        content={"text": "legacy"},
        session_id=SID,
        step=0,
        order=1.0,
        logical_time=5,
    )
    raw = msgspec.msgpack.encode(old)
    decoded = decode_segment(raw)
    assert decoded.kind == "thought"
    assert decoded.content == {"text": "legacy"}
    assert decoded.id == "old-id"
    # the new fields fill in with their defaults
    assert (decoded.turn_id, decoded.expert_span_id, decoded.run_span_id) == ("", "", "")


# ---- replace op (tick + tombstone, as-of-T recoverable) --------------------


def test_replace_swaps_content_in_render(tmp_path):
    ss, logged = _store(tmp_path)
    orig = ss.append(SID, SCOPE, "observation", {"text": "before"}, step=0)
    new = ss.replace(SID, SCOPE, orig.id, {"text": "after"})
    assert new is not None
    rendered = str(ss.render_keys(SID, SCOPE))
    assert "after" in rendered and "before" not in rendered
    # replacement renders in the ORIGINAL's slot (same order), 1:1 provenance
    assert new.order == orig.order
    assert new.derived_from == [orig.id]
    assert logged[-1]["op"] == "replace"
    assert logged[-1]["segments_tombstoned"] == [orig.id]


def test_replace_tombstones_original_keeps_for_replay(tmp_path):
    ss, _ = _store(tmp_path)
    orig = ss.append(SID, SCOPE, "thought", {"text": "v1"}, step=0)
    ss.replace(SID, SCOPE, orig.id, {"text": "v2"})
    all_segs = ss.list_segments(SID, SCOPE, include_tombstoned=True)
    tomb = next(s for s in all_segs if s.id == orig.id)
    assert tomb.status == "tombstoned" and tomb.tombstoned_at > 0
    # exactly one live segment now (the replacement)
    assert len(ss.render(SID, SCOPE)) == 1


def test_replace_as_of_recovers_pre_replace_view(tmp_path):
    ss, _ = _store(tmp_path)
    orig = ss.append(SID, SCOPE, "observation", {"text": "ORIGINAL"}, step=0)
    snapshot = max(s.logical_time for s in ss.list_segments(SID, SCOPE))
    ss.replace(SID, SCOPE, orig.id, {"text": "REPLACED"})
    # current view: replaced
    assert "REPLACED" in str(ss.render_keys(SID, SCOPE))
    assert "ORIGINAL" not in str(ss.render_keys(SID, SCOPE))
    # as-of-T (before the replace tick): the original is recoverable
    assert "ORIGINAL" in str(ss.render_keys(SID, SCOPE, as_of=snapshot))
    assert "REPLACED" not in str(ss.render_keys(SID, SCOPE, as_of=snapshot))


def test_replace_can_rekind_the_slot(tmp_path):
    ss, _ = _store(tmp_path)
    orig = ss.append(SID, SCOPE, "thought", {"text": "T"}, step=0)
    new = ss.replace(SID, SCOPE, orig.id, {"text": "now an obs"}, kind="observation")
    assert new is not None and new.kind == "observation"
    # default (no kind) inherits the original's kind
    new2 = ss.replace(SID, SCOPE, new.id, {"text": "still obs"})
    assert new2 is not None and new2.kind == "observation"


def test_replace_no_live_target_is_noop(tmp_path):
    ss, logged = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "x"})
    n_before = len(logged)
    assert ss.replace(SID, SCOPE, "does-not-exist", {"text": "y"}) is None
    assert len(logged) == n_before  # no op logged for a no-op replace


def test_replace_via_apply_dispatch(tmp_path):
    ss, _ = _store(tmp_path)
    orig = ss.append(SID, SCOPE, "thought", {"text": "old"})
    out = ss.apply("replace", SID, SCOPE, target_id=orig.id, content={"text": "new"})
    assert out is not None and out.content == {"text": "new"}
    assert "new" in str(ss.render_keys(SID, SCOPE))


# ---- correlation span ids (turn_id / expert_span_id / run_span_id) ----------


def test_correlation_ids_stamped_on_append_insert_summarize(tmp_path):
    ss, _ = _store(tmp_path)
    a = ss.append(
        SID, SCOPE, "thought", {"text": "t"}, turn_id="T1", expert_span_id="E1", run_span_id="R1"
    )
    assert (a.turn_id, a.expert_span_id, a.run_span_id) == ("T1", "E1", "R1")
    i = ss.insert(SID, SCOPE, 0, "observation", {"text": "o"}, turn_id="T1", expert_span_id="E1")
    assert (i.turn_id, i.expert_span_id, i.run_span_id) == ("T1", "E1", "")
    summ = ss.summarize(SID, SCOPE, [a.id, i.id], {"text": "s"}, turn_id="T1", expert_span_id="E1")
    assert (summ.turn_id, summ.expert_span_id) == ("T1", "E1")


def test_replace_inherits_correlation_ids_by_default(tmp_path):
    ss, _ = _store(tmp_path)
    orig = ss.append(
        SID, SCOPE, "thought", {"text": "v1"}, turn_id="T1", expert_span_id="E1", run_span_id="R1"
    )
    repl = ss.replace(SID, SCOPE, orig.id, {"text": "v2"})  # no override
    assert repl is not None
    assert (repl.turn_id, repl.expert_span_id, repl.run_span_id) == ("T1", "E1", "R1")
    # explicit override wins
    repl2 = ss.replace(SID, SCOPE, repl.id, {"text": "v3"}, turn_id="T2")
    assert repl2 is not None and repl2.turn_id == "T2"


def test_segment_correlation_fields_round_trip():
    """A Segment carrying the three span ids encodes/decodes (msgspec back-compat)."""
    seg = Segment(
        scope="agentA",
        kind="answer",
        content={"content": "hi"},
        session_id=SID,
        step=-1,
        order=1.0,
        logical_time=1,
        turn_id="T1",
        expert_span_id="E1",
        run_span_id="R1",
    )
    decoded = decode_segment(msgspec.msgpack.encode(seg))
    assert (decoded.turn_id, decoded.expert_span_id, decoded.run_span_id) == ("T1", "E1", "R1")
    assert decoded.kind == "answer"
