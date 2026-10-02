"""A Claude Code request is recorded by SHAPE, never by its attachment bytes.

The per-call record is the ``lm.call`` trace (:func:`clio_agent.lm.call_trace.call_record`):
it keeps how many parts a request carried, in what order, of what media type -- what
a multimodal dispatch bug looks like -- while images and documents are recorded as
their kind and media type only. On the wire, the engine sends attachments as native
content blocks beside the text (a streaming-input message), and the text query never
carries their base64 payload.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from dspy.lm15 import DocumentPart, ImagePart, Message, Request, TextPart

from clio_agent.lm.call_trace import call_record
from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake

_IMAGE_B64 = base64.b64encode(b"\x89PNG-bytes").decode("ascii")
_PDF_B64 = base64.b64encode(b"%PDF-1.4 body").decode("ascii")


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def _multimodal_request() -> Request:
    return Request(
        model="claude_code/sonnet",
        messages=(
            Message(
                role="user",
                parts=(
                    TextPart(text="read these"),
                    ImagePart(data=_IMAGE_B64, media_type="image/png"),
                    DocumentPart(data=_PDF_B64, media_type="application/pdf"),
                ),
            ),
        ),
    )


def test_the_call_record_keeps_the_shape_and_drops_the_bytes() -> None:
    record = call_record("claude_code/sonnet", {"prompt": _multimodal_request()}, None, None)

    assert record["messages"] == [
        {
            "role": "user",
            "parts": [
                {"type": "text", "text": "read these"},
                {"type": "image", "media_type": "image/png"},
                {"type": "document", "media_type": "application/pdf"},
            ],
        }
    ]
    serialized = json.dumps(record)
    assert _IMAGE_B64 not in serialized
    assert _PDF_B64 not in serialized


def test_a_text_only_request_is_recorded_as_is() -> None:
    request = Request(model="claude_code/sonnet", messages=(Message.user("hi"),))
    record = call_record("claude_code/sonnet", {"prompt": request}, None, None)
    assert record["messages"] == [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]


@pytest.mark.parametrize("with_attachments", [False, True])
async def test_attachments_ride_as_native_blocks_never_in_the_query_text(
    monkeypatch: pytest.MonkeyPatch, with_attachments: bool
) -> None:
    sdk = fake.install(monkeypatch)
    request = _multimodal_request() if with_attachments else fake.request()
    await fake.drive(request)

    [(sent, _session)] = sdk.queries()
    if not with_attachments:
        assert sent == "[user]\nhello"  # a plain string query: no blocks to carry
        return
    [message] = sent  # one streaming-input user message
    content = message["message"]["content"]
    assert [block["type"] for block in content] == ["image", "document", "text"]
    assert content[0]["source"]["data"] == _IMAGE_B64
    assert content[1]["source"]["media_type"] == "application/pdf"
    text = content[2]["text"]
    assert _IMAGE_B64 not in text and _PDF_B64 not in text
    assert "(image 1 attached)" in text and "(document 2 attached)" in text
