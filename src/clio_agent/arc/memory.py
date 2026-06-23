"""Core ARC API - Main interface for memory operations

This module provides the ARCMemory class which serves as the main interface
for all ARC (Adaptive Retrieval Cache) operations. It integrates:
    - LRUCache for hot data (O(1) access)
    - BTreeIndex for O(log N) retrieval
    - msgspec for efficient serialization
    - Thread-safe operations

Performance Targets:
    - Cache hit rate > 85%
    - Retrieval latency < 10ms
    - Thread-safe concurrent access

See docs/ARC_MEMORY_LAYER.md for architecture details.
"""

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, cast

from clio_agent import conf
from clio_agent.arc.cache import LRUCache
from clio_agent.arc.index import BTreeIndex
from clio_agent.arc.live import EVENTS_SCOPE, LiveRuntimeContext, build_event_content
from clio_agent.arc.lsm import LSMTree
from clio_agent.arc.schema import (
    Context,
    Conversation,
    DatasetProfile,
    Invocation,
    Metrics,
    ProceduralMemory,
    SegmentKind,
    VariantRecord,
    decode_context,
    decode_conversation,
    decode_dataset_profile,
    decode_invocation,
    decode_metrics,
    decode_procedural_memory,
    decode_variant_record,
    encode_context,
    encode_conversation,
    encode_dataset_profile,
    encode_invocation,
    encode_metrics,
    encode_procedural_memory,
    encode_variant_record,
)
from clio_agent.arc.segments import OpLogger, SegmentStore
from clio_agent.arc.storage import ARCStore, LocalFSStore
from clio_agent.runtime import trace

# ``EVENTS_SCOPE`` (the reserved scope holding ARC's ONE persisted semantic-event log)
# is defined in ``arc.live`` (the observer that projects over it) and imported above so
# the writer (this module) and the reader share one constant. The import re-exports it
# as ``clio_agent.arc.memory.EVENTS_SCOPE`` for back-compat importers. It is its OWN
# scope, so an expert/working-set render never sees it; combined with ``semantic_event``
# not being a working-set kind nor part of the dspy trajectory projection, the persisted
# log can never leak into a model prompt.

# Event types NOT persisted as ``semantic_event`` segments.
#   * ``lm.token.delta`` — the high-volume transient live-token stream (~1840/turn)
#     that rides the highway only; persisting one segment apiece would bloat ARC for
#     zero record value.
# (``arc.op`` is NOT here: it is the DERIVED write-log of a segment mutation and no
# longer enters ``record_semantic_event`` at all — the gact op-logger derives it
# DIRECTLY to the durable trace + SSE bus. With no path back into ARC's record, the
# old recursion (record -> op-logger -> arc.op -> record) cannot form, so neither the
# skip entry nor the thread-local re-entrancy guard is needed.)
_EVENT_LOG_SKIP: frozenset[str] = frozenset({"lm.token.delta"})


