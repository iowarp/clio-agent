"""Observer-only tool presentation contract and bounded transcript projection.

Full blocks live on persisted Parts. Transport previews and content cursors are
derived from those blocks; neither is used to construct a model observation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PAGE_CHARS = 2048


class PresentationBlock(BaseModel):
    """A provider-declared semantic result, independent of the raw tool result."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    type: Literal["text", "markdown", "code", "diff", "terminal", "link", "check", "media"]
    media_type: str = ""
    text: str = ""
    label: str = ""
    language: str = ""
    target: Literal["artifact", "resource", "session", "url", "file"] | None = None
    state: Literal["pending", "in_progress", "completed"] | None = None
    uri: str = ""
    command: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    channel: Literal["stdout", "stderr"] | None = None


class ToolPresentation(BaseModel):
    """Ordered result blocks authored by a tool's declared presenter."""

    model_config = ConfigDict(extra="forbid")
    action: str = ""
    subject: str = ""
    summary: str = ""
    blocks: list[PresentationBlock] = Field(default_factory=list)
    diagnostic: str | None = None


def project_presentation(
    value: dict[str, Any] | None, session_id: str, call_id: str, *, running: bool = False
) -> dict[str, Any] | None:
    """Bound each block's transport body while retaining its durable identity."""

    if value is None:
        return None
    projected = {**value, "blocks": []}
    for source in value.get("blocks", []):
        block = dict(source)
        body = block.get("text", "")
        if block["type"] == "terminal":
            block["stream_offset"] = len(body)
        if len(body) > PAGE_CHARS:
            block["text"] = body[:PAGE_CHARS]
            block["content_ref"] = {
                "session_id": session_id,
                "call_id": call_id,
                "block_id": block["id"],
                "cursor": PAGE_CHARS,
                "total_chars": len(body),
            }
            if running and block["type"] == "terminal":
                block["text"] = body[-PAGE_CHARS:]
        projected["blocks"].append(block)
    return projected


def presentation_page(block: dict[str, Any], cursor: int) -> dict[str, Any]:
    """Read one contiguous character page; reject invalid offsets explicitly."""

    body = block.get("text", "")
    if cursor < 0 or cursor > len(body):
        raise ValueError("presentation cursor outside block content")
    end = min(len(body), cursor + PAGE_CHARS)
    return {
        "text": body[cursor:end],
        "cursor": cursor,
        "next_cursor": end if end < len(body) else None,
        "total_chars": len(body),
    }
