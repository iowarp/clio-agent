"""The model-facing text of an MCP tool result (:func:`result_to_text`).

Owner module for how a ``CallToolResult`` becomes the string the model observes;
:mod:`clio_agent.tools.mcp_executor` calls it for every MCP call.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from clio_agent.tools.mcp_result_json import pydantic_json_default

logger = logging.getLogger(__name__)

#: Typed reason for the logged repr fallback when no real JSON mapping exists.
MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON = "mcp_result_to_text_repr_fallback"


def _content_block_field(block: Any, *names: str) -> Any:
    """Read the first present field from a content block (mapping or SDK model)."""

    if isinstance(block, Mapping):
        for field_name in names:
            value = block.get(field_name)
            if value is not None:
                return value
        return None
    for field_name in names:
        value = getattr(block, field_name, None)
        if value is not None:
            return value
    return None


def _base64_decoded_length(data: str) -> int:
    """Approximate decoded byte length of a base64 string without decoding it."""

    if not data:
        return 0
    padding = len(data) - len(data.rstrip("="))
    return max(0, (len(data) * 3) // 4 - padding)


def _human_bytes(num_bytes: int) -> str:
    """Compact human-readable byte size for a model-facing placeholder."""

    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GIF_SIGNATURES = (b"GIF87a", b"GIF89a")


def _raster_dimensions(mime_type: str, data_b64: str) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` for a PNG/GIF block from a bounded prefix.

    Decodes only the leading ~48 raw bytes (never the full image) -- just
    enough to cover the PNG IHDR chunk or the GIF logical screen descriptor.
    Any other format, or a prefix too short/garbled to parse, returns ``None``
    (never guessed); the caller falls back to a byte-size placeholder.
    """

    prefix = data_b64[:80]
    prefix += "=" * (-len(prefix) % 4)
    try:
        raw = base64.b64decode(prefix, validate=False)
    except ValueError:
        return None
    mime = (mime_type or "").lower()
    if mime == "image/png" and raw[:8] == _PNG_SIGNATURE and len(raw) >= 24:
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if mime == "image/gif" and raw[:6] in _GIF_SIGNATURES and len(raw) >= 10:
        return int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    return None


def _content_block_model_text(block: Any) -> str:
    """Compact model-facing placeholder/text for one MCP content block.

    A ``text`` block contributes its text VERBATIM (the common unstructured-
    content case: the tool's whole result IS its text). Every other block
    type contributes a short bracket placeholder -- NEVER the block's raw
    payload (base64 image/audio data, an embedded resource blob). The model
    gets enough to know the evidence exists and reason about it; the bytes
    themselves reach the wire only through the tool_result Part's own
    ``content_blocks`` field (``gact/tool_observer.py``), never through this
    model-facing lane.
    """

    block_type = str(_content_block_field(block, "type") or "")
    if block_type == "text":
        return str(_content_block_field(block, "text") or "")
    mime_type = str(_content_block_field(block, "mime_type", "mimeType") or "")
    if block_type == "image":
        data = str(_content_block_field(block, "data") or "")
        dims = _raster_dimensions(mime_type, data)
        detail = f"{dims[0]}x{dims[1]}" if dims else _human_bytes(_base64_decoded_length(data))
        return f"[image {mime_type or 'unknown'} {detail}]"
    if block_type == "audio":
        data = str(_content_block_field(block, "data") or "")
        return f"[audio {mime_type or 'unknown'} {_human_bytes(_base64_decoded_length(data))}]"
    if block_type == "resource":
        resource = _content_block_field(block, "resource")
        uri = str(_content_block_field(resource, "uri") or "") if resource is not None else ""
        resource_mime = (
            str(_content_block_field(resource, "mime_type", "mimeType") or "")
            if resource is not None
            else ""
        )
        return f"[resource {resource_mime or mime_type or 'unknown'} {uri}]".rstrip()
    if block_type == "resource_link":
        uri = str(_content_block_field(block, "uri") or "")
        name = str(_content_block_field(block, "name") or "")
        return f"[resource_link {name or uri}]"
    return f"[{block_type}]" if block_type else ""


def result_to_text(result: Any) -> str:
    """The text the model sees for one MCP tool result: text content first.

    When the result carries text content blocks, the model gets the content, every
    block in order (text verbatim, any other block as a short placeholder): content is
    what a server writes for the model, while ``structuredContent`` is for programs. A
    server that follows the MCP specification repeats its structured result as JSON in
    a text block, so for it nothing changes; a tool whose text is a view of its own
    (``fs_read_file``'s numbered lines) reaches the model as that view, and its
    structured value stays for the UI and ``run_code`` scripts (TD28).

    With no text block, the model gets ``data``, FastMCP's projection of
    ``structuredContent`` (``fastmcp.client.mixins.tools._parse_call_tool_result``,
    which unwraps a non-object return from ``{"result": ...}``): a string verbatim,
    anything else as JSON. A result with only non-text blocks (an image, audio) gets
    their placeholders, never ``"null"``. ``allow_nan=False`` keeps NaN/Infinity out of
    encoded text; a value ``json.dumps`` refuses for any structural reason falls back to
    ``str()`` with the logged reason :data:`MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON`.
    """
    content = getattr(result, "content", None)
    blocks = (
        list(content)
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray))
        else []
    )
    if any(
        str(_content_block_field(block, "type") or "") == "text"
        and str(_content_block_field(block, "text") or "")
        for block in blocks
    ):
        return "\n".join(piece for piece in map(_content_block_model_text, blocks) if piece)
    data = getattr(result, "data", result)
    if isinstance(data, str):
        return data
    if data is None and blocks:
        placeholder = "\n".join(piece for piece in map(_content_block_model_text, blocks) if piece)
        if placeholder:
            return placeholder
    try:
        return json.dumps(data, allow_nan=False, default=pydantic_json_default)
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        logger.warning(
            "mcp result to text degraded to repr fallback reason=%s type=%s error=%s",
            MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON,
            type(data).__name__,
            exc,
        )
        return str(data)


__all__ = ["MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON", "result_to_text"]
