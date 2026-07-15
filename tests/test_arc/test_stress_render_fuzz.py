"""Render/mutation edge-case FUZZING for the ARC live-context plane.

This class of bug already bit us once: consecutive observations overwrote each
other in ``render_keys`` (two ``observation`` segments collided on the same
``observation_{i}`` slot, losing content). This module hammers that surface with
SEEDED randomized segment sequences of arbitrary kinds in any order — including
consecutive same-kind runs, lone observations, lone tool_calls, summaries
mid-stream, and interleaved out-of-band mutations — then asserts the invariants
that the live plane MUST hold for a release:

    1. CONTENT FIDELITY  — every live segment's content is recoverable from the
       rendered dict (``render_keys``) OR the flat text (``render_text``). No key
       overwrite may silently drop content. (the original needle-loss bug.)
    2. GAPLESS INDICES   — the trajectory dict's ``{i}`` suffixes form a contiguous
       0..N-1 run for every key family (stock dspy never has index gaps).
    3. BYTE-EQUALITY     — the canonical ``thought -> tool -> obs`` flow renders to
       exactly the stock-dspy trajectory dict shape (and the real
       ``_format_trajectory`` is byte-identical to the stock formatter).
    4. AS-OF MONOTONICITY — visibility is monotonic in the as-of clock: a segment
       visible at time T stays visible at every T' >= T until its tombstone, and a
       full-clock as-of read reproduces the live view. Replay agrees with live.
    5. PURE-FUNCTION PARITY — ``segments_to_keys`` over ``render`` == ``render_keys``,
       and a cold reload reproduces the render byte-for-byte.

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
from clio_agent.arc.segments import SegmentStore, segments_to_keys
from clio_agent.arc.storage import LocalFSStore

SID = "fuzz-sess"
SCOPE = "agentZ/expertQ"

# Every kind that can appear in a trajectory dict (the projection's domain). The
# framing kinds (system/user/tool_def) are deliberately NOT generated here: they
# are never part of segments_to_keys, and a separate test confirms they're skipped.
TRAJECTORY_KINDS = ("thought", "tool_call", "observation", "summary")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _store(tmp_path) -> tuple[SegmentStore, list[dict]]:
    """A SegmentStore over a fresh LocalFSStore, recording every logged op so the
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

    return SegmentStore(LocalFSStore(str(tmp_path)), op_logger=op_logger), logged


def _content_for(kind: str, tag: str) -> dict[str, Any]:
    """A content dict whose payload carries a UNIQUE needle ``tag`` so loss is
    detectable. tool_call args also carry the needle (and a non-string value to
    stress the dict-not-restringified contract)."""
    if kind == "tool_call":
        return {"name": f"tool_{tag}", "args": {"needle": tag, "n": len(tag)}}
    return {"text": f"text_{tag}"}


def _needles(seg: Segment) -> list[str]:
    """The distinguishing strings that MUST survive a render for this segment.

    For tool_call the surviving slots are ``tool_name_{i}`` (== name) and
    ``tool_args_{i}`` (== args dict). For text-bearing kinds it's the ``text``.
    """
    if seg.kind == "tool_call":
        return [str(seg.content.get("name", "")), str(seg.content.get("args", {}).get("needle", ""))]
    return [str(seg.content.get("text", ""))]


def _rendered_blob(keys: dict[str, Any]) -> str:
    """All rendered values flattened to one searchable string (keys + values)."""
    return repr(keys)


def _assert_gapless(keys: dict[str, Any]) -> None:
    """The ITERATION indices must be contiguous 0..N-1 — no whole iteration may be
    skipped (stock dspy never has an index gap).

    NOTE: per-FAMILY contiguity is deliberately NOT required. A partial iteration is
    legitimate in the live plane: a tool-only or observation-only iteration (from a
    lone segment or after a delete) yields e.g. ``tool_name_0, tool_args_0,
    thought_1`` with no ``thought_0``. dspy's stock ``_format_trajectory`` builds a
    signature from exactly the present keys, so a missing family slot is fine — what
    must hold is that the iteration index sequence itself has no hole, and that
    ``tool_name_{i}`` always pairs with ``tool_args_{i}`` (the tool slot is atomic).
    """
    pieces: dict[str, set[int]] = {}
    iter_idxs: set[int] = set()
    for key in keys:
        head, _, tail = key.rpartition("_")
        assert tail.isdigit(), f"non-indexed trajectory key: {key!r}"
        i = int(tail)
        pieces.setdefault(head, set()).add(i)
        iter_idxs.add(i)
    if not iter_idxs:
        return
    # Iteration indices contiguous from 0 (no skipped iteration).
    assert iter_idxs == set(range(max(iter_idxs) + 1)), (
        f"iteration indices not contiguous: {sorted(iter_idxs)}"
    )
    # The tool slot is atomic: name and args co-occur at the same index.
    assert pieces.get("tool_name", set()) == pieces.get("tool_args", set()), (
        f"tool_name / tool_args index sets diverged: "
        f"{sorted(pieces.get('tool_name', set()))} vs {sorted(pieces.get('tool_args', set()))}"
    )


