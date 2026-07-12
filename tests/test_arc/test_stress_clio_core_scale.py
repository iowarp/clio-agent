"""clio-core backend SCALE + robustness stress tests for the ARC live context plane.

These are ``integration`` tests: they drive the REAL in-process clio-core
runtime (``make_arc_store(backend="cte")``) — no mocking of ``ClioCoreStore``,
``SegmentStore``, or ``ARCMemory``. They exist to buy FULL FAITH in clio-core
backend before a release by exercising it at a scale the unit lane never does:

    * hundreds of segments fanned across many ``(session, scope)`` pairs;
    * large per-segment payloads (10-100 KB) that stress the base64-wrap path;
    * a ``scan`` over a single big scope's record;
    * ``clear()`` wiping every kind across the whole store;
    * the base64 binary guard (non-UTF-8 msgpack bytes) at scale;
    * repeated ``ARCMemory`` construction reusing the one-time-init clio-core runtime.

Correctness is asserted against the same in-memory truth the writer produced,
and each phase is wall-clock-bounded so a pathological slowdown fails loudly
instead of hanging.

The clio-core runtime is process-global and DRAM-backed: data put by one
``make_arc_store(backend="cte")`` is visible to the next within the same
process, and ``clear()`` wipes ALL kinds for ALL sessions. To stay hermetic
under a shared pytest process, every test namespaces its records with a
unique-per-test session prefix, and the destructive ``clear()`` test owns its
setup/teardown so it never races another test's data.

Run:
    cd <worktree> && CLIO_ALLOWED_ROOTS="/tmp:$PWD" \
        uv run python -m pytest tests/test_arc/test_stress_clio_core_scale.py \
        -o addopts="" -q -p no:cacheprovider -m integration
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path

import filelock
import msgspec
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.replay import reconstruct_arc_segments
from clio_agent.arc.schema import decode_segments, encode_segments
from clio_agent.arc.segments import SegmentStore, segments_to_keys
from clio_agent.arc.storage import ARC_KINDS, make_arc_store

pytestmark = pytest.mark.integration


# ---- serial-only enforcement ----------------------------------------------
#
# The clio-core runtime backing these tests is a machine-global shared daemon
# (spawned once, DRAM-backed, attached by every process), and ``ClioCoreStore.clear()``
# is TOTAL across all kinds/sessions. Under pytest-xdist each worker is a
# SEPARATE process attached to the SAME daemon, so the two destructive
# ``clear()`` tests below would wipe records that sibling tests
# (roundtrip / binary-guard / scope-isolation) are mid-way reading in another
# worker — producing "roundtrip mismatch", "binary guard failed", and
# "scan not empty after clear" only under parallelism. This module is
# serial-only by design (see the module docstring); every test passes serially.
#
# ``xdist_group`` cannot enforce that here: it is honored only under
# ``--dist loadgroup``, whereas the suite's parallel lane runs plain ``-n auto``
# (``--dist load``), under which the marker is ignored and tests still spread
# across workers. A cross-process file lock enforces mutual exclusion
# regardless of the dist mode: every test in this module holds the lock for its
# full duration, so no two ever run concurrently across xdist workers.
_CLIO_CORE_GLOBAL_LOCK = filelock.FileLock(
    str(Path(tempfile.gettempdir()) / "clio_clio_core_stress_scale.lock")
)


@pytest.fixture(autouse=True)
def _serialize_clio_core_global_daemon():
    """Serialize every test in this module across processes (xdist workers).

    The tests share one machine-global clio-core daemon and two of them call the
    TOTAL ``clear()``; running any concurrently corrupts the others' data. The
    lock makes the module effectively serial no matter how the run is sharded.
    """
    with _CLIO_CORE_GLOBAL_LOCK:
        yield


# ---- helpers --------------------------------------------------------------


def _clio_core_store():
    """Build a real clio-core-backed ARCStore, skipping if the binding/runtime is
    genuinely unavailable (graceful-degradation falls back to LocalFSStore, which
    would make these "clio-core" tests silently test the wrong backend)."""
    store = make_arc_store(backend="cte")
    if type(store).__name__ != "ClioCoreStore":
        pytest.skip(
            "clio-core backend unavailable (fell back to %s); "
            "build iowarp-core to run the clio-core scale lane" % type(store).__name__
        )
    return store


def _uid(tag: str) -> str:
    """A collision-proof id so tests sharing the process-global CTE namespace
    never read each other's records."""
    return f"{tag}-{uuid.uuid4().hex[:12]}"


