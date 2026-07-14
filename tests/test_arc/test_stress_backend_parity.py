"""Cross-backend PARITY: the ARC live context plane must behave IDENTICALLY on
``LocalFSStore`` and the in-process ``ClioCoreStore``.

This is the release proof that swapping ARC's persistence backend to clio-core
changed NOTHING observable about the live plane. For a battery of op sequences
(append / insert / delete / summarize, mid-scope edits, multi-scope, multi-session,
binary-hostile non-UTF-8 content, large content) we drive the SAME script through
two real ``ARCMemory`` instances -- one ``backend="local"``, one ``backend="cte"`` --
and assert the four observable read surfaces are byte/structure EQUAL:

    * ``render_segments_keys``   (the dspy trajectory dict the ReAct loop reads)
    * ``render_segment_text``    (the flattened text -- byte-equality)
    * ``segment_tokens_by_kind`` (compaction attribution)
    * ``scan_scopes``            (cross-scope discovery)

We also prove PERSISTENCE parity: a SECOND ``ARCMemory`` constructed over the same
backend (cold in-memory state) renders identically -- for local because it re-reads
the dir, for clio-core because the in-process runtime is shared-memory and survives the
construction of a new client.

clio-core cases are marked ``integration`` (need iowarp-core's in-process runtime). The
real ``_RetainingReAct`` machinery is also exercised end-to-end against both backends
so parity is asserted on the actual loop, not just the store API.

Run (unit lane, local only):
    uv run python -m pytest tests/test_arc/test_stress_backend_parity.py \
        -o addopts="" -q -m "not integration"
Run (full parity incl. clio-core):
    CLIO_ALLOWED_ROOTS="/tmp:$PWD" uv run python -m pytest \
        tests/test_arc/test_stress_backend_parity.py -o addopts="" -q
"""

from __future__ import annotations

import os
import uuid
import warnings
from typing import Any, Callable

import dspy
import pytest
from dspy.utils.dummies import DummyLM

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.storage import ClioCoreStore, LocalFSStore, make_arc_store

from .conftest import live_plane_context, make_react_agent

# ---------------------------------------------------------------------------
# Backend construction helpers
# ---------------------------------------------------------------------------

# A process-unique session prefix so CTE's shared-memory (process-global, never
# torn down between tests) never sees leftovers from a prior parity test. Each
# test derives its own session ids off the test name on top of this.
_RUN_TAG = f"parity_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _local_arc(tmp_path, suffix: str = "a") -> ARCMemory:
    """An ARCMemory on a LocalFSStore rooted in this test's tmp dir."""
    store = make_arc_store(backend="local", data_dir=str(tmp_path / f"local_{suffix}"))
    assert isinstance(store, LocalFSStore)
    return ARCMemory(data_dir=str(tmp_path / f"arc_local_{suffix}"), store=store)


