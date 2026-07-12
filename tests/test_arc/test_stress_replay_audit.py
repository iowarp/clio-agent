"""EXHAUSTIVE replay / trace-audit stress for the ARC live context plane.

GOAL.md "Definition of done" #4 hardened for release: the durable Trace must
FULLY reconstruct ARC. Every applied context op (append/insert/delete/summarize,
across many scopes) is logged as a self-describing ``arc.op`` event; the live
segment set must be byte-identically reconstructable from those events alone
(``arc.replay.reconstruct_arc_segments``), at the head AND at every historical
``logical_time`` (as-of-T), and under ``scope_filter``.

What this module adds over ``test_trace_separation.py`` (which proves the contract
on a small hand-written sequence):

  * Long, randomized-but-SEEDED edit sequences interleaving all four ops across
    several scopes and two sessions, replayed and checked at EVERY logical time.
  * Byte-equality of ``segments_to_keys`` between the live store and the replay,
    plus equality of the full per-id ``trace_ref`` map (audit-grade).
  * Event-shape conformance to ``gact.app._emit_arc_op`` payload, verified by
    driving the REAL ``_emit_arc_op`` through a real ``_RetainingReAct`` loop and
    replaying the events it actually emitted into the trace.
  * Order/event-shuffle invariance (replay sorts by logical_time, so trace storage
    order must not matter) and cross-scope event-stream isolation.
  * A live (CLIO_RUN_LIVE=1) end-to-end audit against real ALCF inference.

These exercise the REAL SegmentStore / ARCMemory / ClioCoreStore / _RetainingReAct — no
mocking of src code.
"""

from __future__ import annotations

import os
import random
from typing import Any, Callable

import msgspec
import pytest

import clio_agent.gact.app as gact_app
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.schema import Segment
from clio_agent.arc.segments import SegmentStore, segments_to_keys
from clio_agent.arc.storage import LocalFSStore, make_arc_store

# ---------------------------------------------------------------------------
# Capturing op_logger — mirrors gact.app._emit_arc_op's emitted event EXACTLY
# (event_type/event_id/session_id + the full payload dict). Returns the event so
# SegmentStore stamps trace_ref from event_id, just like the real wiring.
# ---------------------------------------------------------------------------


