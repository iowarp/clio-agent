"""#771 Slice A: the ``_events`` log is a CHUNK FAMILY, not one ever-growing scope.

Every semantic event used to append to a single ``_events`` scope whose *entire*
encoded record was re-written on every append — Θ(N²) bytes per session on the turn
hot path. Slice A rolls the log into fixed-size chunks (``_events``, ``_events/2``,
…) keyed by ``arc.events_chunk_segments`` so a single append re-encodes only the
active chunk (O(chunk)). These tests pin the contract:

* roll-over produces the expected chunk family, and the projections
  (``view`` / ``project_conversation`` / ``project_invocations``) are byte-identical
  to the chunk-∞ (single-scope) baseline — chunking is invisible to every reader;
* the amplification gate: every append sends exactly ONE event's chunk record (plus
  the end marker), whatever the active chunk already holds (append-only chunk
  records, ``arc/chunk_records.py``) — O(record), not O(chunk);
* the ``_events`` family NEVER surfaces in scope search (both backends);
* cold-start recovery resumes the last persisted chunk instead of overwriting it.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import msgspec
import pytest

from clio_agent import conf
from clio_agent.arc.chunk_records import END, MAGIC, encode_records
from clio_agent.arc.live import events_chunk_scope
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.semantic_events import SemanticEvent

SID = "s1"


@pytest.fixture()
def hermetic_conf(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the process-wide config store at an empty home/cwd so a developer's real
    config file can never set ``arc.events_chunk_segments`` over the test's env."""
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))


def _events(n: int, *, sid: str = SID, turn_offset: int = 0) -> list[SemanticEvent]:
    """``n`` coherent semantic events grouped 3-per-turn (started / expert / completed)
    so the projections have real Q/A + invocation content to compare."""
    out: list[SemanticEvent] = []
    for i in range(n):
        turn = turn_offset + i // 3
        phase = i % 3
        tid = f"t{turn}"
        tr = f"tr{turn}"
        if phase == 0:
            out.append(
                SemanticEvent(
                    event_type="turn.started",
                    session_id=sid,
                    trace_id=tr,
                    turn_id=tid,
                    occurred_at=f"2026-06-14T{turn // 60:02d}:{turn % 60:02d}:00+00:00",
                    payload={"input": f"question {turn}"},
                )
            )
        elif phase == 1:
            out.append(
                SemanticEvent(
                    event_type="expert.response.completed",
                    session_id=sid,
                    trace_id=tr,
                    turn_id=tid,
                    occurred_at=f"2026-06-14T{turn // 60:02d}:{turn % 60:02d}:30+00:00",
                    actor={"agent_id": "data"},
                    payload={"answer": f"answer {turn}", "reasoning": "reasoned"},
                )
            )
        else:
            out.append(
                SemanticEvent(
                    event_type="turn.completed",
                    session_id=sid,
                    trace_id=tr,
                    turn_id=tid,
                    occurred_at=f"2026-06-14T{turn // 60:02d}:{turn % 60:02d}:45+00:00",
                    payload={},
                )
            )
    return out


def _projections(arc: ARCMemory, sid: str = SID) -> tuple[Any, Any, Any]:
    """The three reader projections in a stable, id-independent form (``Message`` carries
    a random ``message_id``, so compare its content fields only)."""
    conv = arc.project_live_conversation(sid)
    conv_msgs = (
        None
        if conv is None
        else [(m.role, m.content, m.timestamp, m.metadata) for m in conv.messages]
    )
    invs = [msgspec.to_builtins(i) for i in arc.project_live_invocations(sid)]
    view = arc.get_live_context(sid)
    return conv_msgs, invs, view


def _chunk_counts(arc: ARCMemory, sid: str = SID) -> list[int]:
    return [len(arc.render_segments(sid, s)) for s in arc._live.events_scopes(sid)]


# --------------------------------------------------------------------------- #
# roll-over + byte-identity to the chunk-∞ baseline
# --------------------------------------------------------------------------- #


def test_rollover_projections_byte_identical_to_single_scope(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    events = _events(10)

    # Baseline: chunk-∞ (one huge chunk == the old single-scope behavior).
    monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "100000")
    base = ARCMemory(data_dir=str(tmp_path / "base"))
    for e in events:
        base.record_semantic_event(e)

    # Chunked: chunk 4 -> 10 events roll into 3 chunks of [4, 4, 2].
    monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "4")
    chunked = ARCMemory(data_dir=str(tmp_path / "chunked"))
    for e in events:
        chunked.record_semantic_event(e)

    assert base._live.events_scopes(SID) == ["_events"]
    assert chunked._live.events_scopes(SID) == ["_events", "_events/2", "_events/3"]
    assert _chunk_counts(chunked) == [4, 4, 2]

    # Chunking is invisible: every projection matches the single-scope baseline exactly.
    assert _projections(chunked) == _projections(base)


# --------------------------------------------------------------------------- #
# amplification gate: O(chunk) appends, not Θ(N²)
# --------------------------------------------------------------------------- #


