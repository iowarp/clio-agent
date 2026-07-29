"""Thread-safety / concurrency stress tests for the ARC live-context SegmentStore.

The store guards all four ops + reads with a single ``threading.RLock``. This
module hammers that lock from many threads (``ThreadPoolExecutor``) under several
contention shapes and asserts the invariants a RELEASE depends on:

    * NO LOST WRITES — every appended/inserted segment is durably present.
    * NO CORRUPTION — every persisted Segment round-trips, scope tags are correct,
      ids are unique, no torn msgpack records.
    * MONOTONIC logical_time — the store-wide ``_new_lt()`` clock issues every tick
      exactly once under the lock: no two CREATED segments share a creation tick,
      and total ticks issued match the per-op tick accounting (append/insert/
      summarize = 1 tick; delete = 1 tick per tombstoned segment; summarize REUSES
      its own creation tick as the replaced segments' tombstone time — so the
      creation clock is unique-but-not-gapless once deletes consume ticks).
    * RENDER CONSISTENCY — a concurrent ``render`` always returns a coherent,
      correctly-ordered live view (never a half-applied summarize/delete).
    * EXACT COUNTS — final live/tombstoned counts equal what the recorded ops imply.

All tests use the REAL :class:`SegmentStore` over a REAL :class:`LocalFSStore`
(fast disk backend) and the REAL :class:`ARCMemory` pass-throughs — no mocking of
src code. Determinism of *thread scheduling* is not assumed; the assertions are
invariants that must hold under ANY interleaving, so a flake here is a real bug.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import decode_segments
from clio_agent.arc.segments import SegmentStore, segments_to_keys
from clio_agent.arc.storage import LocalFSStore

# #735 flake-hunt: ARC concurrency invariants run under xdist load x3.
pytestmark = pytest.mark.concurrency

SID = "stress-sess"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fresh_store(tmp_path) -> SegmentStore:
    """A SegmentStore over a fresh LocalFSStore (no op_logger)."""
    return SegmentStore(LocalFSStore(str(tmp_path / "arc")))


def _logging_store(tmp_path) -> tuple[SegmentStore, list[dict], threading.Lock]:
    """A SegmentStore whose op_logger is itself thread-safe and records every op.

    The logger runs UNDER the store's RLock (``_finish_write`` holds it), so a
    plain list append would already be serialized — but we guard it anyway and
    hand back the guard so the test can read the log safely afterward.
    """
    logged: list[dict] = []
    guard = threading.Lock()
    counter = {"n": 0}

    def op_logger(op, session_id, scope, **kw):
        with guard:
            counter["n"] += 1
            event_id = f"ev{counter['n']}"
            logged.append({"event_id": event_id, "op": op, "scope": scope, **kw})
        return {"event_id": event_id}

    return SegmentStore(LocalFSStore(str(tmp_path / "arc")), op_logger=op_logger), logged, guard


def _all_persisted_segments(store: LocalFSStore, session_id: str, scope: str):
    """Decode the persisted record for (session_id, scope) straight off disk —
    bypassing the store's in-memory copy to prove durability / no torn writes."""
    name = SegmentStore._record_name(session_id, scope)
    raw = store.get("segments", name)
    return decode_segments(raw) if raw else []


def _assert_render_ordering(live) -> None:
    """CONCURRENCY-SAFE render invariants: sorted by (order, logical_time) and no
    duplicate ids.

    These read only fields that are IMMUTABLE for a Segment's lifetime
    (``order``, ``logical_time``, ``id`` are stamped at creation and never mutated
    by any op), so they are safe to assert on the shared Segment objects after the
    store releases its lock — even while other threads mutate the plane. NB: we do
    NOT re-read ``status`` here: ``render`` returns references to the live
    SegmentStore objects, and a concurrent delete/summarize can flip ``status`` to
    "tombstoned" on those same objects after ``render`` returns. ``render`` upheld
    its contract (it returned the segments live AT THE CALL, under the lock); a
    later out-of-lock ``status`` read is a reader-side TOCTOU, not a store fault.
    Production reads (``render_keys``->``segments_to_keys``) only touch immutable
    fields (``kind``/``content``/``order``), so this distinction is faithful to how
    the live plane is actually consumed.
    """
    keys = [(s.order, s.logical_time) for s in live]
    assert keys == sorted(keys), "render not sorted by (order, logical_time)"
    ids = [s.id for s in live]
    assert len(ids) == len(set(ids)), "render returned duplicate segment ids"


