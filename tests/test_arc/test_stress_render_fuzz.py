"""Render/mutation edge-case FUZZING for the ARC live-context plane.

This module hammers the live render with SEEDED randomized segment sequences of
arbitrary kinds in any order — including consecutive same-kind runs, lone
observations, lone tool_calls, summaries mid-stream, and interleaved out-of-band
mutations — then asserts the invariants the live plane MUST hold for a release:

    1. CONTENT FIDELITY  — every live segment is in ``render`` exactly once, in
       ``(order, logical_time)`` order, and its content is recoverable from the flat
       text (``render_text``). No mutation may silently drop live content.
    2. AS-OF MONOTONICITY — visibility is monotonic in the as-of clock: a segment
       visible at time T stays visible at every T' >= T until its tombstone, and a
       full-clock as-of read reproduces the live view. Content fidelity holds at
       every historical snapshot too.
    3. REPLAY / RELOAD PARITY — a Trace-replay over the op stream and a cold reload
       both reproduce the live render exactly.

Everything is exercised against the REAL SegmentStore / ARCMemory / replay (no
mocking of src). The fuzz driver is deterministic: ``random.Random(seed)`` only.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.schema import Segment, segment_text
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import ARCStore, make_arc_store

SID = "fuzz-sess"
SCOPE = "agentZ/expertQ"


def _cte_backend() -> ARCStore:
    """The real clio-core store in this test's own namespace (the harness clears it)."""
    return make_arc_store(backend="cte")


# The working-set kinds the agent loop writes (and compaction summarizes). The
# framing kinds (system/user/tool_def) are deliberately NOT generated here.
TRAJECTORY_KINDS = ("thought", "tool_call", "observation", "summary")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _store(tmp_path) -> tuple[SegmentStore, list[dict]]:
    """A SegmentStore over the clio-core store, recording every logged op so the
    Trace-replay invariant can be checked against the exact same op stream."""
    logged: list[dict] = []

    def op_logger(op, session_id, scope, **kw):
        ev = {
            "event_id": f"ev{len(logged) + 1}",
            "event_type": "arc.op",
            "payload": {"op": op, **kw},
        }
        logged.append(ev)
        return ev

    return SegmentStore(_cte_backend(), op_logger=op_logger), logged


def _content_for(kind: str, tag: str) -> dict[str, Any]:
    """A content dict whose payload carries a UNIQUE needle ``tag`` so loss is
    detectable. tool_call args also carry the needle (and a non-string value to
    stress the dict-not-restringified contract)."""
    if kind == "tool_call":
        return {"name": f"tool_{tag}", "args": {"needle": tag, "n": len(tag)}}
    return {"text": f"text_{tag}"}


def _needles(seg: Segment) -> list[str]:
    """The distinguishing strings that MUST survive a render for this segment:
    the tool name + args needle for a tool_call, the ``text`` otherwise."""
    if seg.kind == "tool_call":
        return [
            str(seg.content.get("name", "")),
            str(seg.content.get("args", {}).get("needle", "")),
        ]
    return [str(seg.content.get("text", ""))]


def _proj(segs: list[Segment]) -> list[tuple[str, str, dict[str, Any]]]:
    """A precise projection of an ordered render: ``(id, kind, content)`` per segment."""
    return [(s.id, s.kind, s.content) for s in segs]


def _assert_content_fidelity(ss: SegmentStore, *, as_of: int | None = None) -> None:
    """Every segment live at ``as_of`` is in ``render`` exactly once, in
    ``(order, logical_time)`` order, and its needle(s) survive into ``render_text``
    (which is exactly the ``segment_text`` join of the render — one entry per live
    segment, so no segment's content can be merged into or dropped by another's)."""
    live = ss.render(SID, SCOPE, as_of=as_of)
    text = ss.render_text(SID, SCOPE, as_of=as_of)
    ids = [s.id for s in live]
    assert len(ids) == len(set(ids)), f"duplicate segment in render: {ids}"
    assert live == sorted(live, key=lambda s: (s.order, s.logical_time))
    if as_of is None:
        expected_ids = {s.id for s in ss.list_segments(SID, SCOPE)}
    else:
        expected_ids = {
            s.id
            for s in ss.list_segments(SID, SCOPE, include_tombstoned=True)
            if s.logical_time <= as_of and (s.tombstoned_at == 0 or s.tombstoned_at > as_of)
        }
    assert set(ids) == expected_ids, "render dropped or invented a live segment"
    assert text == "\n".join(segment_text(s) for s in live)
    for seg in live:
        for needle in _needles(seg):
            if needle == "":
                continue
            assert needle in text, (
                f"LOST CONTENT: needle {needle!r} from a live {seg.kind} "
                f"(id={seg.id}) is absent from render_text. text={text!r}"
            )


