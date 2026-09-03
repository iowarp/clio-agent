"""Bounded extraction from Claude Agent SDK raw stream events."""

from __future__ import annotations

from typing import Any


def stream_event_text(event: dict[str, Any]) -> str:
    """Extract user-visible text from one Claude SDK raw event."""

    event_type = str(event.get("type") or "")
    if event_type == "content_block_delta":
        delta = event.get("delta")
        if isinstance(delta, dict):
            if delta.get("type") == "text_delta":
                return str(delta.get("text") or "")
            if isinstance(delta.get("text"), str):
                return delta["text"]
    if event_type == "content_block_start":
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text") or "")
    return ""


def stream_event_thinking(event: dict[str, Any]) -> str:
    """Extract provider-internal thinking from one Claude SDK raw event."""

    if str(event.get("type") or "") != "content_block_delta":
        return ""
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "thinking_delta":
        return ""
    return str(delta.get("thinking") or "")


__all__ = ["stream_event_text", "stream_event_thinking"]