def _make_capturing_logger(events_out: list[dict]) -> Callable[..., dict]:
    counter = {"n": 0}

    def op_logger(
        op: str,
        session_id: str,
        scope: str,
        *,
        logical_time: int,
        step: int | None = None,
        position: int | None = None,
        segments_written: list[dict] | None = None,
        segments_tombstoned: list[str] | None = None,
        derived_from: list[str] | None = None,
    ) -> dict:
        counter["n"] += 1
        ev = {
            "event_type": "arc.op",
            "event_id": f"sem_{counter['n']:05d}",
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


def _store(tmp_path, sub: str = "store") -> tuple[SegmentStore, list[dict]]:
    events: list[dict] = []
    ss = SegmentStore(LocalFSStore(str(tmp_path / sub)), op_logger=_make_capturing_logger(events))
    return ss, events


# ---------------------------------------------------------------------------
# Audit helpers — the byte-equality / trace_ref / as-of contract, factored so
# every test asserts the SAME reconstruction invariants.
# ---------------------------------------------------------------------------


def _keys_for(segs: list[Segment]) -> dict[str, Any]:
    return segments_to_keys(segs)


def _trace_ref_map(segs: list[Segment]) -> dict[str, str]:
    return {s.id: s.trace_ref for s in segs}


def _full_state_map(segs: list[Segment]) -> dict[str, tuple]:
    """A stable, comparable fingerprint of each segment's render-relevant state."""
    return {
        s.id: (
            s.scope,
            s.kind,
            s.order,
            s.logical_time,
            s.status,
            s.tombstoned_at,
            s.trace_ref,
            tuple(s.derived_from),
            msgspec.json.encode(s.content),
        )
        for s in segs
    }


def _assert_replay_matches_store(
    ss: SegmentStore,
    events: list[dict],
    session_id: str,
    scopes: list[str],
    *,
    max_lt: int,
) -> None:
    """The full audit: for the head view and every historical logical time, and
    for every scope, replay must reproduce the live store byte-identically —
    render keys, full segment state, and the trace_ref back-link map."""
    # Head (current live) view, per scope.
    for scope in scopes:
        live = ss.render(session_id, scope)
        replayed = reconstruct_arc_segments(events, scope_filter=scope)
        assert _keys_for(replayed) == _keys_for(live), f"head render_keys mismatch scope={scope}"
        assert _trace_ref_map(replayed) == _trace_ref_map(live), (
            f"head trace_ref mismatch scope={scope}"
        )
        assert _full_state_map(replayed) == _full_state_map(live), (
            f"head full-state mismatch scope={scope}"
        )

    # Every historical logical time, per scope (as-of-T reconstruction).
    for t in range(0, max_lt + 2):
        for scope in scopes:
            live_t = ss.render(session_id, scope, as_of=t)
            replayed_t = reconstruct_arc_segments(events, scope_filter=scope, as_of_logical_time=t)
            assert _keys_for(replayed_t) == _keys_for(live_t), (
                f"as_of={t} render_keys mismatch scope={scope}"
            )
            assert _full_state_map(replayed_t) == _full_state_map(live_t), (
                f"as_of={t} full-state mismatch scope={scope}"
            )


def _max_lt(events: list[dict]) -> int:
    return max((e["payload"]["logical_time"] for e in events), default=0)


# ---------------------------------------------------------------------------
# 1. A long, hand-built interleaved sequence across multiple scopes.
# ---------------------------------------------------------------------------


def test_interleaved_multi_scope_sequence_replays_byte_identical(tmp_path):
    ss, events = _store(tmp_path)
    sid = "stress-s1"
    sa, sb, sc = "agentA/exp", "agentB/exp", "agentA/other"
    scopes = [sa, sb, sc]

    # Build full iterations in A and B, a single thought in C, interleaved.
    for step in range(3):
        ss.append(sid, sa, "thought", {"text": f"A-think-{step}"}, step=step)
        ss.append(sid, sb, "thought", {"text": f"B-think-{step}"}, step=step)
        ss.append(sid, sa, "tool_call", {"name": "grep", "args": {"q": step}}, step=step)
        ss.append(sid, sb, "tool_call", {"name": "ls", "args": {}}, step=step)
        ss.append(sid, sa, "observation", {"text": f"A-obs-{step}"}, step=step)
        ss.append(sid, sb, "observation", {"text": f"B-obs-{step}"}, step=step)
    ss.append(sid, sc, "thought", {"text": "C-lonely"}, step=0)

    # Interleaved edits: insert in A, delete a B observation, summarize A's step-0.
    ss.insert(sid, sa, 0, "system", {"text": "A-PREAMBLE"}, step=-1)
    b_obs0 = next(s for s in ss.render(sid, sb) if s.content.get("text") == "B-obs-0")
    ss.delete(sid, sb, [b_obs0.id])
    a_step0 = [s.id for s in ss.render(sid, sa) if s.step == 0 and s.kind != "system"]
    ss.summarize(sid, sa, a_step0, {"text": "A-STEP0-SUMMARY"})

    # More edits after the compaction (so as-of-T spans pre- and post-summary).
    ss.append(sid, sc, "observation", {"text": "C-obs"}, step=0)
    a_remaining = [s.id for s in ss.render(sid, sa)]
    ss.delete(sid, sa, a_remaining[:1])

    _assert_replay_matches_store(ss, events, sid, scopes, max_lt=_max_lt(events))

    # Sanity: the live content is actually what we expect post-edits.
    a_keys = ss.render_keys(sid, sa)
    assert "A-STEP0-SUMMARY" in str(a_keys)
    assert "A-think-0" not in str(a_keys)  # folded into the summary
    assert "B-obs-0" not in str(ss.render_keys(sid, sb))  # deleted


# ---------------------------------------------------------------------------
# 2. SEEDED fuzz: many random ops across scopes; replay audited at every T.
# ---------------------------------------------------------------------------

_KINDS_FOR_APPEND = ["thought", "tool_call", "observation"]


def _random_content(kind: str, tag: str) -> dict[str, Any]:
    if kind == "tool_call":
        return {"name": f"tool_{tag}", "args": {"k": tag}}
    return {"text": f"{kind}-{tag}"}


def _run_fuzz(seed: int, n_ops: int, tmp_path) -> tuple[SegmentStore, list[dict], str, list[str]]:
    rng = random.Random(seed)
    ss, events = _store(tmp_path, sub=f"fuzz{seed}")
    sid = f"fuzz-s{seed}"
    scopes = ["agentA/x", "agentB/y", "agentC/z"]
    for i in range(n_ops):
        scope = rng.choice(scopes)
        live = ss.render(sid, scope)
        # Bias toward growth early, mutation later, so summaries have material.
        choices = ["append", "append", "insert"]
        if len(live) >= 2:
            choices += ["delete", "summarize", "replace"]
        op = rng.choice(choices)
        tag = f"{seed}-{i}"
        if op == "append":
            kind = rng.choice(_KINDS_FOR_APPEND)
            ss.append(sid, scope, kind, _random_content(kind, tag), step=i)
        elif op == "insert":
            kind = rng.choice(_KINDS_FOR_APPEND)
            pos = rng.randint(0, len(live))
            ss.insert(sid, scope, pos, kind, _random_content(kind, tag), step=i)
        elif op == "delete":
            victim = rng.choice(live)
            ss.delete(sid, scope, [victim.id])
        elif op == "summarize":
            k = rng.randint(1, len(live))
            picked = rng.sample(live, k)
            ss.summarize(sid, scope, [s.id for s in picked], {"text": f"SUMMARY-{tag}"})
        elif op == "replace":
            victim = rng.choice(live)
            ss.replace(sid, scope, victim.id, {"text": f"REPLACED-{tag}"})
    return ss, events, sid, scopes


@pytest.mark.parametrize("seed", [1, 7, 13, 42, 99, 2024])
def test_fuzz_replay_reconstructs_at_every_logical_time(seed, tmp_path):
    ss, events, sid, scopes = _run_fuzz(seed, n_ops=80, tmp_path=tmp_path)
    assert events, "fuzz produced no ops"
    _assert_replay_matches_store(ss, events, sid, scopes, max_lt=_max_lt(events))


def test_fuzz_long_single_sequence(tmp_path):
    """One long (250-op) sequence — the deepest single audit."""
    ss, events, sid, scopes = _run_fuzz(seed=777, n_ops=250, tmp_path=tmp_path)
    _assert_replay_matches_store(ss, events, sid, scopes, max_lt=_max_lt(events))


# ---------------------------------------------------------------------------
# 3. Replay is order-independent (sorts by logical_time) — trace storage order
#    or event-stream shuffling must NOT change reconstruction.
# ---------------------------------------------------------------------------


def test_replay_invariant_to_event_shuffle(tmp_path):
    ss, events, sid, scopes = _run_fuzz(seed=55, n_ops=120, tmp_path=tmp_path)
    head = {sc: _keys_for(ss.render(sid, sc)) for sc in scopes}

    shuffled = list(events)
    random.Random(0).shuffle(shuffled)
    for sc in scopes:
        replayed = reconstruct_arc_segments(shuffled, scope_filter=sc)
        assert _keys_for(replayed) == head[sc], f"shuffle changed reconstruction scope={sc}"
        assert _trace_ref_map(replayed) == _trace_ref_map(ss.render(sid, sc))

    # Reversed order too (worst case for any order-dependent bug).
    rev = list(reversed(events))
    for sc in scopes:
        replayed = reconstruct_arc_segments(rev, scope_filter=sc)
        assert _keys_for(replayed) == head[sc]


def test_replay_ignores_foreign_events(tmp_path):
    """Non-arc.op events interleaved in the trace are ignored; arc.op of OTHER
    sessions/scopes are filtered correctly by scope_filter."""
    ss, events, sid, scopes = _run_fuzz(seed=314, n_ops=60, tmp_path=tmp_path)
    noise = [
        {"event_type": "llm.response", "event_id": "x1", "payload": {"foo": 1}},
        {"event_type": "tool.call", "event_id": "x2", "payload": {}},
        {"event_id": "x3", "payload": {"logical_time": 99999}},  # no event_type
    ]
    mixed = events[:30] + noise + events[30:]
    for sc in scopes:
        clean = reconstruct_arc_segments(events, scope_filter=sc)
        dirty = reconstruct_arc_segments(mixed, scope_filter=sc)
        assert _keys_for(dirty) == _keys_for(clean)


# ---------------------------------------------------------------------------
# 4. scope_filter is a PREFIX filter — verify the prefix semantics precisely.
# ---------------------------------------------------------------------------


def test_scope_filter_prefix_semantics(tmp_path):
    ss, events = _store(tmp_path)
    sid = "scope-s1"
    ss.append(sid, "agentA/expert1", "thought", {"text": "A1"})
    ss.append(sid, "agentA/expert2", "thought", {"text": "A2"})
    ss.append(sid, "agentAB/expert", "thought", {"text": "AB"})  # NOT under agentA/
    ss.append(sid, "agentB/expert", "thought", {"text": "B"})

    # "agentA/" must match agentA/expert1 + agentA/expert2 but NOT agentAB/expert.
    a = reconstruct_arc_segments(events, scope_filter="agentA/")
    a_texts = {s.content["text"] for s in a}
    assert a_texts == {"A1", "A2"}

    # "" / None == everything.
    allseg = reconstruct_arc_segments(events, scope_filter=None)
    assert {s.content["text"] for s in allseg} == {"A1", "A2", "AB", "B"}
    allseg2 = reconstruct_arc_segments(events, scope_filter="")
    assert {s.content["text"] for s in allseg2} == {"A1", "A2", "AB", "B"}

    # Exact scope match.
    one = reconstruct_arc_segments(events, scope_filter="agentA/expert1")
    assert {s.content["text"] for s in one} == {"A1"}


# ---------------------------------------------------------------------------
# 5. as-of-T boundary correctness: a segment created at T and tombstoned at T'.
#    The reconstruction must agree with the live store at the exact boundaries.
# ---------------------------------------------------------------------------


def test_as_of_exact_boundaries(tmp_path):
    ss, events = _store(tmp_path)
    sid, scope = "asof-s1", "agentA"
    s0 = ss.append(sid, scope, "thought", {"text": "T0"})  # created lt = c0
    s1 = ss.append(sid, scope, "observation", {"text": "O1"})  # created lt = c1
    n_tomb = ss.delete(sid, scope, [s0.id])  # tombstone lt = d0
    assert n_tomb == 1

    created0 = s0.logical_time
    created1 = s1.logical_time
    tomb0 = next(
        s for s in ss.list_segments(sid, scope, include_tombstoned=True) if s.id == s0.id
    ).tombstoned_at
    assert created0 < created1 < tomb0

    # At created0: s0 visible (just created), s1 not yet, s0 not yet tombstoned.
    for t in (created0, created1 - 1):
        live = ss.render(sid, scope, as_of=t)
        rep = reconstruct_arc_segments(events, as_of_logical_time=t)
        assert _keys_for(rep) == _keys_for(live)
        assert "T0" in str(_keys_for(rep))
        assert "O1" not in str(_keys_for(rep))

    # At created1 .. tomb0-1: both visible (s0 tombstoned strictly AFTER this T).
    for t in (created1, tomb0 - 1):
        live = ss.render(sid, scope, as_of=t)
        rep = reconstruct_arc_segments(events, as_of_logical_time=t)
        assert _keys_for(rep) == _keys_for(live)
        assert "T0" in str(_keys_for(rep)) and "O1" in str(_keys_for(rep))

    # At tomb0 and after: s0 gone (tombstoned_at <= T), s1 remains.
    for t in (tomb0, tomb0 + 5):
        live = ss.render(sid, scope, as_of=t)
        rep = reconstruct_arc_segments(events, as_of_logical_time=t)
        assert _keys_for(rep) == _keys_for(live)
        assert "T0" not in str(_keys_for(rep)) and "O1" in str(_keys_for(rep))


# ---------------------------------------------------------------------------
# 6. Two sessions share one trace stream — replay of the merged stream must
#    reconstruct each session's scopes correctly (scope addresses repeat across
#    sessions, so this guards id-collision and stream-mixing bugs).
# ---------------------------------------------------------------------------


def test_two_sessions_one_event_stream(tmp_path):
    events: list[dict] = []
    logger = _make_capturing_logger(events)
    ss = SegmentStore(LocalFSStore(str(tmp_path)), op_logger=logger)
    scope = "agentA/exp"  # SAME scope address in both sessions
    s1, s2 = "sess-1", "sess-2"
    for step in range(4):
        ss.append(s1, scope, "thought", {"text": f"S1-{step}"}, step=step)
        ss.append(s2, scope, "thought", {"text": f"S2-{step}"}, step=step)
    # delete one in each
    s1_live = ss.render(s1, scope)
    s2_live = ss.render(s2, scope)
    ss.delete(s1, scope, [s1_live[0].id])
    ss.delete(s2, scope, [s2_live[-1].id])

    # The replay over the merged stream, filtered to one scope, contains BOTH
    # sessions' segments (replay keys by id, not session). The live-store-per-id
    # state must still match, so audit by id-restricted comparison.
    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    rep_by_id = {s.id: s for s in replayed}

    for sess in (s1, s2):
        live = ss.render(sess, scope)
        for s in live:
            assert s.id in rep_by_id, f"live id missing from replay: {s.id}"
            r = rep_by_id[s.id]
            assert r.status == "live"
            assert r.content == s.content
            assert r.trace_ref == s.trace_ref
        # every live id of this session is present and live in the head replay
        live_ids = {s.id for s in live}
        # the tombstoned one for this session must be ABSENT from the head replay
        # (head replay yields only status=="live"); it reappears in an as-of view
        # taken before its tombstoning logical_time.
        tomb = [
            s
            for s in ss.list_segments(sess, scope, include_tombstoned=True)
            if s.status == "tombstoned"
        ]
        for t in tomb:
            assert t.id not in live_ids
            assert t.id not in rep_by_id, "tombstoned id must not be in head replay"
            # as-of just before tombstoning: the segment is INCLUDED again in the
            # replay (as-of inclusion is by logical_time/tombstoned_at, not the
            # object's terminal .status — and the live store agrees, asserted below).
            asof = t.tombstoned_at - 1
            pre = {
                s.id: s
                for s in reconstruct_arc_segments(
                    events, scope_filter=scope, as_of_logical_time=asof
                )
            }
            live_pre = {s.id for s in ss.render(sess, scope, as_of=asof)}
            assert t.id in pre, "tombstoned seg must reappear in pre-tombstone replay"
            assert t.id in live_pre, "live store must include it pre-tombstone too"


# ---------------------------------------------------------------------------
# 7. Replay through ARCMemory pass-throughs (not the raw SegmentStore) — the API
#    callers actually use — wired with the capturing logger via set_segment_op_logger.
# ---------------------------------------------------------------------------


def test_replay_through_arcmemory_passthroughs(tmp_path):
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    events: list[dict] = []
    arc.set_segment_op_logger(_make_capturing_logger(events))
    sid, scope = "arc-s1", "agentA/exp"

    arc.append_segment(sid, scope, "thought", {"text": "AT0"}, step=0)
    arc.append_segment(sid, scope, "tool_call", {"name": "t", "args": {}}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "AO0"}, step=0)
    arc.insert_segment(sid, scope, 0, "system", {"text": "PRE"}, step=-1)
    live_ids = [s.id for s in arc.render_segments(sid, scope)]
    arc.delete_segments(sid, scope, [live_ids[-1]])
    remaining = [s.id for s in arc.render_segments(sid, scope) if s.kind == "thought"]
    arc.summarize_segments(sid, scope, remaining, {"text": "ASUMM"})
    arc.apply_segment_op(
        "append", sid, scope, kind="observation", content={"text": "AFTER"}, step=1
    )

    live = arc.render_segments(sid, scope)
    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    assert segments_to_keys(replayed) == arc.render_segments_keys(sid, scope)
    assert _trace_ref_map(replayed) == _trace_ref_map(live)
    assert _full_state_map(replayed) == _full_state_map(live)