def _assert_render_well_formed(live) -> None:
    """QUIESCENT render invariants: ordering/uniqueness PLUS all-live. The
    ``status`` check is only valid when no writer can be mutating the plane
    concurrently (i.e. after all writer threads have joined)."""
    _assert_render_ordering(live)
    assert all(s.status == "live" for s in live), "render leaked a tombstoned segment"


# --------------------------------------------------------------------------- #
# 1. Concurrent appends to the SAME scope: no lost writes, exact count, clock OK
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("n_threads,per_thread", [(16, 50)])
def test_concurrent_append_same_scope_no_lost_writes(tmp_path, n_threads, per_thread):
    ss = _fresh_store(tmp_path)
    scope = "agentA/expertB"
    total = n_threads * per_thread

    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> list[int]:
        barrier.wait()  # release all threads as simultaneously as possible
        lts: list[int] = []
        for i in range(per_thread):
            seg = ss.append(
                SID, scope, "thought", {"text": f"t{tid}-{i}"}, step=i, token_count=1
            )
            lts.append(seg.logical_time)
        return lts

    returned_lts: list[int] = []
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        for fut in as_completed([ex.submit(worker, t) for t in range(n_threads)]):
            returned_lts.extend(fut.result())

    # ---- exact count, in memory and on disk ----
    live = ss.render(SID, scope)
    assert len(live) == total, f"lost writes: {len(live)} live, expected {total}"
    assert len(ss.list_segments(SID, scope)) == total

    persisted = _all_persisted_segments(ss._store, SID, scope)
    assert len(persisted) == total, "persisted count != appended count (lost write on disk)"

    # ---- no lost writes: every (tid,i) text is present exactly once ----
    texts = Counter(s.content["text"] for s in live)
    assert all(c == 1 for c in texts.values()), "duplicate or missing segment content"
    assert len(texts) == total

    # ---- monotonic, gapless, unique logical_time across ALL writes ----
    all_lts = sorted(s.logical_time for s in live)
    assert len(set(all_lts)) == total, "logical_time collision (RLock failed to serialize)"
    assert all_lts == list(range(1, total + 1)), (
        "logical_time not a gapless 1..N sequence — a write read a stale _next_lt"
    )
    # the lts returned to the workers must be exactly the set on disk
    assert sorted(returned_lts) == all_lts

    # ---- order values unique (append uses max(order)+1 under the lock) ----
    orders = [s.order for s in live]
    assert len(set(orders)) == total, "order collision under concurrent append"

    _assert_render_well_formed(live)


# --------------------------------------------------------------------------- #
# 2. Concurrent appends to DIFFERENT scopes: per-scope isolation + shared clock
# --------------------------------------------------------------------------- #


def test_concurrent_append_different_scopes_isolated(tmp_path):
    ss = _fresh_store(tmp_path)
    n_scopes = 12
    per_scope = 40
    scopes = [f"agent{n}/expert" for n in range(n_scopes)]

    barrier = threading.Barrier(n_scopes)

    def worker(scope: str) -> None:
        barrier.wait()
        for i in range(per_scope):
            ss.append(SID, scope, "thought", {"text": f"{scope}:{i}"}, step=i)

    with ThreadPoolExecutor(max_workers=n_scopes) as ex:
        list(ex.map(worker, scopes))

    seen_lts: list[int] = []
    for scope in scopes:
        live = ss.render(SID, scope)
        # exact per-scope count: no cross-scope corruption / leakage
        assert len(live) == per_scope, f"scope {scope}: {len(live)} != {per_scope}"
        # every segment belongs to its own scope (no tag bleed)
        assert all(s.scope == scope for s in live)
        assert all(s.content["text"].startswith(f"{scope}:") for s in live)
        # render-position dict is gapless 0..per_scope-1 thoughts
        keys = segments_to_keys(live)
        assert list(keys.keys()) == [f"thought_{i}" for i in range(per_scope)]
        seen_lts.extend(s.logical_time for s in live)
        _assert_render_well_formed(live)

    # the store-wide clock spans EVERY scope's writes uniquely and gaplessly
    total = n_scopes * per_scope
    assert sorted(seen_lts) == list(range(1, total + 1)), (
        "store-wide logical_time clock collided/gapped across scopes"
    )


