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


def _within_image_budget(messages: list[Message], limit: int) -> list[Message]:
    """At most ``limit`` images in the request: the latest ones (``limit`` <= 0: all).

    A server can cap images per prompt (vLLM ``--limit-mm-per-prompt``; ALCF's
    nemotron: "At most 1 image(s) may be provided in one prompt", HTTP 400). A
    model that looks at a second figure would then fail every later step, so the
    request keeps the newest images and an older one becomes a one-line note.
    The recorded transcript is unchanged.
    """
    if limit <= 0:
        return messages
    keep = limit
    note = TextPart(text=f"[earlier image omitted: this model takes at most {limit} per request]")

    def budget(parts: tuple[Any, ...]) -> tuple[Any, ...]:
        nonlocal keep
        out: list[Any] = []
        for part in reversed(parts):
            if isinstance(part, ToolResultPart):
                part = dataclasses.replace(part, content=budget(tuple(part.content)))
            elif isinstance(part, ImagePart):
                if keep <= 0:
                    part = note
                keep -= 1
            out.append(part)
        return tuple(reversed(out))

    out = [dataclasses.replace(m, parts=budget(tuple(m.parts))) for m in reversed(messages)]
    return list(reversed(out))


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