# ---- 1. raw ClioCoreStore: hundreds of records across many (kind, name) keys ----


def test_clio_core_many_records_roundtrip_and_scan():
    """Put hundreds of distinct records, then read each back byte-identically and
    confirm a prefix scan returns exactly the right set. This is the core
    durability contract at fan-out scale."""
    store = _clio_core_store()
    pfx = _uid("manyrec")  # unique session-like prefix for this test's records
    # Fan-out scale: enough records across many scopes to exercise the durability
    # + prefix-scan contract, sized so the whole @integration file completes well
    # inside the harness window (every op is a separate CTE IPC round-trip, so wall
    # time is ~linear in n; an over-large n made the file time out and leak orphans
    # into the shared daemon, which is what made clear() pathological — see file docstring).
    n = 150
    expected: dict[str, bytes] = {}

    t0 = time.time()
    for i in range(n):
        name = f"{pfx}__scope{i % 37}~k{i}"  # spread across 37 pseudo-scopes
        # payload size varies so we exercise small and chunky blobs in one pass
        payload = msgspec.msgpack.encode({"i": i, "blob": os.urandom(64 + (i % 11) * 512)})
        store.put("segments", name, payload)
        expected[name] = payload
    put_s = time.time() - t0
    assert put_s < 60.0, f"putting {n} records took {put_s:.1f}s (too slow)"

    # Every record reads back byte-identically.
    t0 = time.time()
    for name, payload in expected.items():
        got = store.get("segments", name)
        assert got == payload, f"roundtrip mismatch for {name}"
    get_s = time.time() - t0
    assert get_s < 60.0, f"reading {n} records took {get_s:.1f}s (too slow)"

    # Prefix scan returns exactly this test's records (and nothing from siblings).
    scanned = dict(store.scan("segments", prefix=f"{pfx}__"))
    assert set(scanned) == set(expected), "scan set must equal the put set"
    assert all(scanned[k] == expected[k] for k in expected), "scan must return live bytes"

    # exists() agrees; a never-written sibling is absent.
    assert store.exists("segments", next(iter(expected)))
    assert not store.exists("segments", f"{pfx}__nope~missing")

    # cleanup this test's footprint (do NOT clear() — that nukes other tests)
    for name in expected:
        store.delete("segments", name)
    assert list(store.scan("segments", prefix=f"{pfx}__")) == []


# ---- 2. large payloads (10-100 KB) through the base64-wrap path -----------


def test_clio_core_large_payloads_roundtrip():
    """The base64 wrap inflates payloads ~4/3x; verify 10-100 KB blobs (incl.
    non-UTF-8 bytes) survive byte-identically and within a sane time budget."""
    store = _clio_core_store()
    pfx = _uid("big")
    sizes = [10_000, 25_000, 50_000, 75_000, 100_000]
    payloads: dict[str, bytes] = {}

    t0 = time.time()
    for idx, size in enumerate(sizes):
        # raw os.urandom is non-UTF-8 with overwhelming probability — exactly the
        # bytes the base64 guard exists for, now at 10-100 KB.
        raw = os.urandom(size)
        body = msgspec.msgpack.encode({"size": size, "raw": raw, "tag": "x" * 1000})
        name = f"{pfx}__big{idx}"
        store.put("segments", name, body)
        payloads[name] = body
    dur = time.time() - t0
    assert dur < 30.0, f"writing {len(sizes)} large payloads took {dur:.1f}s"

    for name, body in payloads.items():
        got = store.get("segments", name)
        assert got == body, f"large payload {name} did not round-trip byte-identically"
        # the embedded non-UTF-8 bytes survived the UTF-8-decoding GetBlob path
        decoded = msgspec.msgpack.decode(got)
        assert len(decoded["raw"]) == decoded["size"]

    for name in payloads:
        store.delete("segments", name)


