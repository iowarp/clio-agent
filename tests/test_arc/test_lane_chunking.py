"""#1339 Slice A: the generic ``_events`` chunk-family grammar (``arc/lane_chunking.py``).

Both lanes riding ``_events`` (the semantic-event log and the ``message_part`` atom
lane, ``tests/test_gact/test_part_atom_lane_chunking.py``) need the SAME grammar: chunk
1 is the bare base scope, chunk ``N>=2`` is ``<base>/<N>``, siblings/malformed names
never collide, the writer cursor recovers from a cold/invalidated state without ever
falling back to a body-downloading scan, and lifecycle erase drops the whole family
(tolerating an anomalous hole) and forgets its cursor. These tests pin that contract
directly against a real :class:`~clio_agent.arc.segments.SegmentStore` /
:class:`~clio_agent.arc.storage.LocalFSStore`, independent of either lane's own
higher-level tests.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from clio_agent import conf
from clio_agent.arc.companion_policy import SEGMENT_NAME_SEP, may_carry_companion
from clio_agent.arc.lane_chunking import (
    ATOM_LANE_CHUNK_GAP,
    LANE_CHUNK_CURSOR_RESET,
    chunk_for_append,
    chunk_index,
    chunk_scope,
    drop_lane,
    forget_cursor,
    is_chunk_of,
    lane_has_segments,
    lane_scopes,
    lane_segments,
    recover_writer,
)
from clio_agent.arc.live import (
    EVENTS_SCOPE,
    events_chunk_index,
    events_chunk_scope,
    is_events_scope,
)
from clio_agent.arc.segments import SegmentStore
from clio_agent.arc.storage import LocalFSStore

BASE_EVENTS = EVENTS_SCOPE  # "_events"
BASE_ATOMS = f"{EVENTS_SCOPE}/m"  # "_events/m"
SID = "sess-lane"


def _store(tmp_path: Any) -> SegmentStore:
    return SegmentStore(LocalFSStore(str(tmp_path)))


def _put(ss: SegmentStore, scope: str, n: int, *, sid: str = SID) -> None:
    """Append ``n`` bare segments directly to ``scope`` (bypasses the chunk cursor —
    used to construct a specific chunk layout for a test, e.g. a hole)."""
    for i in range(n):
        ss.append(sid, scope, "message_part", {"i": i})


# --------------------------------------------------------------------------- #
# Pure grammar: round trip + the collision proof
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("base", [BASE_EVENTS, BASE_ATOMS])
def test_scope_index_round_trip(base: str) -> None:
    assert chunk_scope(base, 1) == base
    for index in range(2, 9):
        scope = chunk_scope(base, index)
        assert scope == f"{base}/{index}"
        assert chunk_index(base, scope) == index
        assert is_chunk_of(base, scope) is True
    assert chunk_index(base, base) == 1
    assert is_chunk_of(base, base) is True


@pytest.mark.parametrize(
    "tail",
    ["edge", "02", "1", "0", "-1", "2x", "x2", ""],
)
def test_non_canonical_tails_are_not_chunks(tail: str) -> None:
    scope = f"{BASE_ATOMS}/{tail}"
    assert is_chunk_of(BASE_ATOMS, scope) is False
    # chunk_index is total (never raises) and maps every non-chunk to 1.
    assert chunk_index(BASE_ATOMS, scope) == 1


def test_siblings_are_not_chunks_of_events_but_are_is_events_scope() -> None:
    siblings = [BASE_ATOMS, f"{EVENTS_SCOPE}/w/span1", f"{EVENTS_SCOPE}/s"]
    for scope in siblings:
        assert is_events_scope(scope) is True
        assert is_chunk_of(EVENTS_SCOPE, scope) is False


def test_events_chunk_scope_index_delegate_to_lane_chunking() -> None:
    """The public ``arc.live`` names are now one-line delegations — parity pin."""
    for index in range(1, 6):
        assert events_chunk_scope(index) == chunk_scope(EVENTS_SCOPE, index)
    for scope in [EVENTS_SCOPE, f"{EVENTS_SCOPE}/2", f"{EVENTS_SCOPE}/9", "agentA"]:
        assert events_chunk_index(scope) == chunk_index(EVENTS_SCOPE, scope)


def test_may_carry_companion_false_for_a_chunked_atom_scope() -> None:
    """Mirrors ``test_storage_companion.py``'s pin for the reserved family, at a chunk
    N>=2 of the message-part lane (record-name shape ``sess___events~m~7``)."""
    name = SegmentStore._record_name(SID, chunk_scope(BASE_ATOMS, 7))
    assert name == f"{SID}{SEGMENT_NAME_SEP}_events~m~7"
    assert may_carry_companion("segments", name) is False


# --------------------------------------------------------------------------- #
# Dense-prefix discovery
# --------------------------------------------------------------------------- #


def test_dense_walk_stops_at_first_absent_chunk(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    _put(ss, chunk_scope(BASE_ATOMS, 1), 3)
    _put(ss, chunk_scope(BASE_ATOMS, 2), 3)
    # chunk 3 is absent -> the walk (and every reader built on it) must stop at 2,
    # even though chunk 4 below "exists" (a hole is not resolved by lane_scopes).
    _put(ss, chunk_scope(BASE_ATOMS, 4), 1)

    assert lane_scopes(ss, SID, BASE_ATOMS) == [BASE_ATOMS, chunk_scope(BASE_ATOMS, 2)]
    segs = lane_segments(ss, SID, BASE_ATOMS)
    assert len(segs) == 6
    assert lane_has_segments(ss, SID, BASE_ATOMS) is True


def test_lane_has_segments_is_chunk_one_only(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    assert lane_has_segments(ss, SID, BASE_ATOMS) is False
    _put(ss, chunk_scope(BASE_ATOMS, 1), 1)
    assert lane_has_segments(ss, SID, BASE_ATOMS) is True


def test_recover_writer_resumes_on_a_fresh_store(tmp_path: Any) -> None:
    ss1 = _store(tmp_path)
    _put(ss1, chunk_scope(BASE_ATOMS, 1), 4)
    _put(ss1, chunk_scope(BASE_ATOMS, 2), 4)
    _put(ss1, chunk_scope(BASE_ATOMS, 3), 2)

    # A brand new SegmentStore instance over the SAME persisted files: no in-memory
    # cursor, no ``_loaded`` cache -- recovery must read the family off disk.
    ss2 = _store(tmp_path)
    assert recover_writer(ss2, SID, BASE_ATOMS) == (3, 2)


# --------------------------------------------------------------------------- #
# The writer cursor + its invalidation
# --------------------------------------------------------------------------- #


def _append_via_cursor(ss: SegmentStore, base: str, *, capacity: int, sid: str = SID) -> str:
    """The real production pattern: reserve a slot, then actually write to it (so
    ``store._loaded`` stays in sync with the cursor, exactly like a real writer)."""
    scope = chunk_for_append(ss, sid, base, capacity=capacity)
    ss.append(sid, scope, "message_part", {})
    return scope


def test_chunk_for_append_rolls_at_capacity(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    scopes = [_append_via_cursor(ss, BASE_ATOMS, capacity=2) for _ in range(5)]
    assert scopes == [
        BASE_ATOMS,
        BASE_ATOMS,
        chunk_scope(BASE_ATOMS, 2),
        chunk_scope(BASE_ATOMS, 2),
        chunk_scope(BASE_ATOMS, 3),
    ]


def test_cursor_invalidated_by_drop_scope_of_the_active_chunk(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    for _ in range(3):
        _append_via_cursor(ss, BASE_ATOMS, capacity=2)
    # chunk1=[2], chunk2=[1] (active). Erase the active chunk directly (not via drop_lane).
    ss.drop_scope(SID, chunk_scope(BASE_ATOMS, 2))

    # The cursor must notice its active chunk is gone (``_loaded`` no longer holds it)
    # and recover from the family that remains, rather than keep writing into a
    # scope the store no longer has any memory of.
    scope = _append_via_cursor(ss, BASE_ATOMS, capacity=2)
    assert scope == chunk_scope(BASE_ATOMS, 2)  # chunk1 was full (2/2) -> rolls again
    assert lane_scopes(ss, SID, BASE_ATOMS) == [BASE_ATOMS, chunk_scope(BASE_ATOMS, 2)]
    assert len(ss.list_segments(SID, chunk_scope(BASE_ATOMS, 2), include_tombstoned=True)) == 1


def test_cursor_invalidated_by_release(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    for _ in range(3):
        _append_via_cursor(ss, BASE_ATOMS, capacity=2)
    ss.release(SID)  # drops the in-memory copy only; the store record survives

    # Recovery from disk must resume exactly where it left off (chunk2, count=1) --
    # release is a memory eviction, not an erase.
    scope = _append_via_cursor(ss, BASE_ATOMS, capacity=2)
    assert scope == chunk_scope(BASE_ATOMS, 2)
    assert len(ss.list_segments(SID, chunk_scope(BASE_ATOMS, 2), include_tombstoned=True)) == 2


def test_cursor_invalidated_by_a_family_erase_next_append_is_chunk_one(tmp_path: Any) -> None:
    ss = _store(tmp_path)
    for _ in range(5):
        _append_via_cursor(ss, BASE_ATOMS, capacity=2)
    assert drop_lane(ss, SID, BASE_ATOMS) == 5

    scope = chunk_for_append(ss, SID, BASE_ATOMS, capacity=2)
    assert scope == BASE_ATOMS  # the whole family is gone -> fresh chunk 1


def test_forget_cursor_audits_typed_reason_only_when_a_cursor_existed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        conf,
        "_STORE",
        conf.ConfigStore(home=tmp_path, cwd=tmp_path),
    )
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    ss = _store(tmp_path)
    forget_cursor(ss, SID, BASE_ATOMS)  # no cursor yet -> no audit row
    chunk_for_append(ss, SID, BASE_ATOMS, capacity=2)
    forget_cursor(ss, SID, BASE_ATOMS)  # a cursor now exists -> audited

    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    rows = [line for line in audit_path.read_text().splitlines() if line.strip()]
    assert any(LANE_CHUNK_CURSOR_RESET in row for row in rows)
    assert len(rows) == 1  # exactly the second forget_cursor call


# --------------------------------------------------------------------------- #
# Lifecycle erase: whole family, tolerating a hole
# --------------------------------------------------------------------------- #


def test_drop_lane_tolerates_a_hole_and_audits_the_gap(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    ss = _store(tmp_path)
    _put(ss, chunk_scope(BASE_ATOMS, 1), 2)
    _put(ss, chunk_scope(BASE_ATOMS, 2), 2)
    # chunk 3 is a hole; chunk 4 exists past it (never produced by real appends, but
    # the erase must still finish cleanly and audibly if it happens).
    _put(ss, chunk_scope(BASE_ATOMS, 4), 1)

    dropped = drop_lane(ss, SID, BASE_ATOMS)
    assert dropped == 5  # 2 + 2 (dense) + 1 (found past the hole)
    assert lane_scopes(ss, SID, BASE_ATOMS) == []
    assert ss.list_segments(SID, chunk_scope(BASE_ATOMS, 4), include_tombstoned=True) == []

    audit_path = tmp_path / "audit.jsonl"
    rows = audit_path.read_text().splitlines()
    assert any(ATOM_LANE_CHUNK_GAP in row for row in rows)


def test_drop_lane_leaves_a_sibling_partition_untouched(tmp_path: Any) -> None:
    """The ``/edge`` sibling (a real production scope, ``live_edge.py``'s checkpoint
    lane) is never touched by a chunk-family erase, however many chunks it drops."""
    ss = _store(tmp_path)
    edge_scope = f"{BASE_ATOMS}/edge"
    _put(ss, edge_scope, 1)
    for _ in range(3):
        ss.append(SID, chunk_for_append(ss, SID, BASE_ATOMS, capacity=1), "message_part", {})

    drop_lane(ss, SID, BASE_ATOMS)

    assert len(ss.list_segments(SID, edge_scope, include_tombstoned=True)) == 1


# --------------------------------------------------------------------------- #
# Append order under concurrency: chunk (discovery) order != logical_time order,
# so ``lane_segments`` must sort by ``logical_time`` (review sabotage case 4).
# --------------------------------------------------------------------------- #


def test_lane_segments_sorts_by_logical_time_across_a_racing_boundary(tmp_path: Any) -> None:
    """Deterministic reproduction of the race: reserve the last slot of chunk N and the
    first slot of chunk N+1 back to back (as two threads racing the boundary would),
    then APPEND to N+1 first and N second -- chunk N (discovered first by the dense
    walk) ends up holding a LATER ``logical_time`` than chunk N+1. ``lane_segments``
    must still return append order, not discovery order."""
    ss = _store(tmp_path)
    capacity = 2

    # Warm up: one prior append leaves chunk 1 at count == capacity - 1 (the last free
    # slot), exactly the boundary state the reviewer's case specifies.
    warm_scope = chunk_for_append(ss, SID, BASE_ATOMS, capacity=capacity)
    ss.append(SID, warm_scope, "message_part", {"which": "warmup"})

    # Reserve the last slot of chunk 1, then the first slot of chunk 2 -- two
    # back-to-back reservations, exactly as two racing threads' ``chunk_for_append``
    # calls would resolve (the cursor lock serializes the RESERVATION, not the append).
    scope_chunk1 = chunk_for_append(ss, SID, BASE_ATOMS, capacity=capacity)
    scope_chunk2 = chunk_for_append(ss, SID, BASE_ATOMS, capacity=capacity)
    assert scope_chunk1 == BASE_ATOMS  # chunk 1 (the reserved-first, last slot)
    assert scope_chunk2 == chunk_scope(BASE_ATOMS, 2)  # chunk 2 (reserved second)

    # APPEND out of reservation order: chunk 2's segment lands FIRST (an earlier
    # logical_time) even though chunk 1 was reserved first and is discovered first.
    ss.append(SID, scope_chunk2, "message_part", {"which": "chunk2_appended_first"})
    ss.append(SID, scope_chunk1, "message_part", {"which": "chunk1_appended_second"})

    segs = lane_segments(ss, SID, BASE_ATOMS)
    lts = [s.logical_time for s in segs]
    assert lts == sorted(lts), lts
    # Matches the REAL append call order: warmup, then chunk2, then chunk1.
    assert [s.content["which"] for s in segs] == [
        "warmup",
        "chunk2_appended_first",
        "chunk1_appended_second",
    ]


def test_lane_segments_sorted_under_concurrent_writers(tmp_path: Any) -> None:
    """8 threads x 60 appends at capacity 16 (review sabotage case 4, threaded twin):
    every segment lands exactly once, ids are unique, and ``lane_segments`` comes back
    sorted by ``logical_time`` -- append order survives concurrent chunk rolls."""
    ss = _store(tmp_path)
    threads_n, per_thread, capacity = 8, 60, 16
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            for _ in range(per_thread):
                scope = chunk_for_append(ss, SID, BASE_ATOMS, capacity=capacity)
                ss.append(SID, scope, "message_part", {})
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors`, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    segs = lane_segments(ss, SID, BASE_ATOMS, include_tombstoned=True)
    assert len(segs) == threads_n * per_thread
    assert len({s.id for s in segs}) == threads_n * per_thread  # every append unique
    lts = [s.logical_time for s in segs]
    assert lts == sorted(lts), "lane_segments must return true append order"