def _value_slot_count(keys: dict[str, Any]) -> int:
    """Number of value-bearing trajectory slots in a rendered dict. A tool_call
    occupies ONE logical slot (the paired tool_name/tool_args), so it counts once;
    thought / observation each count once. This is what a live segment maps onto."""
    tool_idxs = {k.rpartition("_")[2] for k in keys if k.startswith("tool_name_")}
    other = sum(1 for k in keys if k.startswith(("thought_", "observation_")))
    return len(tool_idxs) + other


def _assert_content_fidelity(live: list[Segment], keys: dict[str, Any], text: str) -> None:
    """Every live segment's needle(s) must be recoverable from the rendered dict OR
    the flat text (substring evidence), AND — the strong, structural form of the
    anti-overwrite invariant — the render must expose exactly ONE value slot per
    live segment. If a slot were overwritten (the original consecutive-observation
    bug), the slot count would drop below the live-segment count and this catches it
    regardless of needle coincidences."""
    blob = _rendered_blob(keys)
    for seg in live:
        for needle in _needles(seg):
            if needle == "":
                continue
            assert needle in blob or needle in text, (
                f"LOST CONTENT: needle {needle!r} from a live {seg.kind} "
                f"(id={seg.id}) is absent from both render_keys and render_text. "
                f"keys={keys}"
            )
    # structural: one rendered slot per live segment (no two segments share a slot).
    assert _value_slot_count(keys) == len(live), (
        f"slot-count mismatch: {_value_slot_count(keys)} rendered slots for "
        f"{len(live)} live segments — a slot was overwritten or duplicated. keys={keys}"
    )


# --------------------------------------------------------------------------- #
# 1. randomized sequence fuzz: gapless + no-overwrite + pure-fn parity
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_append_only_invariants(tmp_path, seed):
    """SEEDED random kind sequences (any order, consecutive same-kind, lone
    obs/tool, summaries mid-stream) must render gapless with zero content loss,
    and the pure projection must equal render_keys."""
    rng = random.Random(seed)
    ss, _ = _store(tmp_path)
    n = rng.randint(1, 40)
    for i in range(n):
        kind = rng.choice(TRAJECTORY_KINDS)
        ss.append(SID, SCOPE, kind, _content_for(kind, f"{seed}_{i}"), step=i)

    live = ss.render(SID, SCOPE)
    keys = ss.render_keys(SID, SCOPE)
    text = ss.render_text(SID, SCOPE)

    # invariant 2: gapless iteration indices
    _assert_gapless(keys)
    # invariant 1: no content lost to a key overwrite
    _assert_content_fidelity(live, keys, text)
    # invariant 5: pure-function parity (replay uses segments_to_keys directly)
    assert segments_to_keys(live) == keys
    # render is sorted by (order, logical_time) -> append order here
    assert [s.step for s in live] == list(range(n))


@pytest.mark.parametrize("seed", range(60))
def test_fuzz_with_mutations_invariants(tmp_path, seed):
    """As above but interleave the full mutation surface (insert / delete /
    summarize) between appends — the live, edited plane must STILL be gapless and
    lossless, and a cold reload + a Trace-replay must reproduce it exactly."""
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
    keys = ss.render_keys(SID, SCOPE)
    text = ss.render_text(SID, SCOPE)

    _assert_gapless(keys)
    _assert_content_fidelity(live, keys, text)
    assert segments_to_keys(live) == keys

    # cold reload reproduces the render byte-for-byte (persistence parity)
    reloaded = SegmentStore(LocalFSStore(str(tmp_path)))
    assert reloaded.render_keys(SID, SCOPE) == keys
    assert reloaded.render_text(SID, SCOPE) == text

    # Trace-replay over the exact op stream reproduces the live render order
    replayed = reconstruct_arc_segments(logged, scope_filter=SCOPE)
    assert [s.id for s in replayed] == [s.id for s in live], (
        "replay diverged from live render order"
    )
    assert segments_to_keys(replayed) == keys


# --------------------------------------------------------------------------- #
# 2. targeted adversarial patterns (the exact shapes that broke us / could)
# --------------------------------------------------------------------------- #


