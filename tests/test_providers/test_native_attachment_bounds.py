"""Native attachments are bounded, and the bound is enforced before expansion."""

from __future__ import annotations

import base64

import pytest
from dspy.lm15 import DocumentPart, ImagePart

from clio_agent.providers.claude_code_multimodal import native_blocks
from clio_agent.providers.native_attachment_bounds import (
    NATIVE_ATTACHMENT_REFUSAL_REASONS,
    NativeAttachmentTooLargeError,
    base64_byte_length,
    check_block_bytes,
    check_total_bytes,
    document_max_bytes,
    image_max_bytes,
    total_max_bytes,
)
from tests._config_layer import set_config


def _b64_of(byte_length: int) -> str:
    return base64.b64encode(b"\0" * byte_length).decode("ascii")


def test_base64_length_is_arithmetic_and_matches_a_real_decode() -> None:
    """The whole point is measuring WITHOUT decoding, so the arithmetic must be exact."""

    for size in (0, 1, 2, 3, 4, 5, 100, 1023, 4096):
        encoded = _b64_of(size)
        assert base64_byte_length(encoded) == size == len(base64.b64decode(encoded))
    # Whitespace-wrapped payloads (a common data-URI shape) measure the same.
    wrapped = "\n".join(_b64_of(300)[i : i + 76] for i in range(0, 400, 76))
    assert base64_byte_length(wrapped) == len(base64.b64decode(wrapped))


def test_defaults_track_the_real_provider_limits() -> None:
    assert image_max_bytes() == 5 * 1024 * 1024
    assert document_max_bytes() == 32 * 1024 * 1024
    assert total_max_bytes() == 32 * 1024 * 1024


def test_per_kind_and_aggregate_bounds_raise_typed_reasons() -> None:
    check_block_bytes("image", image_max_bytes())
    with pytest.raises(NativeAttachmentTooLargeError) as image_exc:
        check_block_bytes("image", image_max_bytes() + 1, label="cell.png")
    assert image_exc.value.reason == "native_attachment_block_too_large"
    assert "cell.png" in str(image_exc.value)

    check_total_bytes(total_max_bytes())
    with pytest.raises(NativeAttachmentTooLargeError) as total_exc:
        check_total_bytes(total_max_bytes() + 1)
    assert total_exc.value.reason == "native_attachment_total_too_large"
    assert set(NATIVE_ATTACHMENT_REFUSAL_REASONS) == {
        "native_attachment_block_too_large",
        "native_attachment_total_too_large",
    }


def test_the_bounds_are_conf_resolved_not_hardcoded() -> None:
    set_config("resources.native_image_max_bytes", 1024)
    assert image_max_bytes() == 1024
    with pytest.raises(NativeAttachmentTooLargeError):
        check_block_bytes("image", 1025)


def test_claude_refuses_an_oversized_image_before_it_is_expanded() -> None:
    set_config("resources.native_image_max_bytes", 1024)

    with pytest.raises(NativeAttachmentTooLargeError, match="per-image ceiling"):
        native_blocks([ImagePart(data=_b64_of(4096), media_type="image/png")])


def test_claude_refuses_an_oversized_pdf() -> None:
    set_config("resources.native_document_max_bytes", 512)

    with pytest.raises(NativeAttachmentTooLargeError, match="per-document ceiling"):
        native_blocks([DocumentPart(data=_b64_of(4096), media_type="application/pdf")])


def test_claude_refuses_individually_legal_attachments_that_sum_past_the_request_bound() -> None:
    """The aggregate is a distinct bound: each block passes, the request does not."""

    set_config("resources.native_image_max_bytes", 4096)
    set_config("resources.native_attachment_total_max_bytes", 5000)
    image = ImagePart(data=_b64_of(3000), media_type="image/png")

    native_blocks([image])  # one is within both bounds
    with pytest.raises(NativeAttachmentTooLargeError, match="per-request ceiling"):
        native_blocks([image, image])


# NOTE (engine port): the Codex direct engine hands images/files to lm15's Responses
# payload builder with no provider-level re-check; the ingestion-level check in
# ``gact/messaging.py`` still applies before any provider is invoked.
