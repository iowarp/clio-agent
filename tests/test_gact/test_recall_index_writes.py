"""The recall index against PTC's transcript write path, restarts and an older CLIO.

The digests are kept current from the ARC segment store's write observer: a transcript
write costs no store work in the steady state, one marker put the first time a
conversation goes stale, and a digest never reads clean while the transcript it was built
from has moved (in this process, after a crash, or after an older CLIO wrote).

SABOTAGE (each run manually; each went RED, then was restored):

* ``RecallIndex._begin`` without its two ``_marked`` checks (a marker put per write):
  ``test_the_transcript_write_path_adds_no_put_per_write`` and
  ``test_a_spell_of_writes_puts_one_marker`` fail.
* ``ScopeCache._commit_scope`` leaves the observer before ``_install_committed``:
  ``test_the_observer_ends_after_the_cached_copy_shows_the_write`` fails.
* ``SegmentGroup._flush_rank`` without the observer: ``test_a_group_flush_is_observed``
  fails.
* ``RecallIndex._decode`` ignores the stamp: ``test_an_older_clio_write_reads_moved``
  serves the stale digest (the new text is missing) and fails.
* ``RecallIndex._load_markers`` adopts nothing:
  ``test_a_crash_before_the_rebuild_reads_dirty`` serves the stale digest and fails.
* ``RecallIndex._load_markers`` also adopts the markers this process put:
  ``test_digests_are_built_and_found_across_conversations`` reads every session dirty.
* ``RecallIndex._retire_marker`` skips the dirty/in-flight check:
  ``test_a_retire_never_drops_the_marker_of_a_written_session`` fails.
* ``session_digests`` skips the transcript fallback of an unusable digest (a partial
  answer): the dirty-search and crash tests fail.
* (``test_recall_index.py``) ``RecallIndex._settle`` ignores the write epoch:
  ``test_a_write_during_the_rebuild_keeps_the_session_dirty`` fails.
"""

from __future__ import annotations

import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segment_group import segment_group
from clio_agent.arc.segments import SegmentStore
from clio_agent.gact.app import build_app
from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE, append_part_atom, mint_message_part_atoms
from clio_agent.gact.recall_digest import digest_messages
from clio_agent.gact.recall_index import (
    DIRTY_REASON,
    KIND,
    MOVED_REASON,
    DigestReadReport,
    RecallIndex,
    recall_index_of,
    session_digests,
)
from clio_agent.gact.runtime.session_recall_search import _recall_search_response
from clio_agent.gact.transcript_projection import assemble_session_messages
from clio_agent.gact.types import Message, Part, Tokens

from .test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


