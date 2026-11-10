"""Estimate what one transcript message contributes to a later prompt.

A message's recorded provider usage (``message.tokens``) describes the API call(s)
that produced it: system prompt, tool definitions and all prior history, counted
again on every step. Summing it across messages counts the shared prompt once per
message, so it is never used here. The estimate is chars/4 over the content the
message replays (as ``conversation_projection.transcript_segments`` rebuilds it):
text and thinking, file paths and diffs, tool names with their arguments, and tool
result text.

Leaf module: imports nothing from GACT so both the context-frame builder
(``gact.enrichment``) and ``/v1/memory/stats`` (``gact.routes.system``) share it.
"""

from __future__ import annotations

import json
from typing import Any

TOKEN_ESTIMATE_METHOD = "chars_div_4"


def estimate_text_tokens(text: str) -> int:
    """chars/4, at least 1 for any non-empty text."""

    return max(1, len(text) // 4) if text else 0


def _json_text(value: Any) -> str:
    if not value:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _part_chunks(part: Any) -> list[str]:
    chunks: list[str] = []
    for attr in ("text", "path", "unified_diff", "new_content"):
        value = getattr(part, attr, "") or ""
        if value:
            chunks.append(str(value))
    part_type = str(getattr(part, "type", "") or "")
    if part_type == "tool_call":
        name = getattr(part, "tool_name", "") or ""
        if name:
            chunks.append(str(name))
        payload = _json_text(getattr(part, "input", None))
        if payload:
            chunks.append(payload)
    elif part_type == "tool_result":
        # The model-facing observation is serialized from the result's text content;
        # ``structured_content`` / ``content_blocks`` are wire-only and never replayed.
        for child in getattr(part, "content", None) or []:
            chunks.extend(_part_chunks(child))
    return chunks


def message_context_text(message: Any) -> str:
    """The replayable content of ``message`` joined into one string."""

    chunks: list[str] = []
    for part in getattr(message, "parts", None) or []:
        chunks.extend(_part_chunks(part))
    return "\n".join(chunks)


def estimate_message_context_tokens(message: Any) -> int:
    """Tokens ``message`` adds to a later prompt (chars/4 of its replayed content)."""

    return estimate_text_tokens(message_context_text(message))