def _clio_core_arc() -> ARCMemory:
    """An ARCMemory on the real in-process ClioCoreStore.

    Asserts we actually got clio-core (never a silent LocalFSStore fallback): a fallback
    would make a "parity" assertion trivially pass while testing local-vs-local, so
    we surface the degradation warning as a hard skip-reason instead.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        try:
            store = make_arc_store(backend="cte")
        except RuntimeWarning as w:  # graceful-degradation fired -> not a real clio-core run
            pytest.skip(f"clio-core backend unavailable, cannot prove parity: {w}")
    if not isinstance(store, ClioCoreStore):
        pytest.skip(f"expected ClioCoreStore, got {type(store).__name__}; cannot prove parity")
    return ARCMemory(store=store)


@pytest.fixture
def clio_core_arc() -> ARCMemory:
    """A fresh ARCMemory over clio-core, with its segment scopes cleaned up afterward.

    CTE is shared-memory and process-global, so we cannot rely on tmp-dir isolation;
    instead every test uses ``_RUN_TAG``-prefixed sessions and we clear them on exit
    so reruns in the same process stay clean.
    """
    arc = _clio_core_arc()
    yield arc
    # Best-effort cleanup of just the segments this run wrote (don't nuke the whole
    # shared runtime -- other integration tests may share the process).
    try:
        store = arc._store
        for name, _ in list(store.scan("segments", prefix=_RUN_TAG)):
            store.delete("segments", name)
    except Exception:  # noqa: BLE001
        pass


def _sid(base: str) -> str:
    """A process-unique session id for a test (keeps CTE shared-mem isolated)."""
    return f"{_RUN_TAG}__{base}"


# ---------------------------------------------------------------------------
# The observable-equality assertion -- the heart of the parity proof
# ---------------------------------------------------------------------------


def _observable(arc: ARCMemory, sessions_scopes: list[tuple[str, str]]) -> dict[str, Any]:
    """Snapshot every observable read surface of the live plane for a set of
    (session, scope) pairs, plus per-session scope discovery. Pure read; no mutation.
    """
    snap: dict[str, Any] = {"scopes": {}, "scope_views": {}}
    seen_sessions: list[str] = []
    for sid, _scope in sessions_scopes:
        if sid not in seen_sessions:
            seen_sessions.append(sid)
    for sid in seen_sessions:
        snap["scopes"][sid] = arc._segments.scan_scopes(sid)
    for sid, scope in sessions_scopes:
        snap["scope_views"][(sid, scope)] = {
            "keys": arc.render_segments_keys(sid, scope),
            "text": arc.render_segment_text(sid, scope),
            "tokens": arc.segment_tokens_by_kind(sid, scope),
        }
    return snap


def _assert_parity(
    local: ARCMemory,
    cte: ARCMemory,
    sessions_scopes: list[tuple[str, str]],
    *,
    label: str,
) -> None:
    """Assert the live-plane read surfaces are EQUAL across the two backends.

    Sessions differ (CTE shared-mem isolation), so we compare backend-agnostic
    views: scan_scopes is compared as the suffix after each backend's own session
    id; render keys/text/tokens are compared verbatim (segment ids never appear in
    these surfaces, only content -- so they are session-id-independent).
    """
    lv = _observable(local, sessions_scopes["local"])
    cv = _observable(cte, sessions_scopes["cte"])

    # scan_scopes parity (per logical session, paired by position)
    for (lsid, _), (csid, _) in zip(
        _unique_sessions(sessions_scopes["local"]),
        _unique_sessions(sessions_scopes["cte"]),
        strict=True,
    ):
        assert lv["scopes"][lsid] == cv["scopes"][csid], (
            f"[{label}] scan_scopes mismatch local={lv['scopes'][lsid]} "
            f"cte={cv['scopes'][csid]}"
        )

    # render keys / text / tokens parity (paired by position)
    for (lsid, lscope), (csid, cscope) in zip(
        sessions_scopes["local"], sessions_scopes["cte"], strict=True
    ):
        lview = lv["scope_views"][(lsid, lscope)]
        cview = cv["scope_views"][(csid, cscope)]
        assert lscope == cscope, f"[{label}] scope pairing bug {lscope!r} vs {cscope!r}"
        assert lview["keys"] == cview["keys"], (
            f"[{label}] render_segments_keys mismatch scope={lscope}\n"
            f"  local={lview['keys']!r}\n  cte  ={cview['keys']!r}"
        )
        assert lview["text"] == cview["text"], (
            f"[{label}] render_segment_text mismatch scope={lscope}\n"
            f"  local={lview['text']!r}\n  cte  ={cview['text']!r}"
        )
        assert lview["tokens"] == cview["tokens"], (
            f"[{label}] segment_tokens_by_kind mismatch scope={lscope}\n"
            f"  local={lview['tokens']!r}\n  cte  ={cview['tokens']!r}"
        )


def _unique_sessions(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for sid, scope in pairs:
        if sid not in seen:
            seen.add(sid)
            out.append((sid, scope))
    return out


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
        sid, scope, "tool_call", {"name": "grep", "args": {"q": "x", "n": 5}},
        step=0, token_count=7,
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
    # deleting an already-tombstoned id is a no-op (must be identical both backends)
    assert arc.delete_segments(sid, scope, [obs[0].id]) == 0
    # deleting an unknown id is a no-op
    assert arc.delete_segments(sid, scope, ["does-not-exist"]) == 0
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
    # backend-identical (scan_scopes is session-scoped on both).
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
    must round-trip byte-identically so the render matches local exactly.
    """
    scope = "agentBin"
    # raw non-UTF-8 bytes stored as a bytes value inside content (msgpack-native)
    arc.append_segment(
        sid, scope, "observation",
        {"text": "ok-text", "raw": b"\x00\x83\xff\x81\xfe", "n": 7},
        step=0, token_count=9,
    )
    # tool_call whose args carry bytes + nested structure
    arc.append_segment(
        sid, scope, "tool_call",
        {"name": "bintool", "args": {"blob": b"\xff\xd8\xff\xe0", "ratio": 0.5}},
        step=0,
    )
    # control chars / newlines / tabs / a high unicode astral char in a text field
    arc.append_segment(
        sid, scope, "thought",
        {"text": "line1\nline2\tTAB\x07BELL\x00NUL emoji-\U0001F9EA-end"},
        step=1,
    )
    return [(sid, scope)]


