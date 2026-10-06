"""Core ARC API - Main interface for memory operations

This module provides the ARCMemory class which serves as the main interface
for all ARC (Adaptive Retrieval Cache) operations. It integrates:
    - LRUCache for hot data (O(1) access)
    - BTreeIndex for retrieval index
    - a pluggable ARCStore backend (clio-core or LocalFS; see storage.py)
      for durable records, an LSM tree (lsm.py) for write-heavy invocation
      metrics, and a SegmentStore (segments.py) for the live context plane
    - msgspec for serialization

Concurrency: ``ARCMemory._lock`` guards only the in-process hot structures
(cache, invocation index, counters); it is never held across store or LSM I/O
(each of those self-locks). See the invariant note at the lock's declaration.

See docs/ARC_MEMORY_LAYER.md for architecture details.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

from clio_agent import conf, paths
from clio_agent.arc.cache import LRUCache
from clio_agent.arc.daemon_progress import drain_while_writes_progress
from clio_agent.arc.index import BTreeIndex
from clio_agent.arc.lane_chunking import chunk_for_append
from clio_agent.arc.live import (
    EVENTS_SCOPE,
    LiveRuntimeContext,
    build_event_content,
)
from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.memory_segments import SearchUnavailableError as SearchUnavailableError
from clio_agent.arc.memory_segments import SegmentPlane
from clio_agent.arc.schema import (
    Conversation,
    Invocation,
    SegmentKind,
    VariantRecord,
    decode_conversation,
    decode_invocation,
    decode_variant_record,
    encode_conversation,
    encode_invocation,
    encode_variant_record,
)
from clio_agent.arc.storage import ARCStore, make_arc_store
from clio_agent.arc.working_set_fold import make_segment_store
from clio_agent.runtime import trace

# ``EVENTS_SCOPE`` (ARC's ONE persisted semantic-event log) is defined in ``arc.live``
# and re-exported here. It is its OWN scope and ``semantic_event`` is not a working-set
# kind, so the persisted log can never leak into a model prompt.

# Event types NOT persisted as ``semantic_event`` segments.
#   * ``lm.token.delta`` / ``variant.try.delta`` — the high-volume transient token streams
#     that ride the highway only; persisting one segment apiece would bloat ARC for
#     zero record value.
# (``arc.op`` is NOT here: it is the DERIVED write-log of a segment mutation and no
# longer enters ``record_semantic_event`` at all — the gact op-logger derives it
# DIRECTLY to the durable trace + SSE bus. With no path back into ARC's record, the
# old recursion (record -> op-logger -> arc.op -> record) cannot form, so neither the
# skip entry nor the thread-local re-entrancy guard is needed.)
_EVENT_LOG_SKIP: frozenset[str] = frozenset({"lm.token.delta", "variant.try.delta"})

logger = logging.getLogger(__name__)

# Backend names that mean the durable semantic trace is DISABLED — the same set
# :func:`clio_agent.gact.semantic_events.build_trace_backend` maps to the no-op
# backend. Kept in sync by ``tests/test_arc/test_events_log_retention.py``.


class ARCMemory(SegmentPlane):
    """Adaptive Retrieval Cache - Main interface for memory operations.

    Provides cache-first storage and retrieval for conversations and
    invocations with O(log N) fallback to disk.

    Args:
        data_dir: Directory for persistent storage (default: ".clio/agent/arc")
        cache_capacity: Maximum cache entries (default: 1000)

    Examples:
        >>> arc = ARCMemory()
        >>> conv = Conversation(session_id="session-1", ...)
        >>> arc.store_conversation(conv)
        >>> retrieved = arc.get_conversation("session-1")
        >>> stats = arc.get_cache_stats()
        >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
    """

    def __init__(
        self,
        data_dir: str | None = None,
        cache_capacity: Optional[int] = None,
        store: "ARCStore | None" = None,
        working_set_fold: Optional[bool] = None,
    ):
        """Initialize ARC memory system.

        Args:
            data_dir: Directory path for persistent storage
            cache_capacity: Maximum number of cached items
            store: Optional ARCStore for record persistence. When ``None`` the
                store is clio-core, from :func:`make_arc_store` (a typed error when it
                cannot be brought up). Pass a store to inject one (tests).
        """
        self.data_dir = Path(data_dir) if data_dir is not None else paths.arc_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Persistence seam: every record kind is read/written through an
        # ARCStore, so ARC never touches the filesystem directly. The LSM tree
        # (below) remains a separate high-throughput subsystem. The store is clio-core,
        # built by the factory.
        self._store: ARCStore = (
            store if store is not None else make_arc_store(data_dir=self.data_dir)
        )

        # Live context plane: the ordered, scoped, mutable segment store the gact
        # ReAct loop reads its prompt from each iteration. It also holds ARC's ONE
        # persisted semantic-event log (the reserved ``_events`` scope). The op_logger
        # that mirrors each op into the durable Trace is injected later by the gact app
        # via set_segment_op_logger (keeps arc/ free of any gact/ import). The
        # ``search_indexed`` keeps the reserved ``_events`` family out of the plain-text
        # search companion so the log never pollutes scope search. Under the #737 S2 fold
        # the store is a ``FoldingSegmentStore`` (working set = a fold of ``_events``).
        self._segments = make_segment_store(self._store, working_set_fold=working_set_fold)

        # Segments held per on-disk chunk of the ``_events`` family before the writer
        # rolls to the next one (so a single event re-encodes only the active chunk --
        # O(chunk) -- instead of the whole log, O(N) => O(N^2)/session). The writer
        # cursor itself (recovery + roll-over) is owned by ``arc.lane_chunking``
        # (:func:`chunk_for_append`), shared with the ``message_part`` atom lane.
        self._events_chunk_segments = conf.resolve(
            "arc.events_chunk_segments",
            env="CLIO_ARC_EVENTS_CHUNK_SEGMENTS",
            default=512,
            cast=conf.as_int,
        )

        # Live runtime context: PROJECTS the canonical semantic-event stream into
        # per-session turn records so Invocation/Conversation are projections of the
        # trace, not post-hoc rebuilds. It has NO private store and NO separate folded
        # copy -- it is a pure READER over the ONE ``_events`` log persisted into the
        # segment buffer above by ``_record_event_segment`` (one ``semantic_event``
        # segment per recorded event). Released by the session lifecycle below.
        self._live = LiveRuntimeContext(self._segments)

        # ARC-as-source highway sink: the closure (injected by gact via
        # ``set_highway_sink``) that DERIVES the data highway (durable trace / SSE /
        # hooks) from a recorded semantic event. ARC records the event FIRST
        # (persist + observer fold), THEN calls this to fan out — so ARC is the
        # source and the highway is a projection of ARC's record. Kept as an injected
        # callable so ``arc/`` never imports ``gact/``. ``None`` until wired (tests /
        # memory-only deployments) -> ``record_semantic_event`` returns ``{}``.
        self._highway_sink: Optional[Callable[[Any], Any]] = None

        # Cache layer (hot data). A hot LRU — a miss re-reads from the store, so a size
        # bound loses NO data — but it must be configurable (conf, file→env→default) so
        # smaller-RAM deployments can tune it. An explicit ``cache_capacity`` arg wins.
        capacity = (
            cache_capacity
            if cache_capacity is not None
            else conf.resolve(
                "arc.cache_capacity",
                env="CLIO_ARC_CACHE_CAPACITY",
                default=1000,
                cast=conf.as_int,
            )
        )
        self._cache = LRUCache(capacity=capacity)

        # Index layer (O(log N) retrieval), keyed by (session_id, timestamp). NO size
        # cap: an arbitrary ceiling would silently fail large workloads (entries falling
        # off the end). Memory is bounded by LIFECYCLE instead — ``release_session``
        # evicts a session's branches on end/delete, and the index is rebuildable from the
        # durable record (trace / stored blobs / clio-core) on restart.
        self._inv_index = BTreeIndex()  # Invocation index

        # LSM tree for high-throughput metrics. Flush/compaction thresholds are storage
        # mechanics (data persists to SSTables; compaction merges, never drops) — kept,
        # but conf-driven rather than hardcoded.
        self._lsm = LSMTree(
            data_dir=str(self.data_dir / "lsm"),
            memtable_size=conf.resolve(
                "arc.lsm_memtable_size",
                env="CLIO_ARC_LSM_MEMTABLE_SIZE",
                default=1000,
                cast=conf.as_int,
            ),
            compaction_threshold=conf.resolve(
                "arc.lsm_compaction_threshold",
                env="CLIO_ARC_LSM_COMPACTION_THRESHOLD",
                default=5,
                cast=conf.as_int,
            ),
        )

        # Thread safety.
        #
        # INVARIANT — ``_lock`` guards ONLY the hot in-memory structures that have no lock
        # of their own: the invocation B-tree (``_inv_index``), the ``_disk_reads`` /
        # ``_disk_writes`` counters, and multi-step *composite* reads/writes over the cache
        # (e.g. scanning ``_cache._cache`` internals, or a cache+index pair that a reader
        # must see consistently). It is NEVER held across ``_store`` I/O (clio-core RPCs /
        # LocalFS reads) or ``_lsm`` writes — those sub-components carry their own locks
        # (``LRUCache._lock`` cache.py, ``LSMTree._lock`` with double-buffered flush lsm.py,
        # ``SegmentStore`` per-scope locks segments.py), and holding this lock across their
        # I/O serialized every session behind one slow store RPC. So the pattern in every
        # method below is: encode / ``store.get`` / ``store.put`` / ``store.scan`` /
        # ``lsm.write`` OUTSIDE the lock; ``_inv_index`` surgery, counter bumps, and
        # cache-composite ops UNDER it. A plain ``LRUCache.put/get/invalidate`` is atomic on
        # its own lock, so those may run outside ``_lock`` when not part of a composite.
        # Reentrancy note: helpers that take ``_lock`` for a counter (``get_invocation``)
        # must be called with ``_lock`` RELEASED (``threading.Lock`` is not reentrant).
        self._lock = threading.Lock()

        # Per-session conversation lock. ``store_conversation`` and
        # ``get_conversation`` do a cache+store PAIR (write: cache.put -> store.put;
        # slow-path read: store.get -> cache.put) that must be atomic *per session* or
        # a cache-miss reader can refill the LRU with a stale disk value AFTER a
        # concurrent writer cached the fresh one — a silent lost update that pins the
        # hot path to the old conversation forever. This lock serializes only ops on
        # the SAME session (distinct sessions still run concurrently and the global
        # ``_lock`` is never held across a store RPC — the #771 narrowing goal holds).
        # Locks are keyed by session_id and never evicted (one tiny Lock per session
        # ever seen), matching the SegmentStore per-scope-lock discipline: evicting a
        # lock an in-flight op still holds would split mutual exclusion.
        self._conv_locks: Dict[str, threading.Lock] = {}
        self._conv_locks_registry = threading.Lock()

        # In-flight invocation writes, per session. ``store_invocation`` persists the
        # record OUTSIDE ``_lock`` (the #771 narrowing) and inserts the ``_inv_index``
        # entry only AFTER that store RPC returns, so there is a window where an
        # invocation is being written but is not yet indexed. A ``release_session`` that
        # counted/evicted the index during that window under-counted the in-flight
        # invocation, and its index entry then landed AFTER the release as a leaked
        # stale entry for an already-released session (#804). This counter is incremented
        # BEFORE the store RPC and decremented AFTER the index insert, so
        # ``release_session`` can DRAIN a session's pending writes before it reads the
        # index. Guarded by a Condition (never held across ``_store``/``_lsm`` I/O nor
        # ``_lock``, so it introduces no new lock-ordering edge).
        self._inflight_inv: Dict[str, int] = {}
        self._inflight_done = 0  # writes finished: the drain's progress signal
        self._inflight_cv = threading.Condition()

        # Performance tracking
        self._disk_reads = 0
        self._disk_writes = 0

    def _conv_lock(self, session_id: str) -> threading.Lock:
        """Return the per-session conversation lock, creating it once. The registry
        lock is held only for the brief lookup/create, never across a store RPC."""
        with self._conv_locks_registry:
            lk = self._conv_locks.get(session_id)
            if lk is None:
                lk = threading.Lock()
                self._conv_locks[session_id] = lk
            return lk

    def store_conversation(self, conversation: Conversation) -> None:
        """Store conversation in cache and on disk.

        Writes conversation to:
        1. In-memory cache (fast access)
        2. Disk (persistent storage)

        Args:
            conversation: Conversation object to store

        Examples:
            >>> conv = Conversation(
            ...     session_id="session-1",
            ...     user_id="user@example.com",
            ...     created_at="2025-01-09T14:30:00Z",
            ...     updated_at="2025-01-09T14:30:00Z",
            ...     last_accessed="2025-01-09T14:30:00Z",
            ...     status="active"
            ... )
            >>> arc.store_conversation(conv)
        """
        session_id = conversation.session_id
        cache_key = f"conv:{session_id}"

        # Encode OUTSIDE any lock (CPU-only, no shared state).
        encoded = encode_conversation(conversation)

        # The cache write and the store write are one atomic unit PER SESSION: a
        # concurrent get_conversation refill on this session cannot interleave and
        # clobber this fresh value with a stale disk read. The store RPC runs under the
        # per-session lock (serializing only same-session ops), never under _lock.
        with self._conv_lock(session_id):
            self._cache.put(cache_key, conversation)
            self._store.put("conversations", session_id, encoded)

        # Only the counter needs the ARC lock (taken separately, never nested).
        with self._lock:
            self._disk_writes += 1

    def get_conversation(self, session_id: str) -> Optional[Conversation]:
        """Retrieve conversation (cache-first).

        Lookup order:
        1. Check cache (O(1))
        2. Check disk (O(1) file read)

        Args:
            session_id: Session identifier

        Returns:
            Conversation object if found, None otherwise

        Examples:
            >>> conv = arc.get_conversation("session-1")
            >>> if conv:
            ...     print(f"Found {len(conv.messages)} messages")
        """
        cache_key = f"conv:{session_id}"

        # Fast path: check cache (lock-free; a plain LRU get is atomic).
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path under the per-session lock with a double-checked cache read: the
        # store.get -> cache.put refill is serialized against a concurrent
        # store_conversation on the same session, so the cache can never be left
        # holding a value older than disk. The store RPC runs under this per-session
        # lock (same-session only), never under _lock.
        with self._conv_lock(session_id):
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            encoded = self._store.get("conversations", session_id)
            if encoded is None:
                return None
            conversation = decode_conversation(encoded)
            self._cache.put(cache_key, conversation)

        # Bump the counter under the ARC lock (taken separately, never nested).
        with self._lock:
            self._disk_reads += 1

        return conversation

    def get_conversation_history(self, session_id: str, limit: int = 10) -> List[Conversation]:
        """Get recent conversations for session.

        Retrieves the most recent conversation states (useful for seeing
        conversation evolution over time).

        Args:
            session_id: Session identifier
            limit: Maximum number of conversation states to retrieve

        Returns:
            List of Conversation objects, most recent first

        Examples:
            >>> history = arc.get_conversation_history("session-1", limit=5)
            >>> for conv in history:
            ...     print(f"Updated: {conv.updated_at}")
        """
        # For now, return single conversation if exists
        # In future, could track conversation snapshots over time
        conv = self.get_conversation(session_id)
        return [conv] if conv else []

    def store_invocation(self, invocation: Invocation) -> None:
        """Store agent invocation.

        Persists invocation trace to cache, index, and disk for later
        analysis and optimization.

        Args:
            invocation: Invocation object to store

        Examples:
            >>> inv = Invocation(
            ...     trace_id="trace-123",
            ...     session_id="session-1",
            ...     parent_trace_id=None,
            ...     agent_id="DataExpert",
            ...     tier=2,
            ...     source="native",
            ...     started_at="2025-01-09T14:30:00Z",
            ...     completed_at="2025-01-09T14:30:01Z",
            ...     duration_ms=1000,
            ...     status="success",
            ...     input={},
            ...     output={}
            ... )
            >>> arc.store_invocation(inv)
        """
        trace_id = invocation.trace_id
        session_id = invocation.session_id
        cache_key = f"inv:{trace_id}"
        timestamp = self._parse_timestamp(invocation.started_at)

        # Encode OUTSIDE _lock (store RPC carries its own cost).
        encoded = encode_invocation(invocation)

        # Mark this write in-flight for the session BEFORE the store RPC and clear it
        # only AFTER the index insert, so a concurrent release_session drains it rather
        # than racing the insert (#804 -- see _drain_inflight_invocations and the
        # counter's declaration). ``finally`` guarantees the mark clears even if the
        # store RPC / LSM write raises, so a failed write can never wedge a drain.
        self._enter_inflight(session_id)
        try:
            self._store.put("invocations", trace_id, encoded)

            # Also store in LSM tree for high-throughput metrics queries. LSMTree is
            # self-locked with a double-buffered flush, so this stays OUTSIDE _lock.
            self._lsm.write(
                timestamp=timestamp,
                metric={
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "agent_id": invocation.agent_id,
                    "tier": invocation.tier,
                    "duration_ms": invocation.duration_ms,
                    "status": invocation.status,
                },
            )

            # Hot structures under the ARC lock: cache + index must be updated as a consistent pair
            # (a reader takes _lock to see both), and _inv_index has no lock of its own. The
            # trace_id is part of the composite index key so two invocations in the same session
            # that share a timestamp (coarse clocks resolve sub-millisecond calls to the same tick)
            # do not collide and silently drop one another.
            index_key = (session_id, timestamp, trace_id)
            with self._lock:
                self._cache.put(cache_key, invocation)
                self._inv_index.insert(index_key, {"trace_id": trace_id})
                self._disk_writes += 1
        finally:
            self._exit_inflight(session_id)

    def get_invocation(self, invocation_id: str) -> Optional[Invocation]:
        """Get specific invocation.

        Args:
            invocation_id: Trace ID of invocation

        Returns:
            Invocation object if found, None otherwise

        Examples:
            >>> inv = arc.get_invocation("trace-123")
            >>> if inv:
            ...     print(f"Duration: {inv.duration_ms}ms")
        """
        cache_key = f"inv:{invocation_id}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk OUTSIDE _lock (store read carries its own cost).
        # Unlike conversations, this refill needs NO per-key lock: an invocation record
        # is keyed by a unique uuid4 trace_id and is write-once (never updated after
        # store_invocation), so disk[trace_id] is immutable and a refill can never read
        # a value staler than a concurrent write — there is no same-key read-modify-write.
        encoded = self._store.get("invocations", invocation_id)
        if encoded is None:
            return None

        invocation = decode_invocation(encoded)

        # Update cache (self-locked); bump counter under _lock.
        self._cache.put(cache_key, invocation)
        with self._lock:
            self._disk_reads += 1

        return invocation

    def get_session_invocations(self, session_id: str, limit: int = 100) -> List[Invocation]:
        """Get invocations for a session.

        Retrieves invocation traces for analysis and debugging.

        Args:
            session_id: Session identifier
            limit: Maximum number of invocations to retrieve

        Returns:
            List of Invocation objects, most recent first

        Examples:
            >>> invocations = arc.get_session_invocations("session-1", limit=10)
            >>> for inv in invocations:
            ...     print(f"{inv.agent_id}: {inv.duration_ms}ms")
        """
        # Snapshot the session's index entries UNDER _lock (BTreeIndex has no lock
        # of its own and a concurrent release_session/store_invocation mutates it).
        # Copy to a plain list so the store loads below run with the lock released.
        with self._lock:
            index_entries = list(self._inv_index.get_session_range(session_id))

        # Extract trace IDs and load invocations. The B-tree is in-memory, so
        # after process restart it may be empty even though invocation files
        # exist on disk. Fall back to scanning persisted invocations.
        invocations = []
        if index_entries:
            for entry in index_entries[-limit:]:  # Get most recent
                trace_id = entry["trace_id"]
                inv = self.get_invocation(trace_id)  # self-locks for its counter
                if inv:
                    invocations.append(inv)
        else:
            # Materialize the whole-kind scan OUTSIDE _lock, then decode + count.
            rows = list(self._store.scan("invocations"))
            decoded = 0
            for _name, encoded in rows:
                try:
                    inv = decode_invocation(encoded)
                    decoded += 1
                except Exception:  # noqa: BLE001 - corrupt/undecodable invocation row skipped; disk read continues
                    continue
                if inv.session_id == session_id:
                    invocations.append(inv)
            with self._lock:
                self._disk_reads += decoded
            invocations.sort(key=lambda inv: inv.started_at)
            invocations = invocations[-limit:]

        # Return most recent first
        return list(reversed(invocations))

    def get_tool_cache_stats(self) -> Dict[str, Any]:
        """Get tool cache statistics.

        Returns:
            Dict with tool cache hit rate and counts

        Examples:
            >>> stats = arc.get_tool_cache_stats()
            >>> print(f"Tool cache hit rate: {stats['tool_cache_hit_rate']:.2%}")
        """
        stats = self._cache.stats()
        return {
            "tool_cache_hit_rate": stats["hit_rate"],
            "tool_cache_hits": stats["hits"],
            "tool_cache_misses": stats["misses"],
            "tool_cache_size": stats["size"],
        }

    def get_lsm_stats(self) -> Dict[str, Any]:
        """Get LSM tree statistics.

        Returns:
            Dict with write throughput, compaction stats

        Examples:
            >>> stats = arc.get_lsm_stats()
            >>> print(f"LSM writes: {stats['write_count']}")
            >>> print(f"Flushes: {stats['flush_count']}")
            >>> print(f"Compactions: {stats['compaction_count']}")
        """
        return self._lsm.get_stats()

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache performance statistics.

        Returns:
            Dictionary containing:
                - hit_rate: Cache hit rate (0.0-1.0)
                - hits: Total cache hits
                - misses: Total cache misses
                - size: Current cache size
                - capacity: Maximum cache capacity
                - disk_reads: Total disk reads
                - disk_writes: Total disk writes
                - inv_index_size: Invocation index entry count

        Examples:
            >>> stats = arc.get_cache_stats()
            >>> print(f"Hit rate: {stats['hit_rate']:.2%}")
            >>> print(f"Disk reads: {stats['disk_reads']}")
        """
        with self._lock:
            cache_stats = self._cache.stats()

            return {
                "hit_rate": cache_stats["hit_rate"],
                "hits": cache_stats["hits"],
                "misses": cache_stats["misses"],
                "size": cache_stats["size"],
                "capacity": cache_stats["capacity"],
                "disk_reads": self._disk_reads,
                "disk_writes": self._disk_writes,
                "inv_index_size": len(self._inv_index),
            }

    # ---- Optimizer: Invocation + Variant queries ----

    def get_invocations_by_agent(
        self,
        agent_id: str,
        status: str | None = None,
        limit: int = 500,
    ) -> list[Invocation]:
        """Get invocations for a specific agent across all sessions.

        Scans invocation files on disk, filters by agent_id and optionally
        by status. Returns list sorted by started_at descending (most recent
        first).

        Args:
            agent_id: Agent identifier (e.g., "data", "analysis", "visualization")
            status: Optional status filter ("success", "failure", "timeout")
            limit: Maximum number of invocations to return (default: 500)

        Returns:
            List of Invocation objects, most recent first

        Examples:
            >>> invocations = arc.get_invocations_by_agent("data", status="success")
            >>> for inv in invocations:
            ...     print(f"{inv.trace_id}: {inv.duration_ms}ms")
        """
        invocations: list[Invocation] = []

        # Materialize the whole-kind scan OUTSIDE _lock; decode + filter, count after.
        rows = list(self._store.scan("invocations"))
        decoded = 0
        for _name, encoded in rows:
            try:
                inv = decode_invocation(encoded)
                decoded += 1

                if inv.agent_id != agent_id:
                    continue
                if status is not None and inv.status != status:
                    continue

                invocations.append(inv)
            except Exception:  # noqa: BLE001 - corrupt/undecodable invocation row skipped
                continue
        with self._lock:
            self._disk_reads += decoded

        # Sort by started_at descending (most recent first)
        invocations.sort(key=lambda inv: inv.started_at, reverse=True)
        return invocations[:limit]

    def iter_invocations(self) -> list[Invocation]:
        """Return every persisted invocation (decode-tolerant, unordered).

        Public accessor for whole-corpus scans (e.g. optimizer training-data
        counts) so callers go through the ARCStore seam instead of reaching
        into physical storage.
        """
        invocations: list[Invocation] = []
        # Materialize the whole-kind scan OUTSIDE _lock; decode, count after.
        rows = list(self._store.scan("invocations"))
        decoded = 0
        for _name, encoded in rows:
            try:
                invocations.append(decode_invocation(encoded))
                decoded += 1
            except Exception:  # noqa: BLE001 - corrupt/undecodable invocation row skipped
                continue
        with self._lock:
            self._disk_reads += decoded
        return invocations

    def store_variant_record(self, record: VariantRecord) -> None:
        """Store a variant record in ARC.

        Persists variant metadata to cache and disk for tracking
        optimization results and variant lifecycle.

        Args:
            record: VariantRecord object to store

        Examples:
            >>> from clio_agent.arc.schema import VariantRecord
            >>> record = VariantRecord(
            ...     variant_id="data_v2",
            ...     agent_id="data",
            ...     before_score=0.65,
            ...     after_score=0.82,
            ... )
            >>> arc.store_variant_record(record)
        """
        cache_key = f"variant:{record.variant_id}"
        self._cache.put(cache_key, record)

        # Encode + persist to disk OUTSIDE _lock.
        encoded = encode_variant_record(record)
        self._store.put("variants", record.variant_id, encoded)
        with self._lock:
            self._disk_writes += 1

    def get_variant_records(self, agent_id: str) -> list[VariantRecord]:
        """Get all variant records for a specific agent.

        Scans variant files on disk, returns all records matching
        the given agent_id sorted by created_at descending.

        Args:
            agent_id: Agent identifier (e.g., "data", "analysis")

        Returns:
            List of VariantRecord objects, most recent first

        Examples:
            >>> records = arc.get_variant_records("data")
            >>> for r in records:
            ...     print(f"{r.variant_id}: {r.before_score} -> {r.after_score}")
        """
        records: list[VariantRecord] = []

        # Materialize the whole-kind scan OUTSIDE _lock; decode + filter, count after.
        rows = list(self._store.scan("variants"))
        decoded = 0
        for _name, encoded in rows:
            try:
                record = decode_variant_record(encoded)
                decoded += 1

                if record.agent_id == agent_id:
                    records.append(record)
            except Exception:  # noqa: BLE001 - corrupt/undecodable record skipped
                continue
        with self._lock:
            self._disk_reads += decoded

        # Sort by created_at descending (most recent first)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    # ---- Live runtime context (trace fold) ----

    def set_highway_sink(self, sink: "Callable[[Any], Any] | None") -> None:
        """Inject the highway-derive sink (called by gact at the arc choke point).

        ``sink`` fans a recorded semantic event out to the data highway (durable
        trace / SSE / hooks). ARC calls it LAST in :meth:`record_semantic_event`,
        AFTER persisting + folding the event, so the highway is a projection of
        ARC's record rather than a parallel consumer. Kept injected so ``arc/``
        never imports ``gact/``.
        """
        self._highway_sink = sink

    def record_semantic_event(self, event: Any) -> Any:
        """ARC-as-source entry: record a semantic event, THEN derive the highway.

        This is the inversion point. Every semantic event flows through ARC FIRST
        so ARC holds everything, and the data highway is DERIVED from ARC's record:

        1. ``on_semantic_event(event)`` — persist the event as one ``semantic_event``
           segment under the reserved ``_events`` scope (ARC's complete, freeze-anytime
           record AND the single substrate the live observer projects over).
        2. ``self._highway_sink(event)`` — derive the highway (durable trace / SSE /
           hooks). Returns the sink's value (the projected event dict) so callers
           that expected ``sink.emit(event)``'s return are unaffected; ``{}`` when
           no sink is wired.

        clio-core is the record: a persist failure raises, and nothing is derived
        from an event clio-core does not hold.
        """
        self.on_semantic_event(event)
        sink = self._highway_sink
        if sink is None:
            return {}
        return sink(event)

    def _event_skip_reason(self, etype: str, sid: str) -> str:
        """ONE decision for whether to persist an event to the ``_events`` log.

        Returns the skip reason (``""`` => persist). Collapses every drop into one
        place so a dropped event is a single explainable line, not a scattered set of
        silent early-returns:

        * ``"no-event-type"`` — malformed event with no type (anomaly).
        * ``"skip-listed"``   — a derived/high-volume highway-only type (lm.token.delta).
        * ``"no-session-id"`` — an EXPECTED category: some top-level events are
          session-less (no session to file them under). Not an error.
        """
        if not etype:
            return "no-event-type"
        if etype in _EVENT_LOG_SKIP:
            return "skip-listed"
        if not sid:
            return "no-session-id"
        return ""

    def _record_event_segment(self, event: Any) -> None:
        """Persist one semantic event as a ``semantic_event`` segment (append-only).

        ONE skip decision (:meth:`_event_skip_reason`) gates the persist; every
        persist/skip is logged via ``runtime.trace`` (HF_ON-guarded ``ARC-EVENTS`` hot
        tag) so a dropped event is one ``CLIO_DEBUG=high`` line. Builds a lean content
        dict from the SemanticEvent's fields (stored verbatim, no caps), correlated by the
        event's trajectory span ids, and appends ONE segment under the reserved
        ``_events`` scope. Never rendered into a prompt (own scope + non-working-set
        kind)."""
        etype = str(getattr(event, "event_type", "") or "")
        sid = str(getattr(event, "session_id", "") or "")
        reason = self._event_skip_reason(etype, sid)
        if reason:
            if trace.HF_ON:
                trace.hot(
                    "ARC-EVENTS", "skip %s etype=%r sid=%r arc=%#x", reason, etype, sid, id(self)
                )
            return
        if trace.HF_ON:
            trace.hot(
                "ARC-EVENTS",
                "persist etype=%r sid=%r arc=%#x",
                etype,
                sid,
                id(self),
            )
        self._append_event_segment(event, etype, sid)

    def _append_event_segment(self, event: Any, etype: str, sid: str) -> None:
        """Build (via the shared :func:`~clio_agent.arc.live.build_event_content`) +
        append the lean ``semantic_event`` segment to the session's ACTIVE ``_events``
        chunk. ONE builder is shared with the standalone observer so the persisted log
        is identical regardless of path. The chunk cursor
        (:func:`~clio_agent.arc.lane_chunking.chunk_for_append`) bounds each append's
        re-encode to one chunk instead of the whole log."""
        content = build_event_content(event)
        if content is None:
            return
        scope = chunk_for_append(
            self._segments, sid, EVENTS_SCOPE, capacity=self._events_chunk_segments
        )
        self._segments.append(
            sid,
            scope,
            cast(SegmentKind, "semantic_event"),
            content,
            step=-1,
            turn_id=str(getattr(event, "turn_id", "") or ""),
            expert_span_id=str(getattr(event, "expert_span_id", "") or ""),
        )

    def on_semantic_event(self, event: Any) -> None:
        """Persist one RAW semantic event as the single ``_events`` log record.

        Registered as a ``live_consumer`` on the SemanticEventSink so ARC sees the
        same events the durable trace captures. The persisted ``semantic_event``
        segments under ``_events`` are the ONE log: the live observer
        (:class:`LiveRuntimeContext`) projects its view / Conversation / Invocation
        records directly over them — there is no separate folded copy. Best-effort by
        construction.
        """
        self._record_event_segment(event)

    def get_live_context(
        self, session_id: str, *, max_turns: Optional[int] = None
    ) -> Dict[str, Any]:
        """Live summary of an open session's turns (or empty). ``max_turns`` is an
        OPTIONAL recent-window the caller may pass (its own configurable budget);
        ``None`` (default) returns every turn — no hardcoded cap."""
        return self._live.view(session_id, max_turns=max_turns)

    def project_live_conversation(self, session_id: str, *, user_id: str = "") -> Any:
        """Project the live fold of a session into a Conversation (or None)."""
        return self._live.project_conversation(session_id, user_id=user_id)

    def project_live_invocations(self, session_id: str) -> List[Invocation]:
        """Project the live fold of a session into per-expert Invocations."""
        return self._live.project_invocations(session_id)

    def _enter_inflight(self, session_id: str) -> None:
        """Register an in-flight invocation write for ``session_id`` (#804).

        Called BEFORE the store RPC in :meth:`store_invocation` so a concurrent
        :meth:`release_session` observes the pending write and drains it.
        """
        with self._inflight_cv:
            self._inflight_inv[session_id] = self._inflight_inv.get(session_id, 0) + 1

    def _exit_inflight(self, session_id: str) -> None:
        """Clear one in-flight invocation write for ``session_id`` and wake drainers.

        Called AFTER the ``_inv_index`` insert (in a ``finally``) so a draining
        :meth:`release_session` unblocks only once the index reflects the write.
        """
        with self._inflight_cv:
            remaining = self._inflight_inv.get(session_id, 0) - 1
            self._inflight_done += 1
            if remaining > 0:
                self._inflight_inv[session_id] = remaining
            else:
                self._inflight_inv.pop(session_id, None)
            self._inflight_cv.notify_all()

    def _drain_inflight_invocations(self, session_id: str, window_s: float | None = None) -> int:
        """Block while ``session_id`` invocation writes are in flight AND progressing (a write finishing, or the daemon working: :func:`~clio_agent.arc.daemon_progress.drain_while_writes_progress`; no fixed deadline), then return the residual in-flight count -- 0 on a clean drain, ``>0`` only when they stopped progressing for a whole ``window_s`` (``arc.liveness.stall_after_s``) or hit ``arc.liveness.max_wait_s`` (typed reason ``inflight_drain_timeout``, logged). :meth:`release_session` drains before it counts/evicts the index so an in-flight ``store_invocation`` is not under-counted, and surfaces this as ``inflight_pending`` (#804)."""
        cv, inflight, op = self._inflight_cv, self._inflight_inv, "inflight_drain"
        settled = drain_while_writes_progress(
            cv,
            lambda: not inflight.get(session_id),
            lambda: self._inflight_done,
            op_name=op,
            no_progress_s=window_s,
        )
        pending = 0 if settled.done else inflight.get(session_id, 0)
        if pending:
            msg = "arc: release_session proceeding with %d in-flight invocation write(s) session=%s reason=inflight_drain_timeout wait=%s (index count may under-report; #804)"
            logger.warning(msg, pending, session_id, settled.reason)
        return pending

    def release_session(self, session_id: str) -> Dict[str, int]:
        """Release a session's hot footprint from cache and indexes.

        Persistence is write-through, so eviction loses nothing: a later read
        re-loads from the store. Called when a session goes idle or closes so an
        otherwise-idle server returns toward baseline memory instead of pinning
        every session's objects in the never-evicted hot path.

        Args:
            session_id: Session to release.

        Returns:
            Counts of evicted cache and index entries (for diagnostics/tests).
        """
        # Drain any in-flight invocation write for this session BEFORE reading the index, so a concurrent store_invocation
        # whose durable write is mid-flight is counted/evicted here rather than leaking a stale index entry after the release
        # (#804). Done outside _lock -- the drain waits on its own Condition. RESIDUAL WINDOW (not closed here): the drain and
        # the ``with self._lock`` below are not atomic, so a store_invocation that BEGINS after the drain sees count==0 but
        # before _lock is taken can still insert its index entry post-evict (only when a session is released mid-turn); the
        # complete fix -- a caller-side active-session guard like rollback's -- is a follow-up, and the non-zero ``inflight_pending`` returned below surfaces this meanwhile.
        inflight_pending = self._drain_inflight_invocations(session_id)

        with self._lock:
            evicted_cache = 0

            # Invocations are cached by trace_id; resolve them through the index
            # before its entries are removed below.
            for entry in self._inv_index.get_session_range(session_id):
                trace_id = entry.get("trace_id") if isinstance(entry, dict) else None
                if trace_id:
                    self._cache.invalidate(f"inv:{trace_id}")
                    evicted_cache += 1

            self._cache.invalidate(f"conv:{session_id}")
            evicted_cache += 1

            evicted_index = self._inv_index.delete_session(session_id)

        # Outside the lock: the SegmentStore has its own locks. Release drops only the
        # hot in-memory copy (write-through): clio-core keeps the session's whole
        # ``_events`` family -- its event log and transcript atoms -- whatever the trace
        # backend. clio-core is the one context store; nothing else is trusted to hold it.
        segments = self._segments.release(session_id)
        return {
            "cache": evicted_cache,
            "index": evicted_index,
            "segments": segments,
            # 0 on a clean drain; >0 only when the in-flight drain timed out, so a caller detects an under-counted release without grepping logs (#804).
            "inflight_pending": inflight_pending,
        }

    def flush_and_release(self) -> None:
        """Release ALL in-memory state to return to baseline (tests/memprof).

        Flushes the LSM MemTable to disk, clears the cache, and clears the
        invocation index. The persistent store is untouched and fully
        re-loadable; this only drops the hot/heap copies so memory profiling
        sees a clean floor.

        Examples:
            >>> arc.flush_and_release()
            >>> arc.get_cache_stats()['size']
            0
        """
        # LSM flush is self-locked (double-buffered) — keep it OUTSIDE _lock.
        self._lsm.flush()
        with self._lock:
            self._cache.clear()
            self._inv_index.clear()
        # Drops only in-memory copies: clio-core keeps every session's ``_events``
        # family (see ``release_session``).
        self._segments.clear()

    def clear_cache(self) -> None:
        """Clear in-memory cache (preserves disk storage).

        Useful for testing or memory pressure situations.

        Examples:
            >>> arc.clear_cache()
            >>> stats = arc.get_cache_stats()
            >>> assert stats['size'] == 0
        """
        self._cache.clear()

    def clear_all(self) -> None:
        """Clear all data (cache, indexes, and disk).

        WARNING: This deletes all persistent data. Use with caution.

        Examples:
            >>> arc.clear_all()  # Only use in tests or to reset state
        """
        # Hot-structure surgery under _lock: cache, index, and counters. The _events
        # chunk family is wiped from disk below (``_store.clear()`` / ``_segments.clear()``),
        # which already invalidates every writer cursor (arc.lane_chunking checks
        # ``store._loaded``, cleared there) — no separate cursor reset needed.
        with self._lock:
            self._cache.clear()
            self._inv_index.clear()
            self._disk_reads = 0
            self._disk_writes = 0

        # Disk + in-memory segment-plane wipe OUTSIDE _lock (store and SegmentStore
        # carry their own locks). ``_store.clear`` wipes the "segments" kind too
        # (it is in ARC_KINDS); ``_segments.clear`` drops the in-memory plane after.
        self._store.clear()
        self._segments.clear()

    def __del__(self) -> None:
        """Cleanup LSM tree on delete."""
        lsm = getattr(self, "_lsm", None)
        if lsm is None:
            return

        try:
            lsm.close()
        except Exception:  # noqa: BLE001,S110 - destructors must not raise during shutdown/tmpdir cleanup
            # Destructors must not raise during interpreter shutdown or tmpdir cleanup.
            pass

    @staticmethod
    def _parse_timestamp(timestamp: float | str) -> float:
        """Parse timestamp to float.

        Args:
            timestamp: Float (Unix timestamp) or str (ISO 8601)

        Returns:
            Float timestamp

        Examples:
            >>> ts = ARCMemory._parse_timestamp("2025-01-09T14:30:00Z")
            >>> ts > 0
            True
            >>> ARCMemory._parse_timestamp(1736433000.0)
            1736433000.0
        """
        # If already float, return as-is
        if isinstance(timestamp, (int, float)):
            return float(timestamp)

        # Parse string timestamp (ISO 8601)
        if isinstance(timestamp, str):
            from datetime import datetime

            if timestamp.endswith("Z"):
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(timestamp)

            return dt.timestamp()

        # Fallback: return current timestamp
        return time.time()
