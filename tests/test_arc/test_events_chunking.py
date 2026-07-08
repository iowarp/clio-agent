"""#771 Slice A: the ``_events`` log is a CHUNK FAMILY, not one ever-growing scope.

Every semantic event used to append to a single ``_events`` scope whose *entire*
encoded record was re-written on every append — Θ(N²) bytes per session on the turn
hot path. Slice A rolls the log into fixed-size chunks (``_events``, ``_events/2``,
…) keyed by ``arc.events_chunk_segments`` so a single append re-encodes only the
active chunk (O(chunk)). These tests pin the contract:

* roll-over produces the expected chunk family, and the projections
  (``view`` / ``project_conversation`` / ``project_invocations``) are byte-identical
  to the chunk-∞ (single-scope) baseline — chunking is invisible to every reader;
* the amplification gate: 64 events at chunk 8 write a small fraction of the bytes a
  chunk-∞ (single-scope, Θ(N²)) replay of the same events writes, and no single
  append re-encodes more than one chunk — measured against that exogenous baseline;
* the ``_events`` family NEVER surfaces in scope search (both backends);
* cold-start recovery resumes the last persisted chunk instead of overwriting it.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import msgspec
import pytest

from clio_agent import conf
from clio_agent.arc.live import events_chunk_scope, is_events_scope
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
    """In-memory :class:`~clio_agent.arc.storage.ARCStore` that counts the bytes put to
    the ``segments`` kind (the ``_events`` records, since the test writes only events),
    so the append amplification can be measured directly."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self.segment_put_bytes = 0
        self.max_segment_record = 0

    def put(
        self, kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: Any = None
    ) -> None:
        self._data[(kind, name)] = data
        if kind == "segments":
            self.segment_put_bytes += len(data)
            self.max_segment_record = max(self.max_segment_record, len(data))

    def get(self, kind: str, name: str) -> Optional[bytes]:
        return self._data.get((kind, name))

    def exists(self, kind: str, name: str) -> bool:
        return (kind, name) in self._data

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


def test_append_amplification_is_o_chunk(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    events = _events(64)

    # Exogenous yardstick: replay the SAME 64 events at chunk-infinity (the old
    # single-scope behavior) through its own CountingStore. Every append re-encodes
    # the ENTIRE growing log — Sigma_{i=1..64} i == Theta(N^2) event-records of total
    # bytes — and its largest single record is the full 64-event log.
    monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "100000")
    quadratic = CountingStore()
    base = ARCMemory(data_dir=str(tmp_path / "base"), store=quadratic)
    for e in events:
        base.record_semantic_event(e)
    assert base._live.events_scopes(SID) == ["_events"]
    assert quadratic.max_segment_record > 0

    monkeypatch.setenv("CLIO_ARC_EVENTS_CHUNK_SEGMENTS", "8")
    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    for e in events:
        arc.record_semantic_event(e)

    # 64 events at chunk 8 -> exactly 8 chunk records (structural proof that the log
    # rolled; a regression to the single ``_events`` scope would leave one record).
    assert arc._live.events_scopes(SID) == [events_chunk_scope(i) for i in range(1, 9)]

    # O(chunk), measured against the quadratic baseline — NOT against the chunked
    # run's own records, which would normalize away the very amplification under test:
    #
    # * no single append re-encodes more than one chunk: the largest chunked record
    #   holds 8 of 64 events (~1/8 of the baseline's full-log record; bound 1/4
    #   leaves headroom for per-record overhead);
    # * the TOTAL bytes across all 64 appends are a small fraction of the quadratic
    #   total (~288 vs ~2080 event-records ~= 0.14 ideal; bound 1/4 again).
    #
    # Both a full regression to the single scope (total == baseline) and a dual-write
    # regression (chunk family intact but each append also rewriting a full-log
    # record: total > baseline, max record == full log) exceed BOTH bounds.
    assert store.max_segment_record > 0
    assert store.max_segment_record < quadratic.max_segment_record / 4
    assert store.segment_put_bytes < quadratic.segment_put_bytes / 4


# --------------------------------------------------------------------------- #
# search hygiene: the _events family never pollutes scope search (both backends)
# --------------------------------------------------------------------------- #


def test_events_family_never_in_search_scopes(arc: ARCMemory) -> None:
    for e in _events(6):
        arc.record_semantic_event(e)
    # A normal expert scope DOES get indexed, so search has a legitimate hit to return.
    arc.append_segment(SID, "agentA", "observation", {"text": "alpha beta question answer"})

    # Even a query built from the event log's own words must never surface an _events
    # chunk (its search companion is deliberately never written).
    hits = arc.search_segment_scopes(SID, "question answer reasoned alpha")
    returned = [scope for scope, _ in hits]
    assert all(not is_events_scope(s) for s in returned), returned
    # The legitimate expert scope is still discoverable (search itself works).
    assert "agentA" in returned


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
