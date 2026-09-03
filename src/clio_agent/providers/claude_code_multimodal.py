"""Native multimodal input helpers for the Claude Agent SDK transport.

DSPy's chat adapter emits OpenAI-shaped ``image_url`` and ``file`` parts.  The
Claude Agent SDK's streaming-input boundary accepts Anthropic content blocks,
so this module translates the two representations without putting base64 data
into CLIO's serialized transcript or provider trace logs.

Three properties this boundary must hold, each of which was previously missing:

* **Detection is by part TYPE, per shape.** A key-presence test (``"file" in
  part``) misclassifies any part that merely carries that key, and an
  already-Anthropic-shaped image part (``{"type": "image", "source": {...}}``)
  was routed through the OpenAI reader and rejected as "requires a non-empty
  URL". Each recognised shape is read on its own terms; a part that matches a
  shape but is malformed gets a typed refusal instead of being silently passed
  through as text.
* **Size is bounded before expansion.** Every attachment is measured in SOURCE
  bytes (arithmetic on the base64 length, never a decode) and refused against the
  shared per-kind and per-request ceilings in
  :mod:`clio_agent.providers.native_attachment_bounds`.
* **Egress is explicit.** A remote image URL hands the provider a fetch CLIO
  never performed and cannot bound. Only ``data:`` URIs are accepted by default;
  a remote host must be named in a conf allowlist, and using one records a typed
  egress line rather than happening invisibly.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from clio_agent.providers.native_attachment_bounds import (
    AttachmentKind,
    NativeAttachmentTooLargeError,
    base64_byte_length,
    check_block_bytes,
    check_total_bytes,
)

logger = logging.getLogger(__name__)

CLAUDE_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
CLAUDE_PDF_MEDIA_TYPE = "application/pdf"

#: Part ``type`` values that name an image, by the representation they use.
_OPENAI_IMAGE_TYPES = frozenset({"image_url", "input_image"})
_OPENAI_FILE_TYPES = frozenset({"file", "input_file"})
_ANTHROPIC_IMAGE_TYPE = "image"
_ANTHROPIC_DOCUMENT_TYPE = "document"


def native_image_url_allowlist() -> frozenset[str]:
    """Hosts whose ``http(s)`` image URLs may be handed to the provider.

    Empty by default: a remote URL makes the PROVIDER fetch bytes CLIO never saw,
    cannot size-check, and cannot attribute — so it is refused unless an operator
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


def _data_uri(
    value: str, *, allowed_media_types: frozenset[str], kind: AttachmentKind, label: str = ""
) -> tuple[str, str]:
    """Return ``(media_type, base64_data)`` for one supported, in-bounds data URI."""

    header, separator, data = value.partition(",")
    if not separator or not header.startswith("data:") or ";base64" not in header.lower():
        raise ValueError("native Claude attachments require a base64 data URI")
    media_type = header[5:].split(";", 1)[0].strip().lower()
    if media_type not in allowed_media_types:
        supported = ", ".join(sorted(allowed_media_types))
        raise ValueError(
            f"unsupported native Claude media type {media_type!r}; expected {supported}"
        )
    payload = data.strip()
    if not payload:
        raise ValueError("native Claude attachment data cannot be empty")
    # Measured, not decoded: an oversized attachment is refused without ever
    # materialising its bytes.
    check_block_bytes(kind, base64_byte_length(payload), label=label or media_type)
    return media_type, payload


def _image_source(value: Any) -> dict[str, Any]:
    """Translate an OpenAI-shaped image value to one Claude image ``source``."""

    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("native Claude image parts require a non-empty URL")
    resolved = value.strip()
    if resolved.startswith("data:"):
        media_type, data = _data_uri(
            resolved, allowed_media_types=CLAUDE_IMAGE_MEDIA_TYPES, kind="image"
        )
        return {"type": "base64", "media_type": media_type, "data": data}
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("native Claude image URLs must be a data: URI or an http(s) URL")
    host = parsed.hostname or ""
    allowlist = native_image_url_allowlist()
    if host.lower() not in allowlist:
        raise ValueError(
            f"remote native Claude image host {host!r} is not in "
            "providers.native_image_url_allowlist; the provider would fetch bytes CLIO "
            "never saw and cannot size-check. Inline the image as a data: URI or "
            "allowlist the host"
        )
    # Allowlisted, but never invisible: the fetch happens outside CLIO, so the
    # decision to permit it is recorded with the host it was permitted for.
    logger.info(
        "permitted a remote native image source reason=native_image_url_egress host=%s", host
    )
    return {"type": "url", "url": resolved}