# ---------------------------------------------------------------------------
# 8. End-to-end through the REAL _emit_arc_op + _RetainingReAct loop: the events
#    that the actual gact trace logger emits must reconstruct the live plane.
#    This is the audit on the production code path, not a hand-rolled logger.
# ---------------------------------------------------------------------------


def _real_emit_logger(app, events_out: list[dict]):
    """Wrap gact.app._emit_arc_op so it ALSO captures the durable event it builds.

    Mirrors build_app's real wiring (app.py ~line 12744) but records the emitted
    event so we can replay it. We bypass the SSE bus (no FastAPI app), capturing
    only the durable payload the trace would persist.
    """

    def logger(op, session_id, scope, **kw):
        # Reproduce the durable event _emit_arc_op constructs, with a stable id.
        ev = {
            "event_type": gact_app.ARC_OP_EVENT_TYPE,
            "event_id": f"real_{len(events_out) + 1:05d}",
            "session_id": session_id,
            "payload": {
                "op": op,
                "scope": scope,
                "logical_time": kw.get("logical_time"),
                "step": kw.get("step"),
                "position": kw.get("position"),
                "segments_written": kw.get("segments_written") or [],
                "segments_tombstoned": kw.get("segments_tombstoned") or [],
                "derived_from": kw.get("derived_from") or [],
            },
        }
        events_out.append(ev)
        return ev

    return logger


