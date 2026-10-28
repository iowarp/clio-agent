"""SegmentStore: the ARC live context plane.

An ordered, scoped, mutable sequence of :class:`~clio_agent.arc.schema.Segment`s that the gact
ReAct loop reads from on every iteration. The loop *writes* one segment per produced piece
(thought / tool_call / observation) and *reads* the prompt back by rendering the live ordered
set (``render``, folded into messages by ``clio_react_record.fold_steps``). The context operations — ``append`` / ``insert`` / ``delete`` /
``summarize`` / ``replace`` — mutate segments between renders, so an out-of-band edit changes
the *next* prompt. That is the whole point of the live plane.

Design (docs/archive/arc-live-context-plane.md, docs/archive/implementation-spec.md):
    * Composed by ``ARCMemory`` (sibling of ``LiveRuntimeContext``); persists through the
      injected ``ARCStore`` as one record per ``(session_id, scope)`` — ``render`` is the
      every-iteration hot path, so the whole scope is batched into a single get/decode.
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
from typing import Any, Callable, Iterable, Optional

import msgspec

from clio_agent.arc.context_restore import PlainContextRestore
from clio_agent.arc.context_view import ViewSnapshot, next_generation, order_at
from clio_agent.arc.loop_guard import assert_store_write_off_loop
from clio_agent.arc.schema import WORKING_SET_KINDS, Segment, SegmentKind, segment_text
from clio_agent.arc.segment_cache import _SCOPE_SEP as _SCOPE_SEP
from clio_agent.arc.segment_cache import _SLASH_SUB as _SLASH_SUB
from clio_agent.arc.segment_cache import ScopeCache
from clio_agent.arc.segment_errors import ArcPersistError as ArcPersistError
from clio_agent.arc.segment_errors import SegmentReadError as SegmentReadError
from clio_agent.arc.segment_ids import ContextOpLogError, require_live
from clio_agent.arc.segment_index import SegmentIndex as SegmentIndex
from clio_agent.arc.storage import ARCStore

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


def _tombstone(segment: Segment, at: int) -> Segment:
    """A tombstoned copy of a committed segment (the cached one is never mutated)."""
    return msgspec.structs.replace(segment, status="tombstoned", tombstoned_at=at)


# op_logger(op, session_id, scope, *, logical_time, step, position,
#           segments_written, segments_tombstoned, derived_from) -> event dict | None
OpLogger = Callable[..., Optional[dict[str, Any]]]


class SegmentStore(ScopeCache, PlainContextRestore):
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
            op_logger: Optional callable that logs an applied op to the durable Trace and returns
                the emitted event dict (with ``"event_id"``). Injected so ``arc/`` never depends on
                ``gact/``; ``None`` (unit tests / memory-only) means ops still work, just unlogged.
            search_indexed: Optional predicate ``scope -> bool`` deciding whether a scope's persist
                writes the plain-text search companion. ``None`` (default) indexes every scope
                (historical behavior). ARCMemory injects a predicate that returns ``False`` for the
                reserved ``_events`` chunk family so the semantic-event log never pollutes scope
                search — a deliberate, scope-level exclusion (not per-op) that holds for EVERY
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
        # In-memory read copy: (session_id, scope) -> list[Segment] (all, incl
        # tombstoned). A write commits to clio-core first, then replaces the entry
        # (``ScopeCache._commit_scope``). Touched only under that scope's lock.
        self._scopes: dict[tuple[str, str], list[Segment]] = {}
        self._loaded: set[tuple[str, str]] = set()
        # Per-scope B-tree locator built in parallel with the scope lists (additive;
        # NOT yet on the render/op read path — those still scan). Lets a scope's
        # segments be located in O(log N) by creation logical_time.
        self._index = SegmentIndex()
        # Store-wide monotonic logical clock, recovered past the persisted max.
        self._next_lt = 1
        # Per-scope op generation (bumped by every op but append): see context_view.
        self._generations: dict[tuple[str, str], int] = {}
        self._invocation_counts: dict[str, int] = {}  # counted by the residency owner

    def set_op_logger(self, op_logger: OpLogger | None) -> None:
        """Inject (or replace) the durable-Trace op logger after construction.

        Used by the gact app to wire ``_emit_arc_op`` once both the app handle and
        ARC exist (avoids the app/ARC construction cycle). Keeps ``arc/`` free of any
        ``gact/`` import.
        """
        self._op_logger = op_logger
        logger.debug("segments: op_logger %s", "attached" if op_logger else "cleared")

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

    def _recover_clock(self, segs: Iterable[Segment]) -> None:
        """Advance the shared clock past persisted segments (scope -> clock lock order)."""
        with self._clock_lock:
            self._next_lt = max([self._next_lt, *(s.logical_time + 1 for s in segs)])

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
                [*segs, seg],
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
                [*segs, seg],
                written=[seg],
                step=step,
                position=position,
                logical_time=seg.logical_time,
            )
            return seg

    @staticmethod
    def _order_for_position(segs: list[Segment], live: list[Segment], position: int) -> float:
        """Gap-allocated float order so a mid-insert never renumbers neighbours."""
        orders = [s.order for s in segs]
        return order_at(min(orders, default=None), max(orders, default=None), live, position)

    def delete(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Tombstone live segments by id (render skips them). Tombstone-not-erase
        so the segment survives for Trace reconstruction / as-of-T. Returns the
        number actually tombstoned."""
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            require_live(ids, {s.id for s in segs if s.status == "live"}, op="delete", scope=scope)
            target = set(ids)
            tombstoned: list[str] = []
            op_lt = 0
            staged = list(segs)  # committed copies are never mutated: replace, then commit
            for i, s in enumerate(staged):
                if s.id in target and s.status == "live":
                    op_lt = self._new_lt()  # tombstoning time (as-of-T); keep creation lt
                    staged[i] = _tombstone(s, op_lt)
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
                    session_id, scope, "delete", staged, tombstoned=tombstoned, logical_time=op_lt
                )
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
        position: int | None = None,
    ) -> Segment:
        """summarize = delete(ids) + insert(summary at the first replaced position, or
        at render ``position`` over the live segments), ATOMIC under the lock. The new
        Segment is ``kind="summary"`` with ``derived_from=ids``; the caller produces
        ``summary_content`` (the LLM call). The span ids are stamped on it."""
        summary_content = _coerce_content(summary_content)
        with self._lock_for(session_id, scope):
            segs = self._segs(session_id, scope)
            live_ids = {s.id for s in segs if s.status == "live"}
            require_live(ids, live_ids, op="summarize", scope=scope)
            target = set(ids)
            replaced = [s for s in segs if s.id in target and s.status == "live"]
            first = min(replaced, key=lambda s: (s.order, s.logical_time))  # the range's slot
            at = self._order_for_position(segs, self._live_sorted(segs), position or 0)
            order = first.order if position is None else at
            step = min((s.step for s in replaced), default=-1)
            summary_lt = self._new_lt()
            tombstoned = [s.id for s in replaced]
            staged = [  # replaced exactly when the summary appears
                _tombstone(s, summary_lt) if s.id in target and s.status == "live" else s
                for s in segs
            ]
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
            staged.append(summary)
            live_remaining = sum(1 for s in staged if s.status == "live")
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
                staged,
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
            live_ids = {s.id for s in segs if s.status == "live"}
            require_live([target_id], live_ids, op="replace", scope=scope)
            original = next(s for s in segs if s.id == target_id and s.status == "live")
            op_lt = self._new_lt()  # the original is replaced exactly when the new one appears
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
            staged = [_tombstone(s, op_lt) if s is original else s for s in segs]
            staged.append(replacement)
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
                staged,
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
        staged: list[Segment],
        *,
        written: list[Segment] | None = None,
        tombstoned: list[str] | None = None,
        step: int | None = None,
        position: int | None = None,
        derived_from: list[str] | None = None,
        logical_time: int | None = None,
    ) -> None:
        """Log the op to the Trace, stamp ``trace_ref`` on the written segments, commit
        ``staged`` to clio-core and only then cache it (write-through: a refusal leaves
        the cache at the last committed record). Called under this scope's lock."""
        written = written or []
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
            except Exception as exc:  # noqa: BLE001 - re-raised typed; nothing was cached
                raise ContextOpLogError(op=op, scope=scope, cause=exc) from exc
        self._commit_scope(session_id, scope, staged)
        if op != "append":  # an op may rewrite the prefix: a new view generation
            self._generations[(session_id, scope)] = next_generation()
        for seg in written:  # parallel locator: index every newly-written segment
            self._index.add(session_id, scope, seg)
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
            # Live-as-of-T: created at/before T (logical_time is the immutable creation clock) AND
            # not yet tombstoned at T (tombstoned_at == 0 means never tombstoned).
            visible = [
                s
                for s in segs
                if s.logical_time <= as_of and (s.tombstoned_at == 0 or s.tombstoned_at > as_of)
            ]
            return sorted(visible, key=lambda s: (s.order, s.logical_time))

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> list[Segment]:
        """LIVE WORKING-SET segments — the kinds the PROMPT and the compaction/reset
        paths operate on. Excludes the richer ARC-as-source kinds (``answer`` /
        ``semantic_event``), which are part of ARC's complete freeze-anytime
        state but are NOT working-set context.

        This is compaction's target. The agent's context folds these kinds (see
        ``clio_react_record.fold_steps``), which ignores any kind it does not model.
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

    def context_view(self, session_id: str, scope: str) -> ViewSnapshot:
        """The scope's live segments with their op generation (appends keep it)."""
        with self._lock_for(session_id, scope):
            live = self._live_sorted(self._segs(session_id, scope))
            return ViewSnapshot(self._generations.get((session_id, scope), 0), tuple(live))

    def has_segments(self, session_id: str, scope: str) -> bool:
        """Whether anything was ever recorded in the scope (any status)."""
        with self._lock_for(session_id, scope):
            return bool(self._segs(session_id, scope))

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

    def supports_search(self) -> bool:
        """Whether the backend does real BM25 ranking (clio-core) vs the naive fallback."""
        fn = getattr(self._store, "supports_search", None)
        return bool(fn()) if callable(fn) else False

    def search_degradation_reason(self) -> str:
        """Typed reason real search is unavailable (#905), or ``""`` (works, or LocalFS)."""
        fn = getattr(self._store, "search_degradation_reason", None)
        return str(fn()) if callable(fn) else ""

    def search_scopes(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """Semantic discovery (Thread D): rank a session's scopes by how well their
        content matches ``query_text``. Returns ``[(scope, score)]`` best-first —
        "which expert/scope knows about X". BM25 on clio-core, naive word-overlap on LocalFS.
        """
        prefix = f"{session_id}{_SCOPE_SEP}{scope_prefix.replace('/', _SLASH_SUB)}"
        sep = f"{session_id}{_SCOPE_SEP}"
        out: list[tuple[str, float]] = []
        for record_name, score in self._store.search(
            "segments", query_text, name_prefix=prefix, k=k
        ):
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
            count = len(self._segs(session_id, scope))
            assert_store_write_off_loop("segments.delete", scope=scope)
            self._store.delete("segments", self._record_name(session_id, scope))  # store first
            self._discard_scope(session_id, scope)
            self._generations[(session_id, scope)] = next_generation()  # no reader keeps a prefix
            logger.debug("segments: drop_scope %s/%s dropped=%d", session_id, scope, count)
            return count
