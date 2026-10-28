"""Read-ahead: many clio-core records read together for a one-time pass over the store.

A store migration reads thousands of records, and each :meth:`ClioCoreStore.get` is a
round trip of its own: a liveness check, a stall-watch worker and two blocking RPCs
(``GetBlobSize`` then ``GetBlob``), measured at 10-25 ms each on a loaded machine. Read
one at a time, a 904-conversation store took minutes to migrate. The binding's async
calls let a whole window of reads be in flight at once (``AsyncGetBlobSize`` for the
window, then ``AsyncGetBlob`` for the records that exist), about 2 ms a record.

A pass knows which records it will read before it reads them (a listing of the names,
the references in the rows it holds), so it asks for them first:

* :func:`reading_ahead` installs a :class:`ReadAhead` buffer on the store for the
  pass; :func:`prefetch` reads a list of names into it together, and records the names
  a listing taken in the same pass says are absent;
* :meth:`ClioCoreStore.get` answers from the buffer when it holds the name, so the
  pass's code reads exactly as before, record by record, without the round trips;
* every write the store makes (``put``, ``put_many``, ``delete``) drops its names
  from the buffer first, so a read after a write goes to clio-core.

The buffer is the store's own read copy for the length of one pass (like the segment
store's scope cache), never a second record: it is dropped when the pass ends. A store
without round trips (the in-process test store) has no ``get_many``, and reads stay
direct: there is nothing to batch.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from itertools import batched
from typing import Any

#: Reads kept in flight together (measured on a loaded 4-core machine: 0.6-3 ms a
#: record at 256-512, against 10-25 ms a record read singly).
WINDOW = 256


class ReadAhead:
    """Records read ahead of their use: ``(kind, name) -> bytes`` or ``None`` (absent)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], bytes | None] = {}
        self._listings: dict[str, set[str]] = {}
        self._sorted: dict[str, list[str]] = {}  # a listing in order, for prefix lookups
        self._lock = threading.Lock()

    def lookup(self, kind: str, name: str) -> tuple[bool, bytes | None]:
        """``(True, value)`` when the record was read ahead (value ``None``: absent)."""
        with self._lock:
            key = (kind, name)
            if key in self._rows:
                return True, self._rows[key]
        return False, None

    def holds(self, kind: str, name: str) -> bool:
        """Whether ``(kind, name)`` was read ahead."""
        with self._lock:
            return (kind, name) in self._rows

    def fill(self, kind: str, rows: dict[str, bytes | None]) -> None:
        """Hold ``rows`` (``None``: the record is absent)."""
        with self._lock:
            for name, value in rows.items():
                self._rows[(kind, name)] = value

    def forget(self, kind: str, names: Iterable[str], *, deleted: bool = False) -> None:
        """Drop ``names`` (the store is writing them, or deleting them when ``deleted``);
        a held listing of ``kind`` gains or loses them."""
        with self._lock:
            listing = self._listings.get(kind)
            for name in names:
                self._rows.pop((kind, name), None)
                if listing is not None and (name in listing) == deleted:
                    (listing.discard if deleted else listing.add)(name)
                    self._sorted.pop(kind, None)

    def hold_listing(self, kind: str, names: Iterable[str]) -> None:
        """Hold every record name of ``kind`` (a whole-kind listing taken in this pass)."""
        with self._lock:
            self._listings[kind] = set(names)
            self._sorted.pop(kind, None)

    def names_with_prefix(self, kind: str, prefix: str) -> list[str] | None:
        """The held listing's names under ``prefix``; ``None`` when none is held."""
        with self._lock:
            listing = self._listings.get(kind)
            if listing is None:
                return None
            ordered = self._sorted.get(kind)
            if ordered is None:
                ordered = self._sorted[kind] = sorted(listing)
            found: list[str] = []
            for name in ordered[bisect_left(ordered, prefix) :]:
                if not name.startswith(prefix):
                    break
                found.append(name)
            return found

    def clear(self) -> None:
        """Drop every record held (a batch of the pass is done); listings stay."""
        with self._lock:
            self._rows.clear()

    def reset(self) -> None:
        """Drop records and listings (the store was cleared)."""
        with self._lock:
            self._rows.clear()
            self._listings.clear()
            self._sorted.clear()


def held_ahead(store: Any, kind: str, name: str) -> tuple[bool, bytes | None]:
    """``(True, value)`` when a pass read ``(kind, name)`` ahead into ``store``'s buffer."""
    buffer = buffer_of(store)
    return buffer.lookup(kind, name) if buffer is not None else (False, None)


def forget_written(store: Any, kind: str, names: Iterable[str]) -> None:
    """Drop the records ``store`` is about to write from its buffer (``kind`` ``""``: all)."""
    buffer = buffer_of(store)
    if buffer is None:
        return
    if kind:
        buffer.forget(kind, names)
    else:
        buffer.reset()


def forget_deleted(store: Any, kind: str, names: Iterable[str]) -> None:
    """Drop the records ``store`` is about to delete from its buffer and listing."""
    buffer = buffer_of(store)
    if buffer is not None:
        buffer.forget(kind, names, deleted=True)


def names_with_prefix(store: Any, kind: str, prefix: str) -> list[str] | None:
    """Record names under ``prefix`` from a listing held for this pass (``None``: none)."""
    buffer = buffer_of(store)
    return buffer.names_with_prefix(kind, prefix) if buffer is not None else None


