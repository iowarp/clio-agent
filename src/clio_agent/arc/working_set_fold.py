"""FoldingSegmentStore: the working set as a FOLD of the canonical ``_events`` log.

The #737 S2 slice collapses the dual ARC. TODAY the ReAct loop writes working-set
segments to a per-expert scope AND (separately) the semantic-event bus writes the
same history into the reserved ``_events`` log — two parallel materializations of
one history (RULE 4 / the #737 thesis). This module removes the parallel write: the
loop's content atoms become the ONLY copy, appended to the canonical ``_events``
log, and ``render_working_set`` / ``render_segments`` / ``render_segments_keys`` are
re-expressed as a **fold** of that log (design ``docs/design/unified-arc-highway.md``
§2.8b, §2.9, §2.10).

Design decisions (each answering a named review finding):

* **Raw append lane (§2.9).** Content atoms and op records are appended through
  :meth:`FoldingSegmentStore._append_raw`, which NEVER invokes the ``op_logger`` /
  ``_finish_write`` callback. Routing a log write back through the op-logger re-forms
  the documented ``record -> op_logger -> arc.op -> record`` recursion — "the
  strongest candidate for where the last attempt died". The raw lane also drops the
  ~190 per-turn plain-``append`` ``arc.op`` frames the old path emitted.
* **On the ``_events`` family, span-partitioned (§2.10).** Content atoms live in the
  ``_events/w/<span>`` chunk lane — part of the reserved ``_events`` family (so they
  are search-excluded and lifecycle-erased with the log) but partitioned by
  ``expert_span_id`` so concurrent experts do not serialize on one chunk lock. Each
  atom carries its LOGICAL working-set scope in ``Segment.scope`` (e.g. ``"agentA"``),
  so the fold recovers a scope's working set by filtering the merged lane. The
  semantic-event readers (:class:`~clio_agent.arc.live.LiveRuntimeContext`) are
  unaffected — they keep only ``semantic_event``-kind segments, and these atoms are
  ``thought`` / ``tool_call`` / ``observation`` / ``summary`` / ``ws_op`` /
  ``step_open``.
* **Op records, append-only (§2.5).** A ``delete`` is an appended ``ws_op`` atom
  ``{op, targets}``; ``summarize`` / ``replace`` piggy-back on the produced atom's
  ``derived_from`` (which lists the ids it supersedes). The fold TOMBSTONES a target
  at the ``logical_time`` of the op/producer that retired it — never rewriting stored
  content — so as-of-T replay is exact (the trace view = the log with operations
  visible).
* **Byte-exact ``order`` (§4.1.A).** Each content atom carries a SCOPE-LOCAL ``order``
  computed exactly as :class:`~clio_agent.arc.segments.SegmentStore` computes it
  (``max(order)+1`` for append, gap-midpoint for insert, the replaced slot for
  summarize/replace), so the folded segment list is byte-identical to a
  separately-written working set — the standing equivalence gate.
* **Ingest-time search companion (§2.7).** Because content leaves the per-expert
  scope, the per-scope ``.search`` companion would be orphaned. The fold rewrites it
  at ingest: a zero-segment record under the logical scope carrying the folded live
  text, so ``search_scopes`` ranks the scope identically to the old write.

The store is a drop-in behind the ``SegmentStore`` seam: it subclasses SegmentStore
and overrides only the working-set ops/reads, delegating every reserved-scope
(``_events`` / ``_events/N``) call to ``super()`` unchanged.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from clio_agent import conf
from clio_agent.arc.live import EVENTS_SCOPE, is_events_scope
from clio_agent.arc.schema import (
    WORKING_SET_KINDS,
    Segment,
    SegmentKind,
    encode_segments,
    segment_text,
)
from clio_agent.arc.segments import SegmentStore, _coerce_content, segments_to_keys
from clio_agent.arc.storage import ARCStore

logger = logging.getLogger(__name__)

# The content lane of the canonical log: a chunk family UNDER ``_events`` (so
# ``is_events_scope`` is True — search-excluded + lifecycle-erased with the log),
# partitioned by ``expert_span_id`` so parallel experts do not contend on one lock.
WS_CONTENT_FAMILY = f"{EVENTS_SCOPE}/w"

# Log-internal atom kinds that are NEVER renderable content: the append-only op
# record and the pre-execution crash breadcrumb. Excluded from every fold render.
WS_OP_KIND: SegmentKind = "ws_op"
STEP_OPEN_KIND: SegmentKind = "step_open"
_NON_CONTENT_KINDS = frozenset({WS_OP_KIND, STEP_OPEN_KIND})


def ws_content_partition(expert_span_id: str) -> str:
    """Physical scope of the content lane for one ``expert_span_id``.

    Concurrent experts write disjoint partitions (``_events/w/<span>``) so their
    appends take different per-scope locks. An empty span (unstamped writers, tests)
    maps to the shared ``_events/w/_`` partition.

    Args:
        expert_span_id: The owning expert-turn span id, or ``""``.

    Returns:
        The reserved content-lane scope string.
    """
    return f"{WS_CONTENT_FAMILY}/{expert_span_id or '_'}"


def is_ws_content_scope(scope: str) -> bool:
    """Whether ``scope`` is a content-lane partition (``_events/w`` or ``_events/w/*``)."""
    return scope == WS_CONTENT_FAMILY or scope.startswith(f"{WS_CONTENT_FAMILY}/")


def _default_search_indexed(scope: str) -> bool:
    """The reserved ``_events`` chunk family (log + content lane) is search-excluded so
    it can never pollute scope search; every other scope is indexed."""
    return not is_events_scope(scope)


def emit_step_open(
    arc_memory: Any,
    session_id: str,
    scope: str,
    content: dict[str, Any],
    *,
    step: int = -1,
    turn_id: str = "",
    expert_span_id: str = "",
    run_span_id: str = "",
) -> None:
    """Emit a pre-execution ``step_open`` breadcrumb IF ``arc_memory`` folds the working
    set — a no-op otherwise (caveat b, §2.8b).

    Called by the expert loop BEFORE a step's tools execute. It is excluded from every
    fold render, so it never perturbs the working set; its only purpose is that a crash
    mid-step still leaves the step's opening atoms on the canonical log. Best-effort by
    construction — a breadcrumb must never break a turn.

    Args:
        arc_memory: The ARC memory handle (``_segments`` is inspected for the fold).
        session_id: Owning session.
        scope: The working-set scope the step belongs to.
        content: The breadcrumb payload (e.g. the thought + tool names).
        step: The ReAct iteration index.
        turn_id: Owning expert-turn id.
        expert_span_id: Owning expert-turn span id (also the content-lane partition).
        run_span_id: Owning step span id.
    """
    store = getattr(arc_memory, "_segments", None)
    if not isinstance(store, FoldingSegmentStore):
        return
    try:
        store.append_step_open(
            session_id,
            scope,
            content,
            step=step,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
    except Exception:  # noqa: BLE001 - a crash breadcrumb must never break a turn
        logger.warning("working_set_fold: step_open breadcrumb failed scope=%s", scope, exc_info=True)


def make_segment_store(
    store: ARCStore,
    *,
    search_indexed: Callable[[str], bool] | None = None,
    working_set_fold: bool | None = None,
) -> SegmentStore:
    """Construct the live-plane segment store, folding or not per the S2 flag.

    The working-set fold is a **session-scoped** capability (design §4.4b): the flag
    is resolved once here at ARC construction and pinned for the store's life, so a
    session is either wholly old-regime or wholly new-regime — never a mid-session
    flip. Default OFF (``arc.working_set_fold`` / ``CLIO_ARC_WORKING_SET_FOLD``) until
    every S2 proof is green; callers (and the dual-run harness) pass an explicit
    ``working_set_fold`` to force a regime.

    Args:
        store: The persistence backend.
        search_indexed: Optional scope-search predicate (defaults to excluding the
            reserved ``_events`` family).
        working_set_fold: Force the regime; ``None`` resolves the config flag.

    Returns:
        A :class:`FoldingSegmentStore` when the fold is on, else a plain
        :class:`SegmentStore`.
    """
    predicate = search_indexed or _default_search_indexed
    if working_set_fold is None:
        working_set_fold = conf.resolve(
            "arc.working_set_fold",
            env="CLIO_ARC_WORKING_SET_FOLD",
            default=False,
            cast=conf.as_bool,
        )
    if working_set_fold:
        return FoldingSegmentStore(store, search_indexed=predicate)
    return SegmentStore(store, search_indexed=predicate)


class FoldingSegmentStore(SegmentStore):
    """A :class:`SegmentStore` whose working-set is a fold of the canonical log.

    Working-set-scope writes (any scope that is NOT a reserved ``_events`` family
    scope) are redirected to the ``_events/w`` content lane via the raw append lane;
    working-set reads are derived as a fold with the append-only ops applied. Every
    reserved-scope call (the semantic-event log itself) is delegated to ``super()``
    unchanged, so the observer/highway path is byte-for-byte the old behavior.
    """

    def __init__(
        self,
        store: ARCStore,
        *,
        search_indexed: Any = None,
    ) -> None:
        """Back the folding store with an :class:`ARCStore`.

        Args:
            store: The persistence backend (LocalFS or clio-core), shared verbatim
                with the base store.
            search_indexed: Optional ``scope -> bool`` predicate deciding which
                physical scopes get a plain-text search companion. The content lane is
                already excluded (it is an ``_events`` family scope); the fold writes
                the logical-scope companion itself (§2.7).
        """
        super().__init__(store, search_indexed=search_indexed)
        # The ingest-time search companion (§2.7). Kept toggleable so a deployment that
        # never uses scope search can skip the per-append companion write entirely.
        self._search_companion_enabled = True
        # Per-session cache of the discovered content-lane partition scopes, so a warm
        # fold render does NOT re-scan the store to re-discover partitions (§2.10 read
        # budget): populated by one scan on first access, then kept current as new
        # partitions are appended. Guarded by its own lock (partitions can be created
        # from concurrent expert threads).
        self._lane_cache: dict[str, set[str]] = {}
        self._lane_cache_lock = threading.Lock()

    # ---- scope routing -------------------------------------------------

    @staticmethod
    def _is_working_set_scope(scope: str) -> bool:
        """A working-set scope is any non-empty scope OUTSIDE the reserved ``_events``
        family — i.e. the per-expert scopes the loop renders its prompt from."""
        return bool(scope) and not is_events_scope(scope)

    # ---- raw append lane (§2.9) ----------------------------------------

    def _append_raw(self, session_id: str, storage_scope: str, seg: Segment) -> Segment:
        """Append a pre-built ``Segment`` to a physical scope WITHOUT the op-logger.

        This is the canonical-log write primitive: it persists the atom and keeps the
        per-scope locator in sync, but it does NOT call ``_finish_write`` (which would
        invoke the ``op_logger`` and re-form the ``arc.op`` recursion, §2.9). Held
        under the physical scope's per-scope lock.

        Args:
            session_id: Owning session.
            storage_scope: The physical content-lane scope (``_events/w/<span>``).
            seg: The fully-formed segment (its ``.scope`` is the LOGICAL working-set
                scope, distinct from ``storage_scope``).

        Returns:
            The appended segment.
        """
        with self._lock_for(session_id, storage_scope):
            segs = self._segs(session_id, storage_scope)
            segs.append(seg)
            self._index.add(session_id, storage_scope, seg)
            self._persist(session_id, storage_scope, just_written=[seg])
        self._note_partition(session_id, storage_scope)
        return seg

    def _make_atom(
        self,
        session_id: str,
        scope: str,
        kind: SegmentKind,
        content: dict[str, Any],
        *,
        order: float,
        step: int,
        trace_ref: str,
        derived_from: list[str] | None,
        token_count: int,
        turn_id: str,
        expert_span_id: str,
        run_span_id: str,
    ) -> Segment:
        """Build a content/op atom with a store-wide ``logical_time`` and the given
        scope-local ``order`` (content is coerced through the ONE ingest chokepoint)."""
        return Segment(
            scope=scope,
            kind=kind,
            content=_coerce_content(content),
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

    # ---- the fold (read side) ------------------------------------------

    def _lane_scopes(self, session_id: str) -> list[str]:
        """Every content-lane partition scope persisted for a session, in a stable
        order. The fold merges them by ``logical_time`` (store-wide monotonic), so the
        partition order does not affect the result.

        Discovered by ONE store scan on first access per session, then served from an
        in-memory cache kept current by :meth:`_note_partition` — so warm renders do not
        re-scan the store (§2.10 read budget)."""
        with self._lane_cache_lock:
            cached = self._lane_cache.get(session_id)
            if cached is not None:
                return sorted(cached)
        discovered = {
            s for s in self.scan_scopes(session_id, WS_CONTENT_FAMILY) if is_ws_content_scope(s)
        }
        with self._lane_cache_lock:
            # Union so a partition appended between the scan and here is not lost.
            merged = self._lane_cache.setdefault(session_id, set())
            merged |= discovered
            return sorted(merged)

    def _note_partition(self, session_id: str, storage_scope: str) -> None:
        """Record a content-lane partition in the per-session cache (called on append)."""
        with self._lane_cache_lock:
            self._lane_cache.setdefault(session_id, set()).add(storage_scope)

    def _lane_atoms(self, session_id: str) -> list[Segment]:
        """All atoms (content + op records + breadcrumbs) across the session's content
        lane, merged. Reads each partition under its own lock via the base loader."""
        atoms: list[Segment] = []
        for pscope in self._lane_scopes(session_id):
            with self._lock_for(session_id, pscope):
                atoms.extend(self._segs(session_id, pscope))
        return atoms

    def _fold(
        self,
        session_id: str,
        scope: str,
        *,
        as_of: int | None,
        include_tombstoned: bool,
    ) -> list[Segment]:
        """Fold the content lane into ``scope``'s ordered segment view.

        Content atoms of ``scope`` are ordered by ``(order, logical_time)``; a target
        is tombstoned at the ``logical_time`` of the ``delete`` op or the
        ``summarize`` / ``replace`` producer that retired it. ``as_of`` yields the
        view as it was at that clock (atoms created after it are unborn; a tombstone
        after it has not yet landed) — the trace/as-of-T contract.

        Args:
            session_id: Owning session.
            scope: The LOGICAL working-set scope to reconstruct.
            as_of: Optional ``logical_time`` upper bound (``None`` = live view).
            include_tombstoned: When True, retired atoms are kept (replay/provenance);
                when False, they are dropped (the live render).

        Returns:
            The folded segment list in render order.
        """
        lane = self._lane_atoms(session_id)
        content = [
            a for a in lane if a.scope == scope and a.kind not in _NON_CONTENT_KINDS
        ]
        tomb: dict[str, int] = {}

        def _tombstone(ids: list[str], lt: int) -> None:
            for i in ids:
                cur = tomb.get(i)
                if cur is None or lt < cur:
                    tomb[i] = lt

        # summarize/replace retire their `derived_from` at the producer's clock.
        for a in content:
            if a.derived_from:
                _tombstone(list(a.derived_from), a.logical_time)
        # `delete` op records retire their targets at the op's clock.
        for a in lane:
            if a.scope == scope and a.kind == WS_OP_KIND and a.content.get("op") == "delete":
                _tombstone(list(a.content.get("targets") or []), a.logical_time)

        out: list[Segment] = []
        for a in sorted(content, key=lambda s: (s.order, s.logical_time)):
            if as_of is not None and a.logical_time > as_of:
                continue
            retired_at = tomb.get(a.id)
            retired = retired_at is not None and (as_of is None or retired_at <= as_of)
            if retired and not include_tombstoned:
                continue
            out.append(a)
        return out

    def _live_fold(self, session_id: str, scope: str, *, as_of: int | None) -> list[Segment]:
        """The live (non-retired) folded render of ``scope``."""
        return self._fold(session_id, scope, as_of=as_of, include_tombstoned=False)

    def _next_order(self, live_and_dead: list[Segment]) -> float:
        """``append``'s scope-local order — ``max(order)+1`` over ALL of a scope's
        content atoms (retired included), matching :meth:`SegmentStore.append`."""
        return max((s.order for s in live_and_dead), default=0.0) + 1.0

    def _scope_content(self, session_id: str, scope: str) -> list[Segment]:
        """Every content atom of a logical scope (any tombstone status) — the domain
        the scope-local ``order`` is computed over."""
        return self._fold(session_id, scope, as_of=None, include_tombstoned=True)

    # ---- search companion (§2.7) ---------------------------------------

    def _refresh_search_companion(self, session_id: str, scope: str) -> None:
        """Rewrite the logical scope's plain-text search companion from the fold.

        Content lives on the (search-excluded) ``_events/w`` lane, so the per-scope
        ``.search`` companion is maintained here at INGEST time as a zero-segment
        record whose ``search_text`` is the folded live render — byte-identical to the
        text the old per-scope write produced, so ``search_scopes`` ranks the scope
        the same. Never raises into the write path.
        """
        if not self._search_companion_enabled:
            return
        try:
            live = self._live_fold(session_id, scope, as_of=None)
            text = "\n".join(segment_text(s) for s in live) or None
            self._store.put(
                "segments",
                self._record_name(session_id, scope),
                encode_segments([]),
                search_text=text,
            )
        except Exception:  # noqa: BLE001 - a search-index refresh must never break a write
            logger.warning(
                "working_set_fold: search companion refresh failed scope=%s", scope, exc_info=True
            )

    # ---- overridden write surface --------------------------------------

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
        """Append a working-set atom to the canonical log (or delegate reserved-scope
        appends to the base store unchanged)."""
        if not self._is_working_set_scope(scope):
            return super().append(
                session_id,
                scope,
                kind,
                content,
                step=step,
                trace_ref=trace_ref,
                derived_from=derived_from,
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
        order = self._next_order(self._scope_content(session_id, scope))
        atom = self._make_atom(
            session_id,
            scope,
            kind,
            content,
            order=order,
            step=step,
            trace_ref=trace_ref,
            derived_from=derived_from,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
        self._append_raw(session_id, ws_content_partition(expert_span_id), atom)
        self._refresh_search_companion(session_id, scope)
        return atom

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
        """Insert a working-set atom at a render position (gap-allocated order)."""
        if not self._is_working_set_scope(scope):
            return super().insert(
                session_id,
                scope,
                position,
                kind,
                content,
                step=step,
                trace_ref=trace_ref,
                derived_from=derived_from,
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
        all_content = self._scope_content(session_id, scope)
        live = self._live_fold(session_id, scope, as_of=None)
        order = self._order_for_position(all_content, live, position)
        atom = self._make_atom(
            session_id,
            scope,
            kind,
            content,
            order=order,
            step=step,
            trace_ref=trace_ref,
            derived_from=derived_from,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
        self._append_raw(session_id, ws_content_partition(expert_span_id), atom)
        self._refresh_search_companion(session_id, scope)
        return atom

    def delete(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Retire working-set atoms by id via an append-only ``ws_op`` record.

        Only ids that are currently LIVE in the fold are retired (matching
        :meth:`SegmentStore.delete`'s live-only tombstone); the count returned is the
        number actually retired.
        """
        if not self._is_working_set_scope(scope):
            return super().delete(session_id, scope, ids)
        live_ids = {s.id for s in self._live_fold(session_id, scope, as_of=None)}
        targets = [i for i in ids if i in live_ids]
        if not targets:
            return 0
        op = self._make_atom(
            session_id,
            scope,
            WS_OP_KIND,
            {"op": "delete", "targets": list(targets)},
            order=0.0,  # op records are not rendered; order is immaterial
            step=-1,
            trace_ref="",
            derived_from=None,
            token_count=0,
            turn_id="",
            expert_span_id="",
            run_span_id="",
        )
        self._append_raw(session_id, ws_content_partition(""), op)
        self._refresh_search_companion(session_id, scope)
        return len(targets)

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
        """summarize = tombstone ``ids`` + emit one ``summary`` atom at the first
        replaced slot, atomically at ONE ``logical_time`` (the producer's
        ``derived_from`` drives the tombstoning in the fold)."""
        if not self._is_working_set_scope(scope):
            return super().summarize(
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
        target = set(ids)
        live = self._live_fold(session_id, scope, as_of=None)
        replaced = [s for s in live if s.id in target]
        if replaced:
            first = min(replaced, key=lambda s: (s.order, s.logical_time))
            order = first.order
            step = min((s.step for s in replaced), default=-1)
        else:
            order = self._next_order(self._scope_content(session_id, scope))
            step = -1
        atom = self._make_atom(
            session_id,
            scope,
            "summary",
            summary_content,
            order=order,
            step=step,
            trace_ref=trace_ref,
            derived_from=list(ids),
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
        self._append_raw(session_id, ws_content_partition(expert_span_id), atom)
        self._refresh_search_companion(session_id, scope)
        return atom

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
        """Replace a live atom 1:1 at its render slot (the replacement's
        ``derived_from`` retires the original at the replacement's ``logical_time``)."""
        if not self._is_working_set_scope(scope):
            return super().replace(
                session_id,
                scope,
                target_id,
                content,
                kind=kind,
                trace_ref=trace_ref,
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
        live = self._live_fold(session_id, scope, as_of=None)
        original = next((s for s in live if s.id == target_id), None)
        if original is None:
            return None
        atom = self._make_atom(
            session_id,
            scope,
            kind if kind is not None else original.kind,
            content,
            order=original.order,
            step=original.step,
            trace_ref=trace_ref,
            derived_from=[original.id],
            token_count=token_count,
            turn_id=turn_id or original.turn_id,
            expert_span_id=expert_span_id or original.expert_span_id,
            run_span_id=run_span_id or original.run_span_id,
        )
        self._append_raw(session_id, ws_content_partition(atom.expert_span_id), atom)
        self._refresh_search_companion(session_id, scope)
        return atom

    def append_step_open(
        self,
        session_id: str,
        scope: str,
        content: dict[str, Any],
        *,
        step: int = -1,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment | None:
        """Append a pre-execution ``step_open`` breadcrumb to the log (caveat b).

        Written BEFORE a step's tool executes so a crash mid-step still leaves the
        step's opening atoms on the canonical log. It is NOT renderable content — the
        fold excludes it from every render — so it never perturbs the working set; a
        crash-path reader inspects the raw content lane. A no-op for reserved scopes.
        """
        if not self._is_working_set_scope(scope):
            return None
        atom = self._make_atom(
            session_id,
            scope,
            STEP_OPEN_KIND,
            content,
            order=0.0,
            step=step,
            trace_ref="",
            derived_from=None,
            token_count=0,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )
        self._append_raw(session_id, ws_content_partition(expert_span_id), atom)
        return atom

    # ---- overridden read surface ---------------------------------------

    def render(self, session_id: str, scope: str, *, as_of: int | None = None) -> list[Segment]:
        """Ordered LIVE view of a scope — folded for working-set scopes, delegated for
        reserved ``_events`` scopes."""
        if not self._is_working_set_scope(scope):
            return super().render(session_id, scope, as_of=as_of)
        return self._live_fold(session_id, scope, as_of=as_of)

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> list[Segment]:
        """The folded live view restricted to working-set kinds."""
        if not self._is_working_set_scope(scope):
            return super().render_working_set(session_id, scope, as_of=as_of)
        return [s for s in self._live_fold(session_id, scope, as_of=as_of) if s.kind in WORKING_SET_KINDS]

    def render_keys(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> dict[str, Any]:
        """The folded live view projected into dspy's trajectory dict."""
        if not self._is_working_set_scope(scope):
            return super().render_keys(session_id, scope, as_of=as_of)
        return segments_to_keys(self._live_fold(session_id, scope, as_of=as_of))

    def render_text(
        self,
        session_id: str,
        scope: str,
        *,
        as_of: int | None = None,
        separator: str = "\n",
    ) -> str:
        """The folded live view flattened to text."""
        if not self._is_working_set_scope(scope):
            return super().render_text(session_id, scope, as_of=as_of, separator=separator)
        return separator.join(
            segment_text(s) for s in self._live_fold(session_id, scope, as_of=as_of)
        )

    def list_segments(
        self, session_id: str, scope: str, *, include_tombstoned: bool = False
    ) -> list[Segment]:
        """All of a scope's content atoms in render order (optionally including the
        retired ones, for replay/provenance). Excludes the log-internal op/breadcrumb
        atoms — they are not segments of the working set."""
        if not self._is_working_set_scope(scope):
            return super().list_segments(session_id, scope, include_tombstoned=include_tombstoned)
        return self._fold(session_id, scope, as_of=None, include_tombstoned=include_tombstoned)

    def tokens_by_kind(self, session_id: str, scope: str) -> dict[str, int]:
        """Sum ``token_count`` of LIVE folded segments grouped by kind."""
        if not self._is_working_set_scope(scope):
            return super().tokens_by_kind(session_id, scope)
        out: dict[str, int] = {}
        for s in self._live_fold(session_id, scope, as_of=None):
            out[s.kind] = out.get(s.kind, 0) + s.token_count
        return out

    # ---- lifecycle (keep the lane-scope cache re-derivable) ------------

    def drop_scope(self, session_id: str, scope: str) -> int:
        """Erase a scope; if it is a content-lane partition, forget it in the cache."""
        if is_ws_content_scope(scope):
            with self._lane_cache_lock:
                cached = self._lane_cache.get(session_id)
                if cached is not None:
                    cached.discard(scope)
        return super().drop_scope(session_id, scope)

    def release(self, session_id: str) -> int:
        """Drop a session's in-memory scopes and its lane-scope cache entry (the cache
        re-derives by scan on next access)."""
        with self._lane_cache_lock:
            self._lane_cache.pop(session_id, None)
        return super().release(session_id)

    def clear(self) -> None:
        """Drop all in-memory scope state and the whole lane-scope cache."""
        with self._lane_cache_lock:
            self._lane_cache.clear()
        super().clear()