def _script_large_content(arc: ARCMemory, sid: str) -> list[tuple[str, str]]:
    scope = "agentBig"
    big = "X" * 200_000  # 200 KB text payload in one segment
    arc.append_segment(sid, scope, "observation", {"text": big}, step=0, token_count=50_000)
    arc.append_segment(
        sid, scope, "tool_call",
        {"name": "dump", "args": {"rows": list(range(2_000))}}, step=0,
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
# Parametrized parity battery (clio-core cases are @integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("name,script", ALL_SCRIPTS, ids=[n for n, _ in ALL_SCRIPTS])
def test_backend_parity_op_battery(tmp_path, clio_core_arc, name: str, script: Script) -> None:
    """The same op script on LocalFSStore and ClioCoreStore yields identical live-plane
    renders across every observable read surface."""
    local = _local_arc(tmp_path)
    lsid = _sid(f"{name}_local")
    csid = _sid(f"{name}_cte")

    local_scopes = script(local, lsid)
    clio_core_scopes = script(clio_core_arc, csid)

    # the scripts touch the same scope NAMES on both, only the session id differs
    assert [s for _, s in local_scopes] == [s for _, s in clio_core_scopes], (
        f"[{name}] scripts must touch the same scope names on both backends"
    )

    _assert_parity(
        local, clio_core_arc,
        {"local": local_scopes, "cte": clio_core_scopes},
        label=name,
    )


# ---------------------------------------------------------------------------
# Persistence parity: a SECOND ARCMemory over the same backend sees the same render
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("name,script", ALL_SCRIPTS, ids=[n for n, _ in ALL_SCRIPTS])
def test_persistence_parity_second_arcmemory(
    tmp_path, clio_core_arc, name: str, script: Script
) -> None:
    """Persistence is the other half of the proof: construct a SECOND ARCMemory over
    the same backend (fresh in-memory segment plane) and confirm it renders the live
    plane identically -- for local because it re-reads the dir, for clio-core because the
    in-process runtime is shared-memory."""
    # ---- LOCAL: write, then a cold ARCMemory over the same dir ----
    local_dir = tmp_path / "persist_local"
    store1 = LocalFSStore(str(local_dir))
    local1 = ARCMemory(data_dir=str(tmp_path / "arc_p_local"), store=store1)
    lsid = _sid(f"persist_{name}_local")
    local_scopes = script(local1, lsid)
    # second ARCMemory, brand-new LocalFSStore over the SAME directory (cold reload)
    store2 = LocalFSStore(str(local_dir))
    local2 = ARCMemory(data_dir=str(tmp_path / "arc_p_local2"), store=store2)

    for sid, scope in local_scopes:
        assert local2.render_segments_keys(sid, scope) == local1.render_segments_keys(
            sid, scope
        ), f"[{name}] LOCAL persistence: second ARCMemory render diverged"
        assert local2.render_segment_text(sid, scope) == local1.render_segment_text(
            sid, scope
        )
        assert local2.segment_tokens_by_kind(sid, scope) == local1.segment_tokens_by_kind(
            sid, scope
        )
    for sid in _unique_sessions(local_scopes):
        assert local2._segments.scan_scopes(sid[0]) == local1._segments.scan_scopes(sid[0])

    # ---- clio-core: write, then a second ARCMemory over a fresh ClioCoreStore client ----
    csid = _sid(f"persist_{name}_cte")
    clio_core_scopes = script(clio_core_arc, csid)
    clio_core2 = _clio_core_arc()  # new client into the same in-process shared-memory runtime
    try:
        for sid, scope in clio_core_scopes:
            assert clio_core2.render_segments_keys(sid, scope) == clio_core_arc.render_segments_keys(
                sid, scope
            ), f"[{name}] clio-core persistence: second ARCMemory render diverged"
            assert clio_core2.render_segment_text(sid, scope) == clio_core_arc.render_segment_text(
                sid, scope
            )
            assert clio_core2.segment_tokens_by_kind(sid, scope) == clio_core_arc.segment_tokens_by_kind(
                sid, scope
            )
        for sid in _unique_sessions(clio_core_scopes):
            assert clio_core2._segments.scan_scopes(sid[0]) == clio_core_arc._segments.scan_scopes(sid[0])

        # And local persisted render == cte persisted render (the full cross-backend tie)
        for (lsid_, lscope), (csid_, cscope) in zip(local_scopes, clio_core_scopes, strict=True):
            assert local2.render_segments_keys(lsid_, lscope) == clio_core2.render_segments_keys(
                csid_, cscope
            ), f"[{name}] cross-backend persisted render mismatch on scope {lscope}"
            assert local2.render_segment_text(lsid_, lscope) == clio_core2.render_segment_text(
                csid_, cscope
            )
    finally:
        # clean up clio_core2's view of what clio_core_arc wrote (clio_core_arc fixture also cleans,
        # but clio_core2 wrote nothing new; nothing extra to drop here)
        pass


# ---------------------------------------------------------------------------
# as-of-T parity (the temporal read surface)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_as_of_render_parity(tmp_path, clio_core_arc) -> None:
    """as-of-T reads (pre-edit snapshots) must reconstruct identically on both
    backends. Logical_time is store-assigned and recovered from persisted segments,
    so a divergence here would mean the clock or tombstone semantics differ."""
    local = _local_arc(tmp_path)

    def build(arc: ARCMemory, sid: str) -> tuple[str, int]:
        scope = "agentTime"
        arc.append_segment(sid, scope, "thought", {"text": "t0"}, step=0)
        arc.append_segment(sid, scope, "tool_call", {"name": "a", "args": {}}, step=0)
        arc.append_segment(sid, scope, "observation", {"text": "o0"}, step=0)
        snapshot = max(s.logical_time for s in arc.render_segments(sid, scope))
        # now mutate: delete the observation and append a new iteration
        obs = [s for s in arc.render_segments(sid, scope) if s.content.get("text") == "o0"]
        arc.delete_segments(sid, scope, [obs[0].id])
        arc.append_segment(sid, scope, "thought", {"text": "t1"}, step=1)
        return scope, snapshot

    lsid, csid = _sid("asof_local"), _sid("asof_clio_core")
    lscope, lsnap = build(local, lsid)
    cscope, csnap = build(clio_core_arc, csid)

    # current render parity
    assert local.render_segments_keys(lsid, lscope) == clio_core_arc.render_segments_keys(
        csid, cscope
    )
    # as-of-snapshot render parity: both must still show the pre-delete o0
    lkeys = local._segments.render_keys(lsid, lscope, as_of=lsnap)
    ckeys = clio_core_arc._segments.render_keys(csid, cscope, as_of=csnap)
    assert lkeys == ckeys, f"as-of-T render mismatch local={lkeys} cte={ckeys}"
    assert "o0" in str(lkeys) and "o0" in str(ckeys), "as-of-T must show the pre-delete obs"
    # the snapshots are at the same logical position (3 appends => lt of last)
    assert lsnap == csnap, (
        f"logical_time snapshot diverged across backends: local={lsnap} cte={csnap}"
    )


# ---------------------------------------------------------------------------
# End-to-end parity through the REAL _RetainingReAct loop
# ---------------------------------------------------------------------------


def _scripted_lm() -> DummyLM:
    """A 2-iteration ReAct script: search then submit.

    Speaks the ReActV2 contract (``next_thought`` + typed ``tool_calls``) —
    the SHIPPED default loop since #901; ``make_react_agent`` builds whatever
    ``app._retaining_react_cls()`` resolves, and the old classic-contract
    script (``next_tool_name``/``next_tool_args``) failed the V2 adapter parse
    so the loop never ran its tool (#914).
    """
    return DummyLM(
        [
            {
                "next_thought": "search first",
                "tool_calls": {"tool_calls": [{"name": "search", "args": {"q": "alpha"}}]},
            },
            {
                "next_thought": "done",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "FINAL_ANSWER"}}]},
            },
            {"reasoning": "because", "answer": "FINAL_ANSWER"},
        ]
    )