# ---- 3. base64 binary guard at scale --------------------------------------


def test_clio_core_base64_binary_guard_at_scale():
    """Hammer the base64 guard: hundreds of payloads each crafted to contain the
    byte ranges that a naive UTF-8 GetBlob would choke on (0x00, 0x80-0xFF, lone
    continuation bytes, invalid start bytes). All must round-trip identically."""
    store = _clio_core_store()
    pfx = _uid("guard")
    # Bytes that are individually invalid or dangerous for UTF-8 decoding.
    hostile = bytes(range(256)) + b"\x80\x81\xff\xfe\xc0\xc1\xed\xa0\x80"
    # Hundreds of hostile-byte payloads is plenty to hammer the base64 guard; sized
    # to keep the file inside the harness window (each put/get is a CTE IPC round-trip).
    n = 100
    expected: dict[str, bytes] = {}
    for i in range(n):
        # rotate the hostile bytes + sprinkle randomness so no two are identical
        body = hostile[i % len(hostile) :] + hostile[: i % len(hostile)] + os.urandom(32)
        name = f"{pfx}__g{i}"
        store.put("segments", name, body)
        expected[name] = body

    for name, body in expected.items():
        assert store.get("segments", name) == body, f"binary guard failed for {name}"

    for name in expected:
        store.delete("segments", name)


# ---- 4. SegmentStore on clio-core: many segments in one big scope + scan ---------


def test_clio_core_segment_store_big_scope_render_and_scan():
    """Drive the real SegmentStore over clio-core: write a long ReAct-shaped trajectory
    into one scope (so the whole scope batches into a single large CTE record),
    then verify the dspy render is correct, a cold reload reconstructs it
    identically, and scan_scopes finds the scope."""
    store = _clio_core_store()
    ss = SegmentStore(store)
    sid = _uid("bigscope")
    scope = "agentA/expertB"
    iters = 40  # 120 segments in one scope record (long trajectory, fits harness window)

    t0 = time.time()
    for step in range(iters):
        ss.append(sid, scope, "thought", {"text": f"think{step}"}, step=step)
        ss.append(sid, scope, "tool_call", {"name": "grep", "args": {"q": step}}, step=step)
        ss.append(sid, scope, "observation", {"text": f"obs{step}"}, step=step)
    write_s = time.time() - t0
    assert write_s < 60.0, f"writing {iters * 3} segments took {write_s:.1f}s"

    live = ss.render(sid, scope)
    assert len(live) == iters * 3
    keys = ss.render_keys(sid, scope)
    # gapless dspy projection over the whole big scope
    assert keys["thought_0"] == "think0"
    assert keys[f"thought_{iters - 1}"] == f"think{iters - 1}"
    assert keys[f"observation_{iters - 1}"] == f"obs{iters - 1}"
    assert len(keys) == iters * 4  # thought+tool_name+tool_args+observation per iter

    # Cold reload from the SAME clio-core runtime reconstructs byte-identically.
    ss2 = SegmentStore(make_arc_store(backend="cte"))
    assert ss2.render_keys(sid, scope) == keys
    assert ss2.scan_scopes(sid) == [scope]
    assert ss2.scan_scopes(sid, "agentA/") == [scope]

    # The big scope is one physical CTE record; decoding it yields all segments.
    raw = store.get("segments", SegmentStore._record_name(sid, scope))
    assert raw is not None
    assert len(decode_segments(raw)) == iters * 3

    # cleanup
    store.delete("segments", SegmentStore._record_name(sid, scope))


# ---- 5. SegmentStore on clio-core: fan-out across many (session, scope) pairs ----


