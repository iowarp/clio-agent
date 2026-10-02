"""The model-context projection: which ledger rows a summarizer prompt sees.

A compaction is recorded in the transcript as a summarization injection
(:mod:`clio_agent.gact.summarization_record`) -- mid-turn inside that turn's assistant
message, between turns as its own row. History is retained in full for display; a row
holding a record is a checkpoint here, and the rows it stands in for (its
``compacted_message_ids``) leave the summarizer-facing context.

The rule is COVERAGE, not position: a record mid-turn sits inside the row that also
holds that turn's later steps and answer, so a ``rows[start:]`` slice would be wrong.
Coverage keeps the latest checkpoint row plus every row it does not name.

Coverage is TRANSITIVE across repeated compaction: a second record's
``compacted_message_ids`` includes the first record's row, so every row the first one
covered is excluded too.
"""

from __future__ import annotations

from typing import Any, Mapping

from clio_agent.gact.summarization_record import row_summarization

__all__ = ["model_context_messages"]


def _get(row: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-dict field read: ledger rows are Pydantic models on the live
    path but plain dicts on some legacy/import paths."""

    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _row_id(row: Any) -> str:
    return str(_get(row, "id", "") or "")


def _covered_ids(row: Any) -> list[str]:
    record = row_summarization(row)
    return list(record.compacted_message_ids) if record is not None else []


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
    checkpoint_indices = [i for i, row in enumerate(rows) if row_summarization(row) is not None]
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
