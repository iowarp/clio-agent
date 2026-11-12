"""A conversation is named after the first thing the person asked.

Clients create a conversation before its first message with a placeholder
title ("New conversation" in Desktop and Web). Nothing replaced it, so every
conversation in the list kept that name. When a person's message is accepted
while the title is still a placeholder, the server derives a short title from
that message, persists it and publishes ``session.updated`` so every client's
list follows. A title someone chose is never replaced.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from clio_agent.gact.events import Event
from clio_agent.gact.types import Session

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60
_PLACEHOLDER_TITLES = frozenset({"", "new conversation", "untitled session", "untitled"})
_WHITESPACE = re.compile(r"\s+")
_LEADING_MARKUP = re.compile(r"^(?:[#>*\-+]+\s*|\d+[.)]\s+)+")


def is_placeholder_title(title: str, session_id: str) -> bool:
    """Whether ``title`` is a client or server default rather than a chosen name."""
    folded = _WHITESPACE.sub(" ", title).strip().casefold()
    return folded in _PLACEHOLDER_TITLES or folded == f"session {session_id[-6:]}".casefold()


def derive_title(text: str) -> str:
    """The first meaningful line of ``text``, collapsed and cut at a word boundary."""
    line = next(
        (
            cleaned
            for raw in text.splitlines()
            if (cleaned := _LEADING_MARKUP.sub("", _WHITESPACE.sub(" ", raw).strip()))
        ),
        "",
    )
    if len(line) <= TITLE_MAX_CHARS:
        return line
    cut = line[: TITLE_MAX_CHARS - 1]
    space = cut.rfind(" ")
    if space >= TITLE_MAX_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def title_from_first_message(app: "FastAPI", session_id: str, text: str) -> None:
    """Name a placeholder-titled conversation after an accepted human message.

    A storage or validation failure is logged rather than raised: naming is
    presentation, and the message is already accepted.
    """
    try:
        session = app.state.sessions.get(session_id)
        if session is None or session.parent_session_id:
            return
        if not is_placeholder_title(str(session.title or ""), session_id):
            return
        title = derive_title(text)
        if not title:
            return
        updated = app.state.sessions.update(session_id, title=title)
        if updated is None:
            return
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=session_id,
                payload=Session(**updated.to_wire()).model_dump(exclude_none=True),
            )
        )
    except (OSError, ValueError) as exc:
        logger.warning(
            "session auto-title skipped reason=session_auto_title_failed session_id=%s error=%r",
            session_id,
            exc,
        )


__all__ = ["TITLE_MAX_CHARS", "derive_title", "is_placeholder_title", "title_from_first_message"]