def test_clio_core_segment_store_many_scopes_isolation():
    """Many scopes across several sessions: each scope is an independent CTE
    record; render is correctly isolated and scan_scopes is session-scoped."""
    store = _clio_core_store()
    ss = SegmentStore(store)
    base = _uid("fanout")
    sessions = [f"{base}-s{i}" for i in range(5)]
    scopes = [f"agent{j}/expert{j}" for j in range(8)]  # 5*8 = 40 scope records

    for sid in sessions:
        for scope in scopes:
            ss.append(sid, scope, "thought", {"text": f"{sid}|{scope}|think"})
            ss.append(sid, scope, "observation", {"text": f"{sid}|{scope}|obs"})

    # Each scope renders ONLY its own content (no cross-scope/session bleed).
    for sid in sessions:
        found = ss.scan_scopes(sid)
        assert found == sorted(scopes), f"session {sid} scope set wrong"
        for scope in scopes:
            txt = str(ss.render_keys(sid, scope))
            assert f"{sid}|{scope}|think" in txt
            # a different session's marker must not appear here
            other = sessions[(sessions.index(sid) + 1) % len(sessions)]
            assert f"{other}|{scope}|" not in txt

    # cleanup every record this test created
    for sid in sessions:
        for scope in scopes:
            store.delete("segments", SegmentStore._record_name(sid, scope))


# ---- 6. ARCMemory live plane on clio-core: ops + replay equivalence at scale -----


def test_clio_core_arcmemory_live_plane_ops_and_replay():
    """End-to-end through ARCMemory on clio-core: write a trajectory, capture op events
    via an injected op_logger, apply delete + summarize, and prove the durable
    Trace replay reconstructs the exact same live render (the replayability
    contract) — at multi-iteration scale, on the real backend."""
    arc = ARCMemory(store=_clio_core_store())
    sid = _uid("liveclio_core")
    scope = "agentA"

    events: list[dict] = []

    def op_logger(op, session_id, scope, **kw):
        ev = {
            "event_type": "arc.op",
            "event_id": f"ev{len(events) + 1}",
            "payload": {"op": op, **kw},
        }
        events.append(ev)
        return ev

    arc.set_segment_op_logger(op_logger)

    n_iters = 40
    for step in range(n_iters):
        arc.append_segment(sid, scope, "thought", {"text": f"t{step}"}, step=step)
        arc.append_segment(
            sid, scope, "tool_call", {"name": "lookup", "args": {"i": step}}, step=step
        )
        arc.append_segment(sid, scope, "observation", {"text": f"o{step}"}, step=step)

    live = arc.render_segments(sid, scope)
    assert len(live) == n_iters * 3

    # delete the first iteration's three segments out-of-band
    first_iter_ids = [s.id for s in live if s.step == 0]
    assert arc.delete_segments(sid, scope, first_iter_ids) == 3

    # summarize the next iteration's segments into one summary
    second_iter_ids = [s.id for s in arc.render_segments(sid, scope) if s.step == 1]
    arc.summarize_segments(sid, scope, second_iter_ids, {"text": "SUMMARY_1"})

    live_after = arc.render_segments(sid, scope)
    keys_after = arc.render_segments_keys(sid, scope)
    # only the string-valued keys (thought/observation/tool_name); tool_args are
    # dicts (unhashable) and irrelevant to these text-presence checks
    values = {v for v in keys_after.values() if isinstance(v, str)}
    # deleted iteration's exact texts gone (compare exact values, not substrings:
    # "t1" is a substring of "t10".."t19", so substring checks would false-positive)
    assert "t0" not in values and "o0" not in values  # deleted iteration gone
    assert "SUMMARY_1" in values  # summary present
    assert "t1" not in values and "o1" not in values  # summarized originals gone
    # but the un-touched later iterations survive verbatim
    assert "t39" in values and "o39" in values

    # The Trace replay must reconstruct the identical live render.
    replayed = reconstruct_arc_segments(events, scope_filter=scope)
    assert segments_to_keys(replayed) == segments_to_keys(live_after)

    # token attribution over the live set is well-formed
    tbk = arc.segment_tokens_by_kind(sid, scope)
    assert set(tbk).issubset(
        {"thought", "tool_call", "observation", "summary", "system", "user", "tool_def"}
    )

    # cleanup
    arc._store.delete("segments", SegmentStore._record_name(sid, scope))


# ---- 7. repeated ARCMemory construction reusing the one-time clio-core runtime ----


