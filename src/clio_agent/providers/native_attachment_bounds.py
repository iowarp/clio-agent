"""Byte bounds for native model attachments, refused before base64 expansion.

An attachment on its way to a model is unbounded input: nothing between the
upload custody limit and the provider's own hard error checked how big the
image or PDF actually was, so an oversized resource was base64-expanded (a 1.33x
blow-up), threaded into a request, and rejected by the provider — after CLIO had
already paid the memory and the round-trip, with a raw transport error instead
of an explainable refusal.

The bounds default to the real provider limits Anthropic documents (~5 MB per
image, ~32 MB per PDF, ~32 MB per request), and are shared by BOTH providers'
attach paths so neither can drift. Every refusal is typed, and every check runs
against the SOURCE byte count — the resource's recorded size, or a base64
string's decoded length computed arithmetically — so nothing is expanded in
memory just to discover it was too big.
"""

from __future__ import annotations

from typing import Literal

#: Attachment kinds the native lanes carry. Each has its own ceiling because the
#: provider's own limits differ by an order of magnitude.
AttachmentKind = Literal["image", "document"]

#: Typed refusal reasons, in the ``stream_fallback`` reason-catalog style: the
#: code is the queryable fact, the sentence is what a human reads.
NATIVE_ATTACHMENT_REFUSAL_REASONS: dict[str, str] = {
    "native_attachment_block_too_large": (
        "one native attachment exceeds the per-attachment byte ceiling for its kind; it "
        "would be refused by the provider after CLIO had already expanded and sent it"
    ),
    "native_attachment_total_too_large": (
        "the native attachments on this request exceed the aggregate byte ceiling; the "
        "request would be refused by the provider after CLIO had already assembled it"
    ),
}


class NativeAttachmentTooLargeError(ValueError):
    """Raised when a native attachment (or their sum) exceeds its configured bound.

    Carries the typed ``reason`` code alongside the human sentence so callers can
    surface it structurally rather than re-parsing the message.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def image_max_bytes() -> int:
    """Per-image ceiling in SOURCE bytes (default 5 MiB — Anthropic's own limit)."""

    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return max(
        1,
        conf.resolve(
            "resources.native_image_max_bytes",
            env="CLIO_RESOURCE_NATIVE_IMAGE_MAX_BYTES",
            default=5 * 1024 * 1024,
            cast=conf.as_int,
        ),
    )


def document_max_bytes() -> int:
    """Per-document ceiling in SOURCE bytes (default 32 MiB — Anthropic's PDF limit)."""

    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        conf.resolve(
            "resources.native_document_max_bytes",
            env="CLIO_RESOURCE_NATIVE_DOCUMENT_MAX_BYTES",
            default=32 * 1024 * 1024,
            cast=conf.as_int,
        ),
    )


def total_max_bytes() -> int:
    """Aggregate ceiling across every native attachment on ONE request (default 32 MiB)."""

    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        conf.resolve(
            "resources.native_attachment_total_max_bytes",
            env="CLIO_RESOURCE_NATIVE_ATTACHMENT_TOTAL_MAX_BYTES",
            default=32 * 1024 * 1024,
            cast=conf.as_int,
        ),
    )


def max_bytes_for(kind: AttachmentKind) -> int:
    """Return the per-attachment ceiling for one attachment kind."""

    return image_max_bytes() if kind == "image" else document_max_bytes()


def base64_byte_length(data: str) -> int:
    """Return the DECODED byte length of a base64 payload without decoding it.

    Arithmetic on the encoded length, so a 40 MB attachment is refused without
    ever materialising its decoded bytes — which is the whole point of checking
    before expansion.
    """

    encoded = "".join(data.split())
    if not encoded:
        return 0
    padding = len(encoded) - len(encoded.rstrip("="))
    return max(0, (len(encoded) // 4) * 3 - padding)


def check_block_bytes(kind: AttachmentKind, byte_length: int, *, label: str = "") -> None:
    """Refuse one attachment whose SOURCE size exceeds its kind's ceiling.

    Args:
        kind: ``"image"`` or ``"document"`` — selects the ceiling.
        byte_length: The attachment's decoded/source size in bytes.
        label: Optional human identifier (a filename, a resource id) for the message.

    Raises:
        NativeAttachmentTooLargeError: typed
            ``native_attachment_block_too_large``.
    """

    limit = max_bytes_for(kind)
    if byte_length <= limit:
        return
    named = f" {label!r}" if label else ""
    raise NativeAttachmentTooLargeError(
        "native_attachment_block_too_large",
        f"native {kind} attachment{named} is {byte_length} bytes, over the "
        f"{limit}-byte per-{kind} ceiling "
        f"({NATIVE_ATTACHMENT_REFUSAL_REASONS['native_attachment_block_too_large']})",
    )


def check_total_bytes(byte_length: int) -> None:
    """Refuse a request whose native attachments sum past the aggregate ceiling.

    Raises:
        NativeAttachmentTooLargeError: typed
            ``native_attachment_total_too_large``.
    """

    limit = total_max_bytes()
    if byte_length <= limit:
        return
    raise NativeAttachmentTooLargeError(
        "native_attachment_total_too_large",
        f"native attachments total {byte_length} bytes, over the {limit}-byte "
        "per-request ceiling "
        f"({NATIVE_ATTACHMENT_REFUSAL_REASONS['native_attachment_total_too_large']})",
    )


__all__ = [
    "NATIVE_ATTACHMENT_REFUSAL_REASONS",
    "AttachmentKind",
    "NativeAttachmentTooLargeError",
    "base64_byte_length",
    "check_block_bytes",
    "check_total_bytes",
    "document_max_bytes",
    "image_max_bytes",
    "max_bytes_for",
    "total_max_bytes",
]
