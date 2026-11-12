"""What a browser comment session shows in the transcript (v3 projection).

The session part reads "N comments on <title>". Each of its pictures is named by its
mark and anchor (``comment_attachments._image_chip`` stores it as ``label``), or else
by its caption.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def comment_chip_label(part_type: str, metadata: Mapping[str, Any]) -> str | None:
    """The chip label of a comment session part or picture; ``None`` for any other part."""
    if part_type == "image" and "comment_session" in metadata:
        name = str(metadata.get("label") or metadata.get("caption") or "Image")
        return name[:1].upper() + name[1:]
    if part_type != "comment_session":
        return None
    session = metadata.get("comment_session")
    session = session if isinstance(session, Mapping) else {}
    marks = session.get("marks")
    count = len(marks) if isinstance(marks, list) else 0
    title = str(session.get("title") or "")
    noun = "comment" if count == 1 else "comments"
    return f"{count} {noun} on {title}" if title else f"{count} {noun}"
