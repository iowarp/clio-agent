"""Atomic transcript splices persist each staged chunk once, preserving raw atoms."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from clio_agent.arc.lane_chunking import lane_segments
from clio_agent.arc.lane_generations import current_base, replace_lane
from clio_agent.arc.schema import Segment
from clio_agent.gact import part_atoms
from clio_agent.gact.types import Message, Part


def _atoms(count: int) -> list[dict[str, Any]]:
    """Create independent public transcript inputs with their original identities."""
    return [
        atom
        for index in range(count)
        for atom in part_atoms.build_message_part_atoms(
            Message(
                id=f"message-{index}",
                session_id="bulk-generation",
                role="user",
                created_at=f"2026-10-18T12:{index:02d}:00+00:00",
                updated_at=f"2026-10-18T12:{index:02d}:00+00:00",
                parts=[Part(type="text", text=f"Recorded content {index}")],
            )
        )
    ]


def test_staged_generation_persists_each_chunk_once(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many raw atoms retain order and identity without one full-chunk write per atom."""
    store = arc._segments
    contents = _atoms(23)
    monkeypatch.setattr(part_atoms, "_chunk_capacity", lambda: 7)
    original = store._commit_scope
    writes: list[str] = []

    def commit(sid: str, scope: str, staged: list[Segment]) -> None:
        if scope.startswith(part_atoms.MESSAGE_PART_SCOPE + "/g/"):
            writes.append(scope)
        original(sid, scope, staged)

    monkeypatch.setattr(store, "_commit_scope", commit)
    replace_lane(
        store,
        "bulk-generation",
        part_atoms.MESSAGE_PART_SCOPE,
        lambda lane: part_atoms.append_part_atoms(store, "bulk-generation", contents, lane=lane),
    )
    base = current_base(store, "bulk-generation", part_atoms.MESSAGE_PART_SCOPE)
    recorded = list(lane_segments(store, "bulk-generation", base, include_tombstoned=False))
    assert [segment.content for segment in recorded] == contents
    assert len(writes) == 4 and len(set(writes)) == 4
    assert [segment.logical_time for segment in recorded] == sorted(
        {segment.logical_time for segment in recorded}
    )
    tail = _atoms(24)[-1]
    part_atoms.append_part_atom(store, "bulk-generation", tail)
    assert [
        segment.content
        for segment in lane_segments(store, "bulk-generation", base, include_tombstoned=False)
    ] == [*contents, tail]
    assert len(writes) == 5 and len(set(writes)) == 4


def test_multiple_atoms_require_an_unpublished_generation(arc: Any) -> None:
    """A bulk input cannot leave unconfirmed atoms visible on the current lane."""
    with pytest.raises(ValueError, match="unpublished generation"):
        part_atoms.append_part_atoms(arc._segments, "bulk-generation", _atoms(2))
    assert (
        list(
            lane_segments(
                arc._segments,
                "bulk-generation",
                part_atoms.MESSAGE_PART_SCOPE,
                include_tombstoned=False,
            )
        )
        == []
    )