def _anthropic_source(source: Any, *, kind: AttachmentKind, label: str = "") -> dict[str, Any]:
    """Validate an already-Anthropic-shaped attachment ``source`` block.

    A producer that already speaks Anthropic content blocks must not be routed
    through the OpenAI reader (which looks for ``image_url`` and rejects the part
    outright); its source is validated on its own terms instead.
    """

    if not isinstance(source, dict):
        raise ValueError("native Claude attachment source must be an object")
    source_type = str(source.get("type") or "").strip().lower()
    allowed = CLAUDE_IMAGE_MEDIA_TYPES if kind == "image" else frozenset({CLAUDE_PDF_MEDIA_TYPE})
    if source_type == "base64":
        media_type = str(source.get("media_type") or "").strip().lower()
        data = source.get("data")
        if media_type not in allowed:
            supported = ", ".join(sorted(allowed))
            raise ValueError(
                f"unsupported native Claude media type {media_type!r}; expected {supported}"
            )
        if not isinstance(data, str) or not data.strip():
            raise ValueError("native Claude attachment data cannot be empty")
        payload = data.strip()
        check_block_bytes(kind, base64_byte_length(payload), label=label or media_type)
        return {"type": "base64", "media_type": media_type, "data": payload}
    if source_type == "url" and kind == "image":
        return _image_source(str(source.get("url") or ""))
    raise ValueError(
        f"unsupported native Claude {kind} source type {source_type!r}; expected base64"
        + (" or url" if kind == "image" else "")
    )


def _image_block(part: dict[str, Any]) -> dict[str, Any]:
    """Build one Claude image block from either representation of an image part."""

    source = part.get("source")
    if source is not None:
        return {"type": "image", "source": _anthropic_source(source, kind="image")}
    value = part.get("image_url")
    if value is None:
        value = part.get("url")
    return {"type": "image", "source": _image_source(value)}


def _document_block(part: dict[str, Any]) -> dict[str, Any]:
    """Build one Claude PDF document block from either representation."""

    filename = part.get("filename")
    source = part.get("source")
    if source is not None:
        block: dict[str, Any] = {
            "type": "document",
            "source": _anthropic_source(source, kind="document", label=str(filename or "")),
        }
        title = part.get("title") or filename
        if isinstance(title, str) and title.strip():
            block["title"] = title.strip()
        return block
    file_object = part.get("file")
    if not isinstance(file_object, dict):
        raise ValueError("native Claude file parts require a file object")
    file_data = file_object.get("file_data")
    if not isinstance(file_data, str) or not file_data.strip():
        raise ValueError(
            "native Claude PDF parts require file_data; foreign provider file IDs are not portable"
        )
    filename = file_object.get("filename")
    media_type, data = _data_uri(
        file_data.strip(),
        allowed_media_types=frozenset({CLAUDE_PDF_MEDIA_TYPE}),
        kind="document",
        label=str(filename or ""),
    )
    document: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }
    if isinstance(filename, str) and filename.strip():
        document["title"] = filename.strip()
    return document


def _classify_part(part: dict[str, Any]) -> str:
    """Return ``"image"``, ``"document"`` or ``"text"`` for one content part.

    Classification is by the part's declared ``type``. Only a part that declares
    NO type at all falls back to key inspection — a typed part is never
    reclassified by an incidental key, which is how a text part carrying a
    ``file`` key used to be read as an attachment.
    """

    part_type = str(part.get("type") or "").strip().lower()
    if part_type:
        if part_type == _ANTHROPIC_IMAGE_TYPE or part_type in _OPENAI_IMAGE_TYPES:
            return "image"
        if part_type == _ANTHROPIC_DOCUMENT_TYPE or part_type in _OPENAI_FILE_TYPES:
            return "document"
        return "text"
    if "image_url" in part:
        return "image"
    if "file" in part:
        return "document"
    return "text"


