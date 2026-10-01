"""clio-core PARITY: the live plane a process renders is exactly what clio-core holds.

clio-core is the only ARC store. Each process keeps a write-through copy of the scopes
it has touched, so the release proof is that a COLD ``ARCMemory`` -- a second instance
reading only what clio-core stores -- renders every scope identically to the writer,
on every observable read surface, for a battery of op sequences (append / insert /
delete / summarize, mid-scope edits, multi-scope, multi-session, binary-hostile
non-UTF-8 content, large content), for as-of-T reads, and for the real ``ClioReAct``
loop:

    * ``render_segments``        (ordered live segments -- id/kind/content)
    * ``render_segment_text``    (the flattened text -- byte-equality)
    * ``segment_tokens_by_kind`` (compaction attribution)
    * ``scan_scopes``            (cross-scope discovery)

Every test runs in its own clio-core namespace on the worker's private daemon (the
harness clears it at teardown).
"""

from __future__ import annotations

from typing import Any, Callable

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import ClioCoreStore, make_arc_store
from tests._scripted_engine import calls, scripted_lm

from .conftest import live_plane_context, make_react_agent

# ---------------------------------------------------------------------------
# clio-core ARCMemory helpers
# ---------------------------------------------------------------------------


def _clio_core_arc() -> ARCMemory:
    """An ARCMemory on the real ClioCoreStore in this test's own namespace."""
    store = make_arc_store(backend="cte")
    assert isinstance(store, ClioCoreStore)
    return ARCMemory(store=store)


def _structure(
    arc: ARCMemory, sid: str, scope: str, *, as_of: int | None = None
) -> list[tuple[str, int, Any]]:
    """The ordered live render as ``(kind, step, content)`` per segment."""
    return [(s.kind, s.step, s.content) for s in arc.render_segments(sid, scope, as_of=as_of)]


def _with_ids(arc: ARCMemory, sid: str, scope: str) -> list[tuple[str, str, Any]]:
    """The ordered live render as ``(id, kind, content)`` -- ids survive a cold reload."""
    return [(s.id, s.kind, s.content) for s in arc.render_segments(sid, scope)]


