"""The compact summary's ARC conversation record (#1334 review finding).

``POST /v1/sessions/{sid}/compact`` replaces a session's transcript with one synthetic
summary message and mirrors that summary onto ARC's ``conversations`` record. Both halves
of that mirror are store RPCs -- ``get_conversation`` (read) and ``store_conversation``
(write) -- and they ran inline inside the ``async def compact_session`` handler, i.e. ON
the server loop thread. #1334's inventory moved the ledger write in the very same handler
off the loop (``await run_off_loop(deps.replace_session_messages, ...)``) but left this
pair behind.

That is not merely a latency defect. The ``conversations`` kind does not ride the shared
``segments`` persist seam the loop guard hooks, so the in-memory test backend records no
hit and the regression lock stays green; against the REAL clio-core store the write is
guarded at the RPC seam, ``assert_store_write_off_loop`` raises ``LoopThreadStoreWrite``,
and the route's ``except Exception`` turns the refusal into an HTTP 500
``memory_update_failed`` -- ``POST /compact`` fails outright.

This module owns the whole ARC-side mirror as ONE blocking callable the route awaits
through ``off_loop.run_off_loop``. Separate owner because ``routes/sessions.py`` sits at
its CI file-size ratchet baseline (baselines only move down).
"""

from __future__ import annotations

import time
from typing import Any

__all__ = ["ARC_NOT_CONFIGURED", "ARC_STORED", "store_compact_conversation"]

#: ``arc_status`` values the compact memory event reports (frozen wire surface).
ARC_NOT_CONFIGURED = "not_configured"
ARC_STORED = "stored"


def store_compact_conversation(
    arc: Any,
    session_id: str,
    *,
    summary: str,
    event_id: str,
    archived_count: int,
    clio_agent_version: str,
) -> str:
    """Mirror the compact summary onto ARC's conversation record. Blocking; off-loop only.

    Reads the session's existing conversation (creating one when absent), replaces its
    messages with the single synthetic compact-summary message, and stores it back. The
    record's wire shape is unchanged from the inline version this replaces.

    Args:
        arc: The agent's ARC memory (never ``None`` -- the caller gates on that).
        session_id: The session being compacted.
        summary: The compact summary text.
        event_id: The compact memory event id, stamped onto the summary's metadata.
        archived_count: How many messages the compaction archived.
        clio_agent_version: Installed version, stamped on a freshly-created record.

    Returns:
        :data:`ARC_STORED`.

    Raises:
        Exception: Whatever the ARC store raises -- the caller owns the HTTP envelope.
    """

    from clio_agent.arc.schema import Conversation as ARCConversation  # noqa: PLC0415
    from clio_agent.arc.schema import Message as ARCMessage  # noqa: PLC0415

    now_ts = time.time()
    arc_summary = ARCMessage(
        role="assistant",
        content="[compact summary]\n" + (summary or "").strip(),
        timestamp=now_ts,
        metadata={
            "source": "gact_compact",
            "synthetic": "compact_summary",
            "memory_event_id": event_id,
            "archived_count": archived_count,
        },
    )
    conv = arc.get_conversation(session_id)
    if conv is None:
        conv = ARCConversation(
            session_id=session_id,
            user_id="default_user",
            created_at=now_ts,
            updated_at=now_ts,
            last_accessed=now_ts,
            status="active",
            messages=[arc_summary],
            routing_decisions=[],
            metadata={
                "clio_agent_version": clio_agent_version,
                "arc_enabled": True,
                "compacted_by": "gact",
            },
            storage_tier="warm",
        )
    else:
        conv.messages = [arc_summary]
        conv.updated_at = now_ts
        conv.last_accessed = now_ts
        conv.metadata["compacted_by"] = "gact"
        conv.metadata["compacted_at"] = now_ts
        conv.metadata["archived_message_count"] = archived_count
    arc.store_conversation(conv)
    return ARC_STORED
