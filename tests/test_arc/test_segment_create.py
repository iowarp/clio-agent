"""Content records are created once, atomically, on clio-core (iowarp-core 2.4).

A content-addressed record used to cost a probe (a get) and a put, and two writers could
both see it absent. ``create_scope_once`` puts the scope's first chunk with
``put_if_absent`` (``AsyncPutBlobIfAbsent``): one round trip, and clio-core decides who
creates it.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.live import _MemoryStore
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segment_create import create_scope_once
from clio_agent.arc.segment_errors import ArcPersistError
from clio_agent.arc.segment_group import segment_group
from clio_agent.gact.task_history import TaskHistory, content_reference

_CALLS = ("get", "get_raw", "exists", "sizes", "put", "put_at", "put_many", "put_if_absent")


def _counting(store: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    for method in _CALLS:
        original = getattr(store, method, None)
        if original is None:
            continue

        def wrapped(*args: Any, _m: str = method, _f: Any = original, **kw: Any) -> Any:
            calls.append(_m)
            return _f(*args, **kw)

        monkeypatch.setattr(store, method, wrapped)
    return calls


def _sid() -> str:
    return f"sess_create_{uuid.uuid4().hex[:8]}"


def test_new_content_is_one_atomic_put_and_existing_content_is_verified(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new record is one ``put_if_absent`` and no read; a record held elsewhere is read.

    SABOTAGE: restore the probe in ``create_scope_once`` (``store.list_segments`` before
    the put) and a new record costs a read as well.
    """
    sid = _sid()
    history = TaskHistory(arc)
    calls = _counting(arc._store, monkeypatch)
    reference = history.write_content(sid, {"text": "first"})
    assert calls == ["put_if_absent"]

    arc._segments.release(sid)  # a later process: nothing cached
    calls.clear()
    assert history.write_content(sid, {"text": "first"}) == reference
    assert "put_if_absent" in calls and calls.count("put_if_absent") == 1
    assert any(call in calls for call in ("get", "get_raw"))  # the existing record verified
    assert history.read_content(reference) == {"text": "first"}

    calls.clear()  # held by this process now: no round trip at all
    assert history.write_content(sid, {"text": "first"}) == reference
    assert calls == []


def test_two_writers_of_the_same_content_create_it_once(arc: ARCMemory, tmp_path: Path) -> None:
    """Two processes write the same content at once: clio-core lets exactly one create it.

    SABOTAGE: return ``True`` from ``ClioCoreWrites.put_if_absent`` whatever the runtime
    answered and both writers report a create.
    """
    sid = _sid()
    other = ARCMemory(data_dir=str(tmp_path / "other"), store=arc._store)
    writers = [arc, other]
    value = {"text": "shared", "n": 7}
    scope = content_reference(sid, value).scope
    barrier = threading.Barrier(2)
    created: list[bool] = []
    errors: list[BaseException] = []

    def write(memory: ARCMemory) -> None:
        try:
            barrier.wait(10)
            created.append(
                create_scope_once(memory._segments, sid, scope, "task_content", {"value": value})
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(memory,)) for memory in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert sorted(created) == [False, True]
    for memory in writers:
        memory._segments.release(sid)
        [segment] = memory._segments.list_segments(sid, scope)
        assert segment.content == {"value": value}


def test_a_refused_create_is_typed_and_caches_nothing(
    arc: ARCMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A put clio-core refuses raises typed; the history then requires recovery.

    SABOTAGE: return ``False`` from ``create_scope_once`` on any exception and the
    refusal reads as "already held".
    """
    sid = _sid()
    history = TaskHistory(arc)

    def refused(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("clio-core refused the put")

    monkeypatch.setattr(arc._store, "put_if_absent", refused)
    with pytest.raises(ArcPersistError):
        history.write_content(sid, {"text": "lost"})
    assert history.requires_recovery
    reference = content_reference(sid, {"text": "lost"})
    assert (sid, reference.scope) not in arc._segments._loaded


def test_the_memory_store_creates_once() -> None:
    """The in-process store answers ``put_if_absent`` as clio-core does.

    SABOTAGE: return ``True`` unconditionally from ``_MemoryStore.put_if_absent``.
    """
    store = _MemoryStore()
    assert store.put_if_absent("segments", "a", b"one") is True
    assert store.put_if_absent("segments", "a", b"two") is False
    assert store.get_raw("segments", "a") == b"one"


def test_a_group_keeps_its_batched_path(arc: ARCMemory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a segment group content is staged and put with the group, not created alone.

    SABOTAGE: drop the ``segment_group`` reason in ``_probe_reason`` and the create puts
    on its own inside the group (``put_if_absent`` appears before the flush).
    """
    sid = _sid()
    store = arc._segments
    scope = content_reference(sid, {"text": "grouped"}).scope
    calls = _counting(arc._store, monkeypatch)
    with segment_group(store, lambda _scope: (0,)) as group:
        assert create_scope_once(store, sid, scope, "task_content", {"text": "grouped"})
        assert "put_if_absent" not in calls and "put_many" not in calls
        group.flush()
    assert "put_many" in calls and "put_if_absent" not in calls
    store.release(sid)
    assert [s.content for s in store.list_segments(sid, scope)] == [{"text": "grouped"}]
