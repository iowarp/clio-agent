"""The model-context projection: which ledger rows a compaction/summarizer prompt sees.

#1339: compaction became an appended checkpoint (a ``compaction`` part on an ordinary
ledger row) rather than a destructive ledger replace. History is retained in full for
display, but the MODEL-facing context must still shrink at a checkpoint -- otherwise
"compaction" never bounds anything. This module owns that one query.

The rule is COVERAGE, not position. A naive ``rows[start:]`` slice at the latest
checkpoint's index would drop the compacting turn's own assistant answer -- with
turn-boundary placement (a checkpoint is never inserted ahead of an in-flight
assistant message; see :mod:`clio_agent.gact.compaction`) that answer lands BEFORE
the checkpoint it was summarised by, and the checkpoint's summary does not include
it either (the answer had not been produced yet when the checkpoint's transcript was
built). Coverage keeps exactly the rows the latest checkpoint's own
``compacted_message_ids`` name, plus everything else, so nothing in the model context
is silently dropped by position.

Coverage is TRANSITIVE across repeated compaction: a second checkpoint's
``compacted_message_ids`` includes the first checkpoint's own row id (it was part of
that checkpoint's model context at the time), so the first checkpoint's row is
excluded by the transitive closure below even though its own contents were never
directly named by the second checkpoint's id list -- and so is every row the first
checkpoint had already excluded.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["model_context_messages"]


def _get(row: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-dict field read: ledger rows are Pydantic models on the live
    path but plain dicts on some legacy/import paths."""

    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _row_id(row: Any) -> str:
    return str(_get(row, "id", "") or "")


def _checkpoint_part(row: Any) -> Optional[Any]:
    """The row's ``compaction`` part, or ``None`` when the row is not a checkpoint."""

    for part in _get(row, "parts", []) or []:
        if _get(part, "type", "") == "compaction":
            return part
    return None


def _covered_ids(row: Any) -> list[str]:
    part = _checkpoint_part(row)
    if part is None:
        return []
    return [str(i) for i in (_get(part, "compacted_message_ids", []) or [])]


def model_context_messages(messages: Any) -> list[Any]:
    """The rows a summarizer/compaction prompt should see: coverage-keyed on the
    LATEST checkpoint, transitively resolved through any earlier checkpoint it covers.

    No checkpoint row present -> the whole list (compaction has never run).

    Args:
        messages: The session's full ledger, in append order. Rows may be
            ``Message`` models or plain dicts (attribute-or-dict tolerant).

    Returns:
        The subset of ``messages``, in original ledger order, that make up the
        current model context: the latest checkpoint row plus every row not
        transitively covered by it.
    """

    rows = list(messages)
    checkpoint_indices = [i for i, row in enumerate(rows) if _checkpoint_part(row) is not None]
    if not checkpoint_indices:
        return rows

    latest_idx = checkpoint_indices[-1]
    by_id = {_row_id(row): row for row in rows}

    excluded: set[str] = set()
    frontier: set[str] = set(_covered_ids(rows[latest_idx]))
    while frontier:
        excluded |= frontier
        next_frontier: set[str] = set()
        for row_id in frontier:
            row = by_id.get(row_id)
            if row is None:
                continue
            for covered_id in _covered_ids(row):
                if covered_id not in excluded:
                    next_frontier.add(covered_id)
        frontier = next_frontier

    return [row for i, row in enumerate(rows) if i == latest_idx or _row_id(row) not in excluded]
