"""Retry-path preservation for canonical structured context-reference parts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from clio_agent.gact.context_references import authorize_context_reference_parts
from clio_agent.gact.types import Message, Part

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Session


def retryable_user_message(message: Message | None) -> bool:
    """Return whether a source message has text or structured context to retry."""

    return message is not None and any(
        (part.type == "text" and bool(part.text)) or part.type == "context_ref"
        for part in message.parts
    )


async def authorize_retry_parts(
    app: "FastAPI", session: "Session", message: Message, notes: str
) -> list[Part]:
    """Clone, annotate, and reauthorize the parts for an executable retry."""

    parts = [part.model_copy(update={"id": ""}) for part in message.parts]
    if notes.strip():
        parts.append(Part(type="text", text=f"[Retry notes]\n{notes.strip()}"))
    return await authorize_context_reference_parts(app, session, parts)


__all__ = ["authorize_retry_parts", "retryable_user_message"]
