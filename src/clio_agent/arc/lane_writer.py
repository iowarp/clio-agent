"""The content-lane writer of the working-set fold: index, chunk and companion puts.

Mixed into :class:`~clio_agent.arc.working_set_fold.FoldingSegmentStore`. One atom is
written as: the session index first when it changes (it must never list less than is
stored), then the lane chunk and the atom's search-companion chunk together in one
concurrent batch (``ARCStore.put_many``), so an append costs about one round trip.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, cast

from clio_agent.arc.batch_put import BatchPutError, PutRecord
from clio_agent.arc.lane_chunking import chunk_for_append
from clio_agent.arc.lane_index import INDEX_SCOPE, SessionIndex, encode_index, note_atom
from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.schema import Segment, encode_segments
from clio_agent.arc.scope_locks import ScopeLock
from clio_agent.arc.search_companion import SearchCompanion, SearchCompanionError
from clio_agent.arc.segment_index import SegmentIndex
from clio_agent.arc.segments import ArcPersistError, SegmentStore
from clio_agent.arc.storage import ARCStore

if TYPE_CHECKING:
    from clio_agent.arc.working_set_fold import _Session

__all__ = ["LaneWriter"]

# A content-lane partition's writer lock (chunk choice + persist as one step) is this
# pseudo-scope of the store's lock registry; it is never a cached scope.
_WRITER_SCOPE = "\x00writer/"


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
    _sessions_lock: threading.Lock
    _scopes: dict[tuple[str, str], list[Segment]]
    _loaded: set[tuple[str, str]]
    _index: SegmentIndex

    if TYPE_CHECKING:

        def _session(self, session_id: str) -> _Session: ...

        @staticmethod
        def _record_name(session_id: str, scope: str) -> str: ...

        def _lock_for(self, session_id: str, scope: str) -> ScopeLock: ...

        def _segs(self, session_id: str, scope: str) -> list[Segment]: ...

        def _install_committed(
            self, session_id: str, scope: str, staged: list[Segment], nbytes: int
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

    def _write(
        self, session_id: str, partition: str, atom: Segment
    ) -> tuple[str, SearchCompanionError | None]:
        """Persist ONE atom to its partition's active chunk and index its text.

        The index first when it changes (it must never list less than is stored), then
        the lane chunk and the atom's search-companion chunk together in one concurrent
        batch. Never runs ``_finish_write`` (§2.9). Held under the partition's writer
        lock so chunk choice and persist are one step per partition.

        Returns:
            The chunk scope, and the companion's typed failure (the atom itself is
            stored; the caller folds it in, then raises it).

        Raises:
            ArcPersistError: The lane chunk was not stored (nothing to fold in).
        """
        state = self._session(session_id)
        with self._lock_for(session_id, _WRITER_SCOPE + partition):
            with state.lock:
                if partition not in state.index.chunks:
                    self._mark_absent(session_id, partition)
            chunk = chunk_for_append(
                cast("SegmentStore", self), session_id, partition, capacity=_chunk_capacity()
            )
            with state.lock:
                if chunk not in state.index.chunks:
                    self._mark_absent(session_id, chunk)
                if note_atom(state.index, chunk, atom):
                    self._put_index(session_id, state.index)
            with self._lock_for(session_id, chunk):
                staged = [*self._segs(session_id, chunk), atom]
                late = self._put_batch(session_id, chunk, staged, atom)
                self._index.add(session_id, chunk, atom)
        return chunk, late

    def _put_batch(
        self, session_id: str, chunk: str, staged: list[Segment], atom: Segment
    ) -> SearchCompanionError | None:
        """Put the lane chunk and the atom's companion chunk concurrently (under the
        chunk's lock), then cache the chunk clio-core accepted (write-through). A lost
        lane chunk leaves the cache at the committed chunk and raises typed."""
        assert_store_write_off_loop("segments.put", scope=chunk)
        lane = self._record_name(session_id, chunk)
        data = encode_segments(staged)
        late: SearchCompanionError | None = None
        try:
            records = [PutRecord(lane, data), *self._search.stage(session_id, [atom])]
            self._store.put_many("segments", records)
        except BatchPutError as exc:
            if lane in exc.failed:
                self._search.unstage(session_id, atom)
                raise ArcPersistError(session_id, chunk, exc) from exc
            late = SearchCompanionError(session_id, atom.scope, exc)
        except Exception as exc:  # noqa: BLE001 - re-raised typed; the cache is unchanged
            self._search.unstage(session_id, atom)
            raise ArcPersistError(session_id, chunk, exc) from exc
        self._install_committed(session_id, chunk, staged, len(data))
        return late
