"""Read one page of a conversation from its message lane by range.

``GET /v1/sessions/{sid}/messages?limit=N`` used to materialize the whole ledger
(every ``message_part`` atom of the conversation, assembled into messages and
installed in the resident set) and then keep the newest ``N``. Opening a long
conversation therefore cost its whole history in time and memory.

:func:`read_message_page` reads the lane from its newest chunk backwards, one
chunk at a time, assembling only what the page needs:

* the lane's chunk count is found by existence probes (``exists`` is one cheap
  size lookup; no chunk body is read to count them);
* chunks the segment store has not loaded are decoded from clio-core directly and
  are NOT cached in it, so reading a page leaves no residency behind (an already
  loaded chunk is read from memory under its scope lock);
* for an older page, chunks newer than the ``before`` message are skipped without
  being assembled, so a deep page costs its own chunks, not the distance to them;
* reading stops as soon as the page is full; the oldest groups of the window are
  set aside because a message can straddle the chunk boundary the window starts
  at (its first atoms are in the older chunk). They are read with the next page.

A conversation already resident (a live turn or stream holds it) is paged from the
resident copy, which also carries in-flight writes. Without a canonical store
(History mode) the caller pages the materialized ledger instead and the audit
records :data:`PAGE_FULL_READ_REASON`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.arc.lane_chunking import chunk_scope
from clio_agent.arc.lane_generations import current_base
from clio_agent.arc.schema import Segment, decode_segments
from clio_agent.gact.part_atoms import (
    MESSAGE_PART_SCOPE,
    fully_retracted,
    group_atoms_in_order,
    reproduce_message_wire,
    retracted_part_ids,
)
from clio_agent.gact.types import Message
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from fastapi import FastAPI

#: The page was served from the whole materialized ledger (no canonical store).
PAGE_FULL_READ_REASON = "transcript_page_full_read"

#: Groups set aside at the start of a window that does not reach the lane's start:
#: the first may be a message whose earlier atoms are in the older chunk, and an
#: atom of another message persisted mid-turn can split one more.
_BOUNDARY_GROUPS = 2


class PageCursorNotFound(LookupError):
    """``before`` names no message of the conversation."""


@dataclass(frozen=True)
class MessagePage:
    """One page, oldest first, and whether older messages remain."""

    messages: list[Message]
    has_more: bool
    source: str  # "resident" | "lane"


def page_of(
    chronological: list[Message], *, limit: int, before: Optional[str], include_system: bool
) -> MessagePage:
    """Page an already materialized chronological list (the resident copy)."""

    rows = chronological
    if before is not None:
        index = _last_index(rows, before)
        if index < 0:
            raise PageCursorNotFound(before)
        rows = rows[:index]
    if not include_system:
        rows = [m for m in rows if m.role != "system"]
    return MessagePage(rows[-limit:], len(rows) > limit, "resident")


def _last_index(rows: list[Message], message_id: str) -> int:
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].id == message_id:
            return index
    return -1


def _chunk_present(store: Any, session_id: str, scope: str) -> bool:
    key = (session_id, scope)
    if key in store._loaded:
        return bool(store._scopes.get(key))
    return bool(store._store.exists("segments", store._record_name(session_id, scope)))


def _chunk_count(store: Any, session_id: str, base: str) -> int:
    """How many chunks the lane has (dense prefix), by existence probes only."""

    if not _chunk_present(store, session_id, chunk_scope(base, 1)):
        return 0
    low, high = 1, 2
    while _chunk_present(store, session_id, chunk_scope(base, high)):
        low, high = high, high * 2
    while high - low > 1:  # low present, high absent
        mid = (low + high) // 2
        if _chunk_present(store, session_id, chunk_scope(base, mid)):
            low = mid
        else:
            high = mid
    return low


def _read_chunk(store: Any, session_id: str, scope: str) -> list[Segment]:
    """One chunk's segments; never installs a cold chunk in the segment store."""

    key = (session_id, scope)
    with store._lock_for(session_id, scope):
        if key in store._loaded:
            return list(store._scopes.get(key) or [])
        raw = store._store.get("segments", store._record_name(session_id, scope))
    return decode_segments(raw) if raw else []