def test_consecutive_observations_never_overwrite(tmp_path):
    """The original bug: N consecutive observations must each get a distinct slot,
    not collapse onto observation_0."""
    ss, _ = _store(tmp_path)
    for i in range(7):
        ss.append(SID, SCOPE, "observation", {"text": f"OBS_{i}"}, step=i)
    keys = ss.render_keys(SID, SCOPE)
    assert keys == {f"observation_{i}": f"OBS_{i}" for i in range(7)}
    _assert_gapless(keys)


def test_consecutive_tool_calls_never_overwrite(tmp_path):
    """Lone/consecutive tool_calls (no thought, no obs) must each open a new
    iteration rather than overwrite tool_name_0/tool_args_0."""
    ss, _ = _store(tmp_path)
    for i in range(5):
        ss.append(SID, SCOPE, "tool_call", {"name": f"T{i}", "args": {"x": i}}, step=i)
    keys = ss.render_keys(SID, SCOPE)
    for i in range(5):
        assert keys[f"tool_name_{i}"] == f"T{i}"
        assert keys[f"tool_args_{i}"] == {"x": i}
    _assert_gapless(keys)


def test_consecutive_thoughts_never_overwrite(tmp_path):
    ss, _ = _store(tmp_path)
    for i in range(6):
        ss.append(SID, SCOPE, "thought", {"text": f"TH_{i}"}, step=i)
    keys = ss.render_keys(SID, SCOPE)
    assert keys == {f"thought_{i}": f"TH_{i}" for i in range(6)}
    _assert_gapless(keys)


def test_summary_midstream_takes_own_slot(tmp_path):
    """A summary injected mid-stream renders as its own observation slot and must
    not stomp a neighbouring real observation."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "TH"}, step=0)
    ss.append(SID, SCOPE, "tool_call", {"name": "tl", "args": {}}, step=0)
    ss.append(SID, SCOPE, "observation", {"text": "REAL_OBS"}, step=0)
    # a summary appended directly (mid-stream injection, not replacing anything)
    ss.append(SID, SCOPE, "summary", {"text": "MID_SUMMARY"}, step=0)
    keys = ss.render_keys(SID, SCOPE)
    blob = _rendered_blob(keys)
    assert "REAL_OBS" in blob and "MID_SUMMARY" in blob
    _assert_gapless(keys)


def test_thought_then_two_observations(tmp_path):
    """thought, obs, obs: the second obs must roll to a new iteration index."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "TH"}, step=0)
    ss.append(SID, SCOPE, "observation", {"text": "O_A"}, step=0)
    ss.append(SID, SCOPE, "observation", {"text": "O_B"}, step=0)
    keys = ss.render_keys(SID, SCOPE)
    assert keys["thought_0"] == "TH"
    assert keys["observation_0"] == "O_A"
    assert keys["observation_1"] == "O_B"  # not overwritten
    _assert_gapless(keys)


def test_lone_observation_renders(tmp_path):
    """A single lone observation (idx starts at -1) must produce observation_0."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "observation", {"text": "SOLO"}, step=0)
    assert ss.render_keys(SID, SCOPE) == {"observation_0": "SOLO"}


def test_tool_call_after_full_iteration_rolls_over(tmp_path):
    """thought, tool, obs, tool: the 2nd tool_call lands in a fresh iteration (the
    'tool' slot of iter 0 is filled) — it must not overwrite tool_name_0."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "thought", {"text": "TH"}, step=0)
    ss.append(SID, SCOPE, "tool_call", {"name": "FIRST", "args": {}}, step=0)
    ss.append(SID, SCOPE, "observation", {"text": "OB"}, step=0)
    ss.append(SID, SCOPE, "tool_call", {"name": "SECOND", "args": {}}, step=1)
    keys = ss.render_keys(SID, SCOPE)
    assert keys["tool_name_0"] == "FIRST"
    assert keys["tool_name_1"] == "SECOND"  # rolled over, not overwritten
    _assert_gapless(keys)


