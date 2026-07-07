"""SegmentStore: the ARC live context plane.

An ordered, scoped, mutable sequence of :class:`~clio_agent.arc.schema.Segment`s
that the gact ReAct loop reads from on every iteration. The loop *writes* one
segment per produced piece (thought / tool_call / observation) and *reads* the
prompt back by rendering the live ordered set (see ``render_keys``). The context
operations — ``append`` / ``insert`` / ``delete`` / ``summarize`` / ``replace`` —
mutate segments between renders, so an out-of-band edit changes the *next*
prompt. That is the whole point of the live plane.

Design (docs/design/arc-live-context-plane.md, docs/design/implementation-spec.md):
    * Composed by ``ARCMemory`` (sibling of ``LiveRuntimeContext``); persists
      through the injected ``ARCStore`` as one record per ``(session_id, scope)``
      — ``render`` is the every-iteration hot path, so the whole scope is batched
      into a single get/decode.
    * ``order`` is a gap-allocated float: a mid-sequence ``insert`` picks a
      midpoint and never renumbers later segments.
    * ``delete`` tombstones (never erases) so segments survive for Trace
      reconstruction and as-of-T reads before their tombstoning ``logical_time``.
    * Every applied op is logged to the durable Trace via an injected
      ``op_logger`` (kept injected so ``arc/`` never imports ``gact/``); ARC is
      replayable from those ``arc.op`` events (see ``arc/replay.py``).

The KV-surgery backend (future work) swaps in behind the same ``apply`` interface
— the naive path here re-renders and lets the model recompute from the edit point.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any, Callable, Optional

import msgspec
from sortedcontainers import SortedDict

from clio_agent.arc.schema import (
    WORKING_SET_KINDS,
    Segment,
    SegmentKind,
    decode_segments,
    encode_segments,
    segment_text,
)
from clio_agent.arc.storage import ARCStore
from clio_agent.runtime import trace

logger = logging.getLogger(__name__)


def _encode_safe(value: Any) -> Any:
    """Recursively coerce ``value`` to a plain msgpack/JSON-native form so ARC can
    ALWAYS persist it, regardless of which emit site produced it.

    ARC's durable record (``Segment`` content) is encoded with msgspec/msgpack (strict:
    it throws on any type it doesn't natively understand). Emit sites can put arbitrary
    objects in a segment's ``content`` — a tool_call's nested ``args``, an observation
    value, an event's ``payload`` / ``actor`` / ``provider`` (e.g. litellm/openai usage
    objects ``Usage`` / ``CompletionTokensDetailsWrapper`` / ``PromptTokensDetailsWrapper``,
    pydantic models, dataclasses, sets/tuples). Without coercion the encode throws and the
    write is DROPPED (or, worse, durably wedges the scope). This makes the persisted
    content encode-safe for ANY value, not a one-off for a single litellm type:

    * native scalars (``str`` / ``int`` / ``float`` / ``bool`` / ``None``) pass through;
    * ``dict`` -> recurse over values (keys coerced to ``str``);
    * ``list`` / ``tuple`` / ``set`` -> recurse into a ``list``;
    * pydantic ``BaseModel`` (``model_dump`` / legacy ``dict``) -> coerce its dict;
    * dataclass instance -> coerce ``dataclasses.asdict``;
    * objects exposing ``_asdict`` (namedtuple-ish) or ``model_dump`` -> coerce that;
    * objects with ``__dict__`` -> coerce their attribute dict;
    * anything else (or a coercion that itself raises) -> ``str(value)``.

    The result round-trips through encode/decode and stays lean (no live objects, just
    plain containers/scalars). This lives in ``segments.py`` (the lowest write chokepoint)
    and is re-exported by ``arc.live`` for back-compat with the event path.
    """
    # Fast path: msgpack-native scalars.
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _encode_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode_safe(v) for v in value]
    # Pydantic BaseModel (v2 model_dump / v1 dict) — duck-typed so arc/ never imports it.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _encode_safe(dump())
        except Exception:  # noqa: BLE001,S110 - fall through to the next coercion strategy
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _encode_safe(dataclasses.asdict(value))
        except Exception:  # noqa: BLE001,S110 - fall through to the next coercion strategy
            pass
    asdict = getattr(value, "_asdict", None)
    if callable(asdict):
        try:
            return _encode_safe(asdict())
        except Exception:  # noqa: BLE001,S110 - fall through to the next coercion strategy
            pass
    obj_dict = getattr(value, "__dict__", None)
    if isinstance(obj_dict, dict) and obj_dict:
        return _encode_safe(obj_dict)
    # Last resort: a stable string form (never throws on a foreign object).
    return str(value)


def _coerce_content(content: dict[str, Any]) -> dict[str, Any]:
    """Make any segment ``content`` dict encode-safe at the ONE write chokepoint.

    Every Segment-creating write path (append/insert/summarize/replace, AND the
    observer's ``_events`` log) routes its raw ``content`` through here BEFORE the
    Segment is constructed, so ARC's strict msgspec/msgpack encode can NEVER throw on
    an exotic value an emit site happened to put in the content (a tool_call's nested
    ``args``, an observation value, a litellm usage object, a pydantic model, a
    dataclass, a set/tuple). :func:`clio_agent.arc.live._encode_safe` recursively
    coerces any non-native value to a plain serializable form and is itself guarded so
    it can never raise; on a ``dict`` it returns a ``dict``, so the content shape is
    preserved. This is the single guarantee that no segment write can durably wedge a
    scope's persistence with un-encodable content.
    """
    coerced = _encode_safe(content)
    # _encode_safe(dict) -> dict; the isinstance guard keeps the declared dict contract
    # even for a degenerate non-dict input (which the write surface never produces).
    return coerced if isinstance(coerced, dict) else {"value": coerced}


# op_logger(op, session_id, scope, *, logical_time, step, position,
#           segments_written, segments_tombstoned, derived_from) -> event dict | None
OpLogger = Callable[..., Optional[dict[str, Any]]]

_SCOPE_SEP = "__"  # session_id <SEP> scope, in the store record name
_SLASH_SUB = "~"  # scope's '/' replaced so the record name is one path segment


def segments_to_keys(segments: list[Segment]) -> dict[str, Any]:
    """Project an ordered list of LIVE segments into dspy's trajectory dict
    (``thought_{i}`` / ``tool_name_{i}`` / ``tool_args_{i}`` / ``observation_{i}``).

    ``i`` is the RENDER POSITION (not ``Segment.step``), recomputed so the dict is
    gapless after deletes/summaries (stock dspy never has index gaps). A ``thought``
    opens a new iteration; ``tool_call`` / ``observation`` attach to the current
    one; a ``summary`` renders as its own ``observation_{i}``. ``system`` / ``user``
    / ``tool_def`` are dspy's own framing — never in the trajectory dict.

    Pure function shared by ``SegmentStore.render_keys`` and ``arc.replay`` so the
    live store and a Trace-replay produce byte-identical trajectories.

    A new iteration starts on a ``thought``/``summary`` OR whenever the slot a
    segment needs is already filled in the current iteration — so consecutive
    observations (from injection or edits) never overwrite each other, while the
    normal ``thought -> tool_call -> observation`` flow renders exactly as stock dspy.
    """
    keys: dict[str, Any] = {}
    idx = -1
    filled: set[str] = set()
    for seg in segments:
        kind = seg.kind
        if kind == "thought":
            idx += 1
            filled = {"thought"}
            keys[f"thought_{idx}"] = seg.content.get("text", "")
        elif kind == "tool_call":
            if idx < 0 or "tool" in filled:
                idx += 1
                filled = set()
            filled.add("tool")
            keys[f"tool_name_{idx}"] = seg.content.get("name", "")
            keys[f"tool_args_{idx}"] = seg.content.get("args", {})
        elif kind in ("observation", "summary"):
            if idx < 0 or "obs" in filled:
                idx += 1
                filled = set()
            filled.add("obs")
            keys[f"observation_{idx}"] = seg.content.get("text", "")
        # system / user / tool_def: not part of the trajectory dict
    return keys


class SegmentIndex:
    """Per-scope B-tree-style locator: ``(session, scope) -> SortedDict[logical_time
    -> segment_id]`` so a scope's segments can be LOCATED in O(log N) by creation
    ``logical_time`` (the immutable, store-unique creation clock).

    This is a pure ACCELERATION structure built in parallel with the in-memory scope
    lists. It is keyed by the creation ``logical_time`` (unique per segment), so the
    id set it yields for a scope is exactly the set the scan over the scope list yields
    — a property the parallel-consistency tests assert across the stress corpus. The
    render/op paths still read via the scan; the index is not yet on the read path.

    Thread-safety: all mutators/readers are called under the SegmentStore lock, so the
    index itself takes no lock.
    """

    def __init__(self) -> None:
        self._by_scope: dict[tuple[str, str], SortedDict] = {}

    def _scope_map(self, session_id: str, scope: str) -> SortedDict:
        key = (session_id, scope)
        sd = self._by_scope.get(key)
        if sd is None:
            sd = SortedDict()
            self._by_scope[key] = sd
        return sd

    def add(self, session_id: str, scope: str, seg: Segment) -> None:
        """Index a segment by its creation ``logical_time`` (unique per segment)."""
        self._scope_map(session_id, scope)[seg.logical_time] = seg.id

    def bulk_load(self, session_id: str, scope: str, segs: list[Segment]) -> None:
        """Index a whole freshly-loaded scope at once (cold-load path)."""
        sd = self._scope_map(session_id, scope)
        for s in segs:
            sd[s.logical_time] = s.id

    def locate_ids(
        self,
        session_id: str,
        scope: str,
        *,
        lt_min: int | None = None,
        lt_max: int | None = None,
    ) -> list[str]:
        """Locate the ids in a scope whose creation ``logical_time`` falls in the
        inclusive ``[lt_min, lt_max]`` window (``None`` = unbounded), in logical-time
        order. The ``irange`` is the O(log N) B-tree slice; an open window returns the
        whole scope (still in clock order)."""
        sd = self._by_scope.get((session_id, scope))
        if sd is None:
            return []
        return [sd[lt] for lt in sd.irange(lt_min, lt_max)]

    def remove(self, session_id: str, scope: str, seg: Segment) -> None:
        """Forget a single segment (mirrors a non-poisoning drop of an un-encodable
        segment from the scan, so the locator stays consistent with the scope list)."""
        sd = self._by_scope.get((session_id, scope))
        if sd is not None and sd.get(seg.logical_time) == seg.id:
            del sd[seg.logical_time]

    def drop_session(self, session_id: str) -> None:
        """Forget a session's scopes (mirrors SegmentStore.release)."""
        for key in [k for k in self._by_scope if k[0] == session_id]:
            self._by_scope.pop(key, None)

    def drop_scope(self, session_id: str, scope: str) -> None:
        """Forget a single scope's locator (mirrors SegmentStore.drop_scope)."""
        self._by_scope.pop((session_id, scope), None)

    def clear(self) -> None:
        self._by_scope.clear()


class SegmentStore:
    """Ordered, scoped, mutable live-context store. Thread-safe."""

    def __init__(
        self,
        store: ARCStore,
        op_logger: OpLogger | None = None,
        *,
        search_indexed: Callable[[str], bool] | None = None,
    ) -> None:
        """Initialize the store.

        Args:
            store: Durable backend (one record per ``(session_id, scope)``).
            op_logger: Optional callable that logs an applied op to the durable
                Trace and returns the emitted event dict (with ``"event_id"``).
                Injected so ``arc/`` never depends on ``gact/``; ``None`` (unit
                tests / memory-only) means ops still work, just unlogged.
            search_indexed: Optional predicate ``scope -> bool`` deciding whether a
                scope's persist writes the plain-text search companion. ``None``
                (default) indexes every scope (historical behavior). ARCMemory injects
                a predicate that returns ``False`` for the reserved ``_events`` chunk
                family so the semantic-event log never pollutes scope search — a
                deliberate, scope-level exclusion (not per-op) that holds for EVERY
                write to those scopes regardless of the op path.
        """
        self._store = store
        self._op_logger = op_logger
        self._search_indexed = search_indexed
        # PER-SCOPE locking: one lock per (session_id, scope) so ops on different
        # scopes (overlapping experts) run concurrently instead of serializing on a
        # single store-wide lock held through disk I/O. The lock-registry itself is
        # guarded by a tiny ``_registry_lock`` (held only to look up / create a scope
        # lock and for store-wide structural ops, never through disk I/O). LOCK ORDER
        # is fixed to avoid deadlock: registry -> scope -> clock (never reverse).
        self._registry_lock = threading.RLock()
        self._scope_locks: dict[tuple[str, str], threading.RLock] = {}
        # The shared logical-time clock gets its OWN tiny lock so the brief tick is
        # serialized across all scopes without serializing the scopes themselves.
        self._clock_lock = threading.Lock()
        # In-memory working copy: (session_id, scope) -> list[Segment] (all, incl
        # tombstoned), kept loaded write-through. Scopes loaded lazily once. Each
        # scope's entry is touched only under that scope's lock.
        self._scopes: dict[tuple[str, str], list[Segment]] = {}
        self._loaded: set[tuple[str, str]] = set()
        # Per-scope B-tree locator built in parallel with the scope lists (additive;
        # NOT yet on the render/op read path — those still scan). Lets a scope's
        # segments be located in O(log N) by creation logical_time.
        self._index = SegmentIndex()
        # Store-wide monotonic logical clock, recovered past the persisted max.
        self._next_lt = 1

    # ---- record naming -------------------------------------------------

    def set_op_logger(self, op_logger: OpLogger | None) -> None:
        """Inject (or replace) the durable-Trace op logger after construction.

        Used by the gact app to wire ``_emit_arc_op`` once both the app handle and
        ARC exist (avoids the app/ARC construction cycle). Keeps ``arc/`` free of any
        ``gact/`` import.
        """
        self._op_logger = op_logger
        logger.debug("segments: op_logger %s", "attached" if op_logger else "cleared")

    @staticmethod
    def _record_name(session_id: str, scope: str) -> str:
        return f"{session_id}{_SCOPE_SEP}{scope.replace('/', _SLASH_SUB)}"

    def _lock_for(self, session_id: str, scope: str) -> threading.RLock:
        """Return the per-scope lock for ``(session_id, scope)``, creating it once.
        The registry lock is held only for the brief lookup/create — never through any
        op body or disk I/O."""
        key = (session_id, scope)
        with self._registry_lock:
            lk = self._scope_locks.get(key)
            if lk is None:
                lk = threading.RLock()
                self._scope_locks[key] = lk
            return lk

    # ---- load / persist ------------------------------------------------

    def _segs(self, session_id: str, scope: str) -> list[Segment]:
        """Return the in-memory segment list for a scope, loading it once.

        Always called under this scope's per-scope lock, so the scope's own entry in
        ``_scopes``/``_loaded``/``_index`` is never concurrently mutated. The shared
        clock recovery below is done under the dedicated clock lock so it stays
        serialized across scopes (lock order scope -> clock is honored)."""
        key = (session_id, scope)
        if key not in self._loaded:
            raw = self._store.get("segments", self._record_name(session_id, scope))
            segs = decode_segments(raw) if raw else []
            self._scopes[key] = segs
            self._loaded.add(key)
            self._index.bulk_load(session_id, scope, segs)  # parallel locator
            # recover the monotonic clock past anything persisted (shared -> clock lock)
            with self._clock_lock:
                for s in segs:
                    if s.logical_time >= self._next_lt:
                        self._next_lt = s.logical_time + 1
            logger.debug(
                "segments: cold-load session=%s scope=%s loaded=%d next_lt=%d",
                session_id,
                scope,
                len(segs),
                self._next_lt,
            )
        return self._scopes[key]

    def _persist(
        self, session_id: str, scope: str, *, just_written: list[Segment] | None = None
    ) -> None:
        """Encode + put the whole scope record. NON-POISONING: a single segment that
        still fails to encode (despite the :func:`_coerce_content` chokepoint) is REMOVED
        from the in-memory list and logged via ``runtime.trace`` (never silently), so it
        can NEVER durably wedge the scope's future persists — one bad write must not break
        the whole scope. ``just_written`` is the segment(s) the current op produced; they
        are the prime suspects and are dropped first."""
        segs = self._scopes[(session_id, scope)]
        try:
            self._put_scope(session_id, scope, segs)
            return
        except Exception:  # noqa: BLE001,S110 - encode/put failed; isolate the offender below
            pass
        # Drop the just-written segment(s) first (the most likely offender), then any
        # other segment that fails to encode in isolation, so the rest of the scope
        # persists cleanly and never re-throws on the next op.
        suspects = list(just_written or [])
        dropped: list[str] = []
        for seg in suspects:
            if seg in segs and not self._segment_encodes(seg):
                segs.remove(seg)
                self._index_remove(session_id, scope, seg)
                dropped.append(seg.id)
        try:
            self._put_scope(session_id, scope, segs)
        except Exception:  # noqa: BLE001 - a non-just-written segment is also bad; isolate it
            survivors = [s for s in segs if self._segment_encodes(s)]
            for seg in segs:
                if seg not in survivors:
                    self._index_remove(session_id, scope, seg)
                    dropped.append(seg.id)
            segs[:] = survivors
            self._put_scope(session_id, scope, segs)
        if dropped:
            trace.event(
                "SEGMENT-DROP",
                "scope=%s session=%s dropped=%d ids=%s (un-encodable content removed; "
                "scope persisted without it, no durable wedge)",
                scope,
                session_id,
                len(dropped),
                dropped,
            )

    def _put_scope(self, session_id: str, scope: str, segs: list[Segment]) -> None:
        """Encode the scope's segments and put the record (with the live search_text
        companion). Raises if ``encode_segments`` / ``store.put`` rejects any segment."""
        # search_text: the live render flattened to plain text, so semantic discovery
        # (Thread D) can find this scope by content. Empty -> None drops the companion.
        # A scope the ``search_indexed`` predicate excludes (the reserved ``_events``
        # chunk family) NEVER writes the companion, so the semantic-event log can never
        # surface in scope search.
        if self._search_indexed is None or self._search_indexed(scope):
            live_text = "\n".join(segment_text(s) for s in self._live_sorted(segs))
            search_text = live_text or None
        else:
            search_text = None
        self._store.put(
            "segments",
            self._record_name(session_id, scope),
            encode_segments(segs),
            search_text=search_text,
        )

    @staticmethod
    def _segment_encodes(seg: Segment) -> bool:
        """Whether a single segment survives the strict msgpack encode in isolation."""
        try:
            encode_segments([seg])
            return True
        except Exception:  # noqa: BLE001 - this segment is the un-encodable offender
            return False

    def _index_remove(self, session_id: str, scope: str, seg: Segment) -> None:
        """Drop a dropped segment from the per-scope locator so the index stays in sync
        with the scan (the parallel-consistency invariant)."""
        self._index.remove(session_id, scope, seg)

    def _new_lt(self) -> int:
        """Issue the next monotonic logical tick. Guarded by its OWN tiny lock so the
        shared clock is serialized across ALL scopes (each scope holds only its own
        lock); the critical section is a bare increment, never disk I/O. Lock order is
        scope -> clock, so callers must already hold a scope lock — never the reverse."""
        with self._clock_lock:
            lt = self._next_lt
            self._next_lt += 1
            return lt

    @staticmethod
    def _live_sorted(segs: list[Segment]) -> list[Segment]:
        """Live segments (not tombstoned) in render order: (order, logical_time)."""
        return sorted(
            (s for s in segs if s.status == "live"),
            key=lambda s: (s.order, s.logical_time),
        )

    # ---- the four ops (write surface; each logs to the Trace) ----------

    def append(
        self,
        session_id: str,
        scope: str,
        kind: SegmentKind,
        content: dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        derived_from: list[str] | None = None,
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment:
        """append = insert(end). ``order`` = max(order)+1; cheap, never breaks the
        cached prefix. Returns the new Segment. The ``turn_id`` / ``expert_span_id``
        / ``run_span_id`` are optional trajectory-correlation span ids (default ``""``)
        stamped on the new segment so every write in a turn is correlated."""
        content = _coerce_content(content)
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            order = (max((s.order for s in segs), default=0.0)) + 1.0
            seg = Segment(
                scope=scope,
                kind=kind,
                content=content,
                session_id=session_id,
                step=step,
                order=order,
                logical_time=self._new_lt(),
                token_count=token_count,
                derived_from=list(derived_from or []),
                trace_ref=trace_ref,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            segs.append(seg)
            logger.debug(
                "segments: append scope=%s kind=%s step=%d lt=%d order=%.4f tokens=%d id=%s",
                scope,
                kind,
                step,
                seg.logical_time,
                seg.order,
                token_count,
                seg.id,
            )
            self._finish_write(
                session_id,
                scope,
                "append",
                written=[seg],
                step=step,
                logical_time=seg.logical_time,
            )
            return seg

    def insert(
        self,
        session_id: str,
        scope: str,
        position: int,
        kind: SegmentKind,
        content: dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        derived_from: list[str] | None = None,
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment:
        """Insert at render ``position`` (0-based over LIVE segments). ``order`` =
        midpoint of neighbours (gap allocation, no renumber). Breaks the prefix
        from here forward. ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are
        optional correlation span ids stamped on the new segment."""
        content = _coerce_content(content)
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            live = self._live_sorted(segs)
            order = self._order_for_position(segs, live, position)
            seg = Segment(
                scope=scope,
                kind=kind,
                content=content,
                session_id=session_id,
                step=step,
                order=order,
                logical_time=self._new_lt(),
                token_count=token_count,
                derived_from=list(derived_from or []),
                trace_ref=trace_ref,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            segs.append(seg)
            logger.debug(
                "segments: insert scope=%s pos=%d kind=%s lt=%d order=%.4f id=%s",
                scope,
                position,
                kind,
                seg.logical_time,
                seg.order,
                seg.id,
            )
            self._finish_write(
                session_id,
                scope,
                "insert",
                written=[seg],
                step=step,
                position=position,
                logical_time=seg.logical_time,
            )
            return seg

    @staticmethod
    def _order_for_position(segs: list[Segment], live: list[Segment], position: int) -> float:
        """Gap-allocated float order so a mid-insert never renumbers neighbours."""
        if position <= 0:
            lo = min((s.order for s in segs), default=1.0)
            return lo - 1.0 if live else 1.0
        if position >= len(live):
            return max((s.order for s in segs), default=0.0) + 1.0
        before = live[position - 1].order
        after = live[position].order
        return (before + after) / 2.0

    def delete(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Tombstone live segments by id (render skips them). Tombstone-not-erase
        so the segment survives for Trace reconstruction / as-of-T. Returns the
        number actually tombstoned."""
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            target = set(ids)
            tombstoned: list[str] = []
            op_lt = 0
            for s in segs:
                if s.id in target and s.status == "live":
                    s.status = "tombstoned"
                    op_lt = self._new_lt()  # tombstoning time (as-of-T); keep creation lt
                    s.tombstoned_at = op_lt
                    tombstoned.append(s.id)
            if tombstoned:
                logger.debug(
                    "segments: delete scope=%s tombstoned=%d/%d lt=%d ids=%s",
                    scope,
                    len(tombstoned),
                    len(ids),
                    op_lt,
                    tombstoned,
                )
                self._finish_write(
                    session_id, scope, "delete", tombstoned=tombstoned, logical_time=op_lt
                )
            else:
                logger.debug("segments: delete scope=%s matched no live ids=%s", scope, ids)
            return len(tombstoned)

    def summarize(
        self,
        session_id: str,
        scope: str,
        ids: list[str],
        summary_content: dict[str, Any],
        *,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment:
        """summarize = delete(ids) + insert(summary at the first replaced position),
        ATOMIC under the lock. The new Segment is ``kind="summary"`` with
        ``derived_from=ids``. The caller produces ``summary_content`` (the LLM
        call). context-compaction = ``summarize(all live ids)``. ``turn_id`` /
        ``expert_span_id`` / ``run_span_id`` are optional correlation span ids
        stamped on the summary segment."""
        summary_content = _coerce_content(summary_content)
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            target = set(ids)
            replaced = [s for s in segs if s.id in target and s.status == "live"]
            # Summary takes the position (order) of the first replaced segment so it
            # renders where the range was; its step is the min replaced step.
            if replaced:
                first = min(replaced, key=lambda s: (s.order, s.logical_time))
                order = first.order
                step = min((s.step for s in replaced), default=-1)
            else:
                order = (max((s.order for s in segs), default=0.0)) + 1.0
                step = -1
            summary_lt = self._new_lt()
            tombstoned: list[str] = []
            for s in replaced:
                s.status = "tombstoned"
                s.tombstoned_at = summary_lt  # replaced exactly when the summary appears
                tombstoned.append(s.id)
            summary = Segment(
                scope=scope,
                kind="summary",
                content=summary_content,
                session_id=session_id,
                step=step,
                order=order,
                logical_time=summary_lt,
                token_count=token_count,
                derived_from=list(ids),
                trace_ref=trace_ref,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            segs.append(summary)
            live_remaining = sum(1 for s in segs if s.status == "live")
            if tombstoned:
                logger.info(
                    "segments: summarize scope=%s replaced=%d/%d -> summary lt=%d "
                    "tokens=%d live_after=%d",
                    scope,
                    len(tombstoned),
                    len(ids),
                    summary_lt,
                    token_count,
                    live_remaining,
                )
            else:
                logger.warning(
                    "segments: summarize scope=%s matched no live ids=%s (summary appended only)",
                    scope,
                    ids,
                )
            self._finish_write(
                session_id,
                scope,
                "summarize",
                written=[summary],
                tombstoned=tombstoned,
                step=step,
                derived_from=list(ids),
                logical_time=summary_lt,
            )
            return summary

    def replace(
        self,
        session_id: str,
        scope: str,
        target_id: str,
        content: dict[str, Any],
        *,
        kind: SegmentKind | None = None,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment | None:
        """Replace a live segment's content in place (a logical-time tick that
        tombstones the original and emits a fresh segment at the SAME render slot).

        Like ``summarize``/``delete``, this TICKS the clock and TOMBSTONES rather than
        mutating in place, so the pre-replace view is recoverable as-of-T: the original
        keeps its creation ``logical_time`` and gets ``tombstoned_at`` = the replace
        tick, while the replacement is created at that tick and inherits the original's
        ``order`` (so it renders exactly where the original was). ``derived_from`` links
        the replacement back to the id it superseded (1:1 provenance, vs summarize's
        many:1). ``kind`` defaults to the original's kind (a pure content edit);
        passing ``kind`` re-kinds the slot. ``turn_id`` / ``expert_span_id`` /
        ``run_span_id`` default to the ORIGINAL's correlation ids (a pure content edit
        stays in the same turn/expert/run); pass them to override. Returns the new
        Segment, or ``None`` if ``target_id`` matched no live segment.
        """
        content = _coerce_content(content)
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            original = next((s for s in segs if s.id == target_id and s.status == "live"), None)
            if original is None:
                logger.debug(
                    "segments: replace scope=%s matched no live id=%s (no-op)",
                    scope,
                    target_id,
                )
                return None
            op_lt = self._new_lt()
            original.status = "tombstoned"
            original.tombstoned_at = op_lt  # replaced exactly when the new segment appears
            replacement = Segment(
                scope=scope,
                kind=kind if kind is not None else original.kind,
                content=content,
                session_id=session_id,
                step=original.step,
                order=original.order,  # same render slot
                logical_time=op_lt,
                token_count=token_count,
                derived_from=[original.id],
                trace_ref=trace_ref,
                turn_id=turn_id or original.turn_id,
                expert_span_id=expert_span_id or original.expert_span_id,
                run_span_id=run_span_id or original.run_span_id,
            )
            segs.append(replacement)
            logger.debug(
                "segments: replace scope=%s target=%s -> id=%s kind=%s lt=%d order=%.4f",
                scope,
                target_id,
                replacement.id,
                replacement.kind,
                op_lt,
                replacement.order,
            )
            self._finish_write(
                session_id,
                scope,
                "replace",
                written=[replacement],
                tombstoned=[original.id],
                step=original.step,
                derived_from=[original.id],
                logical_time=op_lt,
            )
            return replacement

    def apply(self, op: str, session_id: str, scope: str, **kwargs: Any) -> Any:
        """Stable dispatch over the context ops — the KV-backend swap seam.

        Raises:
            ValueError: if ``op`` is not one of
                append/insert/delete/summarize/replace.
        """
        if op == "append":
            return self.append(session_id, scope, **kwargs)
        if op == "insert":
            return self.insert(session_id, scope, **kwargs)
        if op == "delete":
            return self.delete(session_id, scope, **kwargs)
        if op == "summarize":
            return self.summarize(session_id, scope, **kwargs)
        if op == "replace":
            return self.replace(session_id, scope, **kwargs)
        raise ValueError(f"unknown segment op: {op!r}")

    # ---- write finalization (persist + Trace log + trace_ref back-link) ----

    def _finish_write(
        self,
        session_id: str,
        scope: str,
        op: str,
        *,
        written: list[Segment] | None = None,
        tombstoned: list[str] | None = None,
        step: int | None = None,
        position: int | None = None,
        derived_from: list[str] | None = None,
        logical_time: int | None = None,
    ) -> None:
        """Log the applied op to the Trace, stamp ``trace_ref`` on written segments,
        then persist. Called under this scope's per-scope lock."""
        written = written or []
        for seg in written:  # parallel locator: index every newly-written segment
            self._index.add(session_id, scope, seg)
        if logical_time is not None:
            lt = logical_time
        elif written:
            lt = written[0].logical_time
        else:
            with self._clock_lock:  # bare read of the shared clock high-water mark
                lt = self._next_lt - 1
        if self._op_logger is not None:
            try:
                event = self._op_logger(
                    op,
                    session_id,
                    scope,
                    logical_time=lt,
                    step=step,
                    position=position,
                    segments_written=[msgspec.to_builtins(s) for s in written],
                    segments_tombstoned=list(tombstoned or []),
                    derived_from=list(derived_from or []),
                )
                event_id = (event or {}).get("event_id", "")
                if event_id:
                    for s in written:
                        s.trace_ref = event_id
            except Exception:  # noqa: BLE001 - Trace logging must never break a context op
                logger.warning(
                    "segments: op_logger raised for op=%s scope=%s lt=%d (op still applied)",
                    op,
                    scope,
                    lt,
                    exc_info=True,
                )
        self._persist(session_id, scope, just_written=written)
        logger.debug(
            "segments: persisted op=%s scope=%s lt=%d written=%d tombstoned=%d",
            op,
            scope,
            lt,
            len(written),
            len(tombstoned or []),
        )

    # ---- read surface --------------------------------------------------

    def render(self, session_id: str, scope: str, *, as_of: int | None = None) -> list[Segment]:
        """THE decisive method. Ordered LIVE view (summaries already substituted),
        a PURE function of the stored segments.

        ``as_of`` (a ``logical_time``) renders the view *as it was* at that time:
        only segments created at or before ``as_of``, and a tombstone counts only
        if its tombstoning ``logical_time`` is at or before ``as_of``. ``as_of=None``
        is the current live view.
        """
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            if as_of is None:
                live = self._live_sorted(segs)
                logger.debug(
                    "segments: render scope=%s live=%d total=%d",
                    scope,
                    len(live),
                    len(segs),
                )
                return live
            # Live-as-of-T: created at/before T (logical_time is the immutable
            # creation clock) AND not yet tombstoned at T (tombstoned_at == 0 means
            # never tombstoned).
            visible = [
                s
                for s in segs
                if s.logical_time <= as_of and (s.tombstoned_at == 0 or s.tombstoned_at > as_of)
            ]
            return sorted(visible, key=lambda s: (s.order, s.logical_time))

    def render_keys(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> dict[str, Any]:
        """``render`` projected into dspy's trajectory dict
        (``thought_{i}`` / ``tool_name_{i}`` / ``tool_args_{i}`` / ``observation_{i}``).
        This is exactly what the ``_format_trajectory`` override reads. See
        :func:`segments_to_keys` for the projection algorithm.
        """
        return segments_to_keys(self.render(session_id, scope, as_of=as_of))

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> list[Segment]:
        """LIVE WORKING-SET segments — the kinds the PROMPT and the compaction/reset
        paths operate on. Excludes the richer ARC-as-source kinds (``answer`` /
        ``semantic_event``), which are part of ARC's complete freeze-anytime
        state but are NOT working-set context.

        This is the target of the per-turn working-set reset and ``_maybe_autocompact``
        — NOT a new prompt source. ``render`` / ``render_keys`` are UNCHANGED: the
        prompt stays ``segments_to_keys(render(...))``, which is a kind-allowlist that
        already ignores the new kinds, so the prompt is byte-identical whether or not
        the new atoms are present. Until any writer emits the new kinds, this returns
        exactly what ``render`` returns (so adopting it is behavior-preserving), and
        excludes exactly the atoms once they exist.
        """
        return [
            s for s in self.render(session_id, scope, as_of=as_of) if s.kind in WORKING_SET_KINDS
        ]

    def render_text(
        self,
        session_id: str,
        scope: str,
        *,
        as_of: int | None = None,
        separator: str = "\n",
    ) -> str:
        """``render`` flattened via ``segment_text`` — for inspection / byte-equality."""
        return separator.join(segment_text(s) for s in self.render(session_id, scope, as_of=as_of))

    def list_segments(
        self, session_id: str, scope: str, *, include_tombstoned: bool = False
    ) -> list[Segment]:
        """All segments in order (optionally including tombstoned, for replay /
        provenance). ``render`` is the live subset."""
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            pool = segs if include_tombstoned else [s for s in segs if s.status == "live"]
            return sorted(pool, key=lambda s: (s.order, s.logical_time))

    def locate_segment_ids(
        self,
        session_id: str,
        scope: str,
        *,
        lt_min: int | None = None,
        lt_max: int | None = None,
    ) -> list[str]:
        """Locate a scope's segment ids via the O(log N) per-scope index, in
        creation-``logical_time`` order, optionally restricted to the inclusive
        ``[lt_min, lt_max]`` clock window.

        This is the index READ surface (additive). It is NOT yet wired into render/op
        — those still scan — so the index is built+validated here without changing any
        behavior. Ensures the scope is loaded so a cold index is populated first.
        """
        with self._lock_for(session_id, scope):
            self._segs(session_id, scope)  # ensure cold-load (populates the index)
            return self._index.locate_ids(session_id, scope, lt_min=lt_min, lt_max=lt_max)

    def _index_matches_scan(self, session_id: str, scope: str) -> bool:
        """Parallel-consistency check: the id SET the index locates equals the id set
        the scan (the in-memory scope list, all statuses) holds. Used by the
        index-consistency tests to assert the locator never diverges from the scan."""
        with self._lock_for(session_id, scope):
            scan_ids = {s.id for s in self._segs(session_id, scope)}
            index_ids = set(self._index.locate_ids(session_id, scope))
            return scan_ids == index_ids

    def scan_scopes(self, session_id: str, scope_pattern: str = "") -> list[str]:
        """Scope addresses for a session under a prefix (e.g. ``"agentX/"`` for
        agent-level, ``""`` for all). Backs cross-scope reads."""
        prefix = f"{session_id}{_SCOPE_SEP}"
        out: set[str] = set()
        for name, _ in self._store.scan("segments", prefix=prefix):
            scope = name[len(prefix) :].replace(_SLASH_SUB, "/")
            if scope.startswith(scope_pattern):
                out.add(scope)
        return sorted(out)

    def sessions_with_scope(self, scope: str) -> list[str]:
        """Every ``session_id`` that currently holds a record for ``scope`` (across all
        sessions). Backs wholesale lifecycle erase of an ephemeral scope (e.g. the
        observer's ``_events`` log scope) without callers reaching into store internals."""
        suffix = f"{_SCOPE_SEP}{scope.replace('/', _SLASH_SUB)}"
        out: set[str] = set()
        for name, _ in self._store.scan("segments"):
            if name.endswith(suffix):
                out.add(name[: -len(suffix)])
        return sorted(out)

    def supports_search(self) -> bool:
        """Whether the backend does real BM25 ranking (CTE) vs the naive fallback."""
        fn = getattr(self._store, "supports_search", None)
        return bool(fn()) if callable(fn) else False

    def search_scopes(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """Semantic discovery (Thread D): rank a session's scopes by how well their
        content matches ``query_text``. Returns ``[(scope, score)]`` best-first —
        "which expert/scope knows about X". BM25 on CTE, naive word-overlap on LocalFS.
        """
        search = getattr(self._store, "search", None)
        if not callable(search):
            return []
        prefix = f"{session_id}{_SCOPE_SEP}{scope_prefix.replace('/', _SLASH_SUB)}"
        sep = f"{session_id}{_SCOPE_SEP}"
        out: list[tuple[str, float]] = []
        for record_name, score in search("segments", query_text, name_prefix=prefix, k=k):
            if record_name.startswith(sep):
                out.append((record_name[len(sep) :].replace(_SLASH_SUB, "/"), score))
        return out

    def tokens_by_kind(self, session_id: str, scope: str) -> dict[str, int]:
        """Sum ``token_count`` of LIVE segments grouped by kind — the attribution
        that drives compaction targeting. Window fullness still uses the provider's
        exact ``prompt_tokens``; this is the breakdown."""
        out: dict[str, int] = {}
        for s in self.render(session_id, scope):
            out[s.kind] = out.get(s.kind, 0) + s.token_count
        return out

    # ---- lifecycle -----------------------------------------------------

    def drop_scope(self, session_id: str, scope: str) -> int:
        """ERASE one ``(session_id, scope)`` from the in-memory copy AND the durable
        store, returning the number of segments dropped.

        Unlike :meth:`delete` (which tombstones live segments, keeping them for replay)
        and :meth:`release` (which only drops the in-memory copy, leaving the store
        record intact for reload), this fully removes the scope's record so a later
        ``render`` returns nothing and the store no longer holds it. It is the lifecycle
        eraser for reserved/ephemeral scopes (e.g. the observer's ``_events`` log scope),
        which must return to baseline on session release / wholesale clear rather than
        persist for replay. Held under this scope's per-scope lock."""
        with self._lock_for(session_id, scope):
            key = (session_id, scope)
            existing = self._segs(session_id, scope)
            count = len(existing)
            self._scopes.pop(key, None)
            self._loaded.discard(key)
            self._index.drop_scope(session_id, scope)
            self._store.delete("segments", self._record_name(session_id, scope))
            logger.debug(
                "segments: drop_scope session=%s scope=%s dropped=%d", session_id, scope, count
            )
            return count

    def release(self, session_id: str) -> int:
        """Drop a session's in-memory scopes (write-through, nothing lost). Returns
        the number of scopes released.

        Store-wide structural op: it holds the registry lock (freezing the lock set +
        structural maps) AND each affected scope lock, so it never races a per-scope
        op in flight. No deadlock: a per-scope op only re-touches the registry at its
        single ``_lock_for`` entry (before taking its scope lock, releasing the
        registry immediately) — it never holds a scope lock while waiting on the
        registry, so this acquire-registry-then-scopes order has no reverse cycle."""
        with self._registry_lock:
            keys = [k for k in self._scopes if k[0] == session_id]
            lock_keys = sorted({k for k in self._scope_locks if k[0] == session_id} | set(keys))
            held = [self._scope_locks[k] for k in lock_keys if k in self._scope_locks]
            for lk in held:
                lk.acquire()
            try:
                for k in keys:
                    self._scopes.pop(k, None)
                    self._loaded.discard(k)
                self._index.drop_session(session_id)  # keep the locator consistent
            finally:
                for lk in held:
                    lk.release()
            logger.info("segments: release session=%s scopes=%d", session_id, len(keys))
            return len(keys)

    def clear(self) -> None:
        """Drop ALL in-memory scope state (store untouched).

        Store-wide: holds the registry lock + every scope lock (deterministic order)
        so it never races a per-scope op (same no-deadlock argument as ``release``)."""
        with self._registry_lock:
            held = [self._scope_locks[k] for k in sorted(self._scope_locks)]
            for lk in held:
                lk.acquire()
            try:
                self._scopes.clear()
                self._loaded.clear()
                self._index.clear()
            finally:
                for lk in held:
                    lk.release()