# --------------------------------------------------------------------------- #
# 3. Interleaving append / delete / summarize / render: count accounting holds
# --------------------------------------------------------------------------- #


def test_interleaved_ops_count_accounting(tmp_path):
    """Mixed writers (appenders) + mutators (deleters, summarizers) + readers
    (render / render_keys) on ONE scope. Reads must never observe corruption,
    and the final ledger must balance exactly:

        live_now == total_created - total_tombstoned

    where created counts both appends AND the summary segments, and tombstoned
    counts deletes AND summarize-replacements.
    """
    ss, logged, guard = _logging_store(tmp_path)
    scope = "agentA/mix"
    n_appenders = 8
    per_appender = 40

    stop = threading.Event()
    # The op log (written under the store's RLock) is the single source of truth
    # for the count ledger, so the workers stay thin — no test-side accumulators.

    def appender(tid: int) -> None:
        for i in range(per_appender):
            ss.append(SID, scope, "thought", {"text": f"a{tid}-{i}"}, step=i)

    def deleter() -> None:
        while not stop.is_set():
            live = ss.render(SID, scope)
            if len(live) >= 4:
                victim = live[len(live) // 2]
                ss.delete(SID, scope, [victim.id])

    def summarizer() -> None:
        while not stop.is_set():
            live = ss.render(SID, scope)
            if len(live) >= 6:
                # summarize the oldest 3 live segments into one summary segment
                victims = live[:3]
                ss.summarize(SID, scope, [v.id for v in victims], {"text": "SUMMARY"})

    def reader() -> None:
        while not stop.is_set():
            live = ss.render(SID, scope)
            # concurrency-safe checks only (immutable fields) — writers are live
            _assert_render_ordering(live)
            # render_keys must always be a coherent gapless dspy dict — this is the
            # PRODUCTION read path (_format_trajectory), so a torn/half-applied op
            # would show up here as an index gap.
            keys = ss.render_keys(SID, scope)
            _assert_keys_gapless(keys)

    futures = []
    with ThreadPoolExecutor(max_workers=n_appenders + 4) as ex:
        for t in range(n_appenders):
            futures.append(ex.submit(appender, t))
        del_fut = ex.submit(deleter)
        sum_fut = ex.submit(summarizer)
        read_fut1 = ex.submit(reader)
        read_fut2 = ex.submit(reader)
        # wait for all appenders to finish, then stop the long-running tasks
        for f in futures:
            f.result()
        stop.set()
        for f in (del_fut, sum_fut, read_fut1, read_fut2):
            f.result()

    # ---- precise ledger from the op log (source of truth for tombstones) ----
    # op_logger ran under the store lock, so the log is a serialized record of
    # every applied op with the exact segments it wrote/tombstoned.
    with guard:
        log_copy = list(logged)

    created_from_log = 0
    tombstoned_from_log = 0
    for ev in log_copy:
        written = ev.get("segments_written") or []
        tomb = ev.get("segments_tombstoned") or []
        created_from_log += len(written)
        tombstoned_from_log += len(tomb)

    live = ss.render(SID, scope)
    all_segs = ss.list_segments(SID, scope, include_tombstoned=True)

    # created total (appends + summaries) matches what the log recorded
    assert created_from_log == len(all_segs), (
        f"created mismatch: log says {created_from_log}, store has {len(all_segs)}"
    )
    # exact ledger balance: live == created - tombstoned
    assert len(live) == created_from_log - tombstoned_from_log, (
        f"ledger imbalance: live={len(live)} != "
        f"created={created_from_log} - tombstoned={tombstoned_from_log}"
    )
    # tombstoned count on the actual segments matches the log
    tombstoned_now = sum(1 for s in all_segs if s.status == "tombstoned")
    assert tombstoned_now == tombstoned_from_log

    # all originally-appended texts are accounted for (live OR tombstoned), never lost
    appended_texts = {
        s.content.get("text") for s in all_segs if s.kind == "thought"
    }
    expected = {f"a{t}-{i}" for t in range(n_appenders) for i in range(per_appender)}
    assert expected <= appended_texts, "an appended segment vanished (lost write)"

    # Clock integrity under mixed ops. The store-wide ``_new_lt()`` clock hands out
    # one tick per call; ``_next_lt - 1`` is the high-water mark of ticks issued.
    # Tick accounting per op (from the SegmentStore source):
    #   * append / insert  -> 1 tick  (the new segment's creation logical_time)
    #   * summarize        -> 1 tick  (the summary's creation lt; that SAME tick is
    #                         reused as tombstoned_at for every replaced segment,
    #                         so a summary does NOT consume extra ticks for them)
    #   * delete           -> 1 tick PER tombstoned segment (each gets a fresh
    #                         tombstoning lt via _new_lt() inside the loop)
    # So: ticks_issued == (#append + #insert + #summarize) + (#delete-tombstoned).
    # If the RLock failed to serialize, _new_lt would double-issue or skip and this
    # exact equality would break.
    creation_lts = [s.logical_time for s in all_segs]
    assert len(set(creation_lts)) == len(creation_lts), (
        "duplicate creation logical_time — two writes shared a tick (RLock race)"
    )
    summary_creates = 0
    delete_tombstones = 0
    for ev in log_copy:
        op = ev.get("op")
        if op in ("append", "insert", "summarize"):
            summary_creates += len(ev.get("segments_written") or [])
        if op == "delete":
            delete_tombstones += len(ev.get("segments_tombstoned") or [])
    expected_ticks = summary_creates + delete_tombstones
    assert ss._next_lt - 1 == expected_ticks, (
        f"clock tick accounting off: issued={ss._next_lt - 1} != "
        f"expected={expected_ticks} (creates={summary_creates}, "
        f"delete_tombstones={delete_tombstones}) — clock skipped or double-issued"
    )
    # every creation tick is within the issued range, monotonic, no negative/zero
    assert min(creation_lts) >= 1 and max(creation_lts) <= ss._next_lt - 1

    _assert_render_well_formed(live)


def _assert_keys_gapless(keys: dict) -> None:
    """The dspy trajectory dict must have contiguous iteration indices 0..k with
    no gap (stock dspy never has index holes; render recomputes positions)."""
    import re

    idxs = set()
    for k in keys:
        m = re.match(r"(?:thought|tool_name|tool_args|observation)_(\d+)$", k)
        if m:
            idxs.add(int(m.group(1)))
    if idxs:
        assert idxs == set(range(max(idxs) + 1)), f"gap in trajectory indices: {sorted(idxs)}"


# --------------------------------------------------------------------------- #
# 4. Concurrent delete of the SAME ids: tombstoned-exactly-once accounting
# --------------------------------------------------------------------------- #


def test_concurrent_delete_same_ids_tombstoned_once(tmp_path):
    """Many threads race to delete the SAME set of ids. delete() only tombstones
    LIVE segments, so across all threads each id is tombstoned exactly once and
    the returned counts must sum to exactly the number of ids."""
    ss = _fresh_store(tmp_path)
    scope = "agentA/del"
    n = 200
    ids = [ss.append(SID, scope, "thought", {"text": f"x{i}"}).id for i in range(n)]

    n_threads = 10
    barrier = threading.Barrier(n_threads)

    def worker() -> int:
        barrier.wait()
        # every thread tries to delete every id
        return ss.delete(SID, scope, ids)

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        counts = [f.result() for f in [ex.submit(worker) for _ in range(n_threads)]]

    # total tombstones across all racing deletes == n (each id exactly once)
    assert sum(counts) == n, f"double-tombstone or lost delete: sum={sum(counts)} != {n}"
    live = ss.render(SID, scope)
    assert live == [], "some segment survived a delete-all race"
    all_segs = ss.list_segments(SID, scope, include_tombstoned=True)
    assert all(s.status == "tombstoned" for s in all_segs)
    assert len(all_segs) == n
    # each tombstone got a distinct tombstoning logical_time (each under the lock)
    tomb_lts = [s.tombstoned_at for s in all_segs]
    assert len(set(tomb_lts)) == n, "tombstoning logical_time collided"


# --------------------------------------------------------------------------- #
# 5. Persistence under concurrency: cold reload equals the live in-memory view
# --------------------------------------------------------------------------- #


def test_concurrent_writes_then_cold_reload_matches(tmp_path):
    """After a concurrent write storm, a brand-new SegmentStore over the SAME
    backend dir (cold reload) must reproduce the exact live render — proving the
    write-through persistence survived every interleaving with no torn record and
    the recovered clock continues past the persisted max."""
    backend = LocalFSStore(str(tmp_path / "arc"))
    ss = SegmentStore(backend)
    scope = "agentA/persist"
    n_threads, per_thread = 10, 30

    def worker(tid: int) -> None:
        for i in range(per_thread):
            ss.append(SID, scope, "thought", {"text": f"p{tid}-{i}"}, step=i)

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(worker, range(n_threads)))

    total = n_threads * per_thread
    before_keys = ss.render_keys(SID, scope)
    before_texts = {s.content["text"] for s in ss.render(SID, scope)}
    assert len(before_texts) == total

    # cold reload over the SAME directory
    reloaded = SegmentStore(LocalFSStore(str(tmp_path / "arc")))
    after = reloaded.render(SID, scope)
    assert len(after) == total, "cold reload lost segments (torn persist under concurrency)"
    assert {s.content["text"] for s in after} == before_texts
    assert reloaded.render_keys(SID, scope) == before_keys

    # recovered clock continues strictly past the persisted maximum
    max_lt = max(s.logical_time for s in after)
    nxt = reloaded.append(SID, scope, "thought", {"text": "after-reload"})
    assert nxt.logical_time == max_lt + 1, "recovered logical clock did not continue gaplessly"


