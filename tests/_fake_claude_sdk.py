"""A fake ``claude_agent_sdk`` for the Claude Code engine tests (no CLI, no network).

The engine dispatches on the SDK's message class NAMES, so the fakes carry the real
ones (``StreamEvent``, ``AssistantMessage``, ``ResultMessage``, ``RateLimitEvent``,
``TextBlock``). :func:`install` puts the module in ``sys.modules``; every connected
``ClaudeSDKClient`` records its options and each ``query(prompt, session_id)``, and
answers each query from a script (the same reply every time by default).

:func:`drive` runs one engine call through the REAL pooled session transport.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from dspy.lm15 import Message, Request, Response


class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class StreamEvent:
    def __init__(self, event: dict[str, Any]) -> None:
        self.event = event


class AssistantMessage:
    def __init__(
        self,
        text: str = "",
        *,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        stop_reason: str | None = "end_turn",
    ) -> None:
        self.content = [TextBlock(text)] if text else []
        self.usage = usage
        self.error = error
        self.stop_reason = stop_reason


class ResultMessage:
    def __init__(
        self,
        *,
        usage: dict[str, Any] | None = None,
        result: str | None = None,
        is_error: bool = False,
        api_error_status: int | None = None,
        subtype: str | None = None,
        stop_reason: str | None = "end_turn",
        total_cost_usd: float | None = None,
        model_usage: dict[str, Any] | None = None,
    ) -> None:
        self.usage = usage
        self.result = result
        self.is_error = is_error
        self.api_error_status = api_error_status
        self.subtype = subtype
        self.stop_reason = stop_reason
        self.total_cost_usd = total_cost_usd
        self.model_usage = model_usage


class RateLimitEvent:
    def __init__(self, status: str, rate_limit_type: str = "five_hour") -> None:
        self.rate_limit_info = SimpleNamespace(
            status=status, rate_limit_type=rate_limit_type, resets_at=None
        )


def text(chunk: str) -> StreamEvent:
    """A streamed text delta."""
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}
    )


def thinking(chunk: str) -> StreamEvent:
    """A streamed thinking delta."""
    return StreamEvent(
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": chunk}}
    )


def answer(reply: str = "Answer", *, usage: dict[str, Any] | None = None) -> list[Any]:
    """A plain streamed reply: two text deltas, the assistant message, the result."""
    usage = usage or {"input_tokens": 2, "output_tokens": 3}
    half = len(reply) // 2
    return [
        text(reply[:half]),
        text(reply[half:]),
        AssistantMessage(reply, usage=usage),
        ResultMessage(usage=usage, result=reply),
    ]


@dataclass
class FakeSDK:
    """What the fake module recorded, and the script it answers from."""

    reply: Callable[[], list[Any]] = answer
    script: list[list[Any] | BaseException] = field(default_factory=list)
    clients: list[Any] = field(default_factory=list)
    constructed: int = 0
    connected: int = 0
    connect_delay: float = 0.0
    events: list[tuple[str, float]] = field(default_factory=list)

    def queries(self) -> list[tuple[Any, str]]:
        """Every ``(prompt, session_id)`` sent, across all clients."""
        return [q for client in self.clients for q in client.queries]

    def next_reply(self) -> list[Any]:
        if self.script:
            turn = self.script.pop(0)
            if isinstance(turn, BaseException):
                raise turn
            return turn
        return self.reply()


def install(monkeypatch: pytest.MonkeyPatch, **config: Any) -> FakeSDK:
    """Install the fake ``claude_agent_sdk`` module; returns its recorder."""
    import time  # noqa: PLC0415

    state = FakeSDK(**config)

    class ClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            for name, value in kwargs.items():
                setattr(self, name, value)

    class ClaudeSDKClient:
        def __init__(self, options: ClaudeAgentOptions) -> None:
            state.constructed += 1
            self.options = options
            self.queries: list[tuple[Any, str]] = []
            self.disconnected = False
            state.clients.append(self)

        async def connect(self) -> None:
            state.events.append(("connect_enter", time.time()))
            if state.connect_delay:
                await asyncio.sleep(state.connect_delay)
            state.connected += 1

        async def disconnect(self) -> None:
            state.events.append(("disconnect_enter", time.time()))
            self.disconnected = True

        async def interrupt(self) -> None:
            return None

        async def set_model(self, model: str) -> None:
            self.options.kwargs["model"] = model

        async def query(self, prompt: Any, session_id: str = "default") -> None:
            if not isinstance(prompt, str):
                prompt = [message async for message in prompt]
            self.queries.append((prompt, session_id))

        async def receive_response(self) -> Any:
            for message in state.next_reply():
                if isinstance(message, BaseException):
                    raise message
                yield message

    module = ModuleType("claude_agent_sdk")
    for name, value in {
        "AssistantMessage": AssistantMessage,
        "ClaudeAgentOptions": ClaudeAgentOptions,
        "ClaudeSDKClient": ClaudeSDKClient,
        "RateLimitEvent": RateLimitEvent,
        "ResultMessage": ResultMessage,
        "StreamEvent": StreamEvent,
        "TextBlock": TextBlock,
    }.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return state


def request(*messages: Message, system: str = "", model: str = "haiku") -> Request:
    """A request for the engine (one user message when none is given)."""
    return Request(
        model=f"claude_code/{model}",
        system=system or None,
        messages=messages or (Message.user("hello"),),
    )


async def drive(request_: Request, **engine: Any) -> Response:
    """Run one call through the real engine and pooled transport."""
    from clio_agent.providers.claude_code_engine import AsyncClaudeCodeEngine  # noqa: PLC0415

    model = request_.model.removeprefix("claude_code/")
    engine.setdefault("cwd", "/w")
    engine.setdefault("idle_timeout_s", 5.0)
    return await AsyncClaudeCodeEngine(model, **engine).complete(request_)
