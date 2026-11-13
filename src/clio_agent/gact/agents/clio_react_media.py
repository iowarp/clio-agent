"""Where a request's images and documents go: inline data to typed parts, and placement.

Moved out of :mod:`clio_agent.gact.agents.clio_react` (which re-imports these names) so
the loop stays under the file-size cap.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from dspy.lm15 import DocumentPart, ImagePart, Message, TextPart, ToolResultPart


def _media_part(item: Any) -> ImagePart | DocumentPart:
    url = str(getattr(item, "url", "") or getattr(item, "file_data", "") or "")
    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").split(";", 1)[0]
    if not data:
        raise ValueError(f"attachment {type(item).__name__} carries no inline data")
    if media_type.startswith("image/"):
        return ImagePart(data=data, media_type=media_type)
    return DocumentPart(data=data, media_type=media_type or "application/pdf")


def _place_tool_media(messages: list[Message], placement: str) -> list[Message]:
    """Put tool-result images/documents where the LM's API takes them.

    ``native``: inside the tool result. ``user_message`` (chat-completions servers take
    text-only tool rows): the tool result keeps a text marker and the media follows in
    one user message right after the tool message -- the same content, in the one
    place that API carries it. Deterministic, so the context stays prefix-stable.
    """
    if placement == "native":
        return messages
    out: list[Message] = []
    for message in messages:
        media: list[Any] = []
        if message.role == "tool":
            parts = []
            for part in message.parts:
                if not isinstance(part, ToolResultPart):
                    parts.append(part)
                    continue
                kept = [p for p in part.content if not isinstance(p, ImagePart | DocumentPart)]
                moved = [p for p in part.content if isinstance(p, ImagePart | DocumentPart)]
                if moved:
                    note = f"({len(moved)} attachment(s) of this result follow in the next message)"
                    kept.append(TextPart(text=note))
                    media.extend(moved)
                    part = dataclasses.replace(part, content=tuple(kept))
                parts.append(part)
            message = dataclasses.replace(message, parts=tuple(parts))
        out.append(message)
        if media:
            label = TextPart(text="[attachments from the tool results above]")
            out.append(Message(role="user", parts=(label, *media)))
    return out