def messages_to_claude_input(
    messages: list[dict[str, Any]],
    *,
    serialize_text: Callable[[list[dict[str, Any]]], str],
    unsupported_multimodal_exc: type[Exception],
) -> tuple[str, list[dict[str, Any]]]:
    """Return a hardened text transcript plus ordered native Claude blocks.

    Image and PDF bytes are removed before ``serialize_text`` runs.  Unsupported
    or malformed attachment parts fail explicitly instead of being silently
    flattened or dropped, and the assembled attachments are refused as a set if
    they exceed the per-request byte ceiling.
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
                kind = _classify_part(part)
                if kind == "image":
                    native_blocks.append(_image_block(part))
                elif kind == "document":
                    native_blocks.append(_document_block(part))
                else:
                    text_parts.append(part)
            text_messages.append({**message, "content": text_parts})
        # The aggregate bound is checked ONCE, on the assembled set, using the
        # source lengths the summary already computes -- so a request that is
        # individually in-bounds but collectively oversized is refused here
        # rather than by the provider after the whole payload was built.
        check_total_bytes(sum(row["data_length"] for row in native_input_summary(native_blocks)))
    except NativeAttachmentTooLargeError as exc:
        raise unsupported_multimodal_exc(str(exc)) from exc
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


#: Stands in for elided attachment bytes in a trace record. A typed marker, not
#: an empty string, so a reader can tell "redacted here" from "absent upstream".
REDACTED_ATTACHMENT_DATA = "<redacted: native attachment bytes, {length} source bytes>"


def redact_message_attachments(messages: Any) -> Any:
    """Return ``messages`` with attachment bytes elided and structure preserved.

    The provider request trace used to record ``messages`` verbatim; the native
    multimodal change deleted the whole record rather than redact it, so the
    trace stopped showing the request SHAPE — how many parts, in what order, of
    what media type — which is exactly what a multimodal dispatch bug looks like.
    Structure is kept, only the payload is replaced, and the replacement names
    itself and the size it stood in for.
    """

    if isinstance(messages, list):
        return [redact_message_attachments(item) for item in messages]
    if not isinstance(messages, dict):
        return messages
    redacted: dict[str, Any] = {}
    for key, value in messages.items():
        if key in {"data", "file_data"} and isinstance(value, str):
            redacted[key] = REDACTED_ATTACHMENT_DATA.format(length=base64_byte_length(value))
        elif key == "url" and isinstance(value, str) and value.startswith("data:"):
            _header, _sep, payload = value.partition(",")
            redacted[key] = REDACTED_ATTACHMENT_DATA.format(length=base64_byte_length(payload))
        else:
            redacted[key] = redact_message_attachments(value)
    return redacted


def native_input_summary(native_blocks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bounded trace metadata that never includes attachment bytes or URLs.

    ``data_length`` is the DECODED source size, not the base64 length, so the
    number a reader sees is the one the ceilings are expressed in.
    """

    rows: list[dict[str, Any]] = []
    for block in native_blocks:
        source = block.get("source") if isinstance(block, dict) else None
        source = source if isinstance(source, dict) else {}
        rows.append(
            {
                "type": str(block.get("type") or ""),
                "source_type": str(source.get("type") or ""),
                "media_type": str(source.get("media_type") or ""),
                "data_length": base64_byte_length(str(source.get("data") or "")),
            }
        )
    return rows


__all__ = [
    "CLAUDE_IMAGE_MEDIA_TYPES",
    "CLAUDE_PDF_MEDIA_TYPE",
    "REDACTED_ATTACHMENT_DATA",
    "messages_to_claude_input",
    "native_image_url_allowlist",
    "native_input_summary",
    "redact_message_attachments",
    "sdk_prompt",
]
