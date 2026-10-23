"""A formatting-only reply must yield protocol feedback instead of streaming forever."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import FunctionTool, Message, Request

from clio_agent.lm.engines.text_tools import FENCE, INVALID_TOOL_CALL
from clio_agent.providers import claude_code_engine
from clio_agent.providers import claude_code_sessions as sessions
from clio_agent.providers.claude_code_engine import _Turn
from clio_agent.providers.claude_code_output_progress import FormattingProgress


@dataclass
class StreamEvent:
    """SDK-shaped input used only by the unit test."""

    event: dict[str, Any]


def push(turn: _Turn, text: str) -> list[Any]:
    """Send one ordinary SDK text delta through the production folding path."""
    return turn.handle(
        StreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
    )


@pytest.mark.parametrize("width", [1, 3, 7, 40])
def test_formatting_loop_returns_bounded_protocol_feedback(width: int) -> None:
    turn = _Turn("haiku", available_tools=frozenset({"query_tasks"}))
    reply = "\n\n\n<br>" * 2000
    for index in range(0, len(reply), width):
        push(turn, reply[index : index + width])
        if getattr(turn, "protocol_error", None):
            break
    push(turn, FENCE + '\n[{"name":"query_tasks","arguments":{}}]\n```')
    calls = [
        event.delta
        for event in turn.finish("recorded")
        if event.type == "delta" and event.delta.type == "tool_call"
    ]
    assert len(calls) == 1 and calls[0].name == INVALID_TOOL_CALL
    assert turn.protocol_error == "formatting_only_repetition"
    assert len(turn.reply) < 5000
    feedback = json.loads(calls[0].input)
    assert feedback["reason"] == "formatting_only_repetition"
    assert len(json.dumps(feedback)) < 1000


@pytest.mark.parametrize(
    "reply",
    [
        "A useful answer.\n" * 2000,
        "Show line breaks: <br>\n" * 2000,
        "\n<br>" * 20,
        "_tool_calls\n" * 3 + "I'll save that.\n",
        FENCE
        + "\n"
        + json.dumps([{"name": "query_tasks", "arguments": {"description": "\n<br>" * 2000}}])
        + "\n```",
    ],
    ids=["useful_answer", "useful_html", "short_breaks", "a_few_stray_words", "tool_arguments"],
)
def test_meaningful_long_output_and_tool_arguments_remain_valid(reply: str) -> None:
    turn = _Turn("haiku", available_tools=frozenset({"query_tasks"}))
    for index in range(0, len(reply), 3):
        push(turn, reply[index : index + 3])
    assert getattr(turn, "protocol_error", None) is None
    assert turn.reply == reply


def test_short_formatting_can_precede_a_real_tool_call() -> None:
    turn = _Turn("haiku", available_tools=frozenset({"query_tasks"}))
    push(turn, "<br>\n" * 20)
    push(turn, FENCE + '\n[{"name":"query_tasks","arguments":{}}]\n```')
    calls = [
        event.delta
        for event in turn.finish("valid")
        if event.type == "delta" and event.delta.type == "tool_call"
    ]
    assert len(calls) == 1 and calls[0].name == "query_tasks"


def test_plain_model_without_declared_tools_keeps_requested_formatting() -> None:
    turn = _Turn("haiku")
    reply = "<br>\n" * 2000
    push(turn, reply)
    assert getattr(turn, "protocol_error", None) is None
    assert turn.reply == reply


def test_tag_variants_and_fragmentation_share_one_budget() -> None:
    progress = FormattingProgress(enabled=True)
    reply = "\t<BR/>\n<br />\r<br>" * 100
    stopped = None
    for index, character in enumerate(reply):
        if progress.cutoff(character) is not None:
            stopped = index
            break
    assert stopped is not None
    assert reply[: stopped + 1].count(">") == 256


def test_assembled_sdk_result_gets_the_same_bounded_recovery() -> None:
    turn = _Turn("haiku", available_tools=frozenset({"query_tasks"}))
    turn.fallback = "<br>\n" * 2000
    closing = turn.finish("assembled")
    calls = [
        event.delta
        for event in closing
        if event.type == "delta" and event.delta.type == "tool_call"
    ]
    assert turn.protocol_error == "formatting_only_repetition"
    assert len(turn.fallback) <= 4096
    assert len(calls) == 1 and calls[0].name == INVALID_TOOL_CALL
    assert not any(event.type == "delta" and event.delta.type == "text" for event in closing)


class RepeatingEntry:
    """Never-finished SDK stream with an observable close, confined to this test."""

    def __init__(self) -> None:
        self.closed = False
        self.read = 0

    async def stream(self, **_kwargs: Any) -> AsyncIterator[StreamEvent]:
        try:
            for _ in range(2000):
                self.read += 1
                yield StreamEvent(
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "<br>\n"},
                    }
                )
            raise AssertionError("The engine exhausted a non-progressing provider response")
        finally:
            self.closed = True


def test_engine_closes_nonprogressing_stream_before_returning_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = RepeatingEntry()
    monkeypatch.setattr(claude_code_engine, "require_claude_agent_sdk", lambda: None)
    monkeypatch.setattr(
        claude_code_engine,
        "_STREAM_CLIENT_POOL",
        SimpleNamespace(entry_for=lambda **_kwargs: entry, bump_construct=lambda: None),
    )
    request = Request(
        model="claude_code/haiku",
        messages=(Message.user("Continue with the tasks."),),
        tools=(FunctionTool(name="query_tasks", description="Read tasks.", parameters={}),),
    )
    response = asyncio.run(claude_code_engine.AsyncClaudeCodeEngine("haiku").complete(request))
    calls = [part for part in response.message.parts if part.type == "tool_call"]
    assert entry.closed
    assert entry.read == 256
    assert len(calls) == 1 and calls[0].name == INVALID_TOOL_CALL
    assert calls[0].input["reason"] == "formatting_only_repetition"


class IdleClient:
    """A no-network client owned only by the transport-lifecycle test."""

    async def disconnect(self) -> None:
        return None


@pytest.mark.asyncio
async def test_abandoned_stream_is_dead_before_deferred_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = sessions.ClaudeStreamClientPool(max_concurrent=2)
    entry = pool.entry_for(session_id="formatting-test")
    release = threading.Event()
    caller_loop = asyncio.get_running_loop()
    reset_entered = asyncio.Event()
    drops: list[str] = []
    original_reset = entry._mark_dead_and_reset

    async def connect(*_args: Any, **_kwargs: Any) -> IdleClient:
        entry._client = IdleClient()
        entry._bound_session = "formatting-test"
        return entry._client

    async def relay(_client: Any, _query: Any, **kwargs: Any) -> None:
        kwargs["put"]("first")
        await asyncio.to_thread(release.wait)

    async def deferred_reset() -> None:
        caller_loop.call_soon_threadsafe(reset_entered.set)
        await asyncio.to_thread(release.wait)
        await original_reset()

    monkeypatch.setattr(entry, "_ensure_client", connect)
    monkeypatch.setattr(entry, "_mark_dead_and_reset", deferred_reset)
    monkeypatch.setattr(sessions, "relay_with_idle_bound", relay)
    monkeypatch.setattr(sessions, "_notify_client_dropped", drops.append)
    stream = entry.stream(
        payload="request",
        native_blocks=[],
        session_id="sdk-test",
        idle_timeout=None,
        on_construct=lambda: None,
    )
    closing: asyncio.Future[None] | None = None
    try:
        assert await anext(stream) == "first"
        closing = asyncio.ensure_future(stream.aclose())
        await asyncio.wait_for(reset_entered.wait(), 5)
        assert not closing.done()
        assert entry.dead
        assert drops == ["formatting-test"]
        assert pool.entry_for(session_id="formatting-test") is not entry
    finally:
        release.set()
        try:
            if closing is None:
                await stream.aclose()
            else:
                await closing
        finally:
            entry.close_blocking()
            pool.release_session_resources("formatting-test")


@pytest.mark.parametrize("word", ["_tool_calls_placeholder", "_check_placeholder", "_memory"])
def test_a_repeated_placeholder_word_gets_the_same_bounded_recovery(word: str) -> None:
    """Asked to remember, a model looped on one placeholder word for ten minutes before
    its call (memory ledger MD51); the reply is cut and the model gets the feedback."""
    turn = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    reply = f"{word}\n\n" * 2000
    for index in range(0, len(reply), 5):
        push(turn, reply[index : index + 5])
        if turn.protocol_error:
            break
    assert turn.protocol_error == "formatting_only_repetition"
    assert turn.reply.count(word) == 32
    calls = [
        event.delta
        for event in turn.finish("looped")
        if event.type == "delta" and event.delta.type == "tool_call"
    ]
    assert len(calls) == 1 and calls[0].name == INVALID_TOOL_CALL


def _shown(events: list[Any]) -> str:
    return "".join(e.delta.text for e in events if e.type == "delta" and e.delta.type == "text")


def test_a_reply_of_a_repeated_word_is_never_shown_and_never_the_answer() -> None:
    """Twice is enough at the end of a reply: the turn gets feedback, not that answer."""
    turn = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    streamed = push(turn, "_call_placeholder\n\n") + push(turn, "_call_placeholder")
    closing = turn.finish("short")
    assert _shown(streamed + closing) == ""
    calls = [e.delta for e in closing if e.type == "delta" and e.delta.type == "tool_call"]
    assert len(calls) == 1 and calls[0].name == INVALID_TOOL_CALL
    assert json.loads(calls[0].input)["reason"] == "formatting_only_repetition"


def test_a_looping_word_is_held_back_until_it_is_cut() -> None:
    turn = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    streamed: list[Any] = []
    for _ in range(40):
        streamed += push(turn, "_tool_calls_placeholder\n\n")
    assert turn.protocol_error == "formatting_only_repetition"
    assert _shown(streamed + turn.finish("looped")) == ""


def test_words_stream_at_once_and_a_one_word_answer_is_still_an_answer() -> None:
    turn = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    assert _shown(push(turn, "I'll save that ")) == "I'll save that "
    one = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    held = push(one, "42")
    assert _shown(held) == ""
    closing = one.finish("short answer")
    assert _shown(closing) == "42"
    assert not [e for e in closing if e.type == "delta" and e.delta.type == "tool_call"]


def test_a_single_word_beside_calls_is_not_shown() -> None:
    """The step's only text was one placeholder word and the reply still made its call."""
    turn = _Turn("haiku", available_tools=frozenset({"create_memory"}))
    streamed = push(turn, "_tool_call_placeholder\n")
    turn.reply += FENCE + '\n[{"name":"create_memory","arguments":{}}]\n```'  # assembled block
    closing = turn.finish("beside a call")
    assert _shown(streamed + closing) == ""
    calls = [e.delta for e in closing if e.type == "delta" and e.delta.type == "tool_call"]
    assert [c.name for c in calls] == ["create_memory"]