def test_clio_core_repeated_arcmemory_construction_shares_runtime(tmp_path):
    """The clio-core runtime inits exactly ONCE per process (``_initialized`` guard).
    Construct ARCMemory many times over fresh clio-core stores; each must boot fast
    (no re-init), and a record written by an earlier instance must be visible to
    a later one (shared DRAM runtime)."""
    sid = _uid("reuse")
    scope = "agentA"

    # First instance writes a marker.
    arc0 = ARCMemory(data_dir=str(tmp_path / "a0"), store=_clio_core_store())
    arc0.append_segment(sid, scope, "thought", {"text": "MARKER_REUSE"}, step=0)

    # Many subsequent constructions must each be cheap (runtime already up).
    t0 = time.time()
    instances = []
    for i in range(10):
        arc_i = ARCMemory(data_dir=str(tmp_path / f"a{i + 1}"), store=make_arc_store(backend="cte"))
        instances.append(arc_i)
    build_s = time.time() - t0
    # 10 constructions reusing the same runtime should be well under the
    # single ~0.5s settle of a first init; allow generous headroom.
    assert build_s < 10.0, f"10 ARCMemory builds took {build_s:.1f}s (runtime re-init?)"

    # The marker written by arc0 is visible through a later instance.
    assert "MARKER_REUSE" in str(instances[-1].render_segments_keys(sid, scope))

    # cleanup
    arc0._store.delete("segments", SegmentStore._record_name(sid, scope))


# ---- 8. clear() wipes every kind across the whole store --------------------


def test_clio_core_clear_wipes_all_kinds():
    """``clear()`` deletes all blobs across every ARC_KIND. This test OWNS the
    store state for its window: it writes one record into EVERY kind, asserts
    they exist, clears, then asserts the whole store is empty across all kinds.

    NOTE: clear() is global to the process-shared clio-core runtime, so this test must
    not run concurrently with another that relies on persisted CTE data. Under
    the default serial pytest run that holds.
    """
    store = _clio_core_store()
    marker = _uid("clearmark").encode()

    # Seed exactly one record in every kind.
    for kind in ARC_KINDS:
        store.put(kind, f"clrtest__{kind}", marker)
    for kind in ARC_KINDS:
        assert store.exists(kind, f"clrtest__{kind}"), f"seed missing in kind {kind}"

    store.clear()

    # Every kind is now empty (clear is total, not per-kind/per-prefix).
    for kind in ARC_KINDS:
        assert not store.exists(kind, f"clrtest__{kind}"), f"clear left data in {kind}"
        assert list(store.scan(kind)) == [], f"scan over {kind} not empty after clear"


# ---- 9. ARCMemory.clear_all() drops the live plane on clio-core ------------------


def test_clio_core_arcmemory_clear_all_resets_live_plane():
    """``ARCMemory.clear_all()`` must wipe the clio-core-backed segment store AND drop
    the in-memory plane, so a fresh render is empty. Owns store state (clear_all
    calls store.clear())."""
    arc = ARCMemory(store=_clio_core_store())
    sid = _uid("clearall")
    scope = "agentA"
    for i in range(20):
        arc.append_segment(sid, scope, "thought", {"text": f"x{i}"}, step=i)
    assert len(arc.render_segments(sid, scope)) == 20

    arc.clear_all()

    # In-memory plane dropped and store wiped: nothing renders.
    assert arc.render_segments(sid, scope) == []
    # a brand-new memory over the same runtime also sees nothing
    arc2 = ARCMemory(store=make_arc_store(backend="cte"))
    assert arc2.render_segments(sid, scope) == []


# ---- 10. empty-payload guard (documents a real CTE edge) -------------------


def test_clio_core_round_trips_minimal_segment_record():
    """The smallest real ARC payload is a 1-byte msgpack empty list (``encode_segments([])``
    = ``b"\\x90"``); confirm even a 1-byte blob round-trips. (CTE rejects a
    *zero*-length blob, but ARC never writes one — the minimal real record is 1
    byte.)"""
    store = _clio_core_store()
    name = f"{_uid('minimal')}__k"
    one_byte = encode_segments([])
    assert one_byte == b"\x90"
    store.put("segments", name, one_byte)
    assert store.get("segments", name) == one_byte
    store.delete("segments", name)
