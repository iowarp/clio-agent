"""#1339 Slice A: the ``message_part`` atom lane (``_events/m``) rolls into chunks.

Every ``message_part`` atom used to append to the single ``_events/m`` scope, whose
*entire* encoded record was re-written on every atom — the same O(N^2)-per-session
amplification #771 Slice A fixed for the semantic-event log. This slice rolls the atom
lane into fixed-size chunks (``_events/m``, ``_events/m/2``, …) keyed by
``arc.message_part_chunk_segments`` through the shared owner module
``arc.lane_chunking``. These tests pin the contract for THIS lane specifically
(mirroring ``tests/test_arc/test_events_chunking.py``'s amplification-gate style):

* roll-over produces the expected chunk family + per-chunk counts;
* the assembled transcript (``transcript_projection.assemble_session_messages``) is
  byte-identical at chunk-infinity and at a small chunk size — chunking is invisible;
* the amplification gate: an append re-puts only the active chunk, never the whole lane;
* legacy (pre-chunking) single-scope lanes are read as chunk 1 and continue there;
* the lifecycle erasers (``on_ledger_deleted`` / ``on_ledger_replaced``) drop the WHOLE
  family and the projection / ARC working set stay correct across a roll;
* ``has_atoms`` / warm ``assemble_session_messages`` stay O(1) / O(0) cold-get budgets;
* no ``arc.op`` is ever emitted for an atom append (the raw §2.9 lane, chunking or not);
* minting through ``PartAtomMinter`` (the real off-loop writer) across a roll never
  trips the #1334 loop-thread store-write guard.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator, Optional

import pytest

from clio_agent import conf
from clio_agent.arc.lane_chunking import chunk_scope, lane_scopes, lane_segments
from clio_agent.arc.loop_guard import (
    guard_hits,
    register_server_loop,
    reset_guard_hits,
    unregister_server_loop,
)
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.part_atom_minter import PartAtomMinter
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    group_atoms_in_order,
    mint_message_part_atoms,
)
from clio_agent.gact.transcript_projection import (
    assemble_session_messages,
    has_atoms,
    materialize_ledger,
    on_ledger_deleted,
    on_ledger_replaced,
)
from clio_agent.gact.types import Message, Part
from tests._config_layer import set_config

SID = "sess-atoms"


@pytest.fixture()
def hermetic_conf(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the process-wide config store at an empty home/cwd so a developer's real
    config file can never set ``arc.message_part_chunk_segments`` over the test's env
    (mirrors ``tests/test_arc/test_events_chunking.py::hermetic_conf``)."""
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=tmp_path, cwd=tmp_path))


def _message(mid: str, text: str, *, session_id: str = SID, role: str = "assistant") -> Message:
    return Message(
        id=mid,
        turn_id=f"turn_{mid}",
        session_id=session_id,
        role=role,
        created_at="2026-07-12T10:00:00+00:00",
        updated_at="2026-07-12T10:00:00+00:00",
        parts=[Part(id=f"{mid}_p0", type="text", text=text)],
    )


def _fake_app(arc: ARCMemory, *, message_store: Any = None) -> Any:
    state = type("State", (), {"arc": arc, "message_store": message_store})()
    return type("App", (), {"state": state})()


class _FakeMessageStore:
    """Minimal retained-ledger store (``load_session`` only, what ``materialize_ledger``
    reads on the no-atoms path)."""

    def __init__(self, ledger: list[Message]) -> None:
        self._ledger = list(ledger)

    def load_session(self, session_id: str) -> list[Message]:
        return list(self._ledger)


# --------------------------------------------------------------------------- #
# CountingStore -- modelled on test_events_chunking.py:140-181, +put_names
# --------------------------------------------------------------------------- #