def _run_real_loop(arc: ARCMemory, sid: str, scope: str) -> None:
    """Drive the REAL _RetainingReAct loop so the live plane is written by the actual
    machinery (not direct append_segment calls)."""
    agent = make_react_agent()
    lm = _scripted_lm()
    with live_plane_context(arc, session=sid, scope=scope):
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            agent(question="find alpha")


@pytest.mark.integration
def test_real_react_loop_parity(tmp_path, clio_core_arc) -> None:
    """Drive the actual ``_RetainingReAct`` loop against both backends with the same
    scripted LM; the trajectory it WROTE to the live plane must render identically.

    This proves parity on the real write path, including the loop's own
    'reset prior segments at the start of forward' behavior.
    """
    local = _local_arc(tmp_path)
    scope = "agentA"
    lsid, csid = _sid("react_local"), _sid("react_clio_core")

    _run_real_loop(local, lsid, scope)
    _run_real_loop(clio_core_arc, csid, scope)

    lkeys = local.render_segments_keys(lsid, scope)
    ckeys = clio_core_arc.render_segments_keys(csid, scope)
    assert lkeys == ckeys, (
        f"real-loop render diverged across backends\n  local={lkeys}\n  cte  ={ckeys}"
    )
    assert local.render_segment_text(lsid, scope) == clio_core_arc.render_segment_text(csid, scope)
    assert local.segment_tokens_by_kind(lsid, scope) == clio_core_arc.segment_tokens_by_kind(
        csid, scope
    )
    # sanity: the loop actually produced the search observation on both
    assert "SEARCH_RESULT" in str(lkeys)
    assert "SEARCH_RESULT" in str(ckeys)


# ---------------------------------------------------------------------------
# A local-only smoke so the unit lane (-m "not integration") still asserts the
# battery runs end-to-end on at least one backend (guards script correctness).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,script", ALL_SCRIPTS, ids=[n for n, _ in ALL_SCRIPTS])
def test_local_battery_runs(tmp_path, name: str, script: Script) -> None:
    """Every script executes cleanly on LocalFSStore and renders SOME stable view --
    runs in the binding-free unit lane and guards the scripts themselves."""
    local = _local_arc(tmp_path, suffix=name)
    sid = _sid(f"local_only_{name}")
    scopes = script(local, sid)
    assert scopes, f"[{name}] script returned no scopes"
    for s, scope in scopes:
        # idempotent re-render is stable
        assert local.render_segments_keys(s, scope) == local.render_segments_keys(s, scope)
        # tokens_by_kind only counts live segments and is non-negative
        toks = local.segment_tokens_by_kind(s, scope)
        assert all(v >= 0 for v in toks.values())