# --------------------------------------------------------------------------- #
# 6. Through the ARCMemory pass-throughs (the real public surface), multi-scope
# --------------------------------------------------------------------------- #


def test_arcmemory_passthrough_concurrency(tmp_path):
    """Drive the live plane through ARCMemory's public append/delete/summarize/
    render pass-throughs from many threads across many scopes. The ARCMemory
    object itself is shared; SegmentStore's RLock is the only thing serializing
    the segment plane."""
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    n_scopes = 8
    per_scope = 35
    scopes = [f"agent{n}" for n in range(n_scopes)]
    barrier = threading.Barrier(n_scopes)

    def worker(scope: str) -> None:
        barrier.wait()
        for i in range(per_scope):
            arc.append_segment(SID, scope, "thought", {"text": f"{scope}#{i}"}, step=i)
        # then tombstone the first segment via the pass-through
        live = arc.render_segments(SID, scope)
        arc.delete_segments(SID, scope, [live[0].id])

    with ThreadPoolExecutor(max_workers=n_scopes) as ex:
        list(ex.map(worker, scopes))

    all_lts: list[int] = []
    for scope in scopes:
        live = arc.render_segments(SID, scope)
        assert len(live) == per_scope - 1, (
            f"{scope}: expected {per_scope - 1} live after one delete, got {len(live)}"
        )
        assert all(s.scope == scope for s in live)
        keys = arc.render_segments_keys(SID, scope)
        _assert_keys_gapless(keys)
        # tokens_by_kind pass-through must agree with the live count (all thoughts)
        toks = arc.segment_tokens_by_kind(SID, scope)
        assert set(toks) <= {"thought"}
        all_lts.extend(s.logical_time for s in arc.render_segments(SID, scope))

    # global clock uniqueness across the whole ARCMemory-driven run
    assert len(set(all_lts)) == len(all_lts), "logical_time collision via ARCMemory"