class _Counting(_MemoryStore):
    """An in-memory clio-core store that counts writes by kind."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: dict[str, int] = {}

    def _count(self, kind: str, n: int = 1) -> None:
        self.writes[kind] = self.writes.get(kind, 0) + n

    def put(self, kind: str, name: str, data: bytes, **kw: Any) -> None:
        self._count(kind)
        super().put(kind, name, data, **kw)

    def put_at(self, kind: str, name: str, data: bytes, offset: int, **kw: Any) -> None:
        self._count(kind)
        super().put_at(kind, name, data, offset, **kw)

    def put_many(self, kind: str, records: Any) -> None:
        records = list(records)
        self._count(kind, len(records))
        for record in records:  # one write each, counted above (not again by put/put_at)
            if record.offset is not None:
                super().put_at(kind, record.name, record.data, record.offset)
            else:
                super().put(kind, record.name, record.data, search_text=record.search_text)

    def delete(self, kind: str, name: str) -> None:
        self._count(kind)
        super().delete(kind, name)


def _message(sid: str, mid: str, text: str, role: str = "user") -> Message:
    return Message(
        id=mid,
        turn_id=mid,
        session_id=sid,
        role=role,
        created_at="2026-11-01T10:00:00+00:00",
        updated_at="2026-11-01T10:00:00+00:00",
        parts=[Part(id=f"part_{mid}", type="text", text=text)],
        tokens=Tokens(),
        stop_reason="end_turn",
    )


def _atom(n: int) -> dict[str, Any]:
    return {"message_id": f"m{n}", "part_index": 0, "part": {"id": f"p{n}", "type": "text"}}


def _segments(store: _MemoryStore) -> tuple[Any, SegmentStore]:
    segments = SegmentStore(store)
    arc = type("Arc", (), {"_segments": segments, "_store": store})()
    return arc, segments


def _lane_index(store: _MemoryStore, arc: Any, **kw: Any) -> RecallIndex:
    return RecallIndex(store, lambda sid: assemble_session_messages(arc, sid), **kw)


class _Exit:
    def __init__(self, on_exit: Any) -> None:
        self._on_exit = on_exit

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        self._on_exit()


# ---- the write path ----------------------------------------------------------------


def test_the_transcript_write_path_adds_no_put_per_write() -> None:
    """200 transcript appends: the same segment puts, plus ONE marker put in all."""
    plain_store = _Counting()
    _, plain = _segments(plain_store)
    observed_store = _Counting()
    arc, observed = _segments(observed_store)
    index = _lane_index(observed_store, arc, delay_s=3600.0)
    observed.set_write_observer(index)
    for n in range(200):
        append_part_atom(plain, "s", _atom(n))
        append_part_atom(observed, "s", _atom(n))
    assert observed_store.writes.get("segments") == plain_store.writes.get("segments")
    assert observed_store.writes.get(KIND) == 1 and index.marker_puts == 1
    assert index.is_dirty("s") and index.is_marked("s")


def test_a_spell_of_writes_puts_one_marker() -> None:
    """Clean rebuilds inside a spell keep the marker: later writes put nothing."""
    store = _Counting()
    arc, segments = _segments(store)
    index = _lane_index(store, arc, delay_s=0.0, retire_s=3600.0)
    segments.set_write_observer(index)
    for n in range(5):
        append_part_atom(segments, "s", _atom(n))
        assert index.flush(10.0)
        assert not index.is_dirty("s")  # rebuilt clean between the writes
    assert index.marker_puts == 1
    assert store.get(KIND, "w/s") == b"1"  # kept while the spell lasts
    index._retire_s = 0.0  # the spell ends: the next clean settle retires the marker
    append_part_atom(segments, "s", _atom(9))
    assert index.flush(10.0)
    deadline = time.monotonic() + 10.0
    while store.get(KIND, "w/s") is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.get(KIND, "w/s") is None and not index.is_marked("s")
    append_part_atom(segments, "s", _atom(10))  # a new spell: one marker again
    assert index.marker_puts == 2
    index.close()


def test_a_retire_never_drops_the_marker_of_a_written_session() -> None:
    store = _MemoryStore()
    index = RecallIndex(store, lambda sid: [], inline=True, retire_s=3600.0)
    with index.writing("s", MESSAGE_PART_SCOPE):
        index._retire_marker("s")  # due while a write is in flight
        assert store.get(KIND, "w/s") == b"1" and index.is_marked("s")
    index._dirty.add("s")  # written, not rebuilt yet
    index._retire_marker("s")
    assert store.get(KIND, "w/s") == b"1"
    index._dirty.discard("s")
    index._retire_marker("s")
    assert store.get(KIND, "w/s") is None and not index.is_marked("s")


def test_close_retires_clean_markers_and_keeps_dirty_ones() -> None:
    store = _MemoryStore()
    index = RecallIndex(store, lambda sid: [], delay_s=0.0, retire_s=3600.0)
    with index.writing("a", MESSAGE_PART_SCOPE):
        pass
    assert index.flush(10.0) and not index.is_dirty("a")  # clean; its retire is far off
    index._delay_s = 3600.0
    with index.writing("b", MESSAGE_PART_SCOPE):
        pass  # dirty at the stop: the next process rebuilds it
    index.close()
    assert store.get(KIND, "w/a") is None
    assert store.get(KIND, "w/b") == b"1"


def test_the_observer_ends_after_the_cached_copy_shows_the_write() -> None:
    """A rebuild that starts once a write has ended reads that write (no lane lock)."""
    store = _MemoryStore()
    _arc, segments = _segments(store)
    seen: list[int] = []

    class _Observer:
        def writing(self, session_id: str, scope: str) -> Any:
            if scope != MESSAGE_PART_SCOPE:
                return nullcontext()
            return _Exit(lambda: seen.append(len(segments._scopes.get((session_id, scope), []))))

    segments.set_write_observer(_Observer())
    append_part_atom(segments, "s", _atom(1))
    append_part_atom(segments, "s", _atom(2))
    assert seen == [1, 2]


def test_a_group_flush_is_observed() -> None:
    store = _MemoryStore()
    _arc, segments = _segments(store)
    calls: list[tuple[str, bool]] = []

    class _Observer:
        def writing(self, session_id: str, scope: str) -> Any:
            calls.append((scope, store.get("segments", segments._record_name(session_id, scope))
                          is not None))  # fmt: skip
            return nullcontext()

    segments.set_write_observer(_Observer())
    with segment_group(segments, lambda _scope: (0,)) as group:
        segments.append("s", "_events", "semantic_event", {"n": 1})
        assert calls == []  # staged, not written
        group.flush()
    assert ("_events", False) in calls  # told before the put


# ---- staleness across processes --------------------------------------------------


def _app(tmp_path: Path, store: _MemoryStore, name: str) -> Any:
    arc = ARCMemory(data_dir=str(tmp_path / f"arc_{name}"), store=store)
    return build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(), arc=arc)


def _converse(app: Any, sid: str, mid: str, text: str) -> None:
    """Record one message as a CLIO does: its atoms, then the session's message count."""
    mint_message_part_atoms(app.state.arc, sid, _message(sid, mid, text))
    count = len(assemble_session_messages(app.state.arc, sid))
    app.state.sessions.update(sid, message_count=count)