def _assemble(contents: list[dict[str, Any]], *, drop_leading: int) -> list[Message]:
    retracted = retracted_part_ids(contents)
    groups = group_atoms_in_order(contents)[drop_leading:]
    return [
        Message(**reproduce_message_wire(atoms, retracted))
        for atoms in groups
        if not fully_retracted(atoms, retracted)
    ]


def read_lane_page(
    arc: Any,
    session_id: str,
    *,
    limit: int,
    before: Optional[str],
    include_system: bool,
) -> MessagePage:
    """Read the newest ``limit`` messages (older than ``before``) from the lane by range."""

    from clio_agent.gact.workflow_state.state_merge import (  # noqa: PLC0415 - import cycle
        materialize_state_merge_projection,
    )

    store = arc._segments
    lane_lock = store._lock_for(session_id, MESSAGE_PART_SCOPE)
    with lane_lock:  # an in-flight append lands whole before the read
        base = current_base(store, session_id, MESSAGE_PART_SCOPE)
        index = _chunk_count(store, session_id, base)
        segments: list[Segment] = []
        window: list[Message] = []
        reached_start = index == 0
        cursor_found = before is None
        cursor_seen = before is None  # the window holds the cursor message's atoms
        while index >= 1 and not (cursor_found and len(window) > limit):
            chunk = _read_chunk(store, session_id, chunk_scope(base, index))
            index -= 1
            reached_start = index == 0
            if not cursor_seen:
                # Chunks newer than the cursor cannot hold the page: skip them unassembled.
                if not any(str(seg.content.get("message_id") or "") == before for seg in chunk):
                    continue
                cursor_seen = True
            segments = chunk + segments
            live = sorted((s for s in segments if s.status == "live"), key=lambda s: s.logical_time)
            messages = _assemble(
                [s.content for s in live], drop_leading=0 if reached_start else _BOUNDARY_GROUPS
            )
            if before is not None:
                position = _last_index(messages, before)
                if position < 0:
                    continue  # the cursor is older than this window: read further back
                cursor_found = True
                messages = messages[:position]
            window = messages if include_system else [m for m in messages if m.role != "system"]
        if not cursor_found:
            raise PageCursorNotFound(before or "")
    page = window[-limit:]
    materialize_state_merge_projection(arc, session_id, page)
    return MessagePage(page, len(window) > limit or not reached_start, "lane")


def read_message_page(
    app: "FastAPI",
    session_id: str,
    *,
    limit: int,
    before: Optional[str],
    include_system: bool,
) -> Optional[MessagePage]:
    """The page from the resident copy or the lane; ``None`` means page the full ledger.

    Runs off the event loop (it may wait for the lane lock and read clio-core).
    Raises :class:`PageCursorNotFound` for an unknown ``before``.
    """

    from clio_agent.gact.transcript_file import _canonical_log_arc  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415

    get_if_resident = getattr(app.state.messages, "get_if_resident", None)
    resident = get_if_resident(session_id) if callable(get_if_resident) else None
    if resident is not None:
        return page_of(list(resident), limit=limit, before=before, include_system=include_system)
    if canonical_log_arc(app) is None and getattr(app.state, "arc", None) is None:
        stream_audit(
            PAGE_FULL_READ_REASON,
            reason=PAGE_FULL_READ_REASON,
            session_id=session_id,
            detail="no canonical transcript store is bound; the whole ledger is paged",
        )
        return None
    arc = _canonical_log_arc(app, session_id)  # typed not-ready errors while attaching
    page = read_lane_page(
        arc, session_id, limit=limit, before=before, include_system=include_system
    )
    history = getattr(app.state, "task_history", None)
    if history is not None:
        from clio_agent.gact.session_store import project_committed_feedback  # noqa: PLC0415

        project_committed_feedback(history, session_id, page.messages)
    return page


__all__ = [
    "PAGE_FULL_READ_REASON",
    "MessagePage",
    "PageCursorNotFound",
    "page_of",
    "read_lane_page",
    "read_message_page",
]
