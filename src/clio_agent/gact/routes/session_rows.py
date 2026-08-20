"""Session-list row helpers for the ``/v1/sessions`` route.

Everything that shapes stored session rows into a listing response: the
archive-bucket (gact-tui audit E-14) and fork-lineage (#232) filters, and the
stored-row -> wire-model conversion.

Extracted from :mod:`clio_agent.gact.routes.sessions` to keep that route module
under its size ratchet (#774), following the ``routes/mcp_rows.py`` precedent
(#1111). Deliberately not in ``gact/sessions.py``: that module owns the
*stored* ``Session`` dataclass, and this converts to the *wire* ``Session`` —
same name, different type.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Optional, Sequence, TypeVar

from pydantic import ValidationError

from clio_agent.gact.types import Session

__all__ = ["filter_session_rows", "rows_to_wire"]

logger = logging.getLogger(__name__)


def rows_to_wire(rows: Iterable[Any]) -> list[Session]:
    """Convert stored session rows to wire models, one row at a time.

    Deliberately NOT a list comprehension. It was one, and that is why #1171
    returned 500 for the ENTIRE listing when a single stored row carried a mode
    the wire model no longer accepted: the exception escaped the comprehension
    and took every other session with it.

    A row that still cannot be built after normalization is omitted and logged
    rather than failing the request. Losing one session from a listing is
    recoverable; losing all of them is not.
    """

    out: list[Session] = []
    for row in rows:
        try:
            out.append(Session(**row.to_wire()))
        except ValidationError as exc:
            errors = exc.errors()
            logger.warning(
                "session_row_unreadable id=%s reason=%s (row omitted from listing)",
                getattr(row, "id", "<unknown>"),
                errors[0].get("msg") if errors else exc,
            )
    return out


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
