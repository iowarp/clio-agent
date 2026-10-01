"""The record of a compaction: one ``injection`` part with ``source: "summarization"``.

A compaction changes only what the model sees, so the transcript records it as what it
is to the agent: harness data the agent was given (an injection, shown with a syringe).
Its ``text`` is exactly the summary text the model gets (ending with one line that names
the recall tool), ``trigger`` says who asked (``auto`` | ``manual``), ``compaction_id``
correlates it with the ``compaction.*`` events, and its metadata keeps the replaced
clio-core ids (``derived_from``) and the transcript rows it stands in for in a summarizer
prompt (``compacted_message_ids``).

A compaction that fails is recorded where it happened too, as a ``notice`` part
(``source: "compaction_failed"``, plain-language ``text``, the error ``code``,
``compaction_id``, ``trigger``): a UI/provenance record the model is never told.

The ``compaction`` part this replaces is never written any more. A stored transcript
that still holds one is read through :func:`as_summarization` (every reader) and
projected by :func:`legacy_compaction_block` (the v3 wire), so an old session renders
exactly like a new one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.injection_parts import injection_part
from clio_agent.gact.parts import Part

__all__ = [
    "COMPACTION_FAILED_SOURCE",
    "RECALL_TOOL",
    "SUMMARIZATION_SOURCE",
    "SummarizationRecord",
    "as_summarization",
    "failure_notice_part",
    "is_compaction_notice",
    "is_compaction_row",
    "legacy_compaction_block",
    "recall_line",
    "row_summarization",
    "summarization_part",
]

#: The injection ``source`` of a compaction record.
SUMMARIZATION_SOURCE = "summarization"
#: The ``notice`` source of a failed compaction's record.
COMPACTION_FAILED_SOURCE = "compaction_failed"
#: The agent-callable tool that returns what a summary replaced.
RECALL_TOOL = "recall_context"


@dataclass(frozen=True)
class SummarizationRecord:
    """One compaction record as every reader sees it (new or legacy part).

    Attributes:
        part_id: The record part's id.
        text: The summary text the model got.
        trigger: ``"auto"`` or ``"manual"``.
        compaction_id: The compaction's id (legacy parts: their memory event id).
        derived_from: The clio-core ids the summary replaced (empty for legacy parts).
        compacted_message_ids: The transcript rows it stands in for.
    """

    part_id: str
    text: str
    trigger: str
    compaction_id: str
    derived_from: tuple[str, ...]
    compacted_message_ids: tuple[str, ...]


def recall_line(compaction_id: str) -> str:
    """The last line of every summary: how the agent gets the originals back."""
    return (
        f'[The steps this summary replaced are kept in full: {RECALL_TOOL}(ids=["'
        f'{compaction_id}"]) returns them byte-exact; {RECALL_TOOL}(query="...") searches '
        "them.]"
    )


def summarization_part(
    text: str,
    *,
    trigger: str,
    compaction_id: str,
    derived_from: Sequence[str],
    compacted_message_ids: Sequence[str],
    agent_id: str = "",
) -> Part:
    """Build the record part for one compaction (``text``: what the model gets)."""
    part = injection_part(SUMMARIZATION_SOURCE, text, agent_id=agent_id)
    part.trigger = trigger
    part.compaction_id = compaction_id
    part.metadata["derived_from"] = list(derived_from)
    part.metadata["compacted_message_ids"] = list(compacted_message_ids)
    return part


def failure_notice_part(
    text: str, *, code: str, compaction_id: str, trigger: str, agent_id: str = ""
) -> Part:
    """Build the ``notice`` that records a failed compaction (never shown to the model)."""
    return Part(
        id=f"notice_{uuid.uuid4().hex[:12]}",
        type="notice",
        agent_id=agent_id,
        source=COMPACTION_FAILED_SOURCE,
        text=text,
        code=code,
        compaction_id=compaction_id,
        trigger=trigger,
        metadata={"actor": "algorithm"},
    )


def _get(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def as_summarization(part: Any) -> SummarizationRecord | None:
    """The compaction record a part holds, or ``None`` (a model or dict part)."""
    kind = _get(part, "type", "")
    metadata = _get(part, "metadata", None) or {}
    if kind == "injection" and _get(part, "source", "") == SUMMARIZATION_SOURCE:
        return SummarizationRecord(
            part_id=str(_get(part, "id", "") or ""),
            text=str(_get(part, "text", "") or ""),
            trigger=str(_get(part, "trigger", "") or ""),
            compaction_id=str(_get(part, "compaction_id", "") or ""),
            derived_from=tuple(str(i) for i in metadata.get("derived_from") or []),
            compacted_message_ids=tuple(
                str(i) for i in metadata.get("compacted_message_ids") or []
            ),
        )
    if kind == "compaction":  # stored before the summarization record replaced it
        return SummarizationRecord(
            part_id=str(_get(part, "id", "") or ""),
            text=str(_get(part, "summary", "") or ""),
            trigger="auto" if _get(part, "auto", False) else "manual",
            compaction_id=str(metadata.get("memory_event_id") or ""),
            derived_from=(),
            compacted_message_ids=tuple(
                str(i) for i in _get(part, "compacted_message_ids", None) or []
            ),
        )
    return None


def row_summarization(row: Any) -> SummarizationRecord | None:
    """The latest compaction record in a transcript row, or ``None``."""
    found = None
    for part in _get(row, "parts", None) or []:
        record = as_summarization(part)
        if record is not None:
            found = record
    return found


def is_compaction_notice(part: Any) -> bool:
    """Whether a part is a failed compaction's notice."""
    return (
        _get(part, "type", "") == "notice" and _get(part, "source", "") == COMPACTION_FAILED_SOURCE
    )


def is_compaction_row(row: Any) -> bool:
    """Whether a row is a between-turns compaction record or failure notice."""
    parts = list(_get(row, "parts", None) or [])
    return bool(parts) and all(
        as_summarization(part) is not None or is_compaction_notice(part) for part in parts
    )


def legacy_compaction_block(part: Mapping[str, Any]) -> dict[str, Any]:
    """A stored ``compaction`` part as the summarization injection it stands for.

    Returns the part dict an ``injection`` part would have, so the v3 projection
    renders it through the one injection path.
    """
    record = as_summarization(part)
    if record is None:
        raise ValueError(f"not a compaction part: {part.get('type')!r}")
    projected = {k: v for k, v in part.items() if k not in {"summary", "auto"}}
    projected.pop("compacted_message_ids", None)
    return {
        **projected,
        "type": "injection",
        "source": SUMMARIZATION_SOURCE,
        "text": record.text,
        "trigger": record.trigger,
        "compaction_id": record.compaction_id,
    }
