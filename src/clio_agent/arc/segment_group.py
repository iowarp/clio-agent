"""Group commit of segment appends: many appends, one put per record (owner module).

The task-history writer records several owner operations in one writer turn
(:mod:`clio_agent.gact.task_history_group`). Inside :func:`segment_group` on that
thread, an append is staged instead of put:

* **Visibility.** The thread's own later reads and appends of a staged scope see the
  staged list (read your writes). Every other thread still sees the last committed
  record, so write-through (:mod:`clio_agent.arc.segment_cache`) holds for them.
* **One put per record.** :meth:`SegmentGroup.flush` puts each staged scope once: its
  new records at the stored end in the append-only chunk format
  (:mod:`clio_agent.arc.chunk_records`), or the whole record when it is new. All the
  scopes of one rank go in one ``put_many`` (issued together, one round trip). Ranks
  are flushed in ascending order, so the caller keeps the order its records need
  (content before the events that cite it). Measured on a P7-shaped burst
  (``scripts/bench/mcp_burst_bench.py``): 6 appends per read-only call became 3 puts per
  group of calls.
* **Concurrent writers.** A scope another thread appended to after it was staged (the
  shared ``_events`` log) is rebased: the staged segments go after the other thread's
  with new ``order`` values; their ``logical_time`` and identity are unchanged. The
  scopes of a rank stay locked (in sorted order, so two flushes never invert) from
  planning until their puts are installed, so no other append plans against the same
  stored end.
* **Failure.** A lost or unconfirmed append abandons its chunk exactly as a single append
  does (``_abandon_chunk``: the chunk is sealed and the next append rolls). Every other
  staged scope that was not installed is dropped from the cache, so the next read
  reloads clio-core. The flush raises :class:`ArcPersistError`. A staged segment is in
  the parallel locator (``SegmentIndex``) from staging on; dropping the scope drops it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import msgspec

from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.segment_errors import ArcPersistError

if TYPE_CHECKING:
    from clio_agent.arc.schema import Segment
    from clio_agent.arc.scope_locks import ScopeLock
    from clio_agent.arc.segments import SegmentStore

__all__ = [
    "SEGMENT_GROUP_CONFLICT",
    "cache_absent",
    "SegmentGroup",
    "SegmentGroupConflict",
    "active_group",
    "segment_group",
]

#: Typed reason: a staged scope was rewritten while another thread changed its record.
SEGMENT_GROUP_CONFLICT = "segment_group_conflict"

Key = tuple[str, str]
Rank = tuple[int, ...]
_ACTIVE = threading.local()


class SegmentGroupConflict(RuntimeError):
    """A rewrite of a staged scope found its committed record changed by another thread."""

    reason = SEGMENT_GROUP_CONFLICT


@dataclass
class _Staged:
    base: list[Segment]  # the committed list ``staged`` extends
    staged: list[Segment]
    rank: Rank


def _extends(staged: list[Segment], base: list[Segment]) -> bool:
    return len(staged) >= len(base) and all(a is b for a, b in zip(staged, base, strict=False))


def active_group(store: object) -> SegmentGroup | None:
    """This thread's open group over ``store``, if any."""
    group: SegmentGroup | None = getattr(_ACTIVE, "group", None)
    return group if group is not None and group.store is store else None


