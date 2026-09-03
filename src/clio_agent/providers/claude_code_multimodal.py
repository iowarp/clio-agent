"""Native multimodal input helpers for the Claude Agent SDK transport.

DSPy's chat adapter emits OpenAI-shaped ``image_url`` and ``file`` parts.  The
Claude Agent SDK's streaming-input boundary accepts Anthropic content blocks,
so this module translates the two representations without putting base64 data
into CLIO's serialized transcript or provider trace logs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

CLAUDE_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
CLAUDE_PDF_MEDIA_TYPE = "application/pdf"


def _data_uri(value: str, *, allowed_media_types: frozenset[str]) -> tuple[str, str]:
    """Return ``(media_type, base64_data)`` for one supported data URI."""

    header, separator, data = value.partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header.lower():
        raise ValueError("native Claude attachments require a base64 data URI")
    media_type = header[5:].split(";", 1)[0].strip().lower()
    if media_type not in allowed_media_types:
        supported = ", ".join(sorted(allowed_media_types))
        raise ValueError(
            f"unsupported native Claude media type {media_type!r}; expected {supported}"
        )
    if not data.strip():
        raise ValueError("native Claude attachment data cannot be empty")
    return media_type, data.strip()


def _image_block(value: Any) -> dict[str, Any]:
    """Translate an OpenAI-shaped image value to one Claude image block."""

    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("native Claude image parts require a non-empty URL")
    resolved = value.strip()
    if resolved.startswith("data:"):
        media_type, data = _data_uri(
            resolved,
            allowed_media_types=CLAUDE_IMAGE_MEDIA_TYPES,
        )
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("native Claude image URLs must use http or https")
    return {"type": "image", "source": {"type": "url", "url": resolved}}


def _document_block(value: Any) -> dict[str, Any]:
    """Translate a DSPy/OpenAI file part to one Claude PDF document block."""

    if not isinstance(value, dict):
        raise ValueError("native Claude file parts require a file object")
    file_data = value.get("file_data")
    if not isinstance(file_data, str) or not file_data.strip():
        raise ValueError(
            "native Claude PDF parts require file_data; foreign provider file IDs are not portable"
        )
    media_type, data = _data_uri(
        file_data.strip(),
        allowed_media_types=frozenset({CLAUDE_PDF_MEDIA_TYPE}),
    )
    block: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
    filename = value.get("filename")
    if isinstance(filename, str) and filename.strip():
        block["title"] = filename.strip()
    return block


def messages_to_claude_input(
    messages: list[dict[str, Any]],
    *,
    serialize_text: Callable[[list[dict[str, Any]]], str],
    unsupported_multimodal_exc: type[Exception],
) -> tuple[str, list[dict[str, Any]]]:
    """Return a hardened text transcript plus ordered native Claude blocks.

    Image and PDF bytes are removed before ``serialize_text`` runs.  Unsupported
    or malformed attachment parts fail explicitly instead of being silently
    flattened or dropped.
    """

    text_messages: list[dict[str, Any]] = []
    native_blocks: list[dict[str, Any]] = []
    try:
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                text_messages.append(message)
                continue
            text_parts: list[Any] = []
            for part in content:
                if not isinstance(part, dict):
                    text_parts.append(part)
                    continue
                part_type = str(part.get("type") or "").strip().lower()
                if part_type in {"image", "image_url", "input_image"} or "image_url" in part:
                    native_blocks.append(_image_block(part.get("image_url") or part.get("url")))
                    continue
                if part_type in {"file", "input_file"} or "file" in part:
                    native_blocks.append(_document_block(part.get("file") or part))
                    continue
                text_parts.append(part)
            text_messages.append({**message, "content": text_parts})
    except ValueError as exc:
        raise unsupported_multimodal_exc(str(exc)) from exc
    return serialize_text(text_messages), native_blocks


async def sdk_prompt(
    payload: str,
    native_blocks: Sequence[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield one Agent SDK streaming-input message with native attachments."""

    content = [*native_blocks, {"type": "text", "text": payload}]
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


def native_input_summary(native_blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bounded trace metadata that never includes attachment bytes or URLs."""

    rows: list[dict[str, Any]] = []
    for block in native_blocks:
        source = block.get("source") if isinstance(block, dict) else None
        source = source if isinstance(source, dict) else {}
        rows.append(
            {
                "type": str(block.get("type") or ""),
                "source_type": str(source.get("type") or ""),
                "media_type": str(source.get("media_type") or ""),
                "data_length": len(str(source.get("data") or "")),
            }
        )
    return rows


__all__ = [
    "CLAUDE_IMAGE_MEDIA_TYPES",
    "CLAUDE_PDF_MEDIA_TYPE",
    "messages_to_claude_input",
    "native_input_summary",
    "sdk_prompt",
]
