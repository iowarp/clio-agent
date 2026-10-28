"""The recall index: stored search digests, kept current by transcript writes."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import msgspec
import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.segments import SegmentStore
from clio_agent.gact import recall_index as ri
from clio_agent.gact.recall_digest import (
    DIGEST_VERSION,
    TranscriptDigest,
    decode_digest,
    digest_messages,
    encode_digest,
)
from clio_agent.gact.recall_index import (
    BUCKETS,
    DIRTY_REASON,
    FORMAT_REASON,
    KIND,
    MISSING_REASON,
    UNAVAILABLE_REASON,
    UNREADABLE_REASON,
    DigestReadReport,
    RecallIndex,
    bucket_of,
    build_recall_index,
    is_transcript_scope,
    session_digests,
)
from clio_agent.gact.types import Message, Part


def _message(mid: str, *parts: Part, role: str = "assistant", at: str = "2026-10-01T00:00:00Z"):
    return Message(
        id=mid, session_id="s", role=role, created_at=at, updated_at=at, parts=list(parts)
    )


def _transcripts() -> dict[str, list[Message]]:
    return {
        "s1": [
            _message("m1", Part(id="p1", type="text", text="  Survey the dataset  ")),
            _message("m2", Part(id="p2", type="thinking", text="think about survey")),
        ],
        "s2": [_message("m3", Part(id="p3", type="text", text="nothing to see"))],
    }


def _index(transcripts: dict[str, list[Message]], **kw: Any) -> tuple[RecallIndex, _MemoryStore]:
    store = _MemoryStore()
    index = RecallIndex(store, lambda sid: list(transcripts.get(sid, [])), inline=True, **kw)
    return index, store


def _stored(store: _MemoryStore, sid: str) -> TranscriptDigest | None:
    raw = store.get(KIND, bucket_of(sid))
    held = msgspec.msgpack.decode(raw, type=dict[str, bytes]) if raw else {}
    return decode_digest(held[sid]) if sid in held else None


def _put_digest(store: _MemoryStore, sid: str, data: bytes) -> None:
    raw = store.get(KIND, bucket_of(sid))
    held = msgspec.msgpack.decode(raw, type=dict[str, bytes]) if raw else {}
    held[sid] = data
    store.put_many(KIND, [ri.PutRecord(bucket_of(sid), msgspec.msgpack.encode(held))])


# ---- the digest ------------------------------------------------------------------


def test_digest_keeps_searchable_text_plans_and_first_sources() -> None:
    messages = [
        _message(
            "m1",
            Part(id="t", type="text", text="  hello world \n"),
            Part(id="blank", type="text", text="   "),
            Part(id="tool", type="tool_call", text="not searched"),
            Part(id="e", type="error", text="boom"),
            role="user",
        ),
        _message(
            "m2",
            Part(id="plan1", type="plan", title="Do it", summary="step one"),
            Part(type="plan", text="untitled plan"),
            Part(
                id="sum",
                type="injection",
                source="summarization",
                text="compacted",
                trigger="auto",
            ),
            Part(
                id="link",
                type="resource_link",
                uri="https://example.org/data.csv",
                name="data",
                metadata={"source_url": "https://example.org/page"},
            ),
            Part(id="again", type="text", text="x", metadata={"link": "https://example.org/page"}),
        ),
    ]
    digest = digest_messages(messages)
    assert [(p.message_id, p.part_id, p.role, p.text) for p in digest.parts] == [
        ("m1", "t", "user", "hello world"),
        ("m1", "e", "user", "boom"),
        ("m2", "again", "assistant", "x"),
    ]
    assert [(p.part_id, p.title, p.detail, p.type) for p in digest.plans] == [
        ("plan1", "Do it", "step one", "plan"),
        ("1", "Plan", "untitled plan", "plan"),
        ("sum", "Compacted context", "compacted", "summarization"),
    ]
    # A value appears once per session: its first occurrence (the metadata URL of
    # the resource link), never the later part's copy.
    assert [(s.source_id, s.label, s.value) for s in digest.sources] == [
        ("link", "data", "https://example.org/data.csv"),
        ("link:source_url", "page (example.org)", "https://example.org/page"),
    ]


def test_digest_round_trips_and_another_version_reads_as_absent(monkeypatch) -> None:
    digest = digest_messages(_transcripts()["s1"])
    assert decode_digest(encode_digest(digest)) == digest
    import clio_agent.gact.recall_digest as rd

    monkeypatch.setattr(rd, "DIGEST_VERSION", DIGEST_VERSION + 1)
    assert decode_digest(encode_digest(digest)) == digest
    data = encode_digest(digest)
    monkeypatch.setattr(rd, "DIGEST_VERSION", DIGEST_VERSION)
    assert decode_digest(data) is None


# ---- maintenance -------------------------------------------------------------------


def test_only_message_part_lane_scopes_are_transcript_writes() -> None:
    for scope in ("_events/m", "_events/m/2", "_events/m/gen", "_events/m/g/3/2"):
        assert is_transcript_scope(scope)
    for scope in ("_events", "_events/2", "_events/mail", "_events/w/x", "working"):
        assert not is_transcript_scope(scope)


def test_a_transcript_write_marks_then_rebuilds_and_clears_the_marker() -> None:
    transcripts = _transcripts()
    index, store = _index(transcripts)
    with index.writing("s1", "working"):  # not the transcript: nothing happens
        pass
    assert store.get(KIND, "w/s1") is None and not index.is_dirty("s1")
    seen: list[bytes | None] = []
    with index.writing("s1", "_events/m/2"):
        seen.append(store.get(KIND, "w/s1"))  # durable before the record is written
        assert index.is_dirty("s1")
    assert seen == [b"1"]
    assert not index.is_dirty("s1")  # inline: rebuilt as the write ended
    assert store.get(KIND, "w/s1") is None
    assert dict(index.read(["s1"]))["s1"] == digest_messages(transcripts["s1"])


def test_a_write_during_the_rebuild_keeps_the_session_dirty() -> None:
    transcripts = _transcripts()
    store = _MemoryStore()
    holder: dict[str, RecallIndex] = {}

    def assemble(sid: str) -> list[Message]:
        index = holder["index"]
        index._begin(sid)  # a transcript write lands while the digest is built
        index._inflight.pop(sid)
        return list(transcripts[sid])

    index = RecallIndex(store, assemble, inline=True)
    holder["index"] = index
    index._dirty.add("s1")
    store.put_many(KIND, [ri.PutRecord("w/s1", b"1")])
    assert index.rebuild("s1") is False
    assert index.is_dirty("s1") and store.get(KIND, "w/s1") == b"1"
    assert dict(index.read(["s1"])) == {"s1": DIRTY_REASON}


def test_a_rebuild_waits_for_an_inflight_write() -> None:
    index, store = _index(_transcripts())
    index._inflight["s1"] = 1
    index._dirty.add("s1")
    assert index.rebuild("s1") is False
    assert _stored(store, "s1") is None


def test_markers_left_by_a_previous_process_read_as_dirty() -> None:
    transcripts = _transcripts()
    store = _MemoryStore()
    store.put_many(KIND, [ri.PutRecord("w/s1", b"1")])
    _put_digest(store, "s1", encode_digest(TranscriptDigest()))
    index = RecallIndex(store, lambda sid: transcripts[sid], delay_s=3600.0)
    assert dict(index.read(["s1"])) == {"s1": DIRTY_REASON}
    assert "s1" in index._due  # scheduled for a rebuild
    assert index.rebuild("s1") is True
    assert dict(index.read(["s1"]))["s1"] == digest_messages(transcripts["s1"])
    assert store.get(KIND, "w/s1") is None


def test_missing_unreadable_and_old_format_digests_are_typed() -> None:
    index, store = _index(_transcripts())
    _put_digest(store, "bad", b"not a digest")
    import zlib

    old = zlib.compress(msgspec.msgpack.encode({"version": 0, "digest": {}}))
    _put_digest(store, "old", old)
    index._inline = False  # keep the read from rebuilding inline
    index._delay_s = 3600.0
    got = dict(index.read(["nope", "bad", "old"]))
    assert got == {"nope": MISSING_REASON, "bad": UNREADABLE_REASON, "old": FORMAT_REASON}


def test_a_failed_marker_put_keeps_the_session_dirty_in_memory() -> None:
    class _Refusing(_MemoryStore):
        def put_many(self, kind: str, records: Any) -> None:
            if kind == KIND and records[0].name.startswith("w/"):
                raise OSError("refused")
            super().put_many(kind, records)

    store = _Refusing()
    index = RecallIndex(store, lambda sid: [], delay_s=3600.0)
    with index.writing("s1", "_events/m"):
        pass
    assert index.is_dirty("s1")


def test_the_rebuild_thread_builds_after_the_quiet_period() -> None:
    transcripts = _transcripts()
    store = _MemoryStore()
    index = RecallIndex(store, lambda sid: transcripts[sid], delay_s=0.01)
    with index.writing("s1", "_events/m"):
        pass
    assert index.flush(10.0)
    assert not index.is_dirty("s1")
    assert _stored(store, "s1") == digest_messages(transcripts["s1"])


def test_segment_store_writes_pass_the_observer() -> None:
    calls: list[tuple[str, str]] = []

    class _Observer:
        def writing(self, session_id: str, scope: str):  # type: ignore[no-untyped-def]
            from contextlib import nullcontext

            calls.append((session_id, scope))
            return nullcontext()

    from clio_agent.gact.part_atoms import append_part_atom

    segments = SegmentStore(_MemoryStore())
    segments.set_write_observer(_Observer())
    append_part_atom(segments, "s", {"part": {"id": "p", "type": "text", "text": "x"}})
    segments.drop_scope("s", "_events/m")
    assert ("s", "_events/m") in calls
    assert calls.count(("s", "_events/m")) >= 2  # the put and the delete


def test_concurrent_writes_and_rebuilds_end_clean() -> None:
    transcripts = _transcripts()
    store = _MemoryStore()
    index = RecallIndex(store, lambda sid: transcripts[sid], delay_s=0.0)

    def write() -> None:
        for _ in range(50):
            with index.writing("s1", "_events/m"):
                pass

    threads = [threading.Thread(target=write) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert index.flush(10.0)
    assert not index.is_dirty("s1")
    assert store.get(KIND, "w/s1") is None


# ---- reading for a search ---------------------------------------------------------


class _Resident(dict):
    def peek_resident(self, sid: str):  # type: ignore[no-untyped-def]
        return self.get(sid) if sid in self.resident else None

    resident: set[str] = set()


def _app(transcripts: dict[str, list[Message]], index: RecallIndex | None) -> SimpleNamespace:
    messages = _Resident(transcripts)
    messages.resident = set()
    state = SimpleNamespace(messages=messages, recall_index=index, arc=None)
    return SimpleNamespace(state=state)


def test_session_digests_prefer_resident_then_index_then_transcript() -> None:
    transcripts = {**_transcripts(), "s3": [_message("m9", Part(id="q", type="text", text="z"))]}
    index, _store = _index(transcripts)
    index.rebuild("s2")
    app = _app(transcripts, index)
    app.state.messages.resident = {"s1"}
    index._inline = False
    index._delay_s = 3600.0
    report = DigestReadReport()
    got = dict(session_digests(app, ["s1", "s2", "s3"], report))
    assert got == {sid: digest_messages(transcripts[sid]) for sid in ("s1", "s2", "s3")}
    assert report.as_dict() == {
        "from_index": 1,
        "from_resident": 1,
        "from_transcript": 1,
        "transcript_reasons": {MISSING_REASON: 1},
    }


def test_a_fallback_may_skip_a_session_that_cannot_match() -> None:
    transcripts = _transcripts()
    app = _app(transcripts, None)
    report = DigestReadReport()
    got = dict(session_digests(app, ["s1", "s2"], report, may_contain=lambda sid: sid == "s1"))
    assert got == {"s1": digest_messages(transcripts["s1"]), "s2": TranscriptDigest()}
    assert report.reasons == {UNAVAILABLE_REASON: 2}


def test_the_migration_builds_every_missing_digest_once() -> None:
    transcripts = _transcripts()
    index, store = _index(transcripts)
    index.rebuild("s1")
    sessions = SimpleNamespace(ids=lambda: ["s1", "s2"])
    app = SimpleNamespace(state=SimpleNamespace(recall_index=index, sessions=sessions))
    assert build_recall_index(app) == {"sessions": 2, "built": 1, "not_clean": 0}
    assert _stored(store, "s2") == digest_messages(transcripts["s2"])
    assert build_recall_index(app)["built"] == 0  # idempotent


def test_the_migration_needs_a_store() -> None:
    app = SimpleNamespace(state=SimpleNamespace(recall_index=None, arc=None))
    with pytest.raises(RuntimeError):
        build_recall_index(app)


def test_an_unreadable_bucket_reads_typed_and_a_rebuild_starts_it_over() -> None:
    transcripts = _transcripts()
    index, store = _index(transcripts)
    store.put_many(KIND, [ri.PutRecord(bucket_of("s1"), b"\xc1 not msgpack")])
    index._inline = False
    index._delay_s = 3600.0
    assert dict(index.read(["s1"])) == {"s1": UNREADABLE_REASON}
    index._inline = True
    assert index.rebuild("s1")
    assert _stored(store, "s1") == digest_messages(transcripts["s1"])


def test_one_bucket_write_per_bucket_when_many_sessions_rebuild() -> None:
    transcripts = {f"s{n}": _transcripts()["s1"] for n in range(200)}
    puts: list[str] = []

    class _Counting(_MemoryStore):
        def put_many(self, kind: str, records: Any) -> None:
            puts.extend(record.name for record in records)
            super().put_many(kind, records)

    store = _Counting()
    index = RecallIndex(store, lambda sid: transcripts[sid], inline=True)
    assert all(index.rebuild_many(list(transcripts)).values())
    assert len(puts) == len(set(puts)) <= BUCKETS
    assert all(_stored(store, sid) is not None for sid in transcripts)


def test_the_one_time_build_is_a_registered_store_migration() -> None:
    from clio_agent.gact import store_migrations

    item = store_migrations.migration("recall_index_build")
    assert item.has_input is store_migrations._holds_conversation
    names = [m.name for m in store_migrations.MIGRATIONS]
    # It reads transcripts, so it runs after the legacy transcript import.
    assert names.index("recall_index_build") > names.index("transcript_legacy_import")