# --------------------------------------------------------------------------- #
# 1. randomized sequence fuzz: content fidelity + reload/replay parity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_append_only_invariants(tmp_path, seed):
    """SEEDED random kind sequences (any order, consecutive same-kind, lone
    obs/tool, summaries mid-stream) must render in append order with zero content
    loss."""
    rng = random.Random(seed)
    ss, _ = _store(tmp_path)
    n = rng.randint(1, 40)
    for i in range(n):
        kind = rng.choice(TRAJECTORY_KINDS)
        ss.append(SID, SCOPE, kind, _content_for(kind, f"{seed}_{i}"), step=i)

    live = ss.render(SID, SCOPE)
    _assert_content_fidelity(ss)
    assert len(live) == n
    # render is sorted by (order, logical_time) -> append order here
    assert [s.step for s in live] == list(range(n))


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_with_mutations_invariants(tmp_path, seed):
    """As above but interleave the full mutation surface (insert / delete /
    summarize) between appends — the live, edited plane must STILL be lossless,
    and a cold reload + a Trace-replay must reproduce it exactly."""
    rng = random.Random(1000 + seed)
    ss, logged = _store(tmp_path)
    ops = rng.randint(3, 50)

    for i in range(ops):
        choice = rng.random()
        live_ids = [s.id for s in ss.render(SID, SCOPE)]
        if choice < 0.55 or not live_ids:
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.append(SID, SCOPE, kind, _content_for(kind, f"a{seed}_{i}"), step=i)
        elif choice < 0.70:
            pos = rng.randint(0, len(live_ids))
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.insert(SID, SCOPE, pos, kind, _content_for(kind, f"i{seed}_{i}"), step=i)
        elif choice < 0.85:
            k = rng.randint(1, len(live_ids))
            victims = rng.sample(live_ids, k)
            ss.delete(SID, SCOPE, victims)
        else:
            k = rng.randint(1, len(live_ids))
            victims = rng.sample(live_ids, k)
            ss.summarize(SID, SCOPE, victims, {"text": f"sum_{seed}_{i}"}, token_count=1)

    live = ss.render(SID, SCOPE)
    text = ss.render_text(SID, SCOPE)
    _assert_content_fidelity(ss)

    # cold reload reproduces the render byte-for-byte (persistence parity)
    reloaded = SegmentStore(_cte_backend())
    assert _proj(reloaded.render(SID, SCOPE)) == _proj(live)
    assert reloaded.render_text(SID, SCOPE) == text

    # Trace-replay over the exact op stream reproduces the live render
    replayed = reconstruct_arc_segments(logged, scope_filter=SCOPE)
    assert [s.id for s in replayed] == [s.id for s in live], (
        "replay diverged from live render order"
    )
    assert _proj(replayed) == _proj(live)