class ARCMemory:
    """Adaptive Retrieval Cache - Main interface for memory operations.

    Provides cache-first storage and retrieval for conversations, invocations,
    metrics, and context with O(log N) fallback to disk.

    Args:
        data_dir: Directory for persistent storage (default: ".clio_agent/arc")
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
        data_dir: str = ".clio_agent/arc",
        cache_capacity: Optional[int] = None,
        store: "ARCStore | None" = None,
    ):
        """Initialize ARC memory system.

        Args:
            data_dir: Directory path for persistent storage
            cache_capacity: Maximum number of cached items
            store: Optional ARCStore for record persistence. Defaults to a
                local-filesystem store rooted at ``data_dir``; inject a
                clio-core CTE-backed store here to relocate persistence without
                changing any call site.
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Persistence seam: every record kind is read/written through an
        # ARCStore, so ARC never touches the filesystem directly. The LSM tree
        # (below) remains a separate high-throughput subsystem.
        self._store: ARCStore = store if store is not None else LocalFSStore(self.data_dir)

        # Live context plane: the ordered, scoped, mutable segment store the gact
        # ReAct loop reads its prompt from each iteration. It also holds ARC's ONE
        # persisted semantic-event log (the reserved ``_events`` scope). The op_logger
        # that mirrors each op into the durable Trace is injected later by the gact app
        # via set_segment_op_logger (keeps arc/ free of any gact/ import).
        self._segments = SegmentStore(self._store)

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

        # Index layers (O(log N) retrieval), keyed by (session_id, timestamp). NO size
        # cap: an arbitrary ceiling would silently fail large workloads (entries falling
        # off the end). Memory is bounded by LIFECYCLE instead — ``release_session``
        # evicts a session's branches on end/delete, and the index is rebuildable from the
        # durable record (trace / stored blobs / clio-core) on restart.
        self._conv_index = BTreeIndex()  # Conversation index
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

        # Thread safety
        self._lock = threading.Lock()

        # Performance tracking
        self._disk_reads = 0
        self._disk_writes = 0

    def store_conversation(self, conversation: Conversation) -> None:
        """Store conversation in cache and index.

        Writes conversation to:
        1. In-memory cache (fast access)
        2. B-tree index (O(log N) lookup)
        3. Disk (persistent storage)

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
        with self._lock:
            session_id = conversation.session_id
            cache_key = f"conv:{session_id}"

            # Store in cache (hot data)
            self._cache.put(cache_key, conversation)

            # Store in index (for range queries)
            # Parse timestamp for index key
            timestamp = self._parse_timestamp(conversation.updated_at)
            index_key = (session_id, timestamp)
            self._conv_index.insert(index_key, {"session_id": session_id})

            # Persist to disk
            encoded = encode_conversation(conversation)
            self._store.put("conversations", session_id, encoded)
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

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            encoded = self._store.get("conversations", session_id)
            if encoded is None:
                return None

            conversation = decode_conversation(encoded)
            self._disk_reads += 1

            # Update cache for future access
            self._cache.put(cache_key, conversation)

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
        with self._lock:
            trace_id = invocation.trace_id
            session_id = invocation.session_id
            cache_key = f"inv:{trace_id}"

            # Store in cache
            self._cache.put(cache_key, invocation)

            # Store in index (for session-based queries)
            timestamp = self._parse_timestamp(invocation.started_at)
            index_key = (session_id, timestamp)
            self._inv_index.insert(index_key, {"trace_id": trace_id})

            # Persist to disk
            encoded = encode_invocation(invocation)
            self._store.put("invocations", trace_id, encoded)
            self._disk_writes += 1

            # Also store in LSM tree for high-throughput metrics queries
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

        # Slow path: load from disk
        with self._lock:
            encoded = self._store.get("invocations", invocation_id)
            if encoded is None:
                return None

            invocation = decode_invocation(encoded)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, invocation)

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
        # Get all index entries for this session
        index_entries = self._inv_index.get_session_range(session_id)

        # Extract trace IDs and load invocations. The B-tree is in-memory, so
        # after process restart it may be empty even though invocation files
        # exist on disk. Fall back to scanning persisted invocations.
        invocations = []
        if index_entries:
            for entry in index_entries[-limit:]:  # Get most recent
                trace_id = entry["trace_id"]
                inv = self.get_invocation(trace_id)
                if inv:
                    invocations.append(inv)
        else:
            with self._lock:
                for _name, encoded in self._store.scan("invocations"):
                    try:
                        inv = decode_invocation(encoded)
                        self._disk_reads += 1
                    except Exception:
                        continue
                    if inv.session_id == session_id:
                        invocations.append(inv)
            invocations.sort(key=lambda inv: inv.started_at)
            invocations = invocations[-limit:]

        # Return most recent first
        return list(reversed(invocations))

    def store_metrics(self, metrics: Metrics) -> None:
        """Store performance metrics.

        Args:
            metrics: Metrics object to store

        Examples:
            >>> from clio_agent.arc.schema import (
            ...     InvocationStats, LatencyStats, UserSatisfactionStats
            ... )
            >>> metrics = Metrics(
            ...     agent_id="DataExpert",
            ...     tier=2,
            ...     period="2025-01",
            ...     computed_at="2025-01-31T23:59:59Z",
            ...     invocations=InvocationStats(100, 95, 5, 0, 0.95),
            ...     latency=LatencyStats(1500, 1200, 2500, 4000, 200, 8000),
            ...     user_satisfaction=UserSatisfactionStats(50, 45, 5, 0.90)
            ... )
            >>> arc.store_metrics(metrics)
        """
        with self._lock:
            agent_id = metrics.agent_id
            period = metrics.period
            cache_key = f"metrics:{agent_id}:{period}"

            # Store in cache
            self._cache.put(cache_key, metrics)

            # Persist to disk
            encoded = encode_metrics(metrics)
            self._store.put("metrics", f"{agent_id}_{period}", encoded)
            self._disk_writes += 1

    def get_metrics(self, agent_id: str, period: Optional[str] = None) -> Optional[Metrics]:
        """Get metrics for agent.

        Args:
            agent_id: Agent identifier
            period: Time period (e.g., "2025-01"). If None, returns latest.

        Returns:
            Metrics object if found, None otherwise

        Examples:
            >>> metrics = arc.get_metrics("DataExpert", period="2025-01")
            >>> if metrics:
            ...     print(f"Success rate: {metrics.invocations.success_rate:.2%}")
        """
        # If no period specified, find latest metrics file
        if period is None:
            with self._lock:
                matching = sorted(
                    self._store.scan("metrics", prefix=f"{agent_id}_"),
                    key=lambda kv: kv[0],
                )
                if not matching:
                    return None
                # Most recent = lexicographically latest name (agent_period)
                encoded = matching[-1][1]
                metrics = decode_metrics(encoded)
                self._disk_reads += 1

                # Cache it
                cache_key = f"metrics:{agent_id}:{metrics.period}"
                self._cache.put(cache_key, metrics)

                return metrics

        cache_key = f"metrics:{agent_id}:{period}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            raw = self._store.get("metrics", f"{agent_id}_{period}")
            if raw is None:
                return None

            metrics = decode_metrics(raw)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, metrics)

            return metrics

    def store_context(self, context: Context) -> None:
        """Store domain context.

        Args:
            context: Context object to store

        Examples:
            >>> ctx = Context(
            ...     domain="hdf5_optimization",
            ...     created_at="2025-01-09T14:30:00Z",
            ...     updated_at="2025-01-09T14:30:00Z"
            ... )
            >>> arc.store_context(ctx)
        """
        with self._lock:
            domain = context.domain
            cache_key = f"ctx:{domain}"

            # Store in cache
            self._cache.put(cache_key, context)

            # Persist to disk
            encoded = encode_context(context)
            self._store.put("context", domain, encoded)
            self._disk_writes += 1

    def get_context(self, domain: str) -> Optional[Context]:
        """Get context for domain.

        Args:
            domain: Domain identifier (e.g., "hdf5_optimization")

        Returns:
            Context object if found, None otherwise

        Examples:
            >>> ctx = arc.get_context("hdf5_optimization")
            >>> if ctx:
            ...     print(f"Cached tools: {len(ctx.cached_tool_results)}")
        """
        cache_key = f"ctx:{domain}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: load from disk
        with self._lock:
            encoded = self._store.get("context", domain)
            if encoded is None:
                return None

            context = decode_context(encoded)
            self._disk_reads += 1

            # Update cache
            self._cache.put(cache_key, context)

            return context

    def cache_tool_result(
        self,
        server_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any,
        ttl_seconds: int = 3600,
    ) -> None:
        """Cache tool result in ARC.

        Args:
            server_name: MCP server name (e.g., "hdf5")
            tool_name: Tool name (e.g., "analyze_file")
            arguments: Tool arguments dict
            result: Tool result to cache
            ttl_seconds: Cache TTL in seconds (default: 1 hour)

        Examples:
            >>> arc.cache_tool_result(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"path": "/data/experiment.h5"},
            ...     {"shape": [100, 200], "dtype": "float64"},
            ...     ttl_seconds=1800
            ... )
        """
        import hashlib
        import json

        # Create cache key from server + tool + args
        args_str = json.dumps(arguments, sort_keys=True)
        key_str = f"tool_{server_name}_{tool_name}_{args_str}"
        cache_key = hashlib.md5(key_str.encode()).hexdigest()

        # Store in cache with TTL
        self._cache.put(cache_key, result, ttl_seconds=ttl_seconds)

    def get_cached_tool_result(
        self, server_name: str, tool_name: str, arguments: Dict[str, Any]
    ) -> Optional[Any]:
        """Get cached tool result from ARC.

        Args:
            server_name: MCP server name
            tool_name: Tool name
            arguments: Tool arguments dict

        Returns:
            Cached result or None if not found/expired

        Examples:
            >>> result = arc.get_cached_tool_result(
            ...     "hdf5",
            ...     "analyze_file",
            ...     {"path": "/data/experiment.h5"}
            ... )
            >>> if result is not None:
            ...     print(f"Cache hit: {result}")
        """
        import hashlib
        import json

        # Create same cache key
        args_str = json.dumps(arguments, sort_keys=True)
        key_str = f"tool_{server_name}_{tool_name}_{args_str}"
        cache_key = hashlib.md5(key_str.encode()).hexdigest()

        return self._cache.get(cache_key)

    def get_tool_cache_stats(self) -> Dict[str, Any]:
        """Get tool cache statistics.

        Returns:
            Dict with tool cache hit rate and counts

        Examples:
            >>> stats = arc.get_tool_cache_stats()
            >>> print(f"Tool cache hit rate: {stats['tool_cache_hit_rate']:.2%}")
            >>> print(f"Target: {stats['target_hit_rate']:.2%}")
        """
        stats = self._cache.stats()
        return {
            "tool_cache_hit_rate": stats["hit_rate"],
            "tool_cache_hits": stats["hits"],
            "tool_cache_misses": stats["misses"],
            "tool_cache_size": stats["size"],
            "target_hit_rate": 0.50,  # >50% per PLAN.md
        }

    def query_metrics_by_time_range(self, start_ts: float, end_ts: float) -> List[Dict[str, Any]]:
        """Query metrics in time range using LSM tree.

        Provides fast time-range queries over invocation metrics
        stored in the LSM tree.

        Args:
            start_ts: Start timestamp (Unix timestamp)
            end_ts: End timestamp (Unix timestamp)

        Returns:
            List of metrics in range, sorted by timestamp

        Examples:
            >>> import time
            >>> start = time.time() - 3600  # Last hour
            >>> end = time.time()
            >>> metrics = arc.query_metrics_by_time_range(start, end)
            >>> for metric in metrics:
            ...     print(f"{metric['agent_id']}: {metric['duration_ms']}ms")
        """
        return self._lsm.range_scan(start_ts, end_ts)

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
                - conv_index_size: Conversation index entry count
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
                "conv_index_size": len(self._conv_index),
                "inv_index_size": len(self._inv_index),
            }

    # ---- Shared Context: Dataset Profiles ----

    def store_dataset_profile(self, profile: DatasetProfile) -> None:
        """Store a dataset profile for cross-expert collaboration.

        Stores in cache and persists to disk so other experts can
        retrieve the profile within the same session.

        Args:
            profile: DatasetProfile object to store

        Examples:
            >>> from clio_agent.arc.schema import DatasetProfile
            >>> profile = DatasetProfile(
            ...     session_id="session-1",
            ...     filepath="/data/test.parquet",
            ...     file_format="parquet",
            ...     created_by="data",
            ...     created_at=time.time(),
            ... )
            >>> arc.store_dataset_profile(profile)
        """
        import hashlib

        with self._lock:
            cache_key = f"profile:{profile.session_id}:{profile.filepath}"
            self._cache.put(cache_key, profile)

            # Persist to disk
            key_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
            encoded = encode_dataset_profile(profile)
            self._store.put("profiles", f"{profile.session_id}_{key_hash}", encoded)
            self._disk_writes += 1

    def get_dataset_profile(self, session_id: str, filepath: str) -> Optional[DatasetProfile]:
        """Retrieve a dataset profile by session and filepath.

        Checks cache first, then falls back to disk.

        Args:
            session_id: Session identifier
            filepath: Path to the analyzed file

        Returns:
            DatasetProfile if found, None otherwise

        Examples:
            >>> profile = arc.get_dataset_profile("session-1", "/data/test.parquet")
            >>> if profile:
            ...     print(f"Format: {profile.file_format}")
        """
        cache_key = f"profile:{session_id}:{filepath}"

        # Fast path: check cache
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Slow path: scan disk for this session
        with self._lock:
            for _name, encoded in self._store.scan("profiles", prefix=f"{session_id}_"):
                profile = decode_dataset_profile(encoded)
                self._disk_reads += 1
                if profile.filepath == filepath:
                    # Cache for future access
                    self._cache.put(cache_key, profile)
                    return profile
            return None

    def get_session_profiles(self, session_id: str) -> List[DatasetProfile]:
        """Get all dataset profiles for a session.

        Returns all profiles stored by any expert in the given session,
        enabling cross-expert collaboration.

        Args:
            session_id: Session identifier

        Returns:
            List of DatasetProfile objects for the session

        Examples:
            >>> profiles = arc.get_session_profiles("session-1")
            >>> for p in profiles:
            ...     print(f"{p.filepath}: {p.created_by}")
        """
        profiles: List[DatasetProfile] = []
        seen_filepaths: set[str] = set()

        # Check cache first for known keys
        with self._lock:
            if hasattr(self._cache, "_cache"):
                for key in list(self._cache._cache.keys()):
                    if key.startswith(f"profile:{session_id}:"):
                        val = self._cache._cache.get(key)
                        if val is not None and isinstance(val, DatasetProfile):
                            profiles.append(val)
                            seen_filepaths.add(val.filepath)

            # Also scan disk for profiles not in cache
            for _name, encoded in self._store.scan("profiles", prefix=f"{session_id}_"):
                profile = decode_dataset_profile(encoded)
                self._disk_reads += 1
                if profile.filepath not in seen_filepaths:
                    profiles.append(profile)
                    seen_filepaths.add(profile.filepath)
                    # Cache for future access
                    cache_key = f"profile:{session_id}:{profile.filepath}"
                    self._cache.put(cache_key, profile)

        return profiles

    # ---- Shared Context: Procedural Memory ----

    def store_procedural_memory(self, memory: ProceduralMemory) -> None:
        """Store a procedural memory entry (what worked/failed).

        Persists success/failure/optimization patterns so experts can
        learn from past attempts.

        Args:
            memory: ProceduralMemory object to store

        Examples:
            >>> from clio_agent.arc.schema import ProceduralMemory
            >>> mem = ProceduralMemory(
            ...     session_id="session-1",
            ...     expert_id="data",
            ...     pattern_type="success",
            ...     description="gzip-6 worked well",
            ...     context={"file_type": "hdf5"},
            ...     outcome="3x compression",
            ... )
            >>> arc.store_procedural_memory(mem)
        """
        import hashlib

        with self._lock:
            cache_key = f"proc:{memory.session_id}:{memory.expert_id}:{memory.learned_at}"
            self._cache.put(cache_key, memory)

            # Persist to disk
            key_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
            encoded = encode_procedural_memory(memory)
            self._store.put("procedural", f"{memory.expert_id}_{key_hash}", encoded)
            self._disk_writes += 1

    def get_procedural_memories(
        self,
        session_id: str,
        expert_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[ProceduralMemory]:
        """Get procedural memories for a session, optionally filtered by expert.

        Returns most recent memories first.

        Args:
            session_id: Session identifier
            expert_id: Optional expert filter (e.g., "data", "analysis")
            limit: Maximum number of memories to return (default: 10)

        Returns:
            List of ProceduralMemory objects, most recent first

        Examples:
            >>> memories = arc.get_procedural_memories("session-1", expert_id="data")
            >>> for m in memories:
            ...     print(f"[{m.pattern_type}] {m.description}")
        """
        memories: List[ProceduralMemory] = []
        seen_keys: set[str] = set()

        with self._lock:
            # Scan cache
            prefix = f"proc:{session_id}:"
            if hasattr(self._cache, "_cache"):
                for key in list(self._cache._cache.keys()):
                    if key.startswith(prefix):
                        val = self._cache._cache.get(key)
                        if val is not None and isinstance(val, ProceduralMemory):
                            if expert_id is None or val.expert_id == expert_id:
                                memories.append(val)
                                seen_keys.add(key)

            # Scan disk
            scan_prefix = f"{expert_id}_" if expert_id else ""

            for _name, encoded in self._store.scan("procedural", prefix=scan_prefix):
                mem = decode_procedural_memory(encoded)
                self._disk_reads += 1

                if mem.session_id != session_id:
                    continue
                if expert_id is not None and mem.expert_id != expert_id:
                    continue

                cache_key = f"proc:{mem.session_id}:{mem.expert_id}:{mem.learned_at}"
                if cache_key not in seen_keys:
                    memories.append(mem)
                    seen_keys.add(cache_key)
                    self._cache.put(cache_key, mem)

        # Sort by learned_at descending (most recent first)
        memories.sort(key=lambda m: m.learned_at, reverse=True)
        return memories[:limit]

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

        with self._lock:
            for _name, encoded in self._store.scan("invocations"):
                try:
                    inv = decode_invocation(encoded)
                    self._disk_reads += 1

                    if inv.agent_id != agent_id:
                        continue
                    if status is not None and inv.status != status:
                        continue

                    invocations.append(inv)
                except Exception:
                    continue

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
        with self._lock:
            for _name, encoded in self._store.scan("invocations"):
                try:
                    invocations.append(decode_invocation(encoded))
                    self._disk_reads += 1
                except Exception:
                    continue
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
        with self._lock:
            cache_key = f"variant:{record.variant_id}"
            self._cache.put(cache_key, record)

            encoded = encode_variant_record(record)
            self._store.put("variants", record.variant_id, encoded)
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

        with self._lock:
            for _name, encoded in self._store.scan("variants"):
                try:
                    record = decode_variant_record(encoded)
                    self._disk_reads += 1

                    if record.agent_id == agent_id:
                        records.append(record)
                except Exception:
                    continue

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

        Each step is guarded so an observability record can never break a turn.
        """
        try:
            self.on_semantic_event(event)
        except Exception:  # noqa: BLE001 - persistence is best-effort; never break a turn
            pass
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
        dict from the SemanticEvent's fields (large text capped), correlated by the
        event's trajectory span ids, and appends ONE segment under the reserved
        ``_events`` scope. Never rendered into a prompt (own scope + non-working-set
        kind)."""
        etype = str(getattr(event, "event_type", "") or "")
        sid = str(getattr(event, "session_id", "") or "")
        reason = self._event_skip_reason(etype, sid)
        if reason:
            if trace.HF_ON:
                trace.hot("ARC-EVENTS", "skip %s etype=%r sid=%r", reason, etype, sid)
            return
        if trace.HF_ON:
            trace.hot("ARC-EVENTS", "persist etype=%r sid=%r", etype, sid)
        self._append_event_segment(event, etype, sid)

    def _append_event_segment(self, event: Any, etype: str, sid: str) -> None:
        """Build (via the shared :func:`~clio_agent.arc.live.build_event_content`) +
        append the lean ``semantic_event`` segment. ONE builder is shared with the
        standalone observer so the persisted log is identical regardless of path."""
        content = build_event_content(event)
        if content is None:
            return
        self._segments.append(
            sid,
            EVENTS_SCOPE,
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

    # ---- Live context plane (the segment store the ReAct loop reads from) ----

    def set_segment_op_logger(self, op_logger: "OpLogger | None") -> None:
        """Inject the durable-Trace op logger into the segment store.

        Called by the gact app once both the app handle and ARC exist, so each
        applied context op is mirrored to the Trace. Keeps ``arc/`` free of any
        ``gact/`` import.
        """
        self._segments.set_op_logger(op_logger)

    def append_segment(
        self,
        session_id: str,
        scope: str,
        kind: str,
        content: Dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Append one segment to a scope's live context (append = insert at end).

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional
        trajectory-correlation span ids stamped on the new segment (default ``""``)."""
        return self._segments.append(
            session_id,
            scope,
            cast(SegmentKind, kind),
            content,
            step=step,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def insert_segment(
        self,
        session_id: str,
        scope: str,
        position: int,
        kind: str,
        content: Dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Insert one segment at a render position in a scope's live context.

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional
        correlation span ids stamped on the new segment (default ``""``)."""
        return self._segments.insert(
            session_id,
            scope,
            position,
            cast(SegmentKind, kind),
            content,
            step=step,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def delete_segments(self, session_id: str, scope: str, ids: List[str]) -> int:
        """Tombstone segments by id (skipped by render, kept for replay)."""
        return self._segments.delete(session_id, scope, ids)

    def summarize_segments(
        self,
        session_id: str,
        scope: str,
        ids: List[str],
        summary_content: Dict[str, Any],
        *,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Replace a range of segments with one summary (= context-compaction over all).

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional correlation
        span ids stamped on the summary segment (default ``""``)."""
        return self._segments.summarize(
            session_id,
            scope,
            ids,
            summary_content,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def replace_segment(
        self,
        session_id: str,
        scope: str,
        target_id: str,
        content: Dict[str, Any],
        *,
        kind: Optional[str] = None,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Replace a live segment's content in place (1:1 supersede at the same render
        slot; the original is tombstoned + recoverable as-of-T).

        ``kind`` defaults to the original's kind; the correlation span ids default to
        the ORIGINAL's (a pure content edit stays in the same turn/expert/run). Returns
        the new Segment, or ``None`` if ``target_id`` matched no live segment."""
        return self._segments.replace(
            session_id,
            scope,
            target_id,
            content,
            kind=cast(Optional[SegmentKind], kind),
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def apply_segment_op(self, op: str, session_id: str, scope: str, **kwargs: Any) -> Any:
        """Stable dispatch over the five ops — the KV-backend swap seam."""
        return self._segments.apply(op, session_id, scope, **kwargs)

    def render_segments(self, session_id: str, scope: str, *, as_of: Optional[int] = None) -> Any:
        """Ordered LIVE segments for a scope (the decisive read; as-of-T optional)."""
        return self._segments.render(session_id, scope, as_of=as_of)

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: Optional[int] = None
    ) -> Any:
        """Ordered LIVE WORKING-SET segments — the kinds the prompt + the compaction/
        reset paths operate on (excludes ``lm_io`` / ``extract_io`` / ``answer``). The
        target of the per-turn reset and auto-compaction, NOT a new prompt source; see
        :meth:`SegmentStore.render_working_set`."""
        return self._segments.render_working_set(session_id, scope, as_of=as_of)

    def render_segments_keys(
        self, session_id: str, scope: str, *, as_of: Optional[int] = None
    ) -> Dict[str, Any]:
        """The live segments projected into dspy's trajectory dict (what the
        ``_format_trajectory`` override reads)."""
        return self._segments.render_keys(session_id, scope, as_of=as_of)

    def render_segment_text(
        self, session_id: str, scope: str, *, as_of: Optional[int] = None
    ) -> str:
        """The live segments flattened to text (inspection / byte-equality)."""
        return self._segments.render_text(session_id, scope, as_of=as_of)

    def segment_tokens_by_kind(self, session_id: str, scope: str) -> Dict[str, int]:
        """Per-kind token attribution for a scope's live segments (compaction targeting)."""
        return self._segments.tokens_by_kind(session_id, scope)

    def list_segment_scopes(self, session_id: str, scope_prefix: str = "") -> List[str]:
        """Scopes that have context in this session (for discovery / a scope picker)."""
        return self._segments.scan_scopes(session_id, scope_prefix)

    def search_segment_scopes(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> List[Any]:
        """Semantic discovery: rank a session's scopes by content relevance to
        ``query_text`` — "which expert/scope knows about X" (BM25 on CTE)."""
        return self._segments.search_scopes(session_id, query_text, scope_prefix=scope_prefix, k=k)

    def segment_search_is_semantic(self) -> bool:
        """Whether scope search uses real BM25 (CTE backend) vs the naive fallback."""
        return self._segments.supports_search()

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
            evicted_cache += self._cache.invalidate_prefix(f"profile:{session_id}:")
            evicted_cache += self._cache.invalidate_prefix(f"proc:{session_id}:")

            evicted_index = self._conv_index.delete_session(session_id)
            evicted_index += self._inv_index.delete_session(session_id)

        # Outside the lock: LiveRuntimeContext and SegmentStore have their own locks.
        # The observer's release ERASES the reserved ``_events`` scope (the single
        # persisted raw semantic-event stream it projects over) so an idle server
        # returns to baseline; the durable trace keeps the full history.
        live = self._live.release(session_id)
        segments = self._segments.release(session_id)
        return {
            "cache": evicted_cache,
            "index": evicted_index,
            "live": live,
            "segments": segments,
        }

    def flush_and_release(self) -> None:
        """Release ALL in-memory state to return to baseline (tests/memprof).

        Flushes the LSM MemTable to disk, clears the cache, and clears both
        indexes. The persistent store is untouched and fully re-loadable; this
        only drops the hot/heap copies so memory profiling sees a clean floor.

        Examples:
            >>> arc.flush_and_release()
            >>> arc.get_cache_stats()['size']
            0
        """
        with self._lock:
            self._lsm.flush()
            self._cache.clear()
            self._conv_index.clear()
            self._inv_index.clear()
        # The observer's clear ERASES the reserved ``_events`` scope across every
        # session (the single persisted semantic-event stream it projects over); the
        # durable trace retains the full history, so an idle server returns to baseline.
        self._live.clear()
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
        with self._lock:
            # Clear cache
            self._cache.clear()

            # Clear indexes
            self._conv_index.clear()
            self._inv_index.clear()

            # Clear disk storage (wipes the "segments" kind too, since it's in ARC_KINDS)
            self._store.clear()

            # Drop the in-memory segment plane (store already cleared above)
            self._segments.clear()

            # Reset counters
            self._disk_reads = 0
            self._disk_writes = 0

    def __del__(self) -> None:
        """Cleanup LSM tree on delete."""
        lsm = getattr(self, "_lsm", None)
        if lsm is None:
            return

        try:
            lsm.close()
        except Exception:
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
        import time

        return time.time()
