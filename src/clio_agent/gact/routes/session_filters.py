"""Session-list filtering predicates for the ``/v1/sessions`` route.

Extracted from :mod:`clio_agent.gact.routes.sessions` to keep that route module
under its size ratchet (#774): the archive-bucket (gact-tui audit E-14) and
fork-lineage (#232) filters live here as a small, independently-testable leaf
rather than inline in the already-large handler.
"""

from __future__ import annotations

from typing import Optional, Sequence, TypeVar

_Row = TypeVar("_Row")


def filter_session_rows(
    rows: Sequence[_Row],
    *,
    archived: Optional[bool],
    parent_session_id: Optional[str],
) -> list[_Row]:
    """Apply the archive-bucket and fork-lineage filters to session rows.

    Both attributes are read with ``getattr`` defaults so rows lacking them
    simply do not match (defensive, matching the historical ``archived`` handling).

    Args:
        rows: Session rows exposing ``archived`` / ``parent_session_id`` attributes.
        archived: ``None`` (default) or ``False`` → active-only; ``True`` →
            archived-only. Mirrors ``?archived=`` (gact-tui audit E-14).
        parent_session_id: When non-empty, restrict to sessions whose
            ``parent_session_id`` equals it — the direct sub-sessions / forks of
            that parent (#232). Omitted/empty leaves the set unchanged.

    Returns:
        The filtered rows, preserving order.
    """

    if archived is None:
        rows = [r for r in rows if not getattr(r, "archived", False)]
    else:
        rows = [r for r in rows if bool(getattr(r, "archived", False)) == bool(archived)]
    if parent_session_id:
        rows = [r for r in rows if getattr(r, "parent_session_id", "") == parent_session_id]
    return list(rows)
