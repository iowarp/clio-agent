"""Read stored conversations for search without making them resident (R6).

Search, recall and the Evidence board look across every conversation of a workspace.
Reading them through ``app.state.messages[...]`` would make each one resident (its
ledger and its ARC scopes) and push the conversations people are actually viewing out
of the cache. These helpers read the stored records instead:

* :func:`transcript_may_contain` asks the session's stored message records (raw, not
  decoded, not cached) whether any search term occurs at all, so a session that cannot
  match is never assembled;
* :func:`read_transcript` assembles a session's messages through
  :meth:`~clio_agent.gact.resident_ledgers.ResidentLedgerSet.peek`: a resident ledger
  as is, otherwise read from clio-core and dropped after use.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from clio_agent.arc.lane_chunking import chunk_scope
from clio_agent.arc.lane_generations import current_base
from clio_agent.arc.segment_cache import transient_reads

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message

__all__ = ["read_transcript", "transcript_may_contain"]


def read_transcript(app: "FastAPI", session_id: str) -> list["Message"]:
    """A session's messages, read without making the session resident."""
    messages = app.state.messages
    peek = getattr(messages, "peek", None)
    rows = peek(session_id) if callable(peek) else messages.get(session_id)
    return list(rows or [])


def transcript_may_contain(app: "FastAPI", session_id: str, terms: Sequence[str]) -> bool:
    """Whether any of ``terms`` (lowercase) occurs in the session's stored messages.

    Reads the message-atom chunks' committed record bytes from clio-core (one ``get``
    per chunk, never decoded into messages, never cached) and looks for each term in
    their lowercased text. Message text is stored verbatim, so a part whose lowercased
    text contains a term makes its chunk contain it too: ``False`` means no part can
    match. ``True`` when there is no clio-core store to ask.
    """
    from clio_agent.gact.part_atoms import MESSAGE_PART_SCOPE  # noqa: PLC0415
    from clio_agent.gact.transcript_projection import canonical_log_arc  # noqa: PLC0415

    arc = canonical_log_arc(app)
    if arc is None or not terms:
        return True
    store = arc._segments
    with transient_reads():
        base = current_base(store, session_id, MESSAGE_PART_SCOPE)
    index = 1
    while True:
        raw = store.raw_scope_record(session_id, chunk_scope(base, index))
        if not raw:
            return False
        text = raw.decode("utf-8", "ignore").lower()
        if any(term in text for term in terms):
            return True
        index += 1
