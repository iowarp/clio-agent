"""Preserve durable A2UI state across transcript replacement operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from clio_agent.gact.types import Message


def preserve_a2ui(
    session_id: str,
    retained_messages: list[Message],
    removed_messages: list[Message],
    operation: str,
) -> list[Message]:
    """Keep A2UI parts when transcript prose is compacted or rewound.

    A2UI is transcript-owned. Removing its parts would otherwise delete a ready
    surface without an A2UI lifecycle event or typed degradation. Preserved
    parts retain their original ids and order in one synthetic assistant
    message; already-retained parts are never duplicated.
    """
    retained_part_ids = {
        part.id for message in retained_messages for part in message.parts if part.id
    }
    preserved_parts = []
    for message in removed_messages:
        for part in message.parts:
            if part.type != "a2ui" or (part.id and part.id in retained_part_ids):
                continue
            preserved_parts.append(part.model_copy(deep=True))
            if part.id:
                retained_part_ids.add(part.id)
    if not preserved_parts:
        return list(retained_messages)

    now = datetime.now(timezone.utc).isoformat()
    preservation_message = Message(
        id=f"msg_a2ui_preserved_{uuid.uuid4().hex}",
        session_id=session_id,
        role="assistant",
        created_at=now,
        updated_at=now,
        parts=preserved_parts,
        metadata={
            "synthetic": "a2ui_preservation",
            "preserved_by": operation,
        },
    )
    return [*retained_messages, preservation_message]
