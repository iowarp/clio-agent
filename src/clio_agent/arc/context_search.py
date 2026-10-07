"""The search surface of the working-set fold (§2.7): companion chunks resolved to atoms.

Mixed into :class:`~clio_agent.arc.working_set_fold.FoldingSegmentStore`: a hit is a
companion chunk (:mod:`clio_agent.arc.search_companion`) ranked by clio-core's BM25; it
is resolved to the atoms it covers, each marked ``compacted`` from the scope's context
view at query time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.arc.search_companion import (
    ContextSearchHit,
    SearchAtom,
    SearchCompanion,
    parse_search_scope,
)
from clio_agent.arc.segments import _SCOPE_SEP, _SLASH_SUB
from clio_agent.arc.storage import ARCStore

if TYPE_CHECKING:
    from clio_agent.arc.context_view import ContextView

__all__ = ["ContextSearch"]


class ContextSearch:
    """Search over every atom the session's agents ever had in context."""

    _store: ARCStore
    _search: SearchCompanion

    if TYPE_CHECKING:

        def _view(self, session_id: str, scope: str) -> ContextView: ...

        @staticmethod
        def _record_name(session_id: str, scope: str) -> str: ...

    @staticmethod
    def scope_of_record(stem: str) -> str:
        """The scope a record name's scope part spells (inverse of ``_record_name``)."""
        return stem.replace(_SLASH_SUB, "/")

    def search_context(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> list[ContextSearchHit]:
        """Rank the session's companion chunks for ``query_text``; each hit lists the
        atoms it covers, retired ones marked ``compacted`` from the scope's view."""
        search = getattr(self._store, "search", None)
        if not callable(search):
            return []
        head = f"{session_id}{_SCOPE_SEP}"
        prefix = self._record_name(session_id, f"_search/{scope_prefix}")
        hits: list[ContextSearchHit] = []
        for record_name, score in search("segments", query_text, name_prefix=prefix, k=k):
            if not record_name.startswith(head):
                continue
            record_scope = self.scope_of_record(record_name[len(head) :])
            parsed = parse_search_scope(record_scope)
            if parsed is None:
                continue
            hits.append(self.resolve_hit(session_id, record_scope, float(score)))
        return hits

    def resolve_hit(self, session_id: str, record_scope: str, score: float) -> ContextSearchHit:
        """A companion chunk as a hit: its atoms, each marked compacted or live now.

        Raises:
            ValueError: ``record_scope`` is not a companion chunk, or it is not stored.
        """
        parsed = parse_search_scope(record_scope)
        if parsed is None:
            raise ValueError(f"not a search companion chunk: {record_scope!r}")
        scope = parsed[0]
        view = self._view(session_id, scope)
        with view.lock:
            atoms = tuple(
                SearchAtom(atom_id, lt, view.is_retired(atom_id, lt))
                for atom_id, lt in self._search.chunk_atoms(session_id, record_scope)
            )
        return ContextSearchHit(scope=scope, score=score, atoms=atoms)

    def search_scopes(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> list[tuple[str, float]]:
        """Rank the session's scopes by their best companion chunk for ``query_text``."""
        best: dict[str, float] = {}
        for hit in self.search_context(session_id, query_text, scope_prefix=scope_prefix, k=k):
            best[hit.scope] = max(best.get(hit.scope, hit.score), hit.score)
        return sorted(best.items(), key=lambda item: -item[1])[:k]
