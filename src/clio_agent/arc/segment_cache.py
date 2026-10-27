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

Listing is by prefix: a session's scopes and the sessions holding one scope are asked
of clio-core by name pattern (:meth:`ARCStore.scan_names` with a prefix, and
``query_names``), never by listing every record of the kind.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Protocol

import msgspec

from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, decode_segments, encode_segments, segment_text
from clio_agent.arc.segment_errors import ArcPersistError, SegmentReadError

if TYPE_CHECKING:
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
    "reads_are_transient",
    "transient_reads",
]


class ScopeResidency(Protocol):
    """The residency owner ARC reports its in-process scope copies to."""

    def scope_resident(self, session_id: str, scope: str, nbytes: int) -> None:
        """A scope's record is now cached (``nbytes`` = its encoded size)."""

    def scope_dropped(self, session_id: str, scope: str) -> None:
        """A scope's cached copy was dropped (the record is unchanged in clio-core)."""

    def session_released(self, session_id: str) -> None:
        """Every cached scope of a session was dropped."""

    def scope_touched(self, session_id: str) -> None:
        """A cached scope of the session was read (keeps it from going idle)."""


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
    _index: "SegmentIndex"
    _registry_lock: threading.RLock
    _scope_locks: dict[tuple[str, str], threading.RLock]
    _search_indexed: Optional[Callable[[str], bool]]
    _invocation_counts: dict[str, int]
    _residency: Optional[ScopeResidency] = None

    if TYPE_CHECKING:

        def _recover_clock(self, segs: Iterable[Segment]) -> None: ...

        @staticmethod
        def _live_sorted(segs: list[Segment]) -> list[Segment]: ...

    @staticmethod
    def _record_name(session_id: str, scope: str) -> str:
        return f"{session_id}{_SCOPE_SEP}{scope.replace('/', _SLASH_SUB)}"

    def set_residency(self, residency: Optional[ScopeResidency]) -> None:
        """Inject (or clear) the residency owner every cached scope is reported to."""
        self._residency = residency

    # ---- load ----------------------------------------------------------

    def _segs(self, session_id: str, scope: str) -> list[Segment]:
        """Return the scope's segment list, loading it from clio-core once.

        Always called under this scope's per-scope lock. Inside :func:`transient_reads`
        a cold scope is decoded and returned without being cached or reported."""
        key = (session_id, scope)
        residency = self._residency
        if key in self._loaded:
            if residency is not None:
                residency.scope_touched(session_id)
            return self._scopes[key]
        raw = self._store.get("segments", self._record_name(session_id, scope))
        try:
            segs = decode_segments(raw) if raw else []
        except msgspec.DecodeError as exc:
            raise SegmentReadError(session_id, scope) from exc
        self._recover_clock(segs)
        if reads_are_transient():
            return segs
        self._scopes[key] = segs
        self._loaded.add(key)
        self._index.bulk_load(session_id, scope, segs)  # parallel locator
        if residency is not None and raw:
            residency.scope_resident(session_id, scope, len(raw))
        logger.debug(
            "segments: cold-load session=%s scope=%s loaded=%d", session_id, scope, len(segs)
        )
        return segs

    def raw_scope_record(self, session_id: str, scope: str) -> Optional[bytes]:
        """The scope's committed record bytes from clio-core (never cached, not decoded)."""
        return self._store.get("segments", self._record_name(session_id, scope))

    # ---- write-through -------------------------------------------------

    def _put_scope(self, session_id: str, scope: str, segs: list[Segment]) -> int:
        """Encode the scope's segments and put the record (with the live search_text
        companion); returns the encoded size. Raises if ``encode_segments`` /
        ``store.put`` rejects any segment, or (#1334) if the caller runs an event loop."""
        assert_store_write_off_loop("segments.put", scope=scope)
        # search_text: the live render flattened to plain text, so semantic discovery
        # (Thread D) can find this scope by content. A scope the ``search_indexed``
        # predicate excludes (the reserved ``_events`` family) never writes it.
        if self._search_indexed is None or self._search_indexed(scope):
            live_text = "\n".join(segment_text(s) for s in self._live_sorted(segs))
            search_text = live_text or None
        else:
            search_text = None
        data = encode_segments(segs)
        self._store.put(
            "segments", self._record_name(session_id, scope), data, search_text=search_text
        )
        return len(data)

    def _commit_scope(self, session_id: str, scope: str, staged: list[Segment]) -> None:
        """Put ``staged`` to clio-core, THEN make it the cached copy (write-through).

        A refused put raises :class:`ArcPersistError` and leaves the cache at the last
        committed record. Called under this scope's per-scope lock."""
        try:
            nbytes = self._put_scope(session_id, scope, staged)
        except LoopThreadStoreWrite:
            raise  # a write from the event loop is a caller bug, typed as itself
        except Exception as exc:  # noqa: BLE001 - re-raised typed; the cache is unchanged
            raise ArcPersistError(session_id, scope, exc) from exc
        self._install_committed(session_id, scope, staged, nbytes)

    def _install_committed(
        self, session_id: str, scope: str, staged: list[Segment], nbytes: int
    ) -> None:
        """Make a record clio-core accepted the cached copy and report its size."""
        key = (session_id, scope)
        self._scopes[key] = staged
        self._loaded.add(key)
        if self._residency is not None:
            self._residency.scope_resident(session_id, scope, nbytes)

    def _persist(self, session_id: str, scope: str) -> None:
        """Put the cached list as it stands (callers that staged into it under the scope
        lock). On failure the cached copy is discarded -- the next read reloads clio-core's
        record -- and a typed :class:`ArcPersistError` is raised."""
        segs = self._scopes[(session_id, scope)]
        try:
            nbytes = self._put_scope(session_id, scope, segs)
        except Exception as exc:  # noqa: BLE001 - re-raised typed after the memory is discarded
            self._discard_scope(session_id, scope)
            if isinstance(exc, LoopThreadStoreWrite):
                raise
            raise ArcPersistError(session_id, scope, exc) from exc
        if self._residency is not None:
            self._residency.scope_resident(session_id, scope, nbytes)

    def _discard_scope(self, session_id: str, scope: str) -> None:
        """Drop the in-memory copy; the next read reloads clio-core."""
        self._scopes.pop((session_id, scope), None)
        self._loaded.discard((session_id, scope))
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
        """Drop a session's in-memory scopes (write-through, nothing lost); returns how
        many were released.

        Holds the registry lock (freezing the lock set) AND each affected scope lock, so
        it never races a per-scope op. No deadlock: a per-scope op touches the registry
        only at its single ``_lock_for`` entry, before taking its scope lock. The keys
        come from the lock registry, a superset of the loaded keys (every scope is locked
        before it is loaded), never from iterating ``_scopes`` while a cold-load on
        another session inserts into it."""
        with self._registry_lock:
            lock_keys = sorted(k for k in self._scope_locks if k[0] == session_id)
            held = [self._scope_locks[k] for k in lock_keys]
            for lk in held:
                lk.acquire()
            try:
                released = 0
                for k in lock_keys:
                    if self._scopes.pop(k, None) is not None:
                        released += 1
                    self._loaded.discard(k)
                    self._index.drop_scope(k[0], k[1])
                self._invocation_counts.pop(session_id, None)
            finally:
                for lk in held:
                    lk.release()
        if self._residency is not None:
            self._residency.session_released(session_id)
        logger.info("segments: release session=%s scopes=%d", session_id, released)
        return released

    def clear(self) -> None:
        """Drop ALL in-memory scope state (store untouched); same locking as release."""
        with self._registry_lock:
            held = [self._scope_locks[k] for k in sorted(self._scope_locks)]
            sessions = sorted({k[0] for k in self._scope_locks})
            for lk in held:
                lk.acquire()
            try:
                self._scopes.clear()
                self._loaded.clear()
                self._index.clear()
                self._invocation_counts.clear()
            finally:
                for lk in held:
                    lk.release()
        if self._residency is not None:
            for session_id in sessions:
                self._residency.session_released(session_id)
