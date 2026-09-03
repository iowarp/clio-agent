"""The provider request trace keeps the request SHAPE, minus the bytes.

The multimodal change deleted the ``messages`` record from both Claude Code
entry points rather than redacting it, so the trace stopped showing how many
parts a request carried, in what order, of what media type -- exactly what a
multimodal dispatch bug looks like. The record is back, with attachment
payloads replaced by a typed marker naming the size it stood in for.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from clio_agent.providers import claude_code_litellm
from clio_agent.providers.claude_code_litellm import ClaudeCodeLLM
from clio_agent.providers.claude_code_multimodal import (
    REDACTED_ATTACHMENT_DATA,
    redact_message_attachments,
)

_IMAGE_B64 = base64.b64encode(b"png-bytes-here").decode("ascii")


def test_redaction_preserves_structure_and_names_the_elided_size() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare these"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_IMAGE_B64}"}},
                {
                    "type": "file",
                    "file": {
                        "file_data": "data:application/pdf;base64,cGRmLWJ5dGVz",
                        "filename": "paper.pdf",
                    },
                },
            ],
        }
    ]

    redacted = redact_message_attachments(messages)

    # Structure: same message, same three parts, same order, same types.
    parts = redacted[0]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url", "file"]
    assert parts[0]["text"] == "compare these"
    assert parts[2]["file"]["filename"] == "paper.pdf"
    # Payloads: replaced by a typed marker carrying the source byte count.
    assert parts[1]["image_url"]["url"] == REDACTED_ATTACHMENT_DATA.format(
        length=len(b"png-bytes-here")
    )
    assert "redacted" in parts[2]["file"]["file_data"]
    # And no attachment bytes survive anywhere in the record.
    assert _IMAGE_B64 not in json.dumps(redacted)
    assert "cGRmLWJ5dGVz" not in json.dumps(redacted)


def test_redaction_leaves_a_text_only_request_untouched() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert redact_message_attachments(messages) == messages


def test_a_remote_url_is_not_mistaken_for_an_elided_payload() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://a/b.png"}}],
        }
    ]
    assert redact_message_attachments(messages)[0]["content"][0]["image_url"]["url"] == (
        "https://a/b.png"
    )


@pytest.mark.parametrize("native_blocks_present", [True, False])
def test_the_sdk_seam_passes_native_blocks_either_way(
    monkeypatch: pytest.MonkeyPatch, native_blocks_present: bool
) -> None:
    """One choice for the seam: every call site passes native_blocks, empty or not.

    The completion path used to pass it conditionally while every other call site
    passed it unconditionally, so the EMPTY case exercised a different signature
    from the non-empty one -- and only the non-empty one had a test.
    """

    seen: dict[str, Any] = {}

    def _fake_sdk(
        *,
        prompt: str,
        native_blocks: list[dict[str, Any]],
        model: str,
        timeout: float,
        cwd: str | None,
        thinking: Any,
    ) -> tuple[str, dict[str, int]]:
        seen["native_blocks"] = native_blocks
        seen["prompt"] = prompt
        return "answered", {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(claude_code_litellm, "_run_sdk", _fake_sdk)
    content: list[dict[str, Any]] = [{"type": "text", "text": "read it"}]
    if native_blocks_present:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_IMAGE_B64}"}}
        )

    ClaudeCodeLLM().completion(
        model="claude_code/sonnet",
        messages=[{"role": "user", "content": content}],
        api_base="",
        custom_prompt_dict={},
        model_response=MagicMock(),
        print_verbose=None,
        encoding=None,
        api_key=None,
        logging_obj=None,
        optional_params={"claude_code_transport": "sdk"},
    )

    # The keyword is ALWAYS supplied -- the fake's signature makes it required,
    # so a conditional caller would raise here on the empty case.
    assert "native_blocks" in seen
    assert len(seen["native_blocks"]) == (1 if native_blocks_present else 0)
    assert _IMAGE_B64 not in seen["prompt"]
