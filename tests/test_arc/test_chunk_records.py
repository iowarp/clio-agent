"""The append-only chunk record format of ARC segment lists (``arc/chunk_records.py``).

A chunk blob is ``MAGIC``, records ``[u32 len][u32 crc32][msgpack Segment]`` and an end
marker of 8 zero bytes. An append is one put at the stored end; a rewrite one put at 0.
These tests pin the format and every hazard the writer and readers are built for: a
torn tail read mid-append, a checksum mismatch, a shrinking rewrite over a blob that
never shrinks, the end offset recomputed on a cold load, the roll to a fresh chunk after
a failed append (and the duplicate a late put can leave), the retired base64 frame, and
the raw substring search over committed bytes.

Sabotage (each turns its tests red): ``parse_chunk`` returning on a CRC mismatch
without the check; ``plan_write`` ignoring the mark (a rewrite per append); the
writer cache dropping the mark on install; ``chunk_for_append`` ignoring sealed chunks;
``dedupe`` keeping every record; ``raw_scope_record`` returning the whole blob;
``plan_write`` without class padding (the metadata-log test).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from clio_agent import conf
from clio_agent.arc.blob_frame import size_class, stored_body
from clio_agent.arc.chunk_records import (
    CHUNK_TORN_TAIL,
    END,
    MAGIC,
    ChunkFormatUnsupportedError,
    encode_records,
    full_body,
    parse_chunk,
    plan_write,
    read_chunk,
)
from clio_agent.arc.clio_core_async_ops import ClioCoreFutureTimeout
from clio_agent.arc.lane_chunking import chunk_for_append, chunk_scope, lane_segments
from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.schema import Segment
from clio_agent.arc.segments import ArcPersistError, SegmentReadError, SegmentStore
from clio_agent.gact.part_atoms import append_part_atom
from clio_agent.gact.transcript_reads import transcript_may_contain

SID = "sess-chunks"
BASE = "_events"


def _seg(n: int, *, text: str = "") -> Segment:
    return Segment(
        scope=BASE,
        kind="semantic_event",
        content={"n": n, "text": text or f"event {n}"},
        session_id=SID,
        step=-1,
        order=float(n),
        logical_time=n,
        id=f"seg-{n}",
        created_at=0.0,
    )


def _name(scope: str = BASE) -> str:
    return SegmentStore._record_name(SID, scope)


class _RecordingStore(_MemoryStore):
    """The in-process store, recording every raw put and able to lose the next one.

    ``fail_next`` makes the next ``put_at`` raise it; with ``keep_pending`` the lost put
    is kept so a test can land it later, as a timed-out clio-core put may.
    """

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[tuple[str, int, bytes]] = []
        self.fail_next: Optional[BaseException] = None
        self.pending: list[tuple[str, str, bytes, int]] = []

    def put_at(self, kind: str, name: str, data: bytes, offset: int, **kwargs: Any) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            self.pending.append((kind, name, data, offset))
            raise exc
        self.puts.append((name, offset, data))
        super().put_at(kind, name, data, offset, **kwargs)

    def land_pending(self) -> None:
        for kind, name, data, offset in self.pending:
            super().put_at(kind, name, data, offset)
        self.pending.clear()


_OPENED: list[Any] = []


@pytest.fixture()
def clio_core() -> Iterator[Any]:
    """A real clio-core store in its own namespace (the worker's isolated daemon)."""
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    store = make_arc_store(backend="cte", namespace=f"chunks-{uuid.uuid4().hex[:10]}")
    yield store
    store.clear()


@pytest.fixture(params=["memory", "clio-core"])
def backend(request: pytest.FixtureRequest) -> Iterator[Any]:
    if request.param == "memory":
        yield _MemoryStore()
        return
    yield request.getfixturevalue("clio_core")


@pytest.fixture()
def audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stream-audit rows land in this file (hermetic config, no developer file)."""
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("CLIO_STREAM_AUDIT_LOG", str(path))
    return path


def _rows(path: Path, stage: str) -> list[dict[str, Any]]:
    lines = path.read_text().splitlines() if path.exists() else []
    return [row for row in map(json.loads, lines) if row["stage"] == stage]


# --------------------------------------------------------------------------- #
# The format
# --------------------------------------------------------------------------- #


def test_round_trip_of_a_whole_chunk_and_an_append() -> None:
    first = [_seg(1), _seg(2)]
    blob = full_body(first)
    assert blob.startswith(MAGIC)
    parsed = parse_chunk("c", blob)
    assert parsed.segments == first and parsed.terminated
    assert parsed.end == len(MAGIC) + len(encode_records(first))
    assert len(blob) == size_class(parsed.end + len(END))  # padded to its class
    # An append is the new record and a fresh end marker at the stored end.
    mark = SimpleNamespace(end=parsed.end, count=2, alloc=len(blob))
    write = plan_write([*first, _seg(3)], mark)  # type: ignore[arg-type]
    assert write.offset == parsed.end
    assert write.data == encode_records([_seg(3)]) + END  # within the class: no padding
    grown = bytearray(blob)
    grown[write.offset : write.offset + len(write.data)] = write.data
    again = parse_chunk("c", bytes(grown))
    assert again.segments == [*first, _seg(3)] and again.end == write.end


def test_a_put_past_the_class_is_padded_with_zeros_to_the_next_class() -> None:
    big = [_seg(n, text="x" * 900) for n in range(1, 5)]
    write = plan_write(big, None)
    assert len(write.data) == write.alloc == size_class(write.end + len(END))
    assert not any(write.data[write.end :])
    mark = write.committed(big)
    crossing = plan_write([*big, _seg(9, text="y" * 4000)], mark)
    assert crossing.alloc == size_class(crossing.end + len(END)) > mark.alloc
    assert crossing.offset + len(crossing.data) == crossing.alloc


def test_a_torn_tail_at_every_byte_of_the_last_record_ends_the_chunk() -> None:
    """A lock-free reader may read a chunk mid-append: any prefix of the last record
    (and of its end marker) parses as the end, never as corruption."""
    blob = full_body([_seg(1), _seg(2)])
    start = len(MAGIC) + len(encode_records([_seg(1)]))
    stop = start + len(encode_records([_seg(2)]))
    for cut in range(start, stop + len(END)):
        parsed = parse_chunk("c", blob[:cut])
        assert not parsed.terminated
        expect = [_seg(1)] if cut < stop else [_seg(1), _seg(2)]
        assert parsed.segments == expect, cut
        assert parsed.end == (start if cut < stop else stop)


def test_an_append_read_half_written_over_the_old_end_marker_is_not_seen() -> None:
    """The append overwrites the old end marker in place: whatever prefix of it a
    reader catches, it sees the earlier records only, until the whole record is there."""
    base = full_body([_seg(1)])
    start = parse_chunk("c", base).end
    new = encode_records([_seg(2)]) + END
    for written in range(len(new) + 1):
        blob = base[:start] + new[:written] + base[start + written :]
        parsed = parse_chunk("c", blob)
        full = written >= len(new) - len(END)
        assert parsed.segments == ([_seg(1), _seg(2)] if full else [_seg(1)]), written


def test_a_checksum_mismatch_ends_the_chunk_at_that_record() -> None:
    blob = bytearray(full_body([_seg(1), _seg(2)]))
    last = len(MAGIC) + len(encode_records([_seg(1)]))
    blob[last + 8 + 3] ^= 0xFF  # one payload byte of the second record
    parsed = parse_chunk("c", bytes(blob))
    assert parsed.segments == [_seg(1)] and parsed.end == last and not parsed.terminated
    blob[len(MAGIC) + 8 + 3] ^= 0xFF  # and of the first
    assert parse_chunk("c", bytes(blob)).segments == []


def test_an_exact_duplicate_record_reads_once() -> None:
    blob = MAGIC + encode_records([_seg(1), _seg(2), _seg(2)]) + END
    assert parse_chunk("c", blob).segments == [_seg(1), _seg(2)]
    # A different segment at the same logical time and order is not a duplicate.
    other = Segment(
        scope=BASE, kind="ws_op", content={}, session_id=SID, step=-1, order=2.0, logical_time=2
    )
    assert len(parse_chunk("c", MAGIC + encode_records([_seg(2), other]) + END).segments) == 2


def test_the_retired_base64_frame_is_a_typed_error_with_its_remedy() -> None:
    old = stored_body(b"\x91\x01")  # what ClioCoreStore.put stored for every chunk
    with pytest.raises(ChunkFormatUnsupportedError) as caught:
        parse_chunk("sess__events", old)
    assert caught.value.error_type == "arc_chunk_format_unsupported"
    assert isinstance(caught.value, SegmentReadError)
    assert "clio reset-store" in caught.value.message
    assert caught.value.details == {"name": "sess__events", "recovery": "reset_clio_state"}


def test_an_old_chunk_in_the_store_fails_every_reader_typed(backend: Any) -> None:
    backend.put("segments", _name(), b"\x90")  # the old frame, as the old writer put it
    with pytest.raises(ChunkFormatUnsupportedError):
        SegmentStore(backend).list_segments(SID, BASE)
    with pytest.raises(ChunkFormatUnsupportedError):
        SegmentStore(backend).raw_scope_record(SID, BASE)


# --------------------------------------------------------------------------- #
# Reads that race or lag a put
# --------------------------------------------------------------------------- #


def test_a_torn_tail_is_reported_as_a_trace_event_not_an_error(audit: Path) -> None:
    store = _MemoryStore()
    blob = full_body([_seg(1), _seg(2)])
    cut = len(MAGIC) + len(encode_records([_seg(1)])) + 5
    store.put_at("segments", "c", blob[:cut], 0)
    parsed = read_chunk(store, "segments", "c")
    assert parsed is not None and parsed.segments == [_seg(1)]
    (row,) = _rows(audit, CHUNK_TORN_TAIL)
    assert row["name"] == "c" and row["end"] == cut - 5


def test_a_lagging_size_is_read_again_up_to_the_record_it_cuts(audit: Path) -> None:
    """clio-core's size metadata can lag the last put; the reader re-reads to the
    length the cut record declares instead of losing it."""
    blob = full_body([_seg(1), _seg(2)])
    stale = len(MAGIC) + len(encode_records([_seg(1)])) + 12
    lengths: list[int] = []

    class _Lagging:
        def get_raw(self, kind: str, name: str, *, length: int = 0) -> bytes:
            lengths.append(length)
            return blob[: max(stale, length)]

    parsed = read_chunk(_Lagging(), "segments", "c")
    assert parsed is not None and parsed.segments == [_seg(1), _seg(2)] and parsed.terminated
    assert lengths[0] == 0 and len(lengths) >= 2
    assert _rows(audit, CHUNK_TORN_TAIL) == []


# --------------------------------------------------------------------------- #
# The writer: append at the end, rewrite at 0, recompute the end on a cold load
# --------------------------------------------------------------------------- #


def test_appends_put_only_the_new_record_at_the_stored_end() -> None:
    store = _RecordingStore()
    segments = SegmentStore(store)
    first = segments.append(SID, BASE, "semantic_event", {"n": 1})
    second = segments.append(SID, BASE, "semantic_event", {"n": 2})
    (_n0, off0, data0), (_n1, off1, data1) = store.puts
    assert off0 == 0 and data0.startswith(MAGIC + encode_records([first]) + END)
    assert off1 == len(MAGIC) + len(encode_records([first]))
    assert data1 == encode_records([second]) + END


def test_an_append_after_a_cold_load_continues_at_the_recomputed_end(
    backend: Any, audit: Path
) -> None:
    writer = SegmentStore(backend)
    kept = [writer.append(SID, BASE, "semantic_event", {"n": n}) for n in range(3)]
    restarted = SegmentStore(backend)  # a new process: no cache, no mark
    before = len(_rows(audit, "store.put"))
    added = restarted.append(SID, BASE, "semantic_event", {"n": 3})
    if not isinstance(backend, _MemoryStore):  # clio-core audits each put with its offset
        (row,) = _rows(audit, "store.put")[before:]
        assert row["offset"] == len(MAGIC) + len(encode_records(kept))
    assert restarted._chunk_marks[(SID, BASE)].end == len(MAGIC) + len(
        encode_records([*kept, added])
    )
    fresh = SegmentStore(backend).list_segments(SID, BASE)
    assert [s.id for s in fresh] == [*(s.id for s in kept), added.id]


def test_a_cold_append_puts_only_its_record_at_the_recomputed_end() -> None:
    store = _RecordingStore()
    writer = SegmentStore(store)
    kept = [writer.append(SID, BASE, "semantic_event", {"n": n}) for n in range(3)]
    store.puts.clear()
    added = SegmentStore(store).append(SID, BASE, "semantic_event", {"n": 3})
    assert store.puts == [
        (_name(), len(MAGIC) + len(encode_records(kept)), encode_records([added]) + END)
    ]


def test_a_shrinking_rewrite_reads_back_the_shorter_set(backend: Any) -> None:
    """``PutBlob`` never shrinks a blob: the rewrite's end marker stops readers before
    the stale tail (the task-lane rewrite renumbers and can drop rows)."""
    writer = SegmentStore(backend)
    rows = [
        writer.append(SID, BASE, "semantic_event", {"n": n, "pad": "z" * 300}) for n in range(6)
    ]
    before = backend.get_raw("segments", _name())
    with writer._lock_for(SID, BASE):
        writer._discard_scope(SID, BASE)
        writer._put_scope(SID, BASE, rows[:2])
    after = backend.get_raw("segments", _name())
    assert before is not None and after is not None and len(after) >= len(before)
    assert [s.id for s in SegmentStore(backend).list_segments(SID, BASE)] == [
        rows[0].id,
        rows[1].id,
    ]
    # and the next append lands right after the shorter set, not after the stale tail
    third = SegmentStore(backend).append(SID, BASE, "semantic_event", {"n": 99})
    ids = [s.id for s in SegmentStore(backend).list_segments(SID, BASE)]
    assert ids == [rows[0].id, rows[1].id, third.id]


def test_an_op_that_changes_an_earlier_segment_rewrites_the_record_at_zero() -> None:
    store = _RecordingStore()
    segments = SegmentStore(store)
    first = segments.append(SID, "expert", "thought", {"text": "a"})
    segments.append(SID, "expert", "thought", {"text": "b"})
    segments.delete(SID, "expert", [first.id])
    assert [offset for _name, offset, _data in store.puts] == [0, store.puts[1][1], 0]
    assert [s.content["text"] for s in SegmentStore(store).render(SID, "expert")] == ["b"]


def test_residency_is_the_stored_end_offset() -> None:
    reported: list[int] = []

    class _Residency:
        def scope_resident(self, session_id: str, scope: str, nbytes: int) -> None:
            reported.append(nbytes)

        def scope_dropped(self, session_id: str, scope: str) -> None: ...

        def session_released(self, session_id: str) -> None: ...

        def scope_touched(self, session_id: str) -> None: ...

    store = _MemoryStore()
    segments = SegmentStore(store)
    segments.set_residency(_Residency())
    a = segments.append(SID, BASE, "semantic_event", {"n": 1})
    b = segments.append(SID, BASE, "semantic_event", {"n": 2})
    assert reported == [
        len(MAGIC) + len(encode_records([a])),
        len(MAGIC) + len(encode_records([a, b])),
    ]


# --------------------------------------------------------------------------- #
# A failed append: abandon the chunk, roll the lane, read a late landing once
# --------------------------------------------------------------------------- #


def _lane_append(segments: SegmentStore, n: int) -> Segment:
    scope = chunk_for_append(segments, SID, BASE, capacity=4)
    return segments.append(SID, scope, "semantic_event", {"n": n})


def test_a_failed_append_rolls_the_lane_and_a_late_landing_reads_once() -> None:
    store = _RecordingStore()
    segments = SegmentStore(store)
    a, b = _lane_append(segments, 1), _lane_append(segments, 2)
    store.fail_next = ClioCoreFutureTimeout("put", "no_progress", 30.0)
    with pytest.raises(ArcPersistError):
        _lane_append(segments, 3)  # timed out: it may still land
    # The chunk is abandoned: never appended at again, though it holds 2 of 4.
    assert chunk_for_append(segments, SID, BASE, capacity=4) == chunk_scope(BASE, 2)
    d = segments.append(SID, chunk_scope(BASE, 2), "semantic_event", {"n": 4})
    store.land_pending()  # the timed-out put lands late, in chunk 1
    late = parse_chunk("c", store.get_raw("segments", _name()) or b"").segments[-1]
    assert late.content == {"n": 3}
    # The same record written again (a retry of that segment) is read once.
    second = _name(chunk_scope(BASE, 2))
    end = parse_chunk("c", store.get_raw("segments", second) or b"").end
    store.put_at("segments", second, encode_records([late]) + END, end)
    fresh = SegmentStore(store)
    got = lane_segments(fresh, SID, BASE)
    assert [s.content["n"] for s in got] == [1, 2, 3, 4]
    assert [s.id for s in got] == [a.id, b.id, late.id, d.id]


def test_a_refused_first_write_of_a_chunk_does_not_open_a_hole() -> None:
    """A lost write that created nothing is retried in the same chunk: rolling past an
    absent chunk would hide everything after it from the dense walk."""
    store = _RecordingStore()
    segments = SegmentStore(store)
    store.fail_next = OSError("clio-core refused the put")
    with pytest.raises(ArcPersistError):
        _lane_append(segments, 1)
    assert chunk_for_append(segments, SID, BASE, capacity=4) == chunk_scope(BASE, 1)


def test_a_failed_append_to_an_empty_chunk_does_not_open_a_hole() -> None:
    """A sealed chunk that holds no committed segment is not rolled past: the dense walk
    stops at the first empty chunk, so a roll would hide everything written after it."""
    store = _RecordingStore()
    segments = SegmentStore(store)
    with segments._lock_for(SID, BASE):
        segments._commit_scope(SID, BASE, [])  # an empty chunk record: MAGIC + end marker
    store.fail_next = OSError("clio-core refused the put")
    with pytest.raises(ArcPersistError):
        _lane_append(segments, 1)  # an append (at the empty record's end) that is lost
    assert chunk_for_append(segments, SID, BASE, capacity=4) == chunk_scope(BASE, 1)
    kept = segments.append(SID, BASE, "semantic_event", {"n": 2})
    assert [s.id for s in lane_segments(SegmentStore(store), SID, BASE)] == [kept.id]


def test_a_failed_append_inside_a_batch_abandons_the_chunk() -> None:
    """``_persist`` (a writer that staged several atoms into the cached list) loses its
    append the same way: the cache entry is dropped and the chunk sealed."""
    store = _RecordingStore()
    segments = SegmentStore(store)
    _lane_append(segments, 1)
    with segments._lock_for(SID, BASE):
        segments._segs(SID, BASE).append(_seg(50))
        store.fail_next = OSError("refused")
        with pytest.raises(ArcPersistError):
            segments._persist(SID, BASE)
    assert (SID, BASE) not in segments._loaded
    assert chunk_for_append(segments, SID, BASE, capacity=4) == chunk_scope(BASE, 2)


# --------------------------------------------------------------------------- #
# Raw readers
# --------------------------------------------------------------------------- #


def test_substring_search_finds_stored_text_and_never_a_stale_tail() -> None:
    segments = SegmentStore(_MemoryStore())
    append_part_atom(segments, "s1", {"part": {"id": "p0", "type": "text", "text": "kept"}})
    append_part_atom(
        segments, "s1", {"part": {"id": "p1", "type": "text", "text": "x" * 9000 + "Needle ONE"}}
    )
    app = SimpleNamespace(state=SimpleNamespace(arc=SimpleNamespace(_segments=segments)))
    segments.clear()
    assert transcript_may_contain(app, "s1", ["needle one"])  # raw msgpack, lowercased
    assert not transcript_may_contain(app, "s1", ["absent"])
    # Rewrite chunk 1 shorter (into a smaller size class than the blob holds): the
    # dropped text stays stored past the end marker, unread.
    from clio_agent.arc.lane_generations import current_base  # noqa: PLC0415

    base = current_base(segments, "s1", "_events/m")
    with segments._lock_for("s1", base):
        kept = segments._segs("s1", base)[:1]
        segments._discard_scope("s1", base)
        segments._put_scope("s1", base, kept)
    raw = segments._store.get_raw("segments", segments._record_name("s1", base)) or b""
    assert b"Needle ONE" in raw  # still in the blob ...
    assert not transcript_may_contain(app, "s1", ["needle one"])  # ... but not committed
    assert transcript_may_contain(app, "s1", ["kept"])


# --------------------------------------------------------------------------- #
# On real clio-core: growth in size classes keeps the metadata log small
# --------------------------------------------------------------------------- #


def test_appends_grow_the_blob_in_classes_so_the_metadata_log_stays_small(
    clio_core: Any,
) -> None:
    """Every put that grows a clio-core blob adds a block and every later put logs the
    whole block list: 300 unpadded 1 KB appends wrote 3.6 MB of metadata log on the test
    daemon (one block each). Padding a growing put to its size class keeps it near the
    whole-record writer's ~0.4 MB."""
    from tests.test_arc.test_blob_frame import _metadata_log_bytes  # noqa: PLC0415

    segments = SegmentStore(clio_core)
    before = _metadata_log_bytes()
    for n in range(300):
        segments.append(SID, BASE, "semantic_event", {"n": n, "pad": "e" * 1000})
    grown = _metadata_log_bytes() - before
    assert len(SegmentStore(clio_core).list_segments(SID, BASE)) == 300
    assert grown < 1_200_000, f"metadata log grew {grown} bytes for 300 appends"