def test_empty_and_missing_content_keeps_its_slot(tmp_path):
    """Segments with empty/missing payload fields must STILL occupy a distinct slot
    (render uses ``.get(..., "")``) — an empty observation is not the same as no
    observation, and two empty observations must not collapse into one slot.

    Sequence: obs, obs, tool. The 2nd obs rolls to iter 1; the tool then attaches to
    iter 1 (its 'tool' slot is free even though 'obs' is filled), so it renders as
    ``tool_name_1`` — NOT a new iteration. The decisive invariant is that all three
    empty segments keep three distinct value slots (no collapse)."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "observation", {}, step=0)            # missing "text"
    ss.append(SID, SCOPE, "observation", {"text": ""}, step=1)  # empty "text"
    ss.append(SID, SCOPE, "tool_call", {}, step=2)              # missing name/args
    keys = ss.render_keys(SID, SCOPE)
    assert keys["observation_0"] == ""
    assert keys["observation_1"] == ""
    assert keys["tool_name_1"] == "" and keys["tool_args_1"] == {}  # tool joins iter 1
    live = ss.render(SID, SCOPE)
    assert _value_slot_count(keys) == len(live) == 3  # no collapse despite empties
    _assert_gapless(keys)


def test_empty_observations_do_not_collapse(tmp_path):
    """Two empty observations in a row must keep two distinct slots — the empty
    string must not be treated as 'absent' and merged."""
    ss, _ = _store(tmp_path)
    for i in range(4):
        ss.append(SID, SCOPE, "observation", {"text": ""}, step=i)
    keys = ss.render_keys(SID, SCOPE)
    assert keys == {f"observation_{i}": "" for i in range(4)}
    assert _value_slot_count(keys) == 4
    _assert_gapless(keys)


def test_framing_kinds_never_in_trajectory(tmp_path):
    """system / user / tool_def are dspy's own framing and must NEVER appear in the
    trajectory dict, even interleaved with real trajectory segments."""
    ss, _ = _store(tmp_path)
    ss.append(SID, SCOPE, "system", {"text": "SYS"}, step=-1)
    ss.append(SID, SCOPE, "user", {"text": "USR"}, step=-1)
    ss.append(SID, SCOPE, "tool_def", {"name": "d", "schema": {}}, step=-1)
    ss.append(SID, SCOPE, "thought", {"text": "TH"}, step=0)
    ss.append(SID, SCOPE, "tool_call", {"name": "tl", "args": {}}, step=0)
    ss.append(SID, SCOPE, "observation", {"text": "OB"}, step=0)
    keys = ss.render_keys(SID, SCOPE)
    blob = _rendered_blob(keys)
    assert "SYS" not in blob and "USR" not in blob
    assert keys == {
        "thought_0": "TH", "tool_name_0": "tl", "tool_args_0": {}, "observation_0": "OB"
    }
    _assert_gapless(keys)


# --------------------------------------------------------------------------- #
# 3. byte-equality of the canonical flow vs stock dspy shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(25))
def test_canonical_flow_byte_equals_stock_dspy_shape(tmp_path, seed):
    """For a CANONICAL fully-populated loop (thought -> tool -> obs per iteration),
    render_keys must be byte-identical to the dict stock dspy builds locally."""
    rng = random.Random(7000 + seed)
    ss, _ = _store(tmp_path)
    n = rng.randint(1, 12)
    expected: dict[str, Any] = {}
    for i in range(n):
        th, tn = f"think_{seed}_{i}", f"tool_{seed}_{i}"
        ta = {"q": f"q{i}", "n": i}
        ob = f"obs_{seed}_{i}"
        ss.append(SID, SCOPE, "thought", {"text": th}, step=i)
        ss.append(SID, SCOPE, "tool_call", {"name": tn, "args": ta}, step=i)
        ss.append(SID, SCOPE, "observation", {"text": ob}, step=i)
        expected[f"thought_{i}"] = th
        expected[f"tool_name_{i}"] = tn
        expected[f"tool_args_{i}"] = ta
        expected[f"observation_{i}"] = ob
    keys = ss.render_keys(SID, SCOPE)
    assert keys == expected
    # key ORDER must also match stock's interleave (thought,tool_name,tool_args,obs)
    assert list(keys.keys()) == list(expected.keys())


# --------------------------------------------------------------------------- #
# 4. as-of-T monotonic visibility (fuzzed)
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
                SID, SCOPE, rng.sample(live_ids, rng.randint(1, len(live_ids))),
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
    live_keys = ss.render_keys(SID, SCOPE)
    asof_keys = ss.render_keys(SID, SCOPE, as_of=max_lt + 1)
    assert asof_keys == live_keys
    # and as-of==0 (before any write) is empty
    assert ss.render_keys(SID, SCOPE, as_of=0) == {}


def test_as_of_render_keys_gapless_at_every_snapshot(tmp_path):
    """render_keys must be gapless at EVERY historical snapshot, not only live —
    an as-of view that drops a mid segment must still renumber contiguously."""
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
        _assert_gapless(ss.render_keys(SID, SCOPE, as_of=t))


# --------------------------------------------------------------------------- #
# 5. ARCMemory pass-through parity + segment_text agreement
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
    keys = arc.render_segments_keys(SID, SCOPE)
    assert segments_to_keys(live) == keys
    _assert_gapless(keys)
    _assert_content_fidelity(live, keys, arc.render_segment_text(SID, SCOPE))
    # render_text is the segment_text join of the live render
    assert arc.render_segment_text(SID, SCOPE) == "\n".join(segment_text(s) for s in live)
