"""Native image and PDF input for the Claude Agent SDK engine.

The engine collects a request's ``dspy.lm15`` :class:`~dspy.lm15.ImagePart` and
:class:`~dspy.lm15.DocumentPart` s beside the text transcript; this module turns them
into the Anthropic content blocks the SDK's streaming input takes, holding three
properties:

* **Only supported media.** Images are JPEG/PNG/GIF/WebP, documents are PDF; anything
  else is a typed refusal, never flattened or dropped.
* **Size is bounded before expansion.** Every attachment is measured in SOURCE bytes
  (arithmetic on the base64 length, never a decode) and refused against the shared
  per-kind and per-request ceilings in
  :mod:`clio_agent.providers.native_attachment_bounds`.
* **Egress is explicit.** A remote image URL hands the provider a fetch CLIO never
  performed and cannot bound. Only inline data is accepted by default; a remote host
  must be named in ``providers.native_image_url_allowlist``, and using one records a
  typed egress line rather than happening invisibly. Documents are inline only.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlparse

from dspy.lm15 import DocumentPart, ImagePart

from clio_agent.providers.native_attachment_bounds import (
    AttachmentKind,
    base64_byte_length,
    check_block_bytes,
    check_total_bytes,
)

logger = logging.getLogger(__name__)

CLAUDE_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
CLAUDE_PDF_MEDIA_TYPE = "application/pdf"

__all__ = [
    "CLAUDE_IMAGE_MEDIA_TYPES",
    "CLAUDE_PDF_MEDIA_TYPE",
    "native_blocks",
    "native_image_url_allowlist",
    "sdk_prompt",
]


def native_image_url_allowlist() -> frozenset[str]:
    """Hosts whose ``http(s)`` image URLs may be handed to the provider.

    Empty by default: a remote URL makes the PROVIDER fetch bytes CLIO never saw,
    cannot size-check, and cannot attribute -- so it is refused unless an operator
    has named the host. Configured as a comma-separated host list.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    raw = conf.resolve(
        "providers.native_image_url_allowlist",
        env="CLIO_PROVIDER_NATIVE_IMAGE_URL_ALLOWLIST",
        default="",
        cast=conf.as_str,
    )
    return frozenset(host.strip().lower() for host in raw.split(",") if host.strip())


def native_blocks(parts: Sequence[ImagePart | DocumentPart]) -> list[dict[str, Any]]:
    """The Anthropic content blocks for ``parts``, in order, bounded as a set.

    Raises:
        ValueError: An unsupported media type, an empty or remote-but-not-allowlisted
            source, or a document given by URL.
        NativeAttachmentTooLargeError: One attachment, or all of them together, over
            the configured ceiling.
    """
    blocks = [_block(part) for part in parts]
    check_total_bytes(
        sum(
            base64_byte_length(block["source"]["data"])
            for block in blocks
            if block["source"]["type"] == "base64"
        )
    )
    return blocks


def _block(part: ImagePart | DocumentPart) -> dict[str, Any]:
    if isinstance(part, ImagePart):
        return {"type": "image", "source": _image_source(part)}
    return {"type": "document", "source": _inline_source(part, kind="document")}


def _image_source(part: ImagePart) -> dict[str, Any]:
    if part.data:
        return _inline_source(part, kind="image")
    url = (part.url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("a native Claude image needs inline data or an http(s) URL")
    host = (parsed.hostname or "").lower()
    if host not in native_image_url_allowlist():
        raise ValueError(
            f"remote native Claude image host {host!r} is not in "
            "providers.native_image_url_allowlist; the provider would fetch bytes CLIO "
            "never saw and cannot size-check. Inline the image or allowlist the host"
        )
    # Allowlisted, but never invisible: the fetch happens outside CLIO.
    logger.info(
        "permitted a remote native image source reason=native_image_url_egress host=%s", host
    )
    return {"type": "url", "url": url}


def _inline_source(part: ImagePart | DocumentPart, *, kind: AttachmentKind) -> dict[str, Any]:
    allowed = CLAUDE_IMAGE_MEDIA_TYPES if kind == "image" else frozenset({CLAUDE_PDF_MEDIA_TYPE})
    media_type = (part.media_type or "").strip().lower()
    if media_type not in allowed:
        supported = ", ".join(sorted(allowed))
        raise ValueError(
            f"unsupported native Claude media type {media_type!r}; expected {supported}"
        )
    data = (part.data or "").strip()
    if not data:
        raise ValueError(f"a native Claude {kind} needs inline base64 data")
    # Measured, not decoded: an oversized attachment is refused without its bytes.
    check_block_bytes(kind, base64_byte_length(data), label=media_type)
    return {"type": "base64", "media_type": media_type, "data": data}


async def sdk_prompt(
    payload: str,
    blocks: Sequence[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Yield one Agent SDK streaming-input message with native attachments."""
    content = [*blocks, {"type": "text", "text": payload}]
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }
