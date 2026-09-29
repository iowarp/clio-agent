"""The Claude Code engine over the real pooled transport (a fake SDK, no CLI).

Pins what is specific to the provider boundary:

* SDK stream events become typed lm15 events: thinking deltas as thinking, text
  deltas as text, the call's usage at the end;
* the pooled client is a bare model: Claude Code's own tools off and partial
  messages on (clio's loop owns every tool);
* an async call never holds the caller's event loop (#1333): the pooled session is
  driven on its own loop, so the awaiting loop keeps running while the SDK works.

The request/response mapping itself (text tool protocol, native media, kept
conversations, typed refusals) is pinned in ``tests/test_providers/test_claude_code_*``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    """Each test starts with a clean client pool and conversation registry."""
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


async def test_the_engine_translates_sdk_stream_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(
        monkeypatch,
        reply=lambda: [
            fake.thinking("plan"),
            fake.text("Hel"),
            fake.text("lo"),
            fake.AssistantMessage("Hello", usage={"input_tokens": 2, "output_tokens": 3}),
            fake.ResultMessage(usage={"input_tokens": 2, "output_tokens": 3}, result="Hello"),
        ],
    )
    engine = AsyncClaudeCodeEngine("haiku", cwd="/w", timeout=5.0)
    events = [event async for event in engine.stream(fake.request())]

    deltas = [e.delta for e in events if e.type == "delta"]
    assert [(d.type, d.text) for d in deltas] == [
        ("thinking", "plan"),
        ("text", "Hel"),
        ("text", "lo"),
    ]
    end = events[-1]
    assert (end.type, end.finish_reason) == ("end", "stop")
    assert (end.usage.input_tokens, end.usage.output_tokens) == (2, 3)


async def test_the_pooled_client_is_a_bare_model(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(fake.request())

    options = sdk.clients[0].options.kwargs
    assert options["tools"] == [] and options["allowed_tools"] == []
    assert options["include_partial_messages"] is True
    assert options["mcp_servers"] == {} and options["setting_sources"] == []


def test_an_async_call_does_not_hold_the_callers_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1333: the finalize goal judge awaits this path on the server loop, so the SDK
    work has to happen off it. A reply parked on a threading.Event proves the awaiting
    loop keeps yielding, then the release lets the response through."""
    started = threading.Event()
    release = threading.Event()
    sdk = fake.install(monkeypatch)
    import sys  # noqa: PLC0415

    module = sys.modules["claude_agent_sdk"]
    base = module.ClaudeSDKClient  # type: ignore[attr-defined]

    class _ParkedClient(base):  # type: ignore[misc, valid-type]
        async def receive_response(self) -> Any:
            started.set()
            while not release.is_set():  # parks on the pool's own loop, not the caller's
                await asyncio.sleep(0.01)
            for message in fake.answer("judged"):
                yield message

    module.ClaudeSDKClient = _ParkedClient  # type: ignore[attr-defined]

    async def exercise() -> None:
        task = asyncio.create_task(fake.drive(fake.request(model="sonnet")))
        while not started.is_set():
            await asyncio.sleep(0.01)
        # A bridge blocking this loop could not let this coroutine resume here.
        assert not task.done()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        response = await task
        assert response.message.parts[0].text == "judged"

    asyncio.run(exercise())
    assert sdk.constructed == 1