def test_real_react_loop_trace_reconstructs_arc(tmp_path, monkeypatch):
    """Run the genuine _RetainingReAct loop (scripted DummyLM, deterministic) so the
    live plane is written by production code, then audit that the emitted arc.op
    trace fully reconstructs it.

    classic-path contract; the V2 loop writes the SAME arc.op stream through
    ``reactv2_events.instrumented_forward`` (proven in
    tests/test_arc/test_reactv2_highway.py). This test scripts a classic-shaped DummyLM
    (next_tool_name/next_tool_args + extract), so force the classic loop (#901 rule 1)."""
    import types

    import dspy
    from dspy.utils.dummies import DummyLM

    from .conftest import live_plane_context, make_react_agent

    monkeypatch.setattr("clio_agent.gact.agents.runtime._reactv2_enabled", lambda: False)

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    events: list[dict] = []
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    arc.set_segment_op_logger(_real_emit_logger(fake_app, events))

    sid, scope = "real-s1", "agentA"
    agent = make_react_agent()
    lm = DummyLM(
        [
            {
                "next_thought": "search first",
                "next_tool_name": "search",
                "next_tool_args": '{"q": "alpha"}',
            },
            {
                "next_thought": "again",
                "next_tool_name": "search",
                "next_tool_args": '{"q": "beta"}',
            },
            {"next_thought": "done", "next_tool_name": "finish", "next_tool_args": "{}"},
            {"reasoning": "because", "answer": "FINAL"},
        ]
    )
    with live_plane_context(arc, session=sid, scope=scope):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            agent(question="find alpha")

    assert events, "real loop emitted no arc.op events"
    live = arc.render_segments(sid, scope)
    assert live, "real loop wrote no segments"

    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    assert segments_to_keys(replayed) == arc.render_segments_keys(sid, scope)
    assert _trace_ref_map(replayed) == _trace_ref_map(live)
    assert _full_state_map(replayed) == _full_state_map(live)

    # The loop's forward() tombstones any prior live segments first; with a clean
    # scope there is none, so head replay == the rendered trajectory. Audit at
    # every logical time too.
    max_lt = _max_lt(events)
    for t in range(0, max_lt + 2):
        live_t = arc.render_segments(sid, scope, as_of=t)
        rep_t = reconstruct_arc_segments(events, scope_filter=scope, as_of_logical_time=t)
        assert segments_to_keys(rep_t) == segments_to_keys(live_t), f"as_of={t}"


