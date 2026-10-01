"""FoldingSegmentStore: the working set as a FOLD of the canonical ``_events`` log.

The #737 S2 slice collapses the dual ARC: the loop's content atoms are the ONLY copy,
appended to the canonical ``_events`` log, and the working-set reads are a **fold** of
that log (design ``docs/design/unified-arc-highway.md`` §2.8b, §2.9, §2.10). Phase 11a
keeps that fold as a **context view** per (session, logical scope) so neither a read
nor an append costs O(history):

* **Raw append lane + separate op-emit (§2.9).** Atoms are persisted through the chunk
  writer below, which NEVER runs ``_finish_write`` -- so the persist itself does not
  re-form the ``record -> op_logger -> arc.op -> record`` recursion. The ``op_logger``
  is invoked SEPARATELY via :meth:`FoldingSegmentStore._emit_op` on the LOGICAL scope,
  because the ``arc.op`` durable stream is a FROZEN reproducibility contract: replay
  rebuilds the live plane byte-identically from ``arc.op`` events alone.
* **Span-partitioned, chunked (§2.10).** Atoms live in the ``_events/w/<span>`` lane
  (search-excluded, lifecycle-erased with the log), partitioned by ``expert_span_id``
  and chunked with :func:`~clio_agent.arc.lane_chunking.chunk_for_append` (capacity
  ``arc.ws_chunk_segments``), so an append re-puts one bounded chunk. Each atom carries
  its LOGICAL scope in ``Segment.scope``.
* **The session index** (:mod:`clio_agent.arc.lane_index`, ``_events/w/_index``) lists
  the chunks, the scopes each holds and each scope's anchor. A cold read gets it, then
  only the chunks holding the scope's atoms from its anchor on -- no store scan. A
  session stored before the index is migrated once (:mod:`clio_agent.arc.lane_migration`).
* **The context view** (:mod:`clio_agent.arc.context_view`): appends extend it, ops
  rebuild it with the ONE fold (:func:`~clio_agent.arc.context_view.fold_atoms`) over
  the atoms from the anchor on; a summarize that retires everything before it moves the
  anchor. ``as_of`` and tombstone (history) reads fold the scope's whole log.
* **Op records, append-only (§2.5).** A ``delete`` is an appended ``ws_op`` atom
  ``{op, targets}``; ``summarize`` / ``replace`` retire the ids in their producer's
  ``derived_from``. Stored content is never rewritten, so as-of-T replay is exact.
* **Byte-exact ``order`` (§4.1.A).** Each content atom carries the scope-local ``order``
  the plain :class:`~clio_agent.arc.segments.SegmentStore` would give it.
* **Search companion (§2.7)** (:mod:`clio_agent.arc.search_companion`): append-only
  per-scope text chunks over every atom ever written; retired atoms are marked at query
  time. A companion write clio-core refuses is a typed write failure.

Every reserved-scope call (the semantic-event log itself) is delegated to ``super()``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import msgspec

from clio_agent import conf
from clio_agent.arc.context_search import ContextSearch
from clio_agent.arc.context_view import (
    STEP_OPEN_KIND,
    WS_OP_KIND,
    AnchorEntry,
    ContextView,
    ViewSnapshot,
    fold_atoms,
)
from clio_agent.arc.lane_chunking import chunk_for_append
from clio_agent.arc.lane_index import (
    INDEX_SCOPE,
    WS_CONTENT_FAMILY,
    SessionIndex,
    chunks_holding,
    encode_index,
    is_ws_content_scope,
    note_atom,
    set_anchor,
    ws_content_partition,
)
from clio_agent.arc.lane_migration import load_or_migrate
from clio_agent.arc.live import is_events_scope
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import WORKING_SET_KINDS, Segment, SegmentKind, segment_text
from clio_agent.arc.search_companion import (
    SearchCompanion,
    is_search_scope,
)
from clio_agent.arc.segment_ids import ContextOpLogError, require_live
from clio_agent.arc.segments import ArcPersistError, SegmentStore
from clio_agent.arc.segments import _coerce_content as _coerce_content
from clio_agent.arc.storage import ARCStore

logger = logging.getLogger(__name__)

__all__ = [
    "STEP_OPEN_KIND",
    "WS_CONTENT_FAMILY",
    "WS_OP_KIND",
    "FoldingSegmentStore",
    "emit_step_open",
    "is_ws_content_scope",
    "make_segment_store",
    "ws_content_partition",
]


def _chunk_capacity() -> int:
    """Atoms per content-lane chunk before the writer rolls (file -> env -> default)."""
    return conf.resolve(
        "arc.ws_chunk_segments",
        env="CLIO_ARC_WS_CHUNK_SEGMENTS",
        default=32,
        cast=conf.as_int,
    )


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
        logger.warning(
            "working_set_fold: step_open breadcrumb failed scope=%s", scope, exc_info=True
        )


def make_segment_store(
    store: ARCStore,
    *,
    search_indexed: Callable[[str], bool] | None = None,
    working_set_fold: bool | None = None,
) -> SegmentStore:
    """Construct the live-plane segment store, folding or not per the S2 flag.

    The fold is THE production working-set semantics. ``working_set_fold=False``
    remains ONLY for the dual-run equivalence harness, which proves the fold against
    the plain store.

    Args:
        store: The persistence backend.
        search_indexed: Optional scope-search predicate (defaults to excluding the
            reserved ``_events`` family).
        working_set_fold: Force the regime; ``None`` folds.

    Returns:
        A :class:`FoldingSegmentStore` when the fold is on, else a plain
        :class:`SegmentStore`.
    """
    predicate = search_indexed or _default_search_indexed
    if working_set_fold is False:
        return SegmentStore(store, search_indexed=predicate)
    return FoldingSegmentStore(store, search_indexed=predicate)


@dataclass
class _Session:
    """One session's index and views (in memory, re-derivable from clio-core)."""

    index: SessionIndex
    lock: threading.RLock = field(default_factory=threading.RLock)
    views: dict[str, ContextView] = field(default_factory=dict)