# --------------------------------------------------------------------------- #
# 7. Concurrent inserts at position 0: gap-allocation never collides orders
# --------------------------------------------------------------------------- #


def test_concurrent_insert_head_unique_orders(tmp_path):
    """Many threads insert at render position 0 of the same scope. Each insert
    picks ``min(order)-1`` under the lock, so orders must stay strictly unique
    and the render must remain a coherent, sorted, gapless view."""
    ss = _fresh_store(tmp_path)
    scope = "agentA/head"
    # seed one segment so there's a defined min order
    ss.append(SID, scope, "thought", {"text": "seed"})
    n_threads, per_thread = 8, 25

    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(per_thread):
            ss.insert(SID, scope, 0, "thought", {"text": f"h{tid}-{i}"}, step=i)

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(worker, range(n_threads)))

    live = ss.render(SID, scope)
    expected = n_threads * per_thread + 1  # + seed
    assert len(live) == expected, f"insert lost writes: {len(live)} != {expected}"
    orders = [s.order for s in live]
    assert len(set(orders)) == expected, "insert-at-head produced colliding orders"
    _assert_render_well_formed(live)
    # logical_time still gapless 1..N over the whole run
    lts = sorted(s.logical_time for s in live)
    assert lts == list(range(1, expected + 1))


# --------------------------------------------------------------------------- #
# 8. PER-SCOPE locking: different scopes run CONCURRENTLY (no global serialize),
#    and the clock + per-scope lock split introduces NO deadlock or lost ticks.
# --------------------------------------------------------------------------- #