def _search(app: Any, query: str, ws: str) -> Any:
    return _recall_search_response(app, query=query, workspace_id=ws, include_cross_session=True)


@pytest.fixture
def quick_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_RECALL_INDEX_REBUILD_DELAY_S", "0.01")
    monkeypatch.setenv("CLIO_TRANSCRIPT_FILE", "false")


@pytest.mark.usefixtures("quick_rebuild")
def test_digests_are_built_and_found_across_conversations(tmp_path: Path) -> None:
    app = _app(tmp_path, _MemoryStore(), "one")
    index = recall_index_of(app)
    assert index is not None
    ws = "ws_default"
    first = app.state.sessions.create(workspace_id=ws, title="first").id
    second = app.state.sessions.create(workspace_id=ws, title="second").id
    _converse(app, first, "m1", "the bathymetry survey of the shelf")
    _converse(app, second, "m2", "a second bathymetry pass, deeper")
    assert index.flush(10.0)
    got = _search(app, "bathymetry", ws)
    assert {hit.session_id for hit in got.hits} == {first, second}
    assert got.metadata["index"] == {"from_index": 2, "from_resident": 0, "from_transcript": 0}
    _converse(app, first, "m3", "bathymetry again, now the slope")  # the index catches up
    assert index.is_dirty(first)
    dirty = _search(app, "slope", ws)  # read from the transcript meanwhile, typed
    assert [hit.message_id for hit in dirty.hits] == ["m3"]
    assert dirty.metadata["index"]["transcript_reasons"] == {DIRTY_REASON: 1}
    assert index.flush(10.0)
    clean = _search(app, "slope", ws)
    assert [hit.message_id for hit in clean.hits] == ["m3"]
    assert clean.metadata["index"]["from_index"] == 2
    index.close()


@pytest.mark.usefixtures("quick_rebuild")
def test_a_crash_before_the_rebuild_reads_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore()
    first = _app(tmp_path, store, "first")
    ws = "ws_default"
    sid = first.state.sessions.create(workspace_id=ws, title="crash").id
    _converse(first, sid, "m1", "salinity profile")
    assert recall_index_of(first).flush(10.0)  # type: ignore[union-attr]
    monkeypatch.setenv("CLIO_RECALL_INDEX_REBUILD_DELAY_S", "3600")
    recall_index_of(first)._delay_s = 3600.0  # type: ignore[union-attr]
    _converse(first, sid, "m2", "salinity again, the crash comes before the rebuild")
    # The process dies here: no rebuild, no close. Its marker is durable.
    second = _app(tmp_path, store, "second")
    got = _search(second, "crash", ws)
    assert [hit.message_id for hit in got.hits] == ["m2"]
    assert got.metadata["index"]["transcript_reasons"] == {DIRTY_REASON: 1}


@pytest.mark.usefixtures("quick_rebuild")
def test_an_older_clio_write_reads_moved(tmp_path: Path) -> None:
    """An older CLIO writes transcripts without the index: the stamp catches it."""
    store = _MemoryStore()
    first = _app(tmp_path, store, "first")
    ws = "ws_default"
    sid = first.state.sessions.create(workspace_id=ws, title="downgrade").id
    _converse(first, sid, "m1", "turbidity readings")
    index = recall_index_of(first)
    assert index is not None and index.flush(10.0)
    index.close()  # a clean stop: the session's marker is retired
    assert store.get(KIND, f"w/{sid}") is None
    older = _app(tmp_path, store, "older")
    older.state.arc._segments.set_write_observer(None)  # a CLIO without this index
    time.sleep(0.01)  # the interaction time moves
    _converse(older, sid, "m2", "turbidity after the downgrade")
    third = _app(tmp_path, store, "third")
    got = _search(third, "downgrade", ws)
    assert [hit.message_id for hit in got.hits] == ["m2"]
    assert got.metadata["index"]["transcript_reasons"] == {MOVED_REASON: 1}
    assert recall_index_of(third).flush(10.0)  # type: ignore[union-attr]
    report = DigestReadReport()
    stamps = {sid: third.state.sessions.get(sid).last_interaction_at}
    found = dict(session_digests(third, [sid], report, stamps=stamps))
    assert report.index == 1
    assert found[sid] == digest_messages(assemble_session_messages(third.state.arc, sid))
