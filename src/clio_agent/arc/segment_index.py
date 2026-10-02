"""SegmentIndex: the per-scope logical-time locator of :class:`~clio_agent.arc.segments.SegmentStore`.

Kept in its own owner module so ``segments.py`` stays the store itself; re-exported
from ``clio_agent.arc.segments`` for its existing importers.
"""

from __future__ import annotations

from sortedcontainers import SortedDict

from clio_agent.arc.schema import Segment

__all__ = ["SegmentIndex"]


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

    def drop_scope(self, session_id: str, scope: str) -> None:
        """Forget a single scope's locator (mirrors SegmentStore.drop_scope)."""
        self._by_scope.pop((session_id, scope), None)

    def clear(self) -> None:
        self._by_scope.clear()