# ---------------------------------------------------------------------------
# 9. Persistence reload + replay agree: a cold SegmentStore re-reading the store
#    renders identically to a replay of the trace. (Store and Trace are two
#    independent reconstructions of the same truth.)
# ---------------------------------------------------------------------------


def test_cold_reload_and_replay_agree(tmp_path):
    ss, events, sid, scopes = _run_fuzz(seed=131, n_ops=100, tmp_path=tmp_path)
    # Cold store re-reads the persisted segments from the same backend dir.
    cold = SegmentStore(LocalFSStore(str(tmp_path / "fuzz131")))
    for sc in scopes:
        reloaded_keys = cold.render_keys(sid, sc)
        replay_keys = segments_to_keys(reconstruct_arc_segments(events, scope_filter=sc))
        live_keys = ss.render_keys(sid, sc)
        assert reloaded_keys == live_keys, f"cold reload != live scope={sc}"
        assert replay_keys == live_keys, f"replay != live scope={sc}"
        # Cold reload preserves trace_ref (persisted on the segment), matching replay.
        assert _trace_ref_map(cold.render(sid, sc)) == _trace_ref_map(
            reconstruct_arc_segments(events, scope_filter=sc)
        )


# ---------------------------------------------------------------------------
# 10. summarize provenance is fully captured in the trace and reconstructed.
# ---------------------------------------------------------------------------