def list_all(store: Any, kind: str) -> set[str]:
    """Every record name of ``kind``, asked of clio-core once and held for the pass, so
    a prefix listing (:meth:`ScopeCache.scan_scopes`) is answered without a round trip.
    Writes made by the pass keep it current."""
    names = set(store.query_names(kind, ""))
    buffer = buffer_of(store)
    if buffer is not None:
        buffer.hold_listing(kind, names)
    return names


def ahead_by_batch(
    store: Any,
    kind: str,
    items: Iterable[Any],
    names_of: Callable[[Any], tuple[list[str], list[str]]],
    *,
    size: int = 64,
) -> Iterator[Any]:
    """Yield ``items``, reading the records each batch of them will read first.

    ``names_of(item)`` is ``(present, absent)`` record names (see :func:`prefetch`).
    The previous batch's records are dropped as the next is read, so one batch's are
    held at a time. Without a read-ahead buffer this only yields ``items``.
    """
    buffer = buffer_of(store)
    for part in batched(items, size):
        if buffer is not None:
            present: list[str] = []
            absent: list[str] = []
            for item in part:
                found, missing = names_of(item)
                present += found
                absent += missing
            buffer.clear()
            prefetch(store, kind, present, absent=absent)
        yield from part


def read_many(store: Any, kind: str, names: Sequence[str]) -> dict[str, bytes | None]:
    """Read records of a clio-core store together, :data:`WINDOW` reads in flight at once.

    Each window is one stall-guarded batch (:func:`~clio_agent.arc.rpc_liveness.
    guarded_store_rpc`), like ``put_many``. A record whose body read short of its own
    frame (size metadata lagging its write) is read again singly, as ``get`` does.

    Returns:
        ``{name: record bytes}``, ``None`` for an absent record.
    """
    from clio_agent.arc.clio_core_async_ops import store_get_many  # noqa: PLC0415
    from clio_agent.arc.rpc_liveness import guarded_store_rpc  # noqa: PLC0415

    out: dict[str, bytes | None] = {}
    for window in batched(names, WINDOW):
        rows, stale = guarded_store_rpc(store, "get", store_get_many, store, kind, list(window))
        out.update(rows)
        for name in stale:
            out[name] = store._get_one(kind, name)
    return out


def listed(segments: Any, name_re: str) -> set[str]:
    """``segments`` record names fully matching ``name_re``, asked of clio-core once."""
    return set(segments._store.query_names("segments", name_re))


def family_names(
    segments: Any, session_id: str, base: str, names: set[str], *, lookahead: int = 2
) -> tuple[list[str], list[str]]:
    """The record names a read of one chunk family touches, split by a listing.

    A family is read by a dense walk (:mod:`clio_agent.arc.lane_chunking`): chunk 1,
    2, ... up to the first absent one; an erase also probes ``lookahead`` indices past
    a hole. From the listing ``names`` this returns the present chunk records and the
    absent ones those reads probe, so both are answered from the read-ahead.
    """
    from clio_agent.arc.lane_chunking import chunk_scope  # noqa: PLC0415

    present: list[str] = []
    absent: list[str] = []
    index = 1
    misses = 0
    while misses <= lookahead:
        name = segments._record_name(session_id, chunk_scope(base, index))
        if name in names:
            present.append(name)
            misses = 0
        else:
            absent.append(name)
            misses += 1
        index += 1
    return present, absent


def buffer_of(store: Any) -> ReadAhead | None:
    """The read-ahead buffer installed on ``store`` (``None`` outside a pass)."""
    return getattr(store, "read_ahead", None)


@contextmanager
def reading_ahead(store: Any) -> Iterator[ReadAhead | None]:
    """Install a fresh read-ahead buffer on ``store`` for one pass; drop it after.

    Yields ``None`` for a store that cannot read several records together (no
    ``get_many``): its reads stay direct.
    """
    if not callable(getattr(store, "get_many", None)) or buffer_of(store) is not None:
        yield buffer_of(store)  # nothing to batch, or an enclosing pass already reads ahead
        return
    buffer = ReadAhead()
    store.read_ahead = buffer
    try:
        yield buffer
    finally:
        store.read_ahead = None
        buffer.clear()


def prefetch(store: Any, kind: str, names: Iterable[str], *, absent: Iterable[str] = ()) -> int:
    """Read ``names`` together into the store's read-ahead buffer; return how many were read.

    Args:
        store: The store (its buffer is installed by :func:`reading_ahead`).
        kind: The record kind.
        names: Records the pass is about to read.
        absent: Records a listing taken in this pass says do not exist (held as
            absent without a read; the pass holds the writer, so nothing creates them).
    """
    buffer = buffer_of(store)
    if buffer is None:
        return 0
    wanted = [name for name in dict.fromkeys(names) if not buffer.holds(kind, name)]
    if wanted:
        buffer.fill(kind, store.get_many(kind, wanted))
    missing = {name: None for name in absent if not buffer.holds(kind, name)}
    if missing:
        buffer.fill(kind, missing)
    return len(wanted)


__all__ = [
    "WINDOW",
    "ReadAhead",
    "ahead_by_batch",
    "buffer_of",
    "family_names",
    "forget_deleted",
    "forget_written",
    "held_ahead",
    "list_all",
    "listed",
    "names_with_prefix",
    "prefetch",
    "read_many",
    "reading_ahead",
]