class CountingStore:
    """In-memory :class:`~clio_agent.arc.storage.ARCStore` that counts bytes put to the
    ``segments`` kind and records every put's record NAME in order, so a test can assert
    exactly which chunk record was re-written by one append."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}
        self.segment_put_bytes = 0
        self.max_segment_record = 0
        self.get_calls = 0
        self.put_names: list[str] = []

    def put(
        self, kind: str, name: str, data: bytes, *, tier: str = "warm", search_text: Any = None
    ) -> None:
        self._data[(kind, name)] = data
        if kind == "segments":
            self.segment_put_bytes += len(data)
            self.max_segment_record = max(self.max_segment_record, len(data))
            self.put_names.append(name)

    def get(self, kind: str, name: str) -> Optional[bytes]:
        self.get_calls += 1
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


# --------------------------------------------------------------------------- #
# Roll-over + byte-identity
# --------------------------------------------------------------------------- #


def test_rollover_chunk_family_and_counts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "4")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    for i in range(10):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"text {i}"))

    scopes = lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE)
    assert scopes == [
        MESSAGE_PART_SCOPE,
        chunk_scope(MESSAGE_PART_SCOPE, 2),
        chunk_scope(MESSAGE_PART_SCOPE, 3),
    ]
    counts = [len(arc._segments.list_segments(SID, s, include_tombstoned=True)) for s in scopes]
    assert counts == [4, 4, 2]


def test_assembled_transcript_identical_across_chunk_sizes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    messages = [_message(f"m{i}", f"text {i}") for i in range(9)]

    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", str(10**6))
    huge = ARCMemory(data_dir=str(tmp_path / "huge"))
    for m in messages:
        mint_message_part_atoms(huge, SID, m)
    huge_assembled = assemble_session_messages(huge, SID)

    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "3")
    small = ARCMemory(data_dir=str(tmp_path / "small"))
    for m in messages:
        mint_message_part_atoms(small, SID, m)
    small_assembled = assemble_session_messages(small, SID)

    assert len(lane_scopes(small._segments, SID, MESSAGE_PART_SCOPE)) == 3
    assert [m.model_dump(exclude_none=True) for m in huge_assembled] == [
        m.model_dump(exclude_none=True) for m in small_assembled
    ]


def test_lane_order_matches_logical_time_and_grouping_is_identical(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    for i in range(8):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"text {i}"))

    segs = lane_segments(arc._segments, SID, MESSAGE_PART_SCOPE, include_tombstoned=False)
    logical_times = [s.logical_time for s in segs]
    assert logical_times == sorted(logical_times)

    lane_contents = [s.content for s in segs]
    groups = group_atoms_in_order(lane_contents)
    assert [g[0]["message_id"] for g in groups] == [f"m{i}" for i in range(8)]


# --------------------------------------------------------------------------- #
# Amplification gate: an append re-puts ONLY the active chunk
# --------------------------------------------------------------------------- #


def test_append_amplification_is_o_chunk(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    messages = [_message(f"m{i}", f"text payload number {i}" * 4) for i in range(64)]

    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "100000")
    quadratic = CountingStore()
    base = ARCMemory(data_dir=str(tmp_path / "base"), store=quadratic)
    for m in messages:
        mint_message_part_atoms(base, SID, m)
    assert lane_scopes(base._segments, SID, MESSAGE_PART_SCOPE) == [MESSAGE_PART_SCOPE]
    assert quadratic.max_segment_record > 0

    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "8")
    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    for m in messages:
        mint_message_part_atoms(arc, SID, m)

    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == [
        chunk_scope(MESSAGE_PART_SCOPE, i) for i in range(1, 9)
    ]
    assert store.max_segment_record > 0
    assert store.max_segment_record < quadratic.max_segment_record / 4
    assert store.segment_put_bytes < quadratic.segment_put_bytes / 4


def test_one_append_re_puts_only_the_active_chunk(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    for i in range(3):  # chunk1=[2], chunk2=[1] (active)
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    store.put_names.clear()
    mint_message_part_atoms(arc, SID, _message("m3", "t3"))  # rolls chunk2 -> full(2)

    from clio_agent.arc.segments import SegmentStore

    expected = SegmentStore._record_name(SID, chunk_scope(MESSAGE_PART_SCOPE, 2))
    assert store.put_names == [expected]


# --------------------------------------------------------------------------- #
# Legacy (pre-chunking) lanes read as chunk 1 and continue there
# --------------------------------------------------------------------------- #


def test_legacy_single_scope_lane_reads_as_chunk_one(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "512")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    # Written directly at the bare scope, exactly as pre-#1339 code did (no cursor).
    for content in [{"message_id": "m0", "part_id": "p0", "part_index": 0, "part": {"id": "p0"}}]:
        from clio_agent.gact.part_atoms import _append_segment_raw

        _append_segment_raw(arc._segments, SID, MESSAGE_PART_SCOPE, "message_part", content)

    assert has_atoms(arc, SID) is True
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == [MESSAGE_PART_SCOPE]


def test_legacy_lane_continues_in_chunk_one_until_capacity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "3")
    from clio_agent.gact.part_atoms import _append_segment_raw

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    _append_segment_raw(
        arc._segments, SID, MESSAGE_PART_SCOPE, "message_part", {"message_id": "legacy0"}
    )
    # The cursor is cold (no prior chunk_for_append call); it must recover onto the
    # legacy bare scope and CONTINUE filling it, not start a parallel chunk 2.
    mint_message_part_atoms(arc, SID, _message("m1", "t1"))
    mint_message_part_atoms(arc, SID, _message("m2", "t2"))
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == [MESSAGE_PART_SCOPE]
    assert len(arc._segments.list_segments(SID, MESSAGE_PART_SCOPE, include_tombstoned=True)) == 3


def test_legacy_oversized_lane_rolls_on_next_append_chunk_one_never_re_put(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    from clio_agent.gact.part_atoms import _append_segment_raw

    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    # A legacy lane already OVER capacity (5 > 2), written directly (no cursor ever ran).
    for i in range(5):
        _append_segment_raw(
            arc._segments, SID, MESSAGE_PART_SCOPE, "message_part", {"message_id": f"legacy{i}"}
        )

    store.put_names.clear()
    mint_message_part_atoms(arc, SID, _message("m_new", "fresh"))  # cold cursor recovers

    from clio_agent.arc.segments import SegmentStore

    chunk1_name = SegmentStore._record_name(SID, MESSAGE_PART_SCOPE)
    chunk2_name = SegmentStore._record_name(SID, chunk_scope(MESSAGE_PART_SCOPE, 2))
    assert chunk1_name not in store.put_names  # never re-put
    assert store.put_names == [chunk2_name]
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == [
        MESSAGE_PART_SCOPE,
        chunk_scope(MESSAGE_PART_SCOPE, 2),
    ]


# --------------------------------------------------------------------------- #
# Read-cost budgets
# --------------------------------------------------------------------------- #


def test_has_atoms_is_exactly_one_get(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    for i in range(6):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    fresh_segments = arc._segments.__class__(store, search_indexed=lambda scope: True)
    store.get_calls = 0
    assert has_atoms(type("A", (), {"_segments": fresh_segments})(), SID) is True
    assert store.get_calls == 1


def test_warm_assemble_costs_zero_gets(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    store = CountingStore()
    arc = ARCMemory(data_dir=str(tmp_path / "arc"), store=store)
    for i in range(6):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    assemble_session_messages(arc, SID)  # warms the cache
    store.get_calls = 0
    assemble_session_messages(arc, SID)
    assert store.get_calls == 0


# --------------------------------------------------------------------------- #
# Lifecycle erasers: whole family, projection stays correct, ARC memory untouched
# --------------------------------------------------------------------------- #


def test_delete_drops_every_chunk_and_next_mint_starts_at_chunk_one(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "1")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = _fake_app(arc)
    for i in range(3):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))
    assert len(lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE)) == 3

    on_ledger_deleted(app, SID)
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == []

    mint_message_part_atoms(arc, SID, _message("fresh", "hello"))
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == [MESSAGE_PART_SCOPE]


def test_replace_rematerializes_across_chunks_with_no_resurrected_atoms(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "1")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = _fake_app(arc)
    for i in range(3):
        mint_message_part_atoms(arc, SID, _message(f"old{i}", f"old-text-{i}"))

    new_messages = [_message("new0", "new-text-0"), _message("new1", "new-text-1")]
    on_ledger_replaced(app, SID, new_messages)

    assembled = assemble_session_messages(arc, SID)
    assert [m.id for m in assembled] == ["new0", "new1"]

    lane = lane_segments(arc._segments, SID, MESSAGE_PART_SCOPE, include_tombstoned=True)
    texts = {(seg.content.get("part") or {}).get("text") for seg in lane if seg.content.get("part")}
    assert not any(t and t.startswith("old-text") for t in texts)


def test_arc_working_set_untouched_across_a_roll(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.append_segment(SID, "agentA", "observation", {"text": "unrelated working set"})
    for i in range(6):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    working = arc.render_working_set(SID, "agentA")
    assert [s.content["text"] for s in working] == ["unrelated working set"]
    for scope in lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE):
        assert arc.render_working_set(SID, scope) == []  # message_part is never working-set


def test_edge_survives_drop_lane(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "1")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = _fake_app(arc)
    edge_scope = f"{MESSAGE_PART_SCOPE}/edge"
    from clio_agent.gact.part_atoms import _append_segment_raw

    _append_segment_raw(arc._segments, SID, edge_scope, "message_part", {"text": "streaming..."})
    for i in range(3):
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    on_ledger_deleted(app, SID)

    assert len(arc._segments.list_segments(SID, edge_scope, include_tombstoned=True)) == 1


# --------------------------------------------------------------------------- #
# Backfill after a durable-backend release erase
# --------------------------------------------------------------------------- #


def test_backfill_after_trace_enabled_release_reassembles_from_retained_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "1")
    set_config("trace.backend", "file")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    messages = [_message(f"m{i}", f"t{i}") for i in range(3)]
    for m in messages:
        mint_message_part_atoms(arc, SID, m)
    assert len(lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE)) == 3

    arc.release_session(SID)  # trace-enabled erase: the WHOLE ``_events`` family, atoms incl.
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) == []

    app = _fake_app(arc, message_store=_FakeMessageStore(messages))
    result = materialize_ledger(app, SID)
    assert result is not None
    assert [m.id for m in result] == [m.id for m in messages]
    assert lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE) != []  # re-minted


def test_erase_then_mint_then_read_shows_every_new_atom(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    app = _fake_app(arc)
    for i in range(4):
        mint_message_part_atoms(arc, SID, _message(f"old{i}", f"old{i}"))
    on_ledger_deleted(app, SID)

    fresh = [_message(f"new{i}", f"new{i}") for i in range(5)]
    for m in fresh:
        mint_message_part_atoms(arc, SID, m)

    assembled = assemble_session_messages(arc, SID)
    assert [m.id for m in assembled] == [m.id for m in fresh]


# --------------------------------------------------------------------------- #
# arc.op discipline: the raw lane never logs an op, chunking or not
# --------------------------------------------------------------------------- #


def test_zero_arc_op_across_a_roll(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "2")
    logged: list[dict[str, Any]] = []

    def op_logger(op: str, session_id: str, scope: str, **kw: Any) -> dict[str, Any]:
        ev = {"event_id": f"ev{len(logged) + 1}", "op": op, "scope": scope, **kw}
        logged.append(ev)
        return ev

    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.set_segment_op_logger(op_logger)
    for i in range(9):  # several rolls at capacity 2
        mint_message_part_atoms(arc, SID, _message(f"m{i}", f"t{i}"))

    assert logged == []


# --------------------------------------------------------------------------- #
# PartAtomMinter across a roll: zero #1334 loop-thread guard hits
# --------------------------------------------------------------------------- #


def test_minter_mint_across_chunks_zero_guard_hits(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, hermetic_conf: None
) -> None:
    monkeypatch.setenv("CLIO_ARC_MESSAGE_PART_CHUNK_SEGMENTS", "4")
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    reset_guard_hits()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        register_server_loop(loop)
        try:
            minter = PartAtomMinter(session_id=SID, turn_id="t1", arc=arc)
            for i in range(12):
                part = {"id": f"p{i}", "type": "text", "text": f"chunk atom {i}"}
                assert minter.seal("msg1", part, i, source="live") is True
            minter.drain()
            minter.close()
        finally:
            unregister_server_loop(loop)

    asyncio.run(_run())

    assert len(lane_scopes(arc._segments, SID, MESSAGE_PART_SCOPE)) >= 3  # a real roll happened
    assert [h for h in guard_hits() if h[0] == "write"] == []
