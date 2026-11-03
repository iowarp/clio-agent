"""A new Claude Code conversation on a pooled CLI client starts with ``/clear``.

One ``ClaudeSDKClient`` holds one conversation. A query's ``session_id`` tags the
message; it does not open another history. Probe (2026-11-03): a query tagged ``conv-b``
recalled a word given under ``conv-a`` on the same client, reading conv-a's tokens from
the cache. So every request the conversation registry planned as a full send (the
first after a compaction, a prefix mismatch, an eviction) was appended to the whole
history the CLI already held. After a compaction the model read the old history, the
summary and the new context: H5 run ``cmp1`` sent 69k cached plus 45k new tokens on the
step after the fold, against 69k before it. Compaction only ever shrank the context
when a different system prompt happened to force a reconnect.

``/clear`` empties the CLI's conversation without a model call (zero tokens) and the
system prompt stays cached. :func:`clear_conversation` runs it before a full send on a
client that already holds a conversation. A one-shot call (outside an agent loop) runs
on the session's side client (:func:`side_key`), so it neither clears nor joins an
agent's kept conversation.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["CLEAR_COMMAND", "SIDE_SUFFIX", "clear_conversation", "side_key"]

logger = logging.getLogger(__name__)

#: The CLI command that empties its conversation (no model call).
CLEAR_COMMAND = "/clear"
#: Pool-key suffix of a GACT session's side client (one-shot calls).
SIDE_SUFFIX = "#side"


def side_key(gact_session_id: str) -> str:
    """The pool key of ``gact_session_id``'s side client."""
    return f"{gact_session_id}{SIDE_SUFFIX}"


async def clear_conversation(client: Any, session_id: str) -> None:
    """Empty the conversation ``client`` holds (``/clear``), draining its reply."""
    await client.query(CLEAR_COMMAND, session_id=session_id)
    async for _message in client.receive_response():
        pass
    logger.info("claude_code conversation cleared reason=full_send session=%s", session_id)