def test_per_scope_locks_are_distinct(tmp_path):
    """Each (session, scope) gets its OWN lock; different scopes get different lock
    objects (so they don't contend), and the same scope returns the same lock."""
    ss = _fresh_store(tmp_path)
    la = ss._lock_for(SID, "agentA/x")
    la2 = ss._lock_for(SID, "agentA/x")
    lb = ss._lock_for(SID, "agentB/y")
    lc = ss._lock_for("other", "agentA/x")  # different session, same scope addr
    assert la is la2
    assert la is not lb and la is not lc and lb is not lc


def test_different_scopes_do_not_serialize(tmp_path):
    """Two scopes whose ops each hold their scope lock for a beat must overlap in
    time — if a single store-wide lock still guarded everything they'd run strictly
    sequentially. We sleep INSIDE the op window (a render under the scope lock) and
    assert the two scopes' busy windows overlap."""
    import time

    ss = _fresh_store(tmp_path)
    for scope in ("agentA/x", "agentB/y"):
        ss.append(SID, scope, "thought", {"text": "seed"})

    windows: dict[str, list[float]] = {}
    start_barrier = threading.Barrier(2)

    def churn(scope: str) -> None:
        start_barrier.wait()
        t0 = time.perf_counter()
        for i in range(200):
            ss.append(SID, scope, "thought", {"text": f"{scope}-{i}"}, step=i)
            ss.render(SID, scope)
        windows[scope] = [t0, time.perf_counter()]

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(churn, ["agentA/x", "agentB/y"]))

    a, b = windows["agentA/x"], windows["agentB/y"]
    overlap = min(a[1], b[1]) - max(a[0], b[0])
    assert overlap > 0, "different scopes did not run concurrently (still serialized)"
    # both scopes intact
    assert len(ss.render(SID, "agentA/x")) == 201
    assert len(ss.render(SID, "agentB/y")) == 201


def test_concurrent_ops_with_release_and_clear_no_deadlock(tmp_path):
    """Hammer many scopes with mixed ops while OTHER threads call release()/clear()
    on different sessions. The acquire-registry-then-scope-locks order in the
    store-wide ops must never deadlock with the per-scope op path. A watchdog bounds
    the run so a deadlock fails loudly instead of hanging the suite."""
    import time

    ss = _fresh_store(tmp_path)
    sessions = [f"s{n}" for n in range(4)]
    scopes = [f"agent{n}/exp" for n in range(6)]
    stop = threading.Event()

    def worker(sess: str) -> None:
        i = 0
        while not stop.is_set():
            scope = scopes[i % len(scopes)]
            ss.append(sess, scope, "thought", {"text": f"{sess}-{i}"}, step=i)
            live = ss.render(sess, scope)
            if len(live) >= 3:
                ss.delete(sess, scope, [live[0].id])
            i += 1

    def churner(sess: str) -> None:
        while not stop.is_set():
            ss.release(sess)

    def clearer() -> None:
        while not stop.is_set():
            ss.clear()

    with ThreadPoolExecutor(max_workers=len(sessions) * 2 + 1) as ex:
        futs = [ex.submit(worker, s) for s in sessions]
        futs += [ex.submit(churner, s) for s in sessions]
        futs.append(ex.submit(clearer))
        time.sleep(2.0)  # let them race
        stop.set()
        # bounded join: a deadlock would block these result() calls past the timeout
        for f in futs:
            f.result(timeout=30)

    # store still usable + coherent after the storm
    ss.append(SID, "agentA/exp", "thought", {"text": "after"})
    live = ss.render(SID, "agentA/exp")
    _assert_render_well_formed(live)
    assert any(s.content.get("text") == "after" for s in live)


