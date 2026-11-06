"""The content-lane writer of the working-set fold: index, chunk and companion puts.

Mixed into :class:`~clio_agent.arc.working_set_fold.FoldingSegmentStore`. One atom is
written as: the session index first when it changes (it must never list less than is
stored), then the lane chunk and the atom's search-companion chunk together in one
concurrent batch (``ARCStore.put_many``), so an append costs about one round trip.

The lane chunk is append-only chunk records (:mod:`clio_agent.arc.chunk_records`): an
atom is one raw put of its record at the chunk's stored end. The companion chunk is
still re-put whole: its ``.text`` must stay ONE plain-text blob for the BM25 indexer,
and it is bounded at ``arc.search_chunk_atoms`` (16) atoms, so that re-put is small.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.chunk_records import ChunkMark, plan_write
from clio_agent.arc.fold_sessions import live_session, pinned_session
from clio_agent.arc.lane_chunking import chunks_for_append
from clio_agent.arc.lane_index import INDEX_SCOPE, SessionIndex, encode_index, note_atom
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.arc.scope_locks import ScopeLock, pseudo_scope
from clio_agent.arc.search_companion import SearchCompanion, SearchCompanionError
from clio_agent.arc.segment_index import SegmentIndex
from clio_agent.arc.segments import ArcPersistError, SegmentStore
from clio_agent.arc.storage import ARCStore

if TYPE_CHECKING:
    from clio_agent.arc.context_view import ContextView
    from clio_agent.arc.fold_sessions import FoldSession

__all__ = ["LaneWriter"]

# A content-lane partition's writer lock (chunk choice + persist as one step) is this
# pseudo-scope of the store's lock registry (gone once no writer holds it), plus the
# partition. It is never a cached scope, so a release never waits on a writer.
_WRITER_SCOPE = pseudo_scope("writer/")


def _chunk_capacity() -> int:
    """Atoms per content-lane chunk before the writer rolls (file -> env -> default)."""
    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "arc.ws_chunk_segments",
        env="CLIO_ARC_WS_CHUNK_SEGMENTS",
        default=32,
        cast=conf.as_int,
    )


class LaneWriter:
    """Writes atoms to the content lane (see the module docstring)."""

    _store: ARCStore
    _search: SearchCompanion
    _scopes: dict[tuple[str, str], list[Segment]]
    _loaded: set[tuple[str, str]]
    _index: SegmentIndex

    if TYPE_CHECKING:

        def _session(self, session_id: str) -> FoldSession: ...

        def _view(self, session_id: str, scope: str) -> ContextView: ...

        @staticmethod
        def _record_name(session_id: str, scope: str) -> str: ...

        def _lock_for(self, session_id: str, scope: str) -> ScopeLock: ...

        def _segs(self, session_id: str, scope: str) -> list[Segment]: ...

        def _append_mark(
            self,
            key: tuple[str, str],
            staged: list[Segment],
            extends: list[Segment] | None = None,
        ) -> ChunkMark | None: ...

        def _abandon_chunk(self, session_id: str, scope: str, exc: BaseException) -> None: ...

        def _install_committed(
            self, session_id: str, scope: str, staged: list[Segment], mark: ChunkMark
        ) -> None: ...

    def restore_empty(self, session_id: str, scope: str, segments: list[Segment]) -> bool:
        """Restore a missing scope to the correct canonical storage lane."""
        from clio_agent.arc.context_restore import restore_folded_context, restore_plain_context

        store = cast(Any, self)
        restore = (
            restore_folded_context if store._is_working_set_scope(scope) else restore_plain_context
        )
        return restore(store, session_id, scope, segments)

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

    @contextmanager
    def _write_view(self, session_id: str, scope: str) -> Iterator[ContextView]:
        """The scope's view, locked, for ONE write; the session state stays pinned.

        A release waits until the write ends (:mod:`clio_agent.arc.fold_sessions`), so
        the view the atom folds into, the index it notes and the chunk it lands in all
        belong to the state a reload replaces only after the write.
        """
        with pinned_session(partial(self._session, session_id)):
            view = self._view(session_id, scope)
            with view.lock:
                yield view

    def append_many(
        self,
        session_id: str,
        scope: str,
        rows: Sequence[tuple[SegmentKind, dict[str, Any], int]],
        *,
        step: int = -1,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> list[Segment]:
        """Append ``rows`` (kind, content, token count), in order, as one write.

        A working-set scope gets one ``arc.op`` for all of them and one put per lane
        chunk they land in (one, unless the chunk fills) with their companion records:
        what ``len(rows)`` appends did in ``len(rows)`` ops and puts (an agent step's
        thought, calls and observations; X2: 23-173 ms a step). Other scopes append
        each row as :meth:`append` does.
        """
        store = cast(Any, self)
        if not store._is_working_set_scope(scope):
            return [
                store.append(session_id, scope, kind, content, step=step, token_count=tokens,
                             turn_id=turn_id, expert_span_id=expert_span_id,
                             run_span_id=run_span_id)
                for kind, content, tokens in rows
            ]  # fmt: skip
        from clio_agent.arc.lane_index import ws_content_partition  # noqa: PLC0415

        with self._write_view(session_id, scope) as view:
            base = view.next_order
            atoms = [
                store._make_atom(session_id, scope, kind, content, order=base + number,
                                 step=step, token_count=tokens, turn_id=turn_id,
                                 expert_span_id=expert_span_id, run_span_id=run_span_id)
                for number, (kind, content, tokens) in enumerate(rows)
            ]  # fmt: skip
            if not atoms:
                return []
            store._emit_op(
                "append", session_id, scope, written=atoms, step=step,
                logical_time=atoms[0].logical_time,
            )  # fmt: skip
            late = self._write_atoms(session_id, ws_content_partition(expert_span_id), atoms)
            for atom in atoms:
                view.append(atom)
            if late is not None:
                raise late
        return atoms

    def _write(
        self, session_id: str, partition: str, atom: Segment
    ) -> tuple[str, SearchCompanionError | None]:
        """Persist ONE atom (:meth:`_write_atoms`); its chunk scope and companion failure."""
        chunks: list[str] = []
        late = self._write_atoms(session_id, partition, [atom], chunks)
        return chunks[0], late

    def _write_atoms(
        self,
        session_id: str,
        partition: str,
        atoms: list[Segment],
        chunks: list[str] | None = None,
    ) -> SearchCompanionError | None:
        """Persist atoms, in order, to their partition's active chunk and index their text.

        The index first when it changes (it must never list less than is stored), then
        per chunk the atoms land in, their lane records and search-companion chunks
        together in one concurrent batch. Never runs ``_finish_write`` (§2.9). Held
        under the partition's writer lock so chunk choice and persist are one step per
        partition. ``chunks`` receives each atom's chunk scope.

        Returns:
            The companion's typed failure (the atoms themselves are stored; the caller
            folds them in, then raises it).

        Raises:
            ArcPersistError: A lane chunk was not stored (its atoms are not folded in).
        """
        load = partial(self._session, session_id)
        late: SearchCompanionError | None = None
        with self._lock_for(session_id, _WRITER_SCOPE + partition):
            with live_session(load) as state:
                if partition not in state.index.chunks:
                    self._mark_absent(session_id, partition)
            placed = chunks_for_append(
                cast("SegmentStore", self), session_id, partition, len(atoms),
                capacity=_chunk_capacity(),
            )  # fmt: skip
            # Pinned (fold_sessions): a release waits for this write, so the index change
            # lands in the state a reload reads only after the put below.
            with live_session(load) as state:
                changed = False
                for chunk, atom in zip(placed, atoms, strict=True):
                    if chunk not in state.index.chunks:
                        self._mark_absent(session_id, chunk)
                    changed = note_atom(state.index, chunk, atom) or changed
                if changed:
                    self._put_index(session_id, state.index)
            for chunk in dict.fromkeys(placed):
                group = [atom for at, atom in zip(placed, atoms, strict=True) if at == chunk]
                with self._lock_for(session_id, chunk):
                    staged = [*self._segs(session_id, chunk), *group]
                    late = self._put_batch(session_id, chunk, staged, group) or late
                    for atom in group:
                        self._index.add(session_id, chunk, atom)
        if chunks is not None:
            chunks.extend(placed)
        return late

    def _put_batch(
        self, session_id: str, chunk: str, staged: list[Segment], atoms: list[Segment]
    ) -> SearchCompanionError | None:
        """Put the atoms' lane records (appended at the chunk's stored end, or the whole
        chunk on its first write) and their companion chunks concurrently (under the
        chunk's lock), then cache the chunk clio-core accepted (write-through). A lost
        lane record raises typed: a lost first write leaves the cache at the committed
        chunk, a lost append also abandons the chunk (the next atom rolls to a new one)."""
        assert_store_write_off_loop("segments.put", scope=chunk)
        lane = self._record_name(session_id, chunk)
        mark = self._append_mark((session_id, chunk), staged)
        write = plan_write(staged, mark)
        late: SearchCompanionError | None = None
        try:
            stage = self._search.stage(session_id, atoms)
            records = [PutRecord(lane, write.data, offset=write.offset), *stage]
            self._store.put_many("segments", records)
        except BatchPutError as exc:
            if lane in exc.failed:
                self._lane_lost(session_id, chunk, atoms, mark, exc)
                raise ArcPersistError(session_id, chunk, exc) from exc
            late = SearchCompanionError(session_id, atoms[0].scope, exc)
        except Exception as exc:  # noqa: BLE001 - re-raised typed; the cache is unchanged
            self._lane_lost(session_id, chunk, atoms, mark, exc)
            raise ArcPersistError(session_id, chunk, exc) from exc
        self._install_committed(session_id, chunk, staged, write.committed(staged))
        return late

    def _lane_lost(
        self,
        session_id: str,
        chunk: str,
        atoms: list[Segment],
        mark: ChunkMark | None,
        exc: BaseException,
    ) -> None:
        """The atoms' lane record was not stored: take them back from the companion, and
        abandon the chunk when the lost put was an append that may still land."""
        for atom in atoms:
            self._search.unstage(session_id, atom)
        if mark is not None:
            self._abandon_chunk(session_id, chunk, exc)