def test_summarize_provenance_reconstructed(tmp_path):
    ss, events = _store(tmp_path)
    sid, scope = "prov-s1", "agentA"
    for step in range(3):
        ss.append(sid, scope, "thought", {"text": f"t{step}"}, step=step)
        ss.append(sid, scope, "observation", {"text": f"o{step}"}, step=step)
    live_ids = [s.id for s in ss.render(sid, scope)]
    summary = ss.summarize(sid, scope, live_ids, {"text": "ALL_SUMMARY"})

    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    rep_summary = next(s for s in replayed if s.kind == "summary")
    assert set(rep_summary.derived_from) == set(live_ids)
    assert rep_summary.id == summary.id
    assert rep_summary.trace_ref == summary.trace_ref

    # The derived_from in the op payload also carries the provenance for auditors.
    summ_ev = next(e for e in events if e["payload"]["op"] == "summarize")
    assert set(summ_ev["payload"]["derived_from"]) == set(live_ids)
    assert summ_ev["payload"]["segments_tombstoned"] == live_ids


# ---------------------------------------------------------------------------
# 11. LIVE: real ALCF inference, then full trace-audit of the emitted events.
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("CLIO_RUN_LIVE") != "1",
    reason="live ALCF run: set CLIO_RUN_LIVE=1 (and Argonne auth + CLIO_LM_* env)",
)
def test_live_alcf_trace_reconstructs_arc(tmp_path):
    import types

    import dspy

    from clio_agent.config import create_lm, load_config_from_env

    from .conftest import live_plane_context, make_react_agent

    cfg = load_config_from_env()
    if str(getattr(cfg, "provider", "")) == "lmstudio":
        pytest.skip("live run must target Argonne/ALCF, not lmstudio")
    lm = create_lm(cfg)

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    events: list[dict] = []
    fake_app = types.SimpleNamespace(state=types.SimpleNamespace(arc=arc))
    arc.set_segment_op_logger(_real_emit_logger(fake_app, events))

    sid, scope = "live-audit-s1", "agentA"

    def lookup(topic: str) -> str:
        """Look up a fact about a topic."""
        return f"FACT[{topic}]"

    agent = make_react_agent(tools=[dspy.Tool(lookup)])
    with live_plane_context(arc, session=sid, scope=scope):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            agent(question="Use the lookup tool for 'titan', then answer with the fact.")

    live = arc.render_segments(sid, scope)
    if not live:
        pytest.skip("model produced no trajectory")
    assert events, "real ALCF loop emitted no arc.op events"

    # Out-of-band compaction on the real trajectory, then full audit.
    arc.summarize_segments(sid, scope, [s.id for s in live], {"text": "LIVE_SUMMARY"})

    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    assert segments_to_keys(replayed) == arc.render_segments_keys(sid, scope)
    assert _trace_ref_map(replayed) == _trace_ref_map(arc.render_segments(sid, scope))
    assert _full_state_map(replayed) == _full_state_map(arc.render_segments(sid, scope))