def test_release_does_not_iterate_locator_dict_deterministic(tmp_path):
    """DETERMINISTIC regression for the ``dictionary changed size during iteration``
    race that ``release`` hit at ``SegmentIndex.drop_session`` (segments.py:244).

    The old ``release`` cleared the locator by iterating the whole ``_index._by_scope``
    dict (``[k for k in self._by_scope if k[0] == session_id]``) while holding only the
    RELEASED session's scope locks. A concurrent cold-load (``_segs``) on a DIFFERENT
    session inserts a brand-new ``(session, scope)`` key into that same dict under only
    its own scope lock — so the release-thread's iteration could observe the size change
    and raise ``RuntimeError: dictionary changed size during iteration``.

    We reproduce that interleaving deterministically without threads by swapping in a
    locator dict that inserts a fresh key (exactly what a concurrent cold-load does) the
    first time it is iterated. If ``release`` iterates the dict, this raises; the correct
    per-known-key drop never iterates it, so it must stay clean. This test fails on the
    pre-fix code and passes on the fixed code."""

    class _MutatesOnFirstIter(dict):
        """A dict that, once armed, simulates a concurrent cold-load inserting a NEW
        scope key the moment something starts iterating it — the precise mutation that
        made the pre-fix ``drop_session`` scan raise."""

        armed = False

        def __iter__(self):
            base = dict.__iter__(self)
            first = True
            for k in base:
                if self.armed and first:
                    first = False
                    # a different session's cold-load lands a brand-new locator entry
                    dict.__setitem__(self, ("concurrent-coldload", "scope"), object())
                yield k

    ss = _fresh_store(tmp_path)
    # Load several scopes for the session being released, plus one for another session,
    # so the locator dict holds >=2 keys (needed for the iterator to advance past the
    # mid-iteration insert) and release has real work to do.
    for i in range(3):
        ss.append(SID, f"agentA/exp{i}", "thought", {"text": f"a{i}"})
    ss.append("other", "agentB/exp", "thought", {"text": "b"})

    # Swap the locator's backing dict for the hostile one (preserving current contents),
    # then arm it so the next iteration (if any) triggers the concurrent-insert race.
    hostile = _MutatesOnFirstIter(ss._index._by_scope)
    ss._index._by_scope = hostile
    hostile.armed = True

    # Must NOT raise: the fixed release drops locator entries by known key (pop), never
    # by scanning the dict. Pre-fix this raised RuntimeError.
    released = ss.release(SID)
    hostile.armed = False

    assert released == 3, f"release should have dropped 3 scopes, got {released}"
    # the released session's locator entries are gone; the other session's survive
    remaining = set(dict.keys(ss._index._by_scope))
    assert all(k[0] != SID for k in remaining), "release left a released-session locator entry"
    assert ("other", "agentB/exp") in remaining, "release wrongly dropped another session"
    # the store is still coherent for the untouched session
    assert len(ss.render("other", "agentB/exp")) == 1


def test_clock_unique_across_scopes_under_per_scope_locks(tmp_path):
    """The shared clock has its OWN lock now; concurrent ops across MANY scopes must
    still issue every creation logical_time exactly once (no collision, no skip from
    the scope/clock lock split)."""
    ss = _fresh_store(tmp_path)
    n_scopes, per_scope = 16, 60
    scopes = [f"agent{n}/exp" for n in range(n_scopes)]
    barrier = threading.Barrier(n_scopes)

    def worker(scope: str) -> None:
        barrier.wait()
        for i in range(per_scope):
            ss.append(SID, scope, "thought", {"text": f"{scope}:{i}"}, step=i)

    with ThreadPoolExecutor(max_workers=n_scopes) as ex:
        list(ex.map(worker, scopes))

    all_lts: list[int] = []
    for scope in scopes:
        all_lts.extend(s.logical_time for s in ss.render(SID, scope))
    total = n_scopes * per_scope
    assert sorted(all_lts) == list(range(1, total + 1)), (
        "clock collided/gapped across scopes with the separate clock lock"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