def test_failed_staged_chunk_never_replaces_the_current_transcript(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing generation switch still owns visibility on a partial write failure."""
    from clio_agent.arc.lane_generations import LaneReplaceError

    store = arc._segments
    original_contents = _atoms(2)
    for content in original_contents:
        part_atoms.append_part_atom(store, "bulk-generation", content)
    previous = current_base(store, "bulk-generation", part_atoms.MESSAGE_PART_SCOPE)
    monkeypatch.setattr(part_atoms, "_chunk_capacity", lambda: 7)
    original = store._put_scope
    staged_writes = 0

    def fail_second_chunk(sid: str, scope: str, segs: list[Segment]) -> int:
        nonlocal staged_writes
        if scope.startswith(part_atoms.MESSAGE_PART_SCOPE + "/g/"):
            staged_writes += 1
            if staged_writes == 2:
                raise OSError("second staged chunk refused")
        return original(sid, scope, segs)

    monkeypatch.setattr(store, "_put_scope", fail_second_chunk)
    with pytest.raises(LaneReplaceError) as failed:
        replace_lane(
            store,
            "bulk-generation",
            part_atoms.MESSAGE_PART_SCOPE,
            lambda lane: part_atoms.append_part_atoms(
                store, "bulk-generation", _atoms(23), lane=lane
            ),
        )
    assert failed.value.stage == "write"
    assert current_base(store, "bulk-generation", part_atoms.MESSAGE_PART_SCOPE) == previous
    assert [
        segment.content
        for segment in lane_segments(store, "bulk-generation", previous, include_tombstoned=False)
    ] == original_contents


def test_live_atom_is_hidden_until_its_core_write_settles(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconstruction cannot read a failed, unconfirmed live atom after a switch."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from clio_agent.arc.segments import ArcPersistError
    from clio_agent.gact.transcript_projection import assemble_session_messages

    store = arc._segments
    sid = "bulk-generation"
    replace_lane(
        store,
        sid,
        part_atoms.MESSAGE_PART_SCOPE,
        lambda lane: part_atoms.append_part_atoms(store, sid, _atoms(2), lane=lane),
    )
    previous = assemble_session_messages(arc, sid)
    scope = current_base(store, sid, part_atoms.MESSAGE_PART_SCOPE)
    record = store._record_name(sid, scope)
    original_put = store._store.put
    entered, release = threading.Event(), threading.Event()
    reading, read_done = threading.Event(), threading.Event()

    def failing_put(kind: str, name: str, payload: bytes, **kwargs: Any) -> None:
        if kind == "segments" and name == record:
            entered.set()
            assert release.wait(3), "test did not release its native write"
            raise OSError("live chunk write failed")
        original_put(kind, name, payload, **kwargs)

    def reconstruct() -> Any:
        reading.set()
        try:
            return assemble_session_messages(arc, sid)
        finally:
            read_done.set()

    monkeypatch.setattr(store._store, "put", failing_put)
    with ThreadPoolExecutor(max_workers=2) as threads:
        writer = threads.submit(part_atoms.append_part_atom, store, sid, _atoms(3)[-1])
        try:
            assert entered.wait(2)
            reader = threads.submit(reconstruct)
            assert reading.wait(2)
            assert not read_done.wait(0.1), "reader observed a live atom before write confirmation"
        finally:
            release.set()
        with pytest.raises(ArcPersistError):
            writer.result(timeout=3)
        assert reader.result(timeout=3) == previous


def test_replayed_atoms_copy_confirmed_content_and_normalize_only_new_input(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A splice keeps detached raw payloads without recursively recoercing history."""
    from clio_agent.arc import segments

    store = arc._segments
    sid = "bulk-generation"
    contents = _atoms(3)
    contents[0]["foreign"] = b"original bytes"
    for content in contents:
        part_atoms.append_part_atom(store, sid, content)
    retained = list(
        lane_segments(store, sid, part_atoms.MESSAGE_PART_SCOPE, include_tombstoned=False)
    )
    expected = [segment.content for segment in retained]
    assert expected[0]["foreign"] == "b'original bytes'"
    fresh = _atoms(4)[-1]
    fresh["foreign"] = b"new bytes"
    original = segments._coerce_content
    normalized: list[dict[str, Any]] = []

    def normalize(content: dict[str, Any]) -> dict[str, Any]:
        normalized.append(content)
        return original(content)

    monkeypatch.setattr(segments, "_coerce_content", normalize)
    replace_lane(
        store,
        sid,
        part_atoms.MESSAGE_PART_SCOPE,
        lambda lane: part_atoms.append_part_atoms(store, sid, [*retained, fresh], lane=lane),
    )
    recorded = list(
        lane_segments(
            store,
            sid,
            current_base(store, sid, part_atoms.MESSAGE_PART_SCOPE),
            include_tombstoned=False,
        )
    )
    assert normalized == [fresh]
    assert [segment.content for segment in recorded] == [*expected, original(fresh)]
    assert recorded[-1].content["foreign"] == "b'new bytes'"
    retained[0].content["part"]["text"] = "old generation changed"
    assert recorded[0].content["part"]["text"] == "Recorded content 0"
    recorded[1].content["message"]["metadata"]["new"] = True
    assert "new" not in retained[1].content["message"]["metadata"]


def test_retained_atom_replay_rejects_another_session(arc: Any) -> None:
    """A retained payload cannot be relabelled as another session's history."""
    source = part_atoms.append_part_atom(arc._segments, "bulk-generation", _atoms(1)[0])
    with pytest.raises(ValueError, match="retained atom identity"):
        part_atoms.append_part_atoms(
            arc._segments, "other-session", [source], lane=part_atoms.MESSAGE_PART_SCOPE + "/g/test"
        )


def test_retained_atom_replay_requires_an_unpublished_generation(arc: Any) -> None:
    """Live append retains its original normalization and confirmation boundary."""
    source = part_atoms.append_part_atom(arc._segments, "bulk-generation", _atoms(1)[0])
    with pytest.raises(ValueError, match="unpublished generation"):
        part_atoms.append_part_atoms(arc._segments, "bulk-generation", [source])


def test_retained_atom_replay_rejects_another_kind(arc: Any) -> None:
    """Another canonical lane cannot be reinterpreted as message-part history."""
    import msgspec

    source = part_atoms.append_part_atom(arc._segments, "bulk-generation", _atoms(1)[0])
    incompatible = msgspec.structs.replace(source, kind="observation")
    with pytest.raises(ValueError, match="retained atom identity"):
        part_atoms.append_part_atoms(
            arc._segments,
            "bulk-generation",
            [incompatible],
            lane=part_atoms.MESSAGE_PART_SCOPE + "/g/test",
        )


def test_staged_order_allocation_does_not_rescan_every_prior_atom(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm linear traversal work while preserving actual Core chunk publication."""
    store = arc._segments
    original = store._segs
    visited = 0

    class CountedSegments(list[Segment]):
        """Count Python traversal without replacing storage or segment behavior."""

        def __iter__(self) -> Iterator[Segment]:
            nonlocal visited
            visited += len(self)
            return super().__iter__()

    def observed(sid: str, scope: str) -> list[Segment]:
        result = original(sid, scope)
        if scope.startswith(part_atoms.MESSAGE_PART_SCOPE + "/g/") and not isinstance(
            result, CountedSegments
        ):
            result = CountedSegments(result)
            store._scopes[sid, scope] = result
        return result

    monkeypatch.setattr(store, "_segs", observed)
    contents = [{"message_id": f"linear-{index}", "part_index": 0} for index in range(200)]
    replace_lane(
        store,
        "bulk-generation",
        part_atoms.MESSAGE_PART_SCOPE,
        lambda lane: part_atoms.append_part_atoms(store, "bulk-generation", contents, lane=lane),
    )
    assert visited <= len(contents) * 6, "An unpublished batch repeatedly scanned its whole prefix"
    base = current_base(store, "bulk-generation", part_atoms.MESSAGE_PART_SCOPE)
    recorded = list(lane_segments(store, "bulk-generation", base, include_tombstoned=False))
    assert [segment.content for segment in recorded] == contents
    assert [segment.order for segment in recorded] == list(range(1, 201))


def test_staged_order_continues_from_the_maximum_of_an_existing_chunk(arc: Any) -> None:
    """Continued batches retain legacy gaps even if physical storage order differs."""
    store, sid = arc._segments, "bulk-generation"
    contents = _atoms(6)

    def write(lane: str) -> None:
        first, second = (
            part_atoms._append_segment_raw(
                store, sid, lane, part_atoms.MESSAGE_PART_KIND, content, persist=False, order=o
            )
            for content, o in ((contents[0], 10.0), (contents[1], 2.0))
        )
        with store._lock_for(sid, lane):
            store._commit_scope(sid, lane, [first, second])
        part_atoms.append_part_atoms(store, sid, contents[2:5], lane=lane)
        part_atoms.append_part_atoms(store, sid, contents[5:], lane=lane)

    replace_lane(store, sid, part_atoms.MESSAGE_PART_SCOPE, write)
    base = current_base(store, sid, part_atoms.MESSAGE_PART_SCOPE)
    recorded = list(lane_segments(store, sid, base, include_tombstoned=False))
    assert [segment.order for segment in recorded] == [10, 2, 11, 12, 13, 14]
    assert [segment.content for segment in recorded] == contents


def test_unpublished_atom_requires_an_order(arc: Any) -> None:
    """A staged atom is only built: its batch names the order it continues."""
    with pytest.raises(ValueError, match="names each atom's order"):
        part_atoms._append_segment_raw(
            arc._segments,
            "bulk-generation",
            part_atoms.MESSAGE_PART_SCOPE,
            part_atoms.MESSAGE_PART_KIND,
            _atoms(1)[0],
            persist=False,
        )


def test_staged_batch_reaches_the_cache_only_after_its_commit(
    arc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write-through: while a chunk's put is in flight the cache holds no staged atom,
    and a refused put leaves no staged atom cached."""
    store, sid = arc._segments, "bulk-generation"
    seen_in_cache: list[int] = []
    original = store._put_scope

    def observe_then_refuse(session_id: str, scope: str, segs: list[Segment]) -> int:
        if scope.startswith(part_atoms.MESSAGE_PART_SCOPE + "/g/"):
            seen_in_cache.append(len(store._scopes.get((session_id, scope), [])))
            raise OSError("refused")
        return original(session_id, scope, segs)

    monkeypatch.setattr(store, "_put_scope", observe_then_refuse)
    staged_chunks: list[str] = []

    def write(lane: str) -> None:
        staged_chunks.append(lane)
        part_atoms.append_part_atoms(store, sid, _atoms(3), lane=lane)

    from clio_agent.arc.lane_generations import LaneReplaceError

    with pytest.raises(LaneReplaceError):
        replace_lane(store, sid, part_atoms.MESSAGE_PART_SCOPE, write)
    assert seen_in_cache == [0]
    assert not [
        seg
        for key, segs in store._scopes.items()
        if key[1].startswith(staged_chunks[0])
        for seg in segs
    ]


def test_live_atom_cannot_override_the_existing_order_allocator(arc: Any) -> None:
    """The staged optimization cannot rewrite ordering on a visible live append."""
    with pytest.raises(ValueError, match="unpublished batch"):
        part_atoms._append_segment_raw(
            arc._segments,
            "bulk-generation",
            part_atoms.MESSAGE_PART_SCOPE,
            part_atoms.MESSAGE_PART_KIND,
            _atoms(1)[0],
            order=20,
        )
    assert not list(
        lane_segments(
            arc._segments,
            "bulk-generation",
            part_atoms.MESSAGE_PART_SCOPE,
            include_tombstoned=False,
        )
    )
