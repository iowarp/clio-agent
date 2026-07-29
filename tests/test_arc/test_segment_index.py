"""Parallel-consistency tests for the per-scope :class:`SegmentIndex`.

Phase A of the proper-ARC unification (#714) adds a B-tree-style locator keyed
``(session, scope, logical_time)`` that is built in PARALLEL with the in-memory
scope lists and is NOT yet on the render/op read path. This module asserts the
single property a release depends on for that parallelism: the index LOCATES
exactly the same segment set the scan holds, across the stress corpus, at the head
and after a cold reload — plus that the O(log N) clock-window slice agrees with a
scan-side filter.

Everything runs against the REAL SegmentStore over a REAL LocalFSStore (no mocking
of src). The fuzz driver is deterministic: ``random.Random(seed)`` only.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from clio_agent.arc.segments import SegmentIndex, SegmentStore
from clio_agent.arc.storage import LocalFSStore

SID = "idx-sess"
SCOPES = ["agentA/x", "agentB/y", "agentC/z"]
TRAJECTORY_KINDS = ("thought", "tool_call", "observation", "summary")


def _content_for(kind: str, tag: str) -> dict[str, Any]:
    if kind == "tool_call":
        return {"name": f"tool_{tag}", "args": {"k": tag}}
    return {"text": f"{kind}-{tag}"}


def _fresh(tmp_path, sub: str = "arc") -> SegmentStore:
    return SegmentStore(LocalFSStore(str(tmp_path / sub)))


def _run_fuzz(ss: SegmentStore, seed: int, n_ops: int) -> None:
    """Interleave the full op surface (append/insert/delete/summarize/replace)
    across several scopes, deterministically."""
    rng = random.Random(seed)
    for i in range(n_ops):
        scope = rng.choice(SCOPES)
        live = ss.render(SID, scope)
        choices = ["append", "append", "insert"]
        if len(live) >= 2:
            choices += ["delete", "summarize", "replace"]
        op = rng.choice(choices)
        tag = f"{seed}-{i}"
        if op == "append":
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.append(SID, scope, kind, _content_for(kind, tag), step=i)
        elif op == "insert":
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.insert(SID, scope, rng.randint(0, len(live)), kind, _content_for(kind, tag), step=i)
        elif op == "delete":
            ss.delete(SID, scope, [rng.choice(live).id])
        elif op == "summarize":
            k = rng.randint(1, len(live))
            ss.summarize(SID, scope, [s.id for s in rng.sample(live, k)], {"text": f"S-{tag}"})
        elif op == "replace":
            ss.replace(SID, scope, rng.choice(live).id, {"text": f"R-{tag}"})


# --------------------------------------------------------------------------- #
# 1. index locate set == scan set, across the fuzz corpus, every scope.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", [1, 7, 13, 42, 99, 2024])
def test_index_matches_scan_over_fuzz(tmp_path, seed):
    ss = _fresh(tmp_path, sub=f"f{seed}")
    _run_fuzz(ss, seed, n_ops=90)
    for scope in SCOPES:
        scan_ids = {s.id for s in ss.list_segments(SID, scope, include_tombstoned=True)}
        located = set(ss.locate_segment_ids(SID, scope))
        assert located == scan_ids, f"index/scan divergence seed={seed} scope={scope}"
        assert ss._index_matches_scan(SID, scope)


def test_index_matches_scan_long_sequence(tmp_path):
    ss = _fresh(tmp_path)
    _run_fuzz(ss, seed=777, n_ops=300)
    for scope in SCOPES:
        assert ss._index_matches_scan(SID, scope)


# --------------------------------------------------------------------------- #
# 2. cold reload rebuilds the index from disk to the same set as the scan.
# --------------------------------------------------------------------------- #


def test_cold_reload_index_matches_scan(tmp_path):
    ss = _fresh(tmp_path, sub="cold")
    _run_fuzz(ss, seed=131, n_ops=120)
    # Brand-new store over the SAME backend dir: the index is rebuilt at cold-load.
    cold = SegmentStore(LocalFSStore(str(tmp_path / "cold")))
    for scope in SCOPES:
        scan_ids = {s.id for s in cold.list_segments(SID, scope, include_tombstoned=True)}
        located = set(cold.locate_segment_ids(SID, scope))
        assert located == scan_ids, f"cold-reload index/scan divergence scope={scope}"


# --------------------------------------------------------------------------- #
# 3. located order is by creation logical_time, and the clock-window slice agrees
#    with a scan-side logical_time filter (the O(log N) irange semantics).
# --------------------------------------------------------------------------- #


def test_located_order_is_logical_time(tmp_path):
    ss = _fresh(tmp_path, sub="ord")
    _run_fuzz(ss, seed=55, n_ops=80)
    for scope in SCOPES:
        located = ss.locate_segment_ids(SID, scope)
        by_id = {s.id: s for s in ss.list_segments(SID, scope, include_tombstoned=True)}
        lts = [by_id[i].logical_time for i in located]
        assert lts == sorted(lts), f"located ids not in logical_time order scope={scope}"


def test_clock_window_slice_matches_scan_filter(tmp_path):
    ss = _fresh(tmp_path, sub="win")
    _run_fuzz(ss, seed=314, n_ops=100)
    for scope in SCOPES:
        segs = ss.list_segments(SID, scope, include_tombstoned=True)
        if not segs:
            continue
        lts = sorted(s.logical_time for s in segs)
        lo = lts[len(lts) // 4]
        hi = lts[(3 * len(lts)) // 4]
        scan_window = {s.id for s in segs if lo <= s.logical_time <= hi}
        index_window = set(ss.locate_segment_ids(SID, scope, lt_min=lo, lt_max=hi))
        assert index_window == scan_window, f"clock-window mismatch scope={scope}"


# --------------------------------------------------------------------------- #
# 4. release / clear keep the index consistent with the store.
# --------------------------------------------------------------------------- #


def test_release_drops_session_from_index(tmp_path):
    ss = _fresh(tmp_path, sub="rel")
    _run_fuzz(ss, seed=11, n_ops=40)
    ss.append("other", SCOPES[0], "thought", {"text": "keep"})
    ss.release(SID)
    # released session located via a cold path again still matches scan (no stale ids)
    for scope in SCOPES:
        assert ss._index_matches_scan(SID, scope)
    # the OTHER session is untouched
    assert ss.locate_segment_ids("other", SCOPES[0])


def test_clear_empties_index(tmp_path):
    ss = _fresh(tmp_path, sub="clr")
    _run_fuzz(ss, seed=9, n_ops=30)
    ss.clear()
    # after clear, an in-memory locate (no cold-load forced) is empty; a forced
    # cold-load via locate_segment_ids rebuilds from disk and still matches scan.
    assert ss._index._by_scope == {}
    for scope in SCOPES:
        assert ss._index_matches_scan(SID, scope)  # rebuilt from disk, consistent


# --------------------------------------------------------------------------- #
# 5. the SegmentIndex unit, in isolation.
# --------------------------------------------------------------------------- #


def test_segment_index_unit_irange():
    from clio_agent.arc.schema import Segment

    idx = SegmentIndex()
    for lt in (5, 1, 3, 2, 4):
        seg = Segment(
            scope="s", kind="thought", content={}, session_id="u", step=0,
            order=float(lt), logical_time=lt, id=f"id{lt}",
        )
        idx.add("u", "s", seg)
    # full scope, in clock order
    assert idx.locate_ids("u", "s") == ["id1", "id2", "id3", "id4", "id5"]
    # inclusive window
    assert idx.locate_ids("u", "s", lt_min=2, lt_max=4) == ["id2", "id3", "id4"]
    # unknown scope -> empty
    assert idx.locate_ids("u", "nope") == []
    # per-scope drop forgets just that scope's locator. release() drives eviction per
    # known key rather than scanning _by_scope, which raced a concurrent cold-load
    # inserting into the same dict ("dictionary changed size during iteration").
    idx.drop_scope("u", "s")
    assert idx.locate_ids("u", "s") == []