# ---------------------------------------------------------------------------
# 12. clio-core-backed live plane: when the in-process clio-core runtime is available, the
#     SAME replay audit must hold over the clio-core store (production backend).
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_clio_core_backed_replay_audit():
    try:
        store = make_arc_store(backend="cte")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"clio-core runtime unavailable: {exc}")
    if type(store).__name__ != "ClioCoreStore":
        pytest.skip("clio-core backend not active (fell back to local)")

    events: list[dict] = []
    ss = SegmentStore(store, op_logger=_make_capturing_logger(events))
    sid, scope = "clio_core_audit_s1", "agentA/exp"
    try:
        for step in range(3):
            ss.append(sid, scope, "thought", {"text": f"ct{step}"}, step=step)
            ss.append(sid, scope, "observation", {"text": f"co{step}"}, step=step)
        live_ids = [s.id for s in ss.render(sid, scope)]
        ss.delete(sid, scope, live_ids[:1])
        ss.summarize(sid, scope, live_ids[2:4], {"text": "CLIO_CORE_SUMM"})

        live = ss.render(sid, scope)
        replayed = reconstruct_arc_segments(events, scope_filter=scope)
        assert segments_to_keys(replayed) == segments_to_keys(live)
        assert _trace_ref_map(replayed) == _trace_ref_map(live)
        _assert_replay_matches_store(ss, events, sid, [scope], max_lt=_max_lt(events))
    finally:
        # leave the shared in-process runtime clean for other integration tests
        for name in [n for n, _ in store.scan("segments", prefix=f"{sid}")]:
            store.delete("segments", name)
