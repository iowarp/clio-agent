"""A reply that goes on past its call block stops there instead of streaming to its limit.

Seen live (haiku, campaign 2026-11-12): one complete ``<invoke>``, then an invented
``<invoke_result>`` error repeated 3,034 times, for 6 minutes and 128,000 output tokens,
all of it discarded at the end as ``reply_past_calls``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import FunctionTool, Message, Request

from clio_agent.lm.engines.invoke_calls import writing_past_block
from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_engine import _Turn

KNOWN = frozenset({"fs_read_file", "fs_glob"})
READ = '<invoke name="fs_read_file">\n<parameter name="path">/w/m.csv</parameter>\n</invoke>'
GLOB = '<invoke name="fs_glob">\n<parameter name="pattern">**/*.csv</parameter>\n</invoke>'
INVENTED = "\n\n<invoke_result>\nError: Invalid tool name: fs_read_file\n</invoke_result>" * 3034


@dataclass
class StreamEvent:
    """SDK-shaped input used only by the unit test."""

    event: dict[str, Any]


def _delta(text: str) -> StreamEvent:
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    )


def _calls(turn: _Turn) -> list[Any]:
    return [
        event.delta
        for event in turn.finish("past")
        if event.type == "delta" and event.delta.type == "tool_call"
    ]


@pytest.mark.parametrize(
    "past",
    ["\n<invoke_result>\nError", "\nBased on the file", "\n<function_calls>\n<invoke"],
    ids=["invented_result", "prose_about_results", "a_later_block"],
)
def test_text_past_the_block_is_recognized(past: str) -> None:
    assert writing_past_block(f"<function_calls>\n{READ}\n</function_calls>{past}", KNOWN)


@pytest.mark.parametrize(
    "tail",
    ["", "\n", "\n<", "\n<inv", '\n<invoke name="fs_gl', "\n</function_ca", "\n</invoke>"],
)
def test_what_may_still_continue_the_block_is_not_past_it(tail: str) -> None:
    assert not writing_past_block(f"<function_calls>\n{READ}{tail}", KNOWN)


@pytest.mark.parametrize("width", [1, 3, 17])
def test_a_block_of_several_calls_streams_whole(width: int) -> None:
    reply = f"I'll look.\n<function_calls>\n{GLOB}\n{READ}\n</function_calls>\n"
    turn = _Turn("haiku", available_tools=KNOWN)
    for index in range(0, len(reply), width):
        turn.handle(_delta(reply[index : index + width]))
    assert not turn.past_calls
    assert [call.name for call in _calls(turn)] == ["fs_glob", "fs_read_file"]


class InventingEntry:
    """The live shape: a block, then invented results without end. Observable close."""

    def __init__(self) -> None:
        self.closed = False
        self.read = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[StreamEvent]:
        reply = f"<function_calls>\n{READ}\n</function_calls>{INVENTED}"
        try:
            for index in range(0, len(reply), 7):
                self.read += 7
                yield _delta(reply[index : index + 7])
            raise AssertionError("The engine read the invented results to the end")
        finally:
            self.closed = True


def test_engine_stops_the_stream_past_the_block_and_runs_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = InventingEntry()
    monkeypatch.setattr(claude_code_engine, "require_claude_agent_sdk", lambda: None)
    monkeypatch.setattr(
        claude_code_engine,
        "_STREAM_CLIENT_POOL",
        SimpleNamespace(entry_for=lambda **_kwargs: entry, bump_construct=lambda: None),
    )
    request = Request(
        model="claude_code/haiku",
        messages=(Message.user("Compare January and February."),),
        tools=(
            FunctionTool(name="fs_read_file", description="Read a file.", parameters={}),
            FunctionTool(name="fs_glob", description="Find files.", parameters={}),
        ),
    )
    response = asyncio.run(claude_code_engine.AsyncClaudeCodeEngine("haiku").complete(request))
    calls = [part for part in response.message.parts if part.type == "tool_call"]
    assert entry.closed
    assert entry.read < 600  # the block is ~120 characters; the invented rest is 222,000
    assert [call.name for call in calls] == ["fs_read_file"]
    assert calls[0].input == {"path": "/w/m.csv"}