def _unique_sessions(pairs: list[tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for sid, _scope in pairs:
        if sid not in out:
            out.append(sid)
    return out


def _assert_cold_matches(
    writer: ARCMemory, cold: ARCMemory, pairs: list[tuple[str, str]], *, label: str
) -> None:
    """Every observable read surface of the cold instance equals the writer's."""
    for sid, scope in pairs:
        assert _with_ids(cold, sid, scope) == _with_ids(writer, sid, scope), (
            f"[{label}] cold render diverged from the writer on scope {scope}"
        )
        assert cold.render_segment_text(sid, scope) == writer.render_segment_text(sid, scope)
        assert cold.segment_tokens_by_kind(sid, scope) == writer.segment_tokens_by_kind(sid, scope)
    for sid in _unique_sessions(pairs):
        assert cold._segments.scan_scopes(sid) == writer._segments.scan_scopes(sid)


# ---------------------------------------------------------------------------
# The op-sequence battery -- each is a pure function of (arc, sid) -> scopes used
# ---------------------------------------------------------------------------
#
# A "script" mutates the live plane through the ARCMemory pass-throughs ONLY (the
# real write surface), and returns the list of (session, scope) it touched so the
# harness knows what to compare. Scripts are backend-agnostic: same calls, same
# order, only the session id differs per backend.


Script = Callable[[ARCMemory, str], list[tuple[str, str]]]


def _script_append_only(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentA"
    arc.append_segment(sid, scope, "thought", {"text": "think 0"}, step=0, token_count=3)
    arc.append_segment(
        sid,
        scope,
        "tool_call",
        {"name": "grep", "args": {"q": "x", "n": 5}},
        step=0,
        token_count=7,
    )
    arc.append_segment(sid, scope, "observation", {"text": "OBS_0"}, step=0, token_count=11)
    arc.append_segment(sid, scope, "thought", {"text": "think 1"}, step=1, token_count=3)
    arc.append_segment(
        sid, scope, "tool_call", {"name": "finish", "args": {}}, step=1, token_count=2
    )
    arc.append_segment(sid, scope, "observation", {"text": "Done."}, step=1, token_count=4)
    return [(sid, scope)]


def _script_insert_midscope(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentB"
    arc.append_segment(sid, scope, "thought", {"text": "FIRST"}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "THIRD"}, step=0)
    # insert at render position 1 (between the two) -- exercises gap-allocated order
    arc.insert_segment(sid, scope, 1, "thought", {"text": "SECOND"}, step=0)
    # insert at position 0 (before everything) -- exercises the lo-1.0 path
    arc.insert_segment(sid, scope, 0, "thought", {"text": "ZEROTH"}, step=0)
    # insert past the end -- exercises the append-equivalent path
    arc.insert_segment(sid, scope, 999, "observation", {"text": "LAST"}, step=0)
    return [(sid, scope)]


def _script_delete(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentC"
    arc.append_segment(sid, scope, "thought", {"text": "KEEP_T"}, step=0)
    arc.append_segment(sid, scope, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "DELETE_ME"}, step=0)
    arc.append_segment(sid, scope, "thought", {"text": "KEEP_T2"}, step=1)
    obs = [s for s in arc.render_segments(sid, scope) if s.content.get("text") == "DELETE_ME"]
    assert obs, "setup: DELETE_ME segment must exist"
    n = arc.delete_segments(sid, scope, [obs[0].id])
    assert n == 1
    # deleting an already-tombstoned or an unknown id fails typed, applying nothing
    from clio_agent.arc.segment_ids import StaleSegmentIdError

    for stale in (obs[0].id, "does-not-exist"):
        try:
            arc.delete_segments(sid, scope, [stale])
        except StaleSegmentIdError:
            continue
        raise AssertionError(f"delete of stale id {stale!r} did not fail typed")
    return [(sid, scope)]


def _script_summarize(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentD"
    arc.append_segment(sid, scope, "thought", {"text": "ORIG_T0"}, step=0, token_count=5)
    arc.append_segment(sid, scope, "tool_call", {"name": "a", "args": {"k": 1}}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "ORIG_O0"}, step=0, token_count=8)
    arc.append_segment(sid, scope, "thought", {"text": "ORIG_T1"}, step=1)
    arc.append_segment(sid, scope, "observation", {"text": "ORIG_O1"}, step=1)
    # summarize the first iteration only (range summarize, position-preserving)
    first = [s for s in arc.render_segments(sid, scope) if s.step == 0]
    arc.summarize_segments(
        sid, scope, [s.id for s in first], {"text": "SUMMARY_OF_ITER0"}, token_count=20
    )
    return [(sid, scope)]


def _script_summarize_all(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentE"
    arc.append_segment(sid, scope, "thought", {"text": "t0"}, step=0)
    arc.append_segment(sid, scope, "tool_call", {"name": "a", "args": {}}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "o0"}, step=0)
    arc.append_segment(sid, scope, "thought", {"text": "t1"}, step=1)
    arc.append_segment(sid, scope, "observation", {"text": "o1"}, step=1)
    live_ids = [s.id for s in arc.render_segments(sid, scope)]
    arc.summarize_segments(sid, scope, live_ids, {"text": "EVERYTHING_COLLAPSED"})
    return [(sid, scope)]


def _script_multi_scope(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    # nested scopes with '/' (which the record name encodes via _SLASH_SUB) -- a
    # strong scan_scopes parity probe across the two name<->record mappings.
    scopes = ["agentA/expertX", "agentA/expertY", "agentB/expertZ", "root"]
    for i, scope in enumerate(scopes):
        arc.append_segment(sid, scope, "thought", {"text": f"in {scope}"}, step=i)
        arc.append_segment(
            sid, scope, "observation", {"text": f"obs {scope}"}, step=i, token_count=i + 1
        )
    return [(sid, scope) for scope in scopes]


def _script_multi_session(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    # two distinct sessions sharing a scope name -- proves session isolation is
    # held in clio-core (scan_scopes is session-scoped).
    sid2 = sid + "__second"
    scope = "shared"
    arc.append_segment(sid, scope, "thought", {"text": "session-one"}, step=0)
    arc.append_segment(sid2, scope, "thought", {"text": "session-two"}, step=0)
    arc.append_segment(sid2, scope, "observation", {"text": "two-obs"}, step=0)
    return [(sid, scope), (sid2, scope)]


def _script_binary_hostile(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    """Content carrying non-UTF-8 raw bytes + lone surrogates + control chars.

    This is the regression guard for CTE's base64 wrapping (GetBlob UTF-8-decodes):
    the msgpack payload that persists these segments contains non-UTF-8 bytes, and
    must round-trip byte-identically through clio-core.
    """
    scope = "agentBin"
    # raw non-UTF-8 bytes stored as a bytes value inside content (msgpack-native)
    arc.append_segment(
        sid,
        scope,
        "observation",
        {"text": "ok-text", "raw": b"\x00\x83\xff\x81\xfe", "n": 7},
        step=0,
        token_count=9,
    )
    # tool_call whose args carry bytes + nested structure
    arc.append_segment(
        sid,
        scope,
        "tool_call",
        {"name": "bintool", "args": {"blob": b"\xff\xd8\xff\xe0", "ratio": 0.5}},
        step=0,
    )
    # control chars / newlines / tabs / a high unicode astral char in a text field
    arc.append_segment(
        sid,
        scope,
        "thought",
        {"text": "line1\nline2\tTAB\x07BELL\x00NUL emoji-\U0001f9ea-end"},
        step=1,
    )
    return [(sid, scope)]


def _script_large_content(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentBig"
    big = "X" * 200_000  # 200 KB text payload in one segment
    arc.append_segment(sid, scope, "observation", {"text": big}, step=0, token_count=50_000)
    arc.append_segment(
        sid,
        scope,
        "tool_call",
        {"name": "dump", "args": {"rows": list(range(2_000))}},
        step=0,
    )
    arc.append_segment(sid, scope, "thought", {"text": "after big"}, step=1)
    return [(sid, scope)]


def _script_combined(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    """A long mixed sequence across several scopes interleaving all four ops --
    the closest thing to a real loop's edit history."""
    scope = "agentMix"
    arc.append_segment(sid, scope, "thought", {"text": "m-t0"}, step=0, token_count=2)
    arc.append_segment(sid, scope, "tool_call", {"name": "f", "args": {"i": 0}}, step=0)
    arc.append_segment(sid, scope, "observation", {"text": "m-o0"}, step=0, token_count=4)
    arc.append_segment(sid, scope, "thought", {"text": "m-t1"}, step=1, token_count=2)
    arc.append_segment(sid, scope, "observation", {"text": "m-o1"}, step=1, token_count=4)
    # insert a note before iteration 1's observation
    arc.insert_segment(sid, scope, 4, "thought", {"text": "INSERTED_NOTE"}, step=1)
    # delete iteration 0's observation
    o0 = [s for s in arc.render_segments(sid, scope) if s.content.get("text") == "m-o0"]
    arc.delete_segments(sid, scope, [o0[0].id])
    # summarize iteration 1's surviving pieces
    iter1 = [s for s in arc.render_segments(sid, scope) if s.step == 1]
    arc.summarize_segments(sid, scope, [s.id for s in iter1], {"text": "ITER1_SUMMARY"})
    # one more append after all the surgery
    arc.append_segment(sid, scope, "thought", {"text": "m-t2"}, step=2, token_count=2)
    return [(sid, scope)]


ALL_SCRIPTS: list[tuple[str, Script]] = [
    ("append_only", _script_append_only),
    ("insert_midscope", _script_insert_midscope),
    ("delete", _script_delete),
    ("summarize_range", _script_summarize),
    ("summarize_all", _script_summarize_all),
    ("multi_scope", _script_multi_scope),
    ("multi_session", _script_multi_session),
    ("binary_hostile", _script_binary_hostile),
    ("large_content", _script_large_content),
    ("combined_ops", _script_combined),
]


# ---------------------------------------------------------------------------
# The battery: every script, then a cold ARCMemory over what clio-core holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,script", ALL_SCRIPTS, ids=[n for n, _ in ALL_SCRIPTS])
def test_cold_instance_renders_what_clio_core_holds(name: str, script: Script) -> None:
    """The writer's live plane and a cold instance reading clio-core are identical."""
    writer = _clio_core_arc()
    pairs = script(writer, f"parity_{name}")
    assert pairs, f"[{name}] script returned no scopes"

    _assert_cold_matches(writer, _clio_core_arc(), pairs, label=name)


# ---------------------------------------------------------------------------
# as-of-T reads (the temporal read surface) survive a cold read
# ---------------------------------------------------------------------------


def test_as_of_render_matches_on_a_cold_instance() -> None:
    """as-of-T reads (pre-edit snapshots) reconstruct identically from clio-core:
    logical_time and tombstones are recovered from the persisted segments."""
    writer = _clio_core_arc()
    sid, scope = "asof", "agentTime"
    writer.append_segment(sid, scope, "thought", {"text": "t0"}, step=0)
    writer.append_segment(sid, scope, "tool_call", {"name": "a", "args": {}}, step=0)
    writer.append_segment(sid, scope, "observation", {"text": "o0"}, step=0)
    snapshot = max(s.logical_time for s in writer.render_segments(sid, scope))
    obs = [s for s in writer.render_segments(sid, scope) if s.content.get("text") == "o0"]
    writer.delete_segments(sid, scope, [obs[0].id])
    writer.append_segment(sid, scope, "thought", {"text": "t1"}, step=1)

    cold = _clio_core_arc()
    assert _structure(cold, sid, scope) == _structure(writer, sid, scope)
    assert _structure(cold, sid, scope, as_of=snapshot) == _structure(
        writer, sid, scope, as_of=snapshot
    )
    text = cold._segments.render_text(sid, scope, as_of=snapshot)
    assert text == writer._segments.render_text(sid, scope, as_of=snapshot)
    assert "o0" in text, "as-of-T must show the pre-delete obs"


# ---------------------------------------------------------------------------
# The REAL ClioReAct loop writes a plane clio-core holds in full
# ---------------------------------------------------------------------------


def _scripted_lm() -> dspy.LM:
    """A 2-step ReAct script: search then submit (typed lm15 replies);
    ``make_react_agent`` builds the real loop."""
    lm, _ = scripted_lm(
        [
            calls(("search", {"q": "alpha"}), text="search first"),
            calls(("submit", {"answer": "FINAL_ANSWER"}), text="done"),
        ]
    )
    return lm


def _run_real_loop(arc: ARCMemory, sid: str, scope: str) -> None:
    """Drive the REAL ClioReAct loop so the live plane is written by the actual
    machinery (not direct append_segment calls)."""
    agent = make_react_agent()
    lm = _scripted_lm()
    with live_plane_context(arc, session=sid, scope=scope):
        with dspy.context(lm=lm):
            agent(question="find alpha")


def test_real_react_loop_plane_is_what_clio_core_holds() -> None:
    """Drive the actual ``ClioReAct`` loop with a scripted LM; the trajectory it wrote
    renders identically from a cold instance reading clio-core."""
    writer = _clio_core_arc()
    sid, scope = "react", "agentA"

    _run_real_loop(writer, sid, scope)

    assert "SEARCH_RESULT" in writer.render_segment_text(sid, scope)
    _assert_cold_matches(writer, _clio_core_arc(), [(sid, scope)], label="react")