class CountingStore:
    """In-memory :class:`~clio_agent.arc.storage.ARCStore` that records every put to the
    ``segments`` kind (the ``_events`` records, since the test writes only events), so
    the bytes each append sends can be measured directly. ``put_at`` behaves as
    clio-core's offset put: it grows the record and never shrinks it."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self.segment_puts: list[bytes] = []

    def put(
        self, kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: Any = None
    ) -> None:
        self._data[(kind, name)] = data
        if kind == "segments":
            self.segment_puts.append(data)

    def put_at(
        self, kind: str, name: str, data: bytes, offset: int, *, search_text: Any = None
    ) -> None:
        held = bytearray(self._data.get((kind, name), b""))
        held.extend(bytes(max(0, offset - len(held))))
        held[offset : offset + len(data)] = data
        self._data[(kind, name)] = bytes(held)
        if kind == "segments":
            self.segment_puts.append(data)

    def get(self, kind: str, name: str) -> Optional[bytes]:
        return self._data.get((kind, name))

    def get_raw(self, kind: str, name: str, *, length: int = 0) -> Optional[bytes]:
        return self._data.get((kind, name))

    def exists(self, kind: str, name: str) -> bool:
        return (kind, name) in self._data

    def scan_names(self, kind: str, prefix: str = "") -> Iterator[str]:
        """List test-store record addresses."""
        for name, _body in self.scan(kind, prefix):
            yield name

    def scan(self, kind: str, prefix: str = "") -> Iterator[tuple[str, bytes]]:
        for (k, name), data in list(self._data.items()):
            if k == kind and name.startswith(prefix):
                yield name, data

    def delete(self, kind: str, name: str) -> None:
        self._data.pop((kind, name), None)

    def clear(self) -> None:
        self._data.clear()

    def supports_search(self) -> bool:
        return False

    def search(
        self, kind: str, query_text: str, *, name_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        return []


def _records(arc: ARCMemory) -> list[bytes]:
    """Each stored event's own chunk record (header + msgpack payload), in append order."""
    return [
        encode_records([segment])
        for scope in arc._live.events_scopes(SID)
        for segment in arc._segments.list_segments(SID, scope, include_tombstoned=True)
    ]


def test_append_amplification_is_o_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    """An append sends ONE event's record, whatever the chunk holds already.

    Exact byte accounting, not a ratio: every put is one event's record and the end
    marker (after the two magic bytes when it opens a chunk), then only zero padding --
    sent when the put grows the blob past its size class, never more in all than the
    chunk's own final size. A regression to re-putting the active chunk sends its whole
    growing body instead and fails the accounting from the second event of a chunk on.
    """
    events = _events(64)
    for capacity, chunks in ((100000, 1), (8, 8)):
        monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", str(capacity))
        store = CountingStore()
        arc = ARCMemory(data_dir=str(tmp_path / str(capacity)), store=store)
        for e in events:
            arc.record_semantic_event(e)
        assert arc._live.events_scopes(SID) == [events_chunk_scope(i) for i in range(1, chunks + 1)]
        records = _records(arc)
        assert len(records) == len(store.segment_puts) == 64  # one put per append
        padding = 0
        for index, (record, put) in enumerate(zip(records, store.segment_puts, strict=True)):
            head = (MAGIC if index % capacity == 0 else b"") + record + END
            assert put[: len(head)] == head  # this event's record, nothing earlier
            assert not any(put[len(head) :])  # then zero padding at most
            padding += len(put) - len(head)
        stored = sum(len(blob) for blob in store._data.values())
        assert padding <= stored  # padding grows the blob in classes, never per append


# --------------------------------------------------------------------------- #
# search hygiene: the _events family never pollutes scope search (both backends)
# --------------------------------------------------------------------------- #


def test_events_family_never_gets_a_search_companion(arc: ARCMemory) -> None:
    """The ``_events`` family never writes a search companion (the log must never
    pollute scope search), while an ordinary expert scope does. Read directly from
    clio-core: its search itself is unavailable (#905)."""
    for e in _events(6):
        arc.record_semantic_event(e)
    arc.append_segment(SID, "agentA", "observation", {"text": "alpha beta question answer"})

    store = arc._store
    tag = store._cte.Tag(store.tag("segments"))
    companions = {n for n in tag.GetContainedBlobs() if n.endswith(".text")}
    events_companions = [n for n in companions if "_events" in n]
    assert events_companions == []
    assert any("agentA" in n for n in companions)


# --------------------------------------------------------------------------- #
# cold-start recovery: resume the last persisted chunk, don't overwrite it
# --------------------------------------------------------------------------- #


def test_cold_start_resumes_last_chunk(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "4")
    data_dir = str(tmp_path / "arc")

    arc1 = ARCMemory(data_dir=data_dir)
    for e in _events(10):
        arc1.record_semantic_event(e)  # chunks [4, 4, 2]
    assert arc1._live.events_scopes(SID) == ["_events", "_events/2", "_events/3"]
    assert _chunk_counts(arc1) == [4, 4, 2]

    # A fresh ARCMemory over the SAME persisted store has NO in-memory write cursor: it
    # must recover from the family and resume chunk 3 (which held 2), not restart at 1.
    arc2 = ARCMemory(data_dir=data_dir)
    batch = _events(3, turn_offset=100)
    arc2.record_semantic_event(batch[0])  # chunk 3: 2 -> 3
    arc2.record_semantic_event(batch[1])  # chunk 3: 3 -> 4 (now full)
    assert arc2._live.events_scopes(SID) == ["_events", "_events/2", "_events/3"]
    assert _chunk_counts(arc2) == [4, 4, 4]

    arc2.record_semantic_event(batch[2])  # full -> rolls to chunk 4
    assert arc2._live.events_scopes(SID) == ["_events", "_events/2", "_events/3", "_events/4"]
    assert _chunk_counts(arc2) == [4, 4, 4, 1]

    # The recovered writer never overwrote earlier chunks: all 13 events survive in order.
    all_scopes = arc2._live.events_scopes(SID)
    total = sum(len(arc2.render_segments(SID, s)) for s in all_scopes)
    assert total == 13
