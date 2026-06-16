"""Unit tests for the ARC live-context SegmentStore.

Covers the four ops (append/insert/delete/summarize), the dspy-shaped render,
as-of-T reads, persistence/reload, scope isolation, op-logging, and token
attribution. The byte-equality / mutation-propagation acceptance tests against a
real dspy render live in test_live_plane_byte_equality.py.
"""

from __future__ import annotations

import pytest

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
        "thought_0", "tool_name_0", "tool_args_0", "observation_0",
        "thought_1", "tool_name_1", "tool_args_1", "observation_1",
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
    assert "obs0" not in str(ss.render_keys(SID, SCOPE))           # gone from the prompt
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
    assert "keep" in rendered                                    # untouched range stays
    assert set(summary.derived_from) == {s.id for s in drop}     # provenance recorded


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
    assert "o0" not in str(ss.render_keys(SID, SCOPE))               # current: gone
    assert "o0" in str(ss.render_keys(SID, SCOPE, as_of=snapshot))   # as-of-T: present


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