class SegmentGroup:
    """Appends staged on one thread and put together (see the module docstring).

    Args:
        store: The segment store whose appends are staged.
        rank: Maps a scope to its flush rank; lower ranks are put first.
    """

    def __init__(self, store: SegmentStore, rank: Callable[[str], Rank]) -> None:
        self.store = store
        self.rank = rank
        self.pending: dict[Key, _Staged] = {}

    def stage(
        self, key: Key, staged: list[Segment], *, extends: list[Segment] | None = None
    ) -> bool:
        """Stage ``staged`` for ``key`` (``True``), or leave the put to the caller.

        Only appends are staged. A rewrite of a scope with staged appends is put at once
        by the caller, with the staged segments it holds, if no other thread changed the
        committed record meanwhile.

        ``extends`` is the list ``staged`` was built from by adding segments after it:
        when it is what this scope holds now (its staged list, or the committed record),
        ``staged`` is an append without comparing every segment.

        Raises:
            ArcPersistError: A rewrite of a staged scope whose record another thread
                changed (``segment_group_conflict``).
        """
        entry = self.pending.get(key)
        base = entry.base if entry is not None else self.store._scopes.get(key)
        held = entry.staged if entry is not None else base
        appended = base is not None and extends is not None and extends is held
        if not appended and (base is None or not _extends(staged, base)):
            if entry is not None:
                if self.store._scopes.get(key) is not base:
                    conflict = SegmentGroupConflict(SEGMENT_GROUP_CONFLICT)
                    raise ArcPersistError(key[0], key[1], conflict)
                del self.pending[key]
            return False
        if entry is None:
            assert base is not None  # the returns above leave only a known base
            self.pending[key] = _Staged(base, staged, self.rank(key[1]))
        else:
            entry.staged = staged
        return True

    def flush(self) -> None:
        """Put every staged scope, one ``put_many`` per rank, lowest rank first.

        Raises:
            ArcPersistError: A put was lost or unconfirmed; what was not installed is
                dropped from the cache (module docstring).
        """
        assert_store_write_off_loop("segments.put_many")
        if active_group(self.store) is self:
            _ACTIVE.group = None  # the flush reads committed records, never staged ones
        try:
            for rank in sorted({entry.rank for entry in self.pending.values()}):
                self._flush_rank(sorted(k for k, e in self.pending.items() if e.rank == rank))
        finally:
            self.discard()

    def discard(self) -> None:
        """Drop what is still staged; the next read reloads those scopes from clio-core."""
        for key in sorted(self.pending):
            with self.store._lock_for(*key):
                self.store._discard_scope(*key)
        self.pending.clear()

    def _rebased(self, key: Key, entry: _Staged) -> tuple[list[Segment], list[Segment]]:
        """``entry.staged`` over the record as committed now, and the committed list it
        extends (called under the scope lock). Only appends are staged, so a staged list
        extends its base."""
        store = self.store
        current = store._scopes.get(key) if key in store._loaded else None
        if current is entry.base:
            return entry.staged, entry.base
        if current is None:  # released meanwhile: reload the committed record
            current = store._segs(*key)
        ours = entry.staged[len(entry.base) :]
        top = max((segment.order for segment in current), default=0.0)
        moved = [
            msgspec.structs.replace(segment, order=top + position)
            for position, segment in enumerate(ours, start=1)
        ]
        return [*current, *moved], current

    def _flush_rank(self, keys: list[Key]) -> None:
        store = self.store
        held: list[ScopeLock] = []
        try:
            for key in keys:  # sorted: two flushes never take them in opposite orders
                lock = store._lock_for(*key)
                lock.acquire()
                held.append(lock)
            with ExitStack() as observed:  # the write observer sees the puts and installs
                for key in keys:
                    observed.enter_context(store._observed(*key))
                self._put_rank(keys)
        finally:
            for lock in reversed(held):
                lock.release()

    def _put_rank(self, keys: list[Key]) -> None:
        """Put the rank's planned records in one batch and install what landed (locked)."""
        store = self.store
        plans = []
        for key in keys:
            staged, extends = self._rebased(key, self.pending[key])
            mark = store._append_mark(key, staged, extends)
            name, write, text = store._planned_put(key[0], key[1], staged, mark)
            plans.append((key, staged, mark, name, write, text))
        lost: set[str] = set()
        error: BaseException | None = None
        try:
            store._store.put_many(
                "segments",
                [
                    PutRecord(name, write.data, text, write.offset)
                    for *_, name, write, text in plans
                ],
            )
        except LoopThreadStoreWrite:
            raise
        except BatchPutError as exc:
            lost, error = set(exc.failed), exc
        except Exception as exc:  # noqa: BLE001 - forwards: re-raised typed (ArcPersistError)
            lost, error = {plan[3] for plan in plans}, exc
        for key, staged, mark, name, write, _text in plans:
            del self.pending[key]
            if error is None or name not in lost:
                store._install_committed(key[0], key[1], staged, write.committed(staged))
            elif mark is not None:  # an append that may still land: never extend it again
                store._abandon_chunk(key[0], key[1], error)
            else:
                store._discard_scope(*key)
        if error is not None:
            first = next((plan[0] for plan in plans if plan[3] in lost), keys[0])
            raise ArcPersistError(first[0], first[1], error) from error


def cache_absent(store: SegmentStore, keys: Iterable[Key], *, minted: Iterable[Key] = ()) -> None:
    """Cache the scopes clio-core holds no record for as empty, asking for all at once.

    A cold read of a scope that does not exist costs a round trip of its own. A
    caller about to create several records (content-addressed task content) has them
    probed together: those absent are cached empty, exactly as a cold read of a
    missing record caches them; those present are left to the ordinary cold read.
    ``minted`` scopes are cached empty without asking: the caller derived their names
    from an identity it just minted, so nothing can have written them.
    """
    cold = sorted({key for key in keys if key not in store._loaded})
    absent = set(minted)
    if cold:
        names = {store._record_name(*key): key for key in cold}
        sizes = store._store.sizes("segments", list(names))
        absent.update(key for name, key in names.items() if not sizes.get(name, 1))
    for key in sorted(absent):
        with store._lock_for(*key):
            if key not in store._loaded:
                store._scopes[key] = []
                store._loaded.add(key)
                store._index.bulk_load(key[0], key[1], [])


@contextmanager
def segment_group(store: SegmentStore, rank: Callable[[str], Rank]) -> Iterator[SegmentGroup]:
    """Stage this thread's appends to ``store`` until :meth:`SegmentGroup.flush`.

    On exit anything still staged (no flush, or an exception before it) is discarded.

    Raises:
        RuntimeError: A group over ``store`` is already open on this thread.
    """
    if active_group(store) is not None:
        raise RuntimeError("A segment group is already open on this thread")
    group = SegmentGroup(store, rank)
    _ACTIVE.group = group
    try:
        yield group
    finally:
        if active_group(store) is group:
            _ACTIVE.group = None
        group.discard()
