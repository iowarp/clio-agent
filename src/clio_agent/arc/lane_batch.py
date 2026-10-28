"""Write a batch of atoms into an unpublished lane generation, write-through.

clio-core is the record: a batch (a whole-lane replace writing its new generation)
stages each chunk's atoms in a local list -- the chunk's committed copy plus the new
atoms -- and commits that list to clio-core before the cache holds it
(:meth:`~clio_agent.arc.segment_cache.ScopeCache._commit_scope`). A refused put leaves
the cache at the committed chunk, exactly like every other segment write; nothing
appended to a cached list is ever visible before clio-core accepts it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from clio_agent.arc.lane_chunking import chunk_for_append
from clio_agent.arc.schema import Segment
from clio_agent.arc.segment_errors import ArcPersistError

if TYPE_CHECKING:
    from clio_agent.arc.segments import SegmentStore

__all__ = ["commit_lane_batch"]

# (chunk scope, content, order) -> the new atom, built but not cached or stored.
AtomBuilder = Callable[[str, Any, float], Segment]


def commit_lane_batch(
    store: "SegmentStore",
    session_id: str,
    lane: str,
    contents: Sequence[Any],
    *,
    capacity: int,
    build: AtomBuilder,
) -> list[Segment]:
    """Append ``contents`` to ``lane``'s chunks with one clio-core put per chunk.

    Each chunk is held under its own scope lock from the read of its committed copy to
    its commit, so a release of the session cannot drop the copy the batch builds on.
    Each atom's ``order`` continues the chunk's committed maximum.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        lane: The (unpublished) generation base the batch writes.
        contents: The atom payloads, in order.
        capacity: Segments per chunk before the writer rolls to the next.
        build: Builds one atom for a chunk at an order (never caches or stores it).

    Returns:
        The committed atoms, in order.

    Raises:
        ArcPersistError: clio-core refused a chunk; the cache holds only what it took.
    """
    appended: list[Segment] = []
    chunk: str | None = None
    held: Any = None
    staged: list[Segment] = []
    fresh: list[Segment] = []
    order = 0.0
    try:
        for content in contents:
            selected = chunk_for_append(store, session_id, lane, capacity=capacity)
            if selected != chunk:
                if chunk is not None:
                    _commit_chunk(store, session_id, chunk, staged, fresh)
                    held.release()
                    held = None
                held = store._lock_for(session_id, selected)
                held.acquire()
                chunk = selected
                staged = list(store._segs(session_id, chunk))
                fresh = []
                order = max((segment.order for segment in staged), default=0.0) + 1.0
            seg = build(chunk, content, order)
            order += 1.0
            staged.append(seg)
            fresh.append(seg)
            appended.append(seg)
        if chunk is not None:
            _commit_chunk(store, session_id, chunk, staged, fresh)
    finally:
        if held is not None:
            held.release()
    return appended


def _commit_chunk(
    store: "SegmentStore",
    session_id: str,
    chunk: str,
    staged: list[Segment],
    fresh: list[Segment],
) -> None:
    """Commit one staged chunk (clio-core first, then the cache) and index its new atoms.

    On a refused put the cached copy is dropped too, so the lane's writer cursor (which
    counted the refused atoms) recovers from clio-core; the typed error propagates to
    the generation's rollback."""
    try:
        store._commit_scope(session_id, chunk, staged)
    except ArcPersistError:
        store._discard_scope(session_id, chunk)
        raise
    for seg in fresh:
        store._index.add(session_id, chunk, seg)
