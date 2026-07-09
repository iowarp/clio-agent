"""Compaction-message construction for the ``/v1/sessions/{sid}/compact`` route.

Extracted from :mod:`clio_agent.gact.routes.sessions` to keep that route module
under its size ratchet (#774). The client-facing compaction summary is a
structured ``compaction`` part (SPEC §4.5) — ``summary`` / ``auto`` /
``compacted_message_ids`` — not a magic ``[compact summary]`` text prefix, so
both clients render it from typed fields (#832).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from clio_agent.gact.types import Message, Part, Tokens


def build_compact_summary_message(
    *,
    session_id: str,
    turn_id: str,
    summary: str,
    event_id: str,
    compacted_message_ids: list[str],
    auto: bool = False,
) -> Message:
    """Build the assistant message that carries a session's compaction summary.

    The message holds a single structured ``compaction`` part (SPEC §4.5) whose
    ``summary`` is the client-facing prose. The ``synthetic``/``memory_event_id``
    metadata is kept on both the part and the message so metadata consumers keep
    working; only the legacy ``[compact summary]`` text prefix is dropped (#832).

    Args:
        session_id: The session being compacted.
        turn_id: The active semantic turn id to correlate this message with.
        summary: The generated compaction summary (already evidence-augmented).
        event_id: The memory-event id linking this message to its archive entry.
        compacted_message_ids: Ids of the archived ledger messages this summary
            stands in for. May be empty when ids are unavailable.
        auto: ``True`` for a policy-triggered compaction; ``False`` (default) for
            a user-invoked ``POST /v1/sessions/{sid}/compact``.

    Returns:
        The assembled assistant :class:`Message`.
    """

    synthetic_meta = {"synthetic": "compact_summary", "memory_event_id": event_id}
    now = datetime.now(timezone.utc).isoformat()
    return Message(
        id=f"msg_compact_{uuid.uuid4().hex[:10]}",
        turn_id=turn_id,
        session_id=session_id,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=[
            Part(
                id=f"part_compact_{uuid.uuid4().hex[:10]}",
                type="compaction",
                metadata=dict(synthetic_meta),
                summary=(summary or "").strip(),
                auto=auto,
                compacted_message_ids=list(compacted_message_ids),
            )
        ],
        tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
        cost_usd=0.0,
        stop_reason="end_turn",
        metadata=dict(synthetic_meta),
    )