class FoldingSegmentStore(ContextSearch, SegmentStore):
    """A :class:`SegmentStore` whose working set is a fold of the canonical log.

    Working-set-scope writes (any scope outside the reserved ``_events`` family) go to
    the ``_events/w`` content lane; working-set reads come from the scope's context
    view. Every reserved-scope call is delegated to ``super()`` unchanged.
    """

    def __init__(self, store: ARCStore, *, search_indexed: Any = None) -> None:
        """Back the folding store with an :class:`ARCStore`.

        Args:
            store: The persistence backend, shared verbatim with the base store.
            search_indexed: Optional ``scope -> bool`` predicate for the base store's
                own companion (the content lane is an ``_events`` scope: excluded).
        """
        super().__init__(store, search_indexed=search_indexed)
        self._search = SearchCompanion(store, self._record_name)
        self._sessions: dict[str, _Session] = {}
        self._session_locks: dict[str, threading.Lock] = {}
        self._sessions_lock = threading.Lock()
        # One writer per content-lane partition (chunk choice + persist as one step).
        # Separate from the base per-scope locks so a writer never holds a scope lock
        # while it takes another (the base ``release`` takes them all under its registry
        # lock).
        self._partition_locks: dict[tuple[str, str], threading.Lock] = {}

    # ---- scope routing -------------------------------------------------

    @staticmethod
    def _is_working_set_scope(scope: str) -> bool:
        """A working-set scope is any non-empty scope OUTSIDE the reserved ``_events``
        family — i.e. the per-expert scopes the loop renders its prompt from."""
        return bool(scope) and not is_events_scope(scope)

    # ---- session index + views -----------------------------------------

    def _session(self, session_id: str) -> _Session:
        """The session's index (read, or migrated once) and views, loaded once."""
        with self._sessions_lock:
            state = self._sessions.get(session_id)
            if state is not None:
                return state
            load_lock = self._session_locks.setdefault(session_id, threading.Lock())
        with load_lock:
            with self._sessions_lock:
                state = self._sessions.get(session_id)
            if state is not None:
                return state
            state = _Session(index=load_or_migrate(self, session_id))
            with self._sessions_lock:
                return self._sessions.setdefault(session_id, state)

    def _view(self, session_id: str, scope: str) -> ContextView:
        """The scope's context view, built on first use from the chunks the index lists."""
        state = self._session(session_id)
        with state.lock:
            view = state.views.get(scope)
            entry = state.index.scopes.get(scope)
            chunks = list(entry.chunks) if entry is not None else []
            anchor = entry.anchor if entry is not None else None
        if view is not None:
            return view
        atoms: list[Segment] = []
        for chunk in chunks:
            with self._lock_for(session_id, chunk):
                atoms.extend(a for a in self._segs(session_id, chunk) if a.scope == scope)
        built = ContextView(scope, anchor, atoms)
        with state.lock:
            return state.views.setdefault(scope, built)

    def _full_atoms(self, session_id: str, scope: str) -> list[Segment]:
        """Every atom of ``scope`` ever written (the as-of / history read)."""
        state = self._session(session_id)
        with state.lock:
            chunks = chunks_holding(state.index, scope)
        atoms: list[Segment] = []
        for chunk in chunks:
            with self._lock_for(session_id, chunk):
                atoms.extend(a for a in self._segs(session_id, chunk) if a.scope == scope)
        return atoms

    def raw_lane_atoms(self, session_id: str) -> list[Segment]:
        """Every atom on the session's content lane, breadcrumbs included (crash reads)."""
        state = self._session(session_id)
        with state.lock:
            chunks = list(state.index.chunks)
        atoms: list[Segment] = []
        for chunk in chunks:
            with self._lock_for(session_id, chunk):
                atoms.extend(self._segs(session_id, chunk))
        return sorted(atoms, key=lambda s: s.logical_time)

    def _put_index(self, session_id: str, index: SessionIndex) -> None:
        """Put the session index record; a refusal is a typed :class:`ArcPersistError`."""
        assert_store_write_off_loop("segments.put", scope=INDEX_SCOPE)
        try:
            self._store.put(
                "segments", self._record_name(session_id, INDEX_SCOPE), encode_index(index)
            )
        except LoopThreadStoreWrite:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised typed; the write failed
            raise ArcPersistError(session_id, INDEX_SCOPE, exc) from exc

    def _mark_absent(self, session_id: str, chunk: str) -> None:
        """A chunk the index does not list is not stored: load it as empty, no get."""
        with self._lock_for(session_id, chunk):
            key = (session_id, chunk)
            if key not in self._loaded:
                self._scopes[key] = []
                self._loaded.add(key)

    def _write(self, session_id: str, partition: str, atom: Segment) -> str:
        """Persist ONE atom to its partition's active chunk; returns the chunk scope.

        Index first (it only ever lists more than is stored), then the chunk. Never
        runs ``_finish_write`` (§2.9). Held under the partition's writer lock so chunk
        choice and persist are one step per partition.
        """
        state = self._session(session_id)
        with self._sessions_lock:
            writer = self._partition_locks.setdefault((session_id, partition), threading.Lock())
        with writer:
            with state.lock:
                if partition not in state.index.chunks:
                    self._mark_absent(session_id, partition)
            chunk = chunk_for_append(self, session_id, partition, capacity=_chunk_capacity())
            with state.lock:
                if chunk not in state.index.chunks:
                    self._mark_absent(session_id, chunk)
                if note_atom(state.index, chunk, atom):
                    self._put_index(session_id, state.index)
            with self._lock_for(session_id, chunk):
                segs = self._segs(session_id, chunk)
                segs.append(atom)
                self._index.add(session_id, chunk, atom)
                self._persist(session_id, chunk)
        return chunk

    def _emit_op(
        self,
        op: str,
        session_id: str,
        scope: str,
        *,
        written: list[Segment] | None = None,
        tombstoned: list[str] | None = None,
        step: int | None = None,
        position: int | None = None,
        derived_from: list[str] | None = None,
        logical_time: int,
    ) -> None:
        """Emit ONE ``arc.op`` durable-trace event for a folded working-set mutation.

        Mirrors :meth:`SegmentStore._finish_write`'s op-logger call so the ``arc.op``
        reproducibility contract holds for the fold. The event's ``event_id`` is stamped
        onto each written atom's ``trace_ref``, so it runs BEFORE the atom is persisted;
        a failure is a typed :class:`ContextOpLogError` with nothing persisted.
        """
        if self._op_logger is None:
            return
        written = written or []
        try:
            event = self._op_logger(
                op,
                session_id,
                scope,
                logical_time=logical_time,
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
        except Exception as exc:  # noqa: BLE001 - re-raised typed; nothing persisted yet
            raise ContextOpLogError(op=op, scope=scope, cause=exc) from exc

    def _make_atom(
        self,
        session_id: str,
        scope: str,
        kind: SegmentKind,
        content: dict[str, Any],
        *,
        order: float,
        step: int,
        trace_ref: str = "",
        derived_from: list[str] | None = None,
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Segment:
        """Build an atom with a store-wide ``logical_time`` and the given scope-local
        ``order`` (content is coerced through the ONE ingest chokepoint)."""
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

    def _record_op(
        self, session_id: str, view: ContextView, op: str, atom: Segment, **emit: Any
    ) -> str:
        """Emit, persist and fold in one op's atom; index its text. Returns its chunk."""
        self._emit_op(op, session_id, view.scope, logical_time=atom.logical_time, **emit)
        chunk = self._write(session_id, ws_content_partition(atom.expert_span_id), atom)
        view.apply_op(atom)
        self._search.add(session_id, [atom])
        return chunk

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
        """Append a working-set atom: O(1) on the view, one bounded chunk put (or
        delegate reserved-scope appends to the base store unchanged)."""
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
        view = self._view(session_id, scope)
        with view.lock:
            atom = self._make_atom(
                session_id,
                scope,
                kind,
                content,
                order=view.next_order,
                step=step,
                trace_ref=trace_ref,
                derived_from=derived_from,
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            if atom.derived_from:  # a producer retires its sources: that is an op
                self._record_op(session_id, view, "append", atom, written=[atom], step=step)
                return atom
            self._emit_op(
                "append",
                session_id,
                scope,
                written=[atom],
                step=step,
                logical_time=atom.logical_time,
            )
            self._write(session_id, ws_content_partition(expert_span_id), atom)
            view.append(atom)
            self._search.add(session_id, [atom])
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
        view = self._view(session_id, scope)
        with view.lock:
            atom = self._make_atom(
                session_id,
                scope,
                kind,
                content,
                order=view.order_for_position(position),
                step=step,
                trace_ref=trace_ref,
                derived_from=derived_from,
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            self._record_op(
                session_id, view, "insert", atom, written=[atom], step=step, position=position
            )
        return atom

    def delete(self, session_id: str, scope: str, ids: list[str]) -> int:
        """Retire live working-set atoms by id via an append-only ``ws_op`` record.

        Only ids that are currently LIVE may be retired (matching
        :meth:`SegmentStore.delete`); the count returned is the number retired.
        """
        if not self._is_working_set_scope(scope):
            return super().delete(session_id, scope, ids)
        view = self._view(session_id, scope)
        with view.lock:
            require_live(ids, {s.id for s in view.live}, op="delete", scope=scope)
            targets = list(dict.fromkeys(ids))
            op = self._make_atom(
                session_id,
                scope,
                WS_OP_KIND,
                {"op": "delete", "targets": list(targets)},
                order=0.0,  # op records are not rendered; order is immaterial
                step=-1,
            )
            self._record_op(session_id, view, "delete", op, tombstoned=list(targets))
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
        """summarize = retire ``ids`` + one ``summary`` atom at the first replaced slot,
        at ONE ``logical_time``. When it retires everything before it, it becomes the
        scope's anchor (recorded in the session index)."""
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
        view = self._view(session_id, scope)
        with view.lock:
            require_live(ids, {s.id for s in view.live}, op="summarize", scope=scope)
            target = set(ids)
            replaced = [s for s in view.live if s.id in target]
            first = min(replaced, key=lambda s: (s.order, s.logical_time))
            step = min((s.step for s in replaced), default=-1)
            atom = self._make_atom(
                session_id,
                scope,
                "summary",
                summary_content,
                order=first.order,
                step=step,
                trace_ref=trace_ref,
                derived_from=list(ids),
                token_count=token_count,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            chunk = self._record_op(
                session_id,
                view,
                "summarize",
                atom,
                written=[atom],
                tombstoned=[s.id for s in replaced],
                step=step,
                derived_from=list(ids),
            )
            if view.anchors_at(atom):
                self._move_anchor(session_id, view, atom, chunk)
        return atom

    def _move_anchor(self, session_id: str, view: ContextView, atom: Segment, chunk: str) -> None:
        """Make ``atom`` the scope's anchor in the index (put) and in the view."""
        if view.min_order is None or view.max_order is None:
            raise ValueError("an anchoring summary is a content atom: the order bounds exist")
        anchor = AnchorEntry(
            atom_id=atom.id,
            logical_time=atom.logical_time,
            chunk=chunk,
            min_order=view.min_order,
            max_order=view.max_order,
        )
        state = self._session(session_id)
        with state.lock:
            set_anchor(state.index, view.scope, anchor)
            self._put_index(session_id, state.index)
        view.move_anchor(anchor)

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
        view = self._view(session_id, scope)
        with view.lock:
            require_live([target_id], {s.id for s in view.live}, op="replace", scope=scope)
            original = next(s for s in view.live if s.id == target_id)
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
            self._record_op(
                session_id,
                view,
                "replace",
                atom,
                written=[atom],
                tombstoned=[original.id],
                step=atom.step,
                derived_from=[original.id],
            )
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
        step's opening atoms on the canonical log. It is NOT renderable content (no
        render, no view, no search). A no-op for reserved scopes.
        """
        if not self._is_working_set_scope(scope):
            return None
        view = self._view(session_id, scope)
        with view.lock:
            atom = self._make_atom(
                session_id,
                scope,
                STEP_OPEN_KIND,
                content,
                order=0.0,
                step=step,
                turn_id=turn_id,
                expert_span_id=expert_span_id,
                run_span_id=run_span_id,
            )
            self._write(session_id, ws_content_partition(expert_span_id), atom)
        return atom

    # ---- overridden read surface ---------------------------------------

    def scan_scopes(self, session_id: str, scope_pattern: str = "") -> list[str]:
        """Scope addresses under a prefix, the fold's INTERNAL records hidden.

        The content lane, its index and the search-companion chunks are implementation
        records, never scopes; the logical working-set scopes come from the session
        index (an ``_events`` pattern never names one, so it skips the index).
        """
        found = {
            s
            for s in super().scan_scopes(session_id, scope_pattern)
            if not is_ws_content_scope(s) and not is_search_scope(s)
        }
        if not is_events_scope(scope_pattern):
            state = self._session(session_id)
            with state.lock:
                found.update(s for s in state.index.scopes if s.startswith(scope_pattern))
        return sorted(found)

    def context_view(self, session_id: str, scope: str) -> ViewSnapshot:
        """The scope's live segments with their view generation."""
        if not self._is_working_set_scope(scope):
            return super().context_view(session_id, scope)
        return self._view(session_id, scope).snapshot()

    def has_segments(self, session_id: str, scope: str) -> bool:
        """Whether anything was ever recorded in the scope (the index knows; no fold)."""
        if not self._is_working_set_scope(scope):
            return super().has_segments(session_id, scope)
        state = self._session(session_id)
        with state.lock:
            return scope in state.index.scopes

    def _read(self, session_id: str, scope: str, as_of: int | None) -> list[Segment]:
        if as_of is None:
            return list(self.context_view(session_id, scope).segments)
        return fold_atoms(
            self._full_atoms(session_id, scope), scope, as_of=as_of, include_tombstoned=False
        )

    def render(self, session_id: str, scope: str, *, as_of: int | None = None) -> list[Segment]:
        """Ordered LIVE view of a scope — the context view, or the whole-log fold as of
        ``as_of``; delegated for reserved ``_events`` scopes."""
        if not self._is_working_set_scope(scope):
            return super().render(session_id, scope, as_of=as_of)
        return self._read(session_id, scope, as_of)

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: int | None = None
    ) -> list[Segment]:
        """The live view restricted to working-set kinds."""
        if not self._is_working_set_scope(scope):
            return super().render_working_set(session_id, scope, as_of=as_of)
        return [s for s in self._read(session_id, scope, as_of) if s.kind in WORKING_SET_KINDS]

    def render_text(
        self,
        session_id: str,
        scope: str,
        *,
        as_of: int | None = None,
        separator: str = "\n",
    ) -> str:
        """The live view flattened to text."""
        if not self._is_working_set_scope(scope):
            return super().render_text(session_id, scope, as_of=as_of, separator=separator)
        return separator.join(segment_text(s) for s in self._read(session_id, scope, as_of))

    def list_segments(
        self, session_id: str, scope: str, *, include_tombstoned: bool = False
    ) -> list[Segment]:
        """A scope's content atoms in render order; ``include_tombstoned`` folds the
        whole log (replay/provenance). Op records and breadcrumbs are never segments."""
        if not self._is_working_set_scope(scope):
            return super().list_segments(session_id, scope, include_tombstoned=include_tombstoned)
        if not include_tombstoned:
            return self._read(session_id, scope, None)
        return fold_atoms(
            self._full_atoms(session_id, scope), scope, as_of=None, include_tombstoned=True
        )

    def tokens_by_kind(self, session_id: str, scope: str) -> dict[str, int]:
        """Sum ``token_count`` of LIVE segments grouped by kind."""
        if not self._is_working_set_scope(scope):
            return super().tokens_by_kind(session_id, scope)
        out: dict[str, int] = {}
        for s in self._read(session_id, scope, None):
            out[s.kind] = out.get(s.kind, 0) + s.token_count
        return out

    # ---- lifecycle -----------------------------------------------------

    def drop_scope(self, session_id: str, scope: str) -> int:
        """Erase a scope; erasing a content-lane chunk drops the session's views (they
        rebuild from clio-core, where the index may now list the erased chunk: empty)."""
        if is_ws_content_scope(scope):
            self._forget(session_id)
        return super().drop_scope(session_id, scope)

    def release(self, session_id: str) -> int:
        """Drop a session's in-memory scopes, index and views (re-read on next access)."""
        self._forget(session_id)
        return super().release(session_id)

    def clear(self) -> None:
        """Drop all in-memory scope state, indexes and views."""
        with self._sessions_lock:
            self._sessions.clear()
        self._search.clear()
        super().clear()

    def _forget(self, session_id: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(session_id, None)
        self._search.forget(session_id)