# --------------------------------------------------------------------------- #
# 2. as-of-T monotonic visibility (fuzzed)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(40))
def test_fuzz_as_of_monotonic_visibility(tmp_path, seed):
    """Visibility is monotone in the as-of clock between a segment's creation and
    its tombstone: once visible it stays visible until tombstoned, and never
    reappears after. A full-clock as-of read equals the live render."""
    rng = random.Random(2000 + seed)
    ss, _ = _store(tmp_path)
    ops = rng.randint(4, 35)

    for i in range(ops):
        live_ids = [s.id for s in ss.render(SID, SCOPE)]
        roll = rng.random()
        if roll < 0.6 or not live_ids:
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.append(SID, SCOPE, kind, _content_for(kind, f"{seed}_{i}"), step=i)
        elif roll < 0.8:
            ss.delete(SID, SCOPE, rng.sample(live_ids, rng.randint(1, len(live_ids))))
        else:
            ss.summarize(
                SID,
                SCOPE,
                rng.sample(live_ids, rng.randint(1, len(live_ids))),
                {"text": f"S{seed}_{i}"},
            )

    all_segs = ss.list_segments(SID, SCOPE, include_tombstoned=True)
    max_lt = max((s.logical_time for s in all_segs), default=0)
    # Tombstone clock can exceed creation clock; cover it too.
    max_lt = max(max_lt, max((s.tombstoned_at for s in all_segs), default=0))

    # Per-segment monotonic visibility window: visible exactly on
    # [logical_time, tombstoned_at) (or [logical_time, inf) if never tombstoned).
    # A never-tombstoned segment (tombstoned_at == 0) is visible for ALL t >= lt.
    INF = max_lt + 2  # one past the last probed clock => "never ends"
    for seg in all_segs:
        end = seg.tombstoned_at if seg.tombstoned_at else INF
        prev_visible = False
        for t in range(seg.logical_time, max_lt + 2):
            visible = seg.id in {s.id for s in ss.render(SID, SCOPE, as_of=t)}
            expected = seg.logical_time <= t < end
            assert visible == expected, (
                f"as-of visibility wrong: seg {seg.id} kind={seg.kind} at T={t}: "
                f"got {visible}, want {expected} (lt={seg.logical_time}, ts={seg.tombstoned_at})"
            )
            # monotone: a transition true->false may happen at most once (at tombstone)
            if prev_visible and not visible:
                assert t >= end, "visibility flickered before tombstone"
            prev_visible = visible

    # A full-clock as-of read reproduces the live (None) render exactly.
    assert _proj(ss.render(SID, SCOPE, as_of=max_lt + 1)) == _proj(ss.render(SID, SCOPE))
    assert ss.render_text(SID, SCOPE, as_of=max_lt + 1) == ss.render_text(SID, SCOPE)
    # and as-of==0 (before any write) is empty
    assert ss.render(SID, SCOPE, as_of=0) == []


def test_as_of_content_fidelity_at_every_snapshot(tmp_path):
    """Content fidelity must hold at EVERY historical snapshot, not only live — an
    as-of view that drops a mid segment must still carry every segment visible at
    that instant, in order, with its content intact."""
    rng = random.Random(31337)
    ss, _ = _store(tmp_path)
    for i in range(30):
        live_ids = [s.id for s in ss.render(SID, SCOPE)]
        if rng.random() < 0.7 or not live_ids:
            kind = rng.choice(TRAJECTORY_KINDS)
            ss.append(SID, SCOPE, kind, _content_for(kind, f"x{i}"), step=i)
        else:
            ss.delete(SID, SCOPE, rng.sample(live_ids, rng.randint(1, len(live_ids))))
    max_lt = max(s.logical_time for s in ss.list_segments(SID, SCOPE, include_tombstoned=True))
    for t in range(0, max_lt + 2):
        _assert_content_fidelity(ss, as_of=t)


# --------------------------------------------------------------------------- #
# 3. ARCMemory pass-through parity + segment_text agreement
# --------------------------------------------------------------------------- #


def test_arcmemory_passthrough_matches_store(tmp_path):
    """ARCMemory's segment pass-throughs must return exactly what the underlying
    SegmentStore does for the same fuzzed sequence."""
    rng = random.Random(99)
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    for i in range(25):
        live_ids = [s.id for s in arc.render_segments(SID, SCOPE)]
        if rng.random() < 0.7 or not live_ids:
            kind = rng.choice(TRAJECTORY_KINDS)
            arc.append_segment(SID, SCOPE, kind, _content_for(kind, f"m{i}"), step=i)
        else:
            arc.delete_segments(SID, SCOPE, rng.sample(live_ids, 1))

    live = arc.render_segments(SID, SCOPE)
    assert _proj(live) == _proj(arc._segments.render(SID, SCOPE))
    _assert_content_fidelity(arc._segments)
    # render_text is the segment_text join of the live render
    assert arc.render_segment_text(SID, SCOPE) == "\n".join(segment_text(s) for s in live)
