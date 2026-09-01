"""Structured tool-result failure extraction.

A tool result carries its failure on one of TWO lanes and clio must read both.
The structured lane (``structuredContent`` / ``data`` / ``isError``) is what a
FastMCP server produces. The content lane is what a server returning an
explicit ``ToolResult`` produces: fastmcp leaves ``data`` as ``None`` when no
``structuredContent`` is present, so such a server's error envelope exists ONLY
inside its text content blocks. Reading the structured lane alone recorded
those failures as successes on the transcript, the ledger row and the semantic
event.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any


def _content_block_error(result: Any) -> str | None:
    """Return the error a result's structured content blocks carry, if any.

    Text blocks are the only lane an error envelope can ride; every other block
    type (image, audio, resource) is evidence, never a verdict. A block whose
    text is a JSON object is judged by the same rules as a structured payload,
    so the same envelope gets the same verdict whichever lane the server chose.

    Args:
        result: The raw tool result object or mapping.

    Returns:
        The first content-block error string, or ``None``.
    """

    result_map = result if isinstance(result, Mapping) else {}
    blocks = getattr(result, "content", None)
    if blocks is None:
        blocks = result_map.get("content")
    if isinstance(blocks, (str, bytes, bytearray)) or not isinstance(blocks, Sequence):
        return None
    for block in blocks:
        block_map = block if isinstance(block, Mapping) else {}
        block_type = getattr(block, "type", None)
        if block_type is None:
            block_type = block_map.get("type")
        text = getattr(block, "text", None)
        if text is None:
            text = block_map.get("text")
        if block_type not in (None, "text") or not isinstance(text, str) or not text.strip():
            continue
        stripped = text.strip()
        payload: Any = stripped
        if stripped.startswith("{"):
            with suppress(json.JSONDecodeError, TypeError, ValueError):
                payload = json.loads(stripped)
        if block_error := structured_tool_result_error(payload):
            return block_error
    return None


def structured_tool_result_error(result: Any) -> str | None:
    """Return an error string when a tool returns a structured error payload."""

    decoded = result
    if isinstance(result, str):
        stripped = result.strip()
        if stripped.startswith("{") and '"error"' in stripped:
            with suppress(json.JSONDecodeError, TypeError):
                decoded = json.loads(stripped)
    if isinstance(decoded, Mapping):
        error = decoded.get("error")
        if error:
            if isinstance(error, Mapping):
                code = str(error.get("code") or error.get("type") or "tool_error")
                message = str(error.get("message") or "").strip()
                return f"{code}: {message}" if message else code
            return str(error)
        status = str(decoded.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"status={status}: {message}" if message else f"status={status}"
        if decoded.get("ok") is False:
            message = str(decoded.get("message") or decoded.get("detail") or "").strip()
            return f"ok=false: {message}" if message else "ok=false"
    elif isinstance(decoded, str) and decoded.strip().casefold().startswith("error:"):
        return decoded.strip()

    result_map = result if isinstance(result, Mapping) else {}
    candidates = (
        getattr(result, "structured_content", None),
        getattr(result, "structuredContent", None),
        result_map.get("structuredContent"),
        result_map.get("structured_content"),
        getattr(result, "data", None),
        result_map.get("data"),
    )
    for candidate in candidates:
        if candidate is not None and candidate is not result:
            if nested_error := structured_tool_result_error(candidate):
                return nested_error
    if content_error := _content_block_error(result):
        return content_error
    explicitly_failed = getattr(
        result,
        "is_error",
        getattr(result, "isError", result_map.get("isError", result_map.get("is_error"))),
    )
    return "tool_result_is_error" if explicitly_failed is True else None


__all__ = ["structured_tool_result_error"]
