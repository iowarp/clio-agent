"""The in-process scope cache of :class:`~clio_agent.arc.segments.SegmentStore` (R6).

clio-core is the record; this cache is only a read copy of it. Three rules hold here:

* **Write-through.** A write puts the new scope record to clio-core FIRST and only then
  makes it the cached copy (:meth:`ScopeCache._commit_scope`). A refused put leaves the
  cache at the last committed record, so no reader ever sees a write clio-core does not
  hold.
* **One residency owner.** Every load, commit, drop and release is reported to the
  injected :class:`ScopeResidency` (the gact resident set,
  :class:`~clio_agent.gact.resident_ledgers.ResidentLedgerSet`), which counts ARC scopes
  with the message ledgers and releases idle or over-budget sessions through
  :meth:`ScopeCache.release`. ``arc/`` never imports ``gact/``: the owner is injected.
* **Reads that leave nothing behind.** Inside :func:`transient_reads` a cold scope is
  decoded and returned without entering the cache, so a search over every stored
  conversation does not make every conversation resident.
* **One write observer.** Every committed change of a scope record (a put with its
  cache install, a group flush, an erase) runs inside the injected
  :class:`ScopeWriteObserver`'s :meth:`~ScopeWriteObserver.writing` (the gact recall
  index, which keeps per-conversation search digests current). The observer is told
  before the record changes and after the cached copy shows it; it adds no store work.

Listing is by prefix: a session's scopes and the sessions holding one scope are asked
of clio-core by name pattern (:meth:`ARCStore.scan_names` with a prefix, and
``query_names``), never by listing every record of the kind.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Protocol

import msgspec

from clio_agent.arc.chunk_records import (
    CHUNK_APPEND_ABANDONED,
    ChunkMark,
    ChunkWrite,
    appendable,
    plan_write,
    read_chunk,
)
from clio_agent.arc.lane_chunking import seal_chunk
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, segment_text
from clio_agent.arc.scope_locks import is_pseudo_scope
from clio_agent.arc.segment_errors import ArcPersistError, SegmentReadError
from clio_agent.arc.segment_group import active_group
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.arc.scope_locks import ScopeLock, ScopeLockRegistry
    from clio_agent.arc.segment_index import SegmentIndex
    from clio_agent.arc.storage import ARCStore

logger = logging.getLogger(__name__)

# ``<session_id><SEP><scope with "/" substituted>`` -- the one record-naming grammar.
_SCOPE_SEP = "__"  # session_id <SEP> scope, in the store record name
_SLASH_SUB = "~"  # scope's '/' replaced so the record name is one path segment

# The invocation index (``ARCMemory._inv_index``) is counted against its session under
# this pseudo-scope; one entry is a small dict keyed by a short tuple.
INVOCATION_INDEX_SCOPE = "\x00invocations"
_INVOCATION_ENTRY_BYTES = 256

__all__ = [
    "INVOCATION_INDEX_SCOPE",
    "ScopeCache",
    "ScopeResidency",
    "ScopeWriteObserver",
    "reads_are_transient",
    "transient_reads",
]


class ScopeResidency(Protocol):
    """The residency owner ARC reports its in-process scope copies to."""

    def scope_resident(self, session_id: str, scope: str, nbytes: int) -> None:
        """A scope's record is now cached (``nbytes`` = its encoded size)."""

    def scope_dropped(self, session_id: str, scope: str) -> None:
        """A scope's cached copy was dropped (the record is unchanged in clio-core)."""

    def scope_touched(self, session_id: str) -> None:
        """A cached scope of the session was read (keeps it from going idle)."""


class ScopeWriteObserver(Protocol):
    """Told of every committed change of a scope record (an injected index)."""

    def writing(self, session_id: str, scope: str) -> AbstractContextManager[object]:
        """Enclose one change of ``(session_id, scope)``: entered before the store is
        written, exited once the cached copy shows the change (or the write failed)."""


_UNOBSERVED: AbstractContextManager[object] = nullcontext()
_TRANSIENT = threading.local()


@contextmanager
def transient_reads() -> Iterator[None]:
    """On this thread, read cold scopes from clio-core without caching them."""
    depth = getattr(_TRANSIENT, "depth", 0)
    _TRANSIENT.depth = depth + 1
    try:
        yield
    finally:
        _TRANSIENT.depth = depth


def reads_are_transient() -> bool:
    """Whether this thread is inside :func:`transient_reads`."""
    return getattr(_TRANSIENT, "depth", 0) > 0


