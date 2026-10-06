"""The Claude attach boundary: typed image/document parts to native content blocks.

:func:`clio_agent.providers.claude_code_multimodal.native_blocks` reads each typed
``dspy.lm15`` part on its own terms (an ``ImagePart`` is an image, a ``DocumentPart`` a
document -- no key sniffing), refuses what Claude cannot take (a typed ``ValueError``,
never a silent drop), and makes remote-image egress explicit: refused unless the host
is allowlisted, and logged when permitted.
"""

from __future__ import annotations

import base64

import pytest
from dspy.lm15 import DocumentPart, ImagePart

from clio_agent.providers.claude_code_multimodal import native_blocks
from tests._config_layer import set_config

_IMAGE_B64 = base64.b64encode(b"png-bytes").decode("ascii")
_PDF_B64 = base64.b64encode(b"%PDF-1.4").decode("ascii")


def test_inline_image_and_pdf_become_base64_blocks_in_order() -> None:
    blocks = native_blocks(
        [
            ImagePart(data=_IMAGE_B64, media_type="image/png"),
            DocumentPart(data=_PDF_B64, media_type="application/pdf"),
        ]
    )
    assert blocks == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _IMAGE_B64},
        },
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": _PDF_B64},
        },
    ]


def test_no_parts_is_no_blocks() -> None:
    assert native_blocks([]) == []


def test_an_unsupported_media_type_is_a_typed_refusal() -> None:
    with pytest.raises(ValueError, match="unsupported native Claude media type 'application/zip'"):
        native_blocks([ImagePart(data=_IMAGE_B64, media_type="application/zip")])
    with pytest.raises(ValueError, match="unsupported native Claude media type 'text/plain'"):
        native_blocks([DocumentPart(data=_PDF_B64, media_type="text/plain")])


def test_a_part_without_inline_bytes_or_a_url_is_a_typed_refusal() -> None:
    """A provider file id is not portable to Claude; a document is inline only."""
    with pytest.raises(ValueError, match="inline data or an http"):
        native_blocks([ImagePart(file_id="file_1", media_type="image/png")])
    with pytest.raises(ValueError, match="needs inline base64 data"):
        native_blocks([DocumentPart(url="https://cdn.test/a.pdf", media_type="application/pdf")])


def test_a_remote_image_url_is_refused_unless_its_host_is_allowlisted() -> None:
    """A remote URL makes the PROVIDER fetch bytes CLIO never saw and cannot size."""

    with pytest.raises(ValueError, match="not in"):
        native_blocks([ImagePart(url="https://cdn.test/a.png", media_type="image/png")])


def test_an_allowlisted_remote_image_url_is_permitted_and_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_config("providers.native_image_url_allowlist", "cdn.test, other.test")

    with caplog.at_level("INFO", logger="clio_agent.providers.claude_code_multimodal"):
        blocks = native_blocks([ImagePart(url="https://cdn.test/a.png", media_type="image/png")])

    assert blocks == [{"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.png"}}]
    assert any("native_image_url_egress" in record.message for record in caplog.records)


def test_a_non_http_scheme_is_refused() -> None:
    with pytest.raises(ValueError, match="inline data or an http"):
        native_blocks([ImagePart(url="file:///etc/passwd", media_type="image/png")])
