"""Per-message context token estimates (gact-tui#739)."""

from __future__ import annotations

from clio_agent.gact.message_context_tokens import (
    estimate_message_context_tokens,
    estimate_text_tokens,
    message_context_text,
)
from clio_agent.gact.types import Message, Part, Tokens


def _assistant(parts: list[Part], **tokens: int) -> Message:
    return Message(
        id="msg_asst_1",
        session_id="sess_1",
        role="assistant",
        created_at="2026-11-10T00:00:00+00:00",
        updated_at="2026-11-10T00:00:00+00:00",
        parts=parts,
        tokens=Tokens(**tokens),
    )


def test_estimate_text_tokens_is_chars_div_4_with_floor() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hi") == 1
    assert estimate_text_tokens("x" * 400) == 100


def test_provider_usage_never_counts() -> None:
    message = _assistant(
        [Part(id="p1", type="text", text="y" * 40)],
        input=30_000,
        output=12,
        cache_read=9_000,
        cache_write=500,
    )

    assert estimate_message_context_tokens(message) == 10


def test_tool_call_and_result_content_count_but_wire_only_payloads_do_not() -> None:
    call = Part(
        id="p1", type="tool_call", call_id="c1", tool_name="memory_search", input={"q": "n"}
    )
    result = Part(
        id="p2",
        type="tool_result",
        call_id="c1",
        content=[Part(id="p3", type="text", text="name: Jaime")],
        structured_content={"rows": ["z" * 10_000]},
    )
    text = message_context_text(_assistant([call, result]))

    assert "memory_search" in text
    assert '"q": "n"' in text
    assert "name: Jaime" in text
    assert "zzzz" not in text
    assert estimate_message_context_tokens(_assistant([call, result])) < 20