class ScopeCache:
    """Load, commit, release and list the scope records (mixed into SegmentStore)."""

    _store: "ARCStore"
    _scopes: dict[tuple[str, str], list[Segment]]
    _loaded: set[tuple[str, str]]
    _chunk_marks: dict[tuple[str, str], ChunkMark]  # the stored end of each cached record
    _index: "SegmentIndex"
    _registry_lock: threading.RLock
    _scope_locks: ScopeLockRegistry
    _generations: dict[tuple[str, str], int]
    _search_indexed: Optional[Callable[[str], bool]]
    _invocation_counts: dict[str, int]
    _residency: Optional[ScopeResidency] = None
    _write_observer: Optional[ScopeWriteObserver] = None

    if TYPE_CHECKING:

        def _lock_for(self, session_id: str, scope: str) -> ScopeLock: ...

        def _recover_clock(self, segs: Iterable[Segment]) -> None: ...

        @staticmethod
        def _live_sorted(segs: list[Segment]) -> list[Segment]: ...

    @staticmethod
    def _record_name(session_id: str, scope: str) -> str:
        return f"{session_id}{_SCOPE_SEP}{scope.replace('/', _SLASH_SUB)}"

    def set_residency(self, residency: Optional[ScopeResidency]) -> None:
        """Inject (or clear) the residency owner every cached scope is reported to."""
        self._residency = residency

    def set_write_observer(self, observer: Optional[ScopeWriteObserver]) -> None:
        """Inject (or clear) the observer every committed scope change passes through."""
        self._write_observer = observer

    def _observed(self, session_id: str, scope: str) -> AbstractContextManager[object]:
        """The observer's enclosure of one change of ``(session_id, scope)``."""
        observer = self._write_observer
        return _UNOBSERVED if observer is None else observer.writing(session_id, scope)

    # ---- load ----------------------------------------------------------

    def _segs(self, session_id: str, scope: str) -> list[Segment]:
        """Return the scope's segment list, loading it from clio-core once.

        Always called under this scope's per-scope lock. Inside :func:`transient_reads`
        a cold scope is decoded and returned without being cached or reported."""
        key = (session_id, scope)
        group = active_group(self)
        if group is not None and key in group.pending:  # this thread's staged appends
            return group.pending[key].staged
        residency = self._residency
        if key in self._loaded:
            if residency is not None:
                residency.scope_touched(session_id)
            return self._scopes[key]
        try:  # an old-format record raises typed (ChunkFormatUnsupportedError)
            parsed = read_chunk(self._store, "segments", self._record_name(session_id, scope))
        except msgspec.DecodeError as exc:
            raise SegmentReadError(session_id, scope) from exc
        segs = parsed.segments if parsed is not None else []
        self._recover_clock(segs)
        if reads_are_transient():
            return segs
        self._scopes[key] = segs
        self._loaded.add(key)
        if parsed is not None:  # the next append goes where the stored records end
            self._chunk_marks[key] = ChunkMark(parsed.end, len(segs), segs, len(parsed.blob))
        self._index.bulk_load(session_id, scope, segs)  # parallel locator
        if residency is not None and parsed is not None:
            residency.scope_resident(session_id, scope, parsed.end)
        logger.debug(
            "segments: cold-load session=%s scope=%s loaded=%d", session_id, scope, len(segs)
        )
        return segs

    def raw_scope_record(self, session_id: str, scope: str) -> Optional[bytes]:
        """The scope's committed record bytes from clio-core: the raw msgpack records up
        to the end marker, never the stale tail past it (never cached, not decoded)."""
        name = self._record_name(session_id, scope)
        parsed = read_chunk(self._store, "segments", name, decode=False)
        return parsed.blob[: parsed.end] if parsed is not None else None

    # ---- write-through -------------------------------------------------

    def _put_scope(
        self,
        session_id: str,
        scope: str,
        segs: list[Segment],
        *,
        mark: Optional[ChunkMark] = None,
    ) -> ChunkMark:
        """Write the scope's segments as chunk records (with the live search_text
        companion) and return the new stored end.

        With ``mark`` (the stored end of the record ``segs`` extends) only
        ``segs[mark.count:]`` is put, at the end offset; without it the whole record is
        rewritten at offset 0 (an end marker stops readers before any longer stale
        tail). Raises if a segment does not encode, ``store.put_at`` refuses, or
        (#1334) the caller runs an event loop."""
        assert_store_write_off_loop("segments.put", scope=scope)
        name, write, search_text = self._planned_put(session_id, scope, segs, mark)
        self._store.put_at("segments", name, write.data, write.offset, search_text=search_text)
        return write.committed(segs)

    def _planned_put(
        self, session_id: str, scope: str, segs: list[Segment], mark: Optional[ChunkMark]
    ) -> tuple[str, ChunkWrite, Optional[str]]:
        """The record name, the planned put and the search companion text of ``segs``."""
        # search_text: the live render flattened to plain text, so semantic discovery
        # (Thread D) can find this scope by content. A scope the ``search_indexed``
        # predicate excludes (the reserved ``_events`` family) never writes it.
        if self._search_indexed is None or self._search_indexed(scope):
            live_text = "\n".join(segment_text(s) for s in self._live_sorted(segs))
            search_text = live_text or None
        else:
            search_text = None
        return self._record_name(session_id, scope), plan_write(segs, mark), search_text

    def _append_mark(
        self,
        key: tuple[str, str],
        staged: list[Segment],
        extends: Optional[list[Segment]] = None,
    ) -> Optional[ChunkMark]:
        """The stored end ``staged`` can be appended at, or ``None`` (a full rewrite).

        ``extends`` (the list ``staged`` was built from) settles it at once when it is the
        record the mark belongs to; otherwise every stored segment is compared."""
        mark = self._chunk_marks.get(key)
        cached = self._scopes.get(key)
        if mark is not None and extends is not None and extends is cached is mark.segs:
            return mark
        return mark if appendable(mark, cached, staged) else None

    def _abandon_chunk(self, session_id: str, scope: str, exc: BaseException) -> None:
        """An append failed and may still land: drop the cached copy and never append to
        this record again in this process (the lane's next append rolls to a new chunk)."""
        self._discard_scope(session_id, scope)
        seal_chunk(self, session_id, scope)
        stream_audit(
            CHUNK_APPEND_ABANDONED, session_id=session_id, scope=scope, cause=type(exc).__name__
        )
        logger.warning(
            "reason=%s session=%s scope=%s cause=%s",
            CHUNK_APPEND_ABANDONED,
            session_id,
            scope,
            type(exc).__name__,
        )

    def _commit_scope(
        self,
        session_id: str,
        scope: str,
        staged: list[Segment],
        *,
        extends: Optional[list[Segment]] = None,
    ) -> None:
        """Put ``staged`` to clio-core, THEN make it the cached copy (write-through).

        ``staged`` that only adds segments to the cached record is appended at its stored
        end (one put of the new records); anything else rewrites the record. A refused
        rewrite raises :class:`ArcPersistError` and leaves the cache at the last committed
        record; a refused append also abandons the chunk (:meth:`_abandon_chunk`). Called
        under this scope's per-scope lock. Inside this thread's :func:`segment_group`
        an append is staged instead and put with the group's other appends. ``extends``
        names the list ``staged`` was built from (an append), so neither check has to
        walk the record."""
        group = active_group(self)
        if group is not None and group.stage((session_id, scope), staged, extends=extends):
            return
        mark = self._append_mark((session_id, scope), staged, extends)
        with self._observed(session_id, scope):
            try:
                committed = self._put_scope(session_id, scope, staged, mark=mark)
            except LoopThreadStoreWrite:
                raise  # a write from the event loop is a caller bug, typed as itself
            except Exception as exc:  # noqa: BLE001 - re-raised typed; the cache is unchanged
                if mark is not None:
                    self._abandon_chunk(session_id, scope, exc)
                raise ArcPersistError(session_id, scope, exc) from exc
            self._install_committed(session_id, scope, staged, committed)

    def _install_committed(
        self, session_id: str, scope: str, staged: list[Segment], mark: ChunkMark
    ) -> None:
        """Make a record clio-core accepted the cached copy and report its stored size."""
        key = (session_id, scope)
        self._scopes[key] = staged
        self._loaded.add(key)
        self._chunk_marks[key] = mark
        if self._residency is not None:
            self._residency.scope_resident(session_id, scope, mark.end)

    def _persist(self, session_id: str, scope: str) -> None:
        """Put the cached list as it stands (callers that staged into it under the scope
        lock: only what follows the stored end is put). On failure the cached copy is
        discarded -- the next read reloads clio-core's record -- and a typed
        :class:`ArcPersistError` is raised."""
        key = (session_id, scope)
        segs = self._scopes[key]
        mark = self._append_mark(key, segs)
        with self._observed(session_id, scope):
            try:
                committed = self._put_scope(session_id, scope, segs, mark=mark)
            except Exception as exc:  # noqa: BLE001 - re-raised typed after the memory is discarded
                if mark is not None and not isinstance(exc, LoopThreadStoreWrite):  # may land
                    self._abandon_chunk(session_id, scope, exc)
                else:
                    self._discard_scope(session_id, scope)
                if isinstance(exc, LoopThreadStoreWrite):
                    raise
                raise ArcPersistError(session_id, scope, exc) from exc
            self._chunk_marks[key] = committed
        if self._residency is not None:
            self._residency.scope_resident(session_id, scope, committed.end)

    def _discard_scope(self, session_id: str, scope: str) -> None:
        """Drop the in-memory copy and its view generation (under the scope's lock);
        the next read reloads clio-core, under a fresh generation. The scope's lock
        leaves the registry when its last holder releases it."""
        key = (session_id, scope)
        self._scopes.pop(key, None)
        self._chunk_marks.pop(key, None)
        self._generations.pop(key, None)
        self._loaded.discard(key)
        self._index.drop_scope(session_id, scope)
        if self._residency is not None:
            self._residency.scope_dropped(session_id, scope)

    def note_invocation(self, session_id: str) -> None:
        """Count one invocation-index entry against its session (``ARCMemory``)."""
        residency = self._residency
        if residency is None:
            return
        with self._registry_lock:
            count = self._invocation_counts.get(session_id, 0) + 1
            self._invocation_counts[session_id] = count
        residency.scope_resident(
            session_id, INVOCATION_INDEX_SCOPE, count * _INVOCATION_ENTRY_BYTES
        )

    # ---- listing by prefix ----------------------------------------------

    def scan_scopes(self, session_id: str, scope_pattern: str = "") -> list[str]:
        """Scope addresses for a session under a prefix (``""`` for all), asked of
        clio-core by name prefix. Backs cross-scope reads."""
        prefix = f"{session_id}{_SCOPE_SEP}"
        name_prefix = prefix + scope_pattern.replace("/", _SLASH_SUB)
        out: set[str] = set()
        for name in self._store.scan_names("segments", prefix=name_prefix):
            scope = name[len(prefix) :].replace(_SLASH_SUB, "/")
            if scope.startswith(scope_pattern):
                out.add(scope)
        return sorted(out)

    def sessions_with_scope(self, scope: str) -> list[str]:
        """Every ``session_id`` holding a record for ``scope``, asked of clio-core by
        name pattern (``<anything><SEP><scope>``). Backs wholesale lifecycle erase of an
        ephemeral scope (the observer's ``_events`` log scope)."""
        suffix = f"{_SCOPE_SEP}{scope.replace('/', _SLASH_SUB)}"
        query = getattr(self._store, "query_names", None)
        if callable(query):
            names = query("segments", f".*{re.escape(suffix)}")
        else:  # an in-process store: its listing is its own memory
            names = [n for n in self._store.scan_names("segments") if n.endswith(suffix)]
        return sorted({name[: -len(suffix)] for name in names if name.endswith(suffix)})

    # ---- release -------------------------------------------------------

    def release(self, session_id: str) -> int:
        """Drop a session's in-memory scopes (write-through, nothing lost) and their
        locks; returns how many cached scopes were released.

        Each scope is dropped under its own lock, one at a time, and reported to the
        residency owner under it: a release never races a per-scope op, never holds
        one scope lock while waiting on another (no lock-order cycle with ops that nest
        scope locks, such as lane writers), and never holds the lock registry while it
        waits. The keys come from the lock registry, a superset of the cached keys
        (a cached scope's lock stays registered). A scope first locked while the
        release runs is dropped by a further pass; one visited already and loaded again
        stays cached, and counted."""
        released = 0
        visited: set[tuple[str, str]] = set()
        while True:
            keys = [
                key
                for key in self._scope_locks.keys(session_id)
                if key not in visited and not is_pseudo_scope(key[1])
            ]
            if not keys:
                break
            for key in keys:
                visited.add(key)
                with self._lock_for(*key):
                    if key in self._loaded:
                        released += 1
                        self._discard_scope(*key)
        with self._registry_lock:
            invocations = self._invocation_counts.pop(session_id, None)
        if invocations is not None and self._residency is not None:
            self._residency.scope_dropped(session_id, INVOCATION_INDEX_SCOPE)
        logger.info("segments: release session=%s scopes=%d", session_id, released)
        return released

    def clear(self) -> None:
        """Drop ALL in-memory scope state (store untouched): every session is released
        as :meth:`release` does (same locking)."""
        with self._registry_lock:
            counted = set(self._invocation_counts)
        for session_id in sorted({key[0] for key in self._scope_locks.keys()} | counted):
            self.release(session_id)
