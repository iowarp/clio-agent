"""Part detection at the Claude attach boundary: by shape, never by key presence."""

from __future__ import annotations

import base64

import pytest

from clio_agent.providers.claude_code_litellm import (
    ClaudeCodeUnsupportedMultimodalError,
    _messages_to_claude_input,
)
from tests._config_layer import set_config

_IMAGE_B64 = base64.b64encode(b"png-bytes").decode("ascii")
_PDF_B64 = base64.b64encode(b"%PDF-1.4").decode("ascii")


def _user(*parts: object) -> list[dict[str, object]]:
    return [{"role": "user", "content": list(parts)}]


def test_an_anthropic_shaped_image_part_is_read_on_its_own_terms() -> None:
    """A producer already speaking Anthropic blocks must not be routed to the OpenAI reader.

    ``{"type": "image", "source": {...}}`` has no ``image_url``, so the old reader
    raised "native Claude image parts require a non-empty URL" on a perfectly
    valid part.
    """

    _prompt, blocks = _messages_to_claude_input(
        _user(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": _IMAGE_B64},
            }
        )
    )

    assert blocks == [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _IMAGE_B64},
        }
    ]


def test_an_anthropic_shaped_document_part_is_read_on_its_own_terms() -> None:
    _prompt, blocks = _messages_to_claude_input(
        _user(
            {
                "type": "document",
                "title": "paper.pdf",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _PDF_B64,
                },
            }
        )
    )

    assert blocks[0]["type"] == "document"
    assert blocks[0]["title"] == "paper.pdf"
    assert blocks[0]["source"]["data"] == _PDF_B64


def test_a_text_part_carrying_a_file_key_is_not_misclassified() -> None:
    """Key presence was the test; a typed part is now classified by its type."""

    prompt, blocks = _messages_to_claude_input(
        _user({"type": "text", "text": "see the file", "file": "notes.txt"})
    )

    assert blocks == []
    assert "see the file" in prompt


def test_a_text_part_carrying_an_image_url_key_is_not_misclassified() -> None:
    prompt, blocks = _messages_to_claude_input(
        _user({"type": "text", "text": "the image_url field was empty"})
    )
    assert blocks == []
    assert "image_url" in prompt


def test_an_untyped_part_still_falls_back_to_key_inspection() -> None:
    """Only a part declaring NO type at all is classified by its keys."""

    _prompt, blocks = _messages_to_claude_input(
        _user({"image_url": {"url": f"data:image/png;base64,{_IMAGE_B64}"}})
    )
    assert [block["type"] for block in blocks] == ["image"]


def test_a_genuinely_malformed_image_part_is_a_typed_refusal() -> None:
    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="non-empty URL"):
        _messages_to_claude_input(_user({"type": "image"}))

    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="must be an object"):
        _messages_to_claude_input(_user({"type": "image", "source": "not-an-object"}))

    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="unsupported native Claude"):
        _messages_to_claude_input(
            _user(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "application/zip", "data": "x"},
                }
            )
        )

    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="source type"):
        _messages_to_claude_input(
            _user({"type": "document", "source": {"type": "file_id", "file_id": "f_1"}})
        )


def test_a_remote_image_url_is_refused_unless_its_host_is_allowlisted() -> None:
    """A remote URL makes the PROVIDER fetch bytes CLIO never saw and cannot size."""

    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="not in"):
        _messages_to_claude_input(
            _user({"type": "image_url", "image_url": {"url": "https://cdn.test/a.png"}})
        )


def test_an_allowlisted_remote_image_url_is_permitted_and_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_config("providers.native_image_url_allowlist", "cdn.test, other.test")

    with caplog.at_level("INFO", logger="clio_agent.providers.claude_code_multimodal"):
        _prompt, blocks = _messages_to_claude_input(
            _user({"type": "image_url", "image_url": {"url": "https://cdn.test/a.png"}})
        )

    assert blocks == [{"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.png"}}]
    assert any("native_image_url_egress" in record.message for record in caplog.records)


def test_a_non_http_non_data_scheme_is_refused() -> None:
    with pytest.raises(ClaudeCodeUnsupportedMultimodalError, match="data: URI or an http"):
        _messages_to_claude_input(
            _user({"type": "image_url", "image_url": {"url": "file:///etc/passwd"}})
        )
