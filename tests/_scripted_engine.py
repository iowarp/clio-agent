"""A scripted DSPy 3.4 LM engine for the agent-loop tests (no network).

Each scripted reply is one :class:`Reply` -- thinking, visible text, tool calls --
returned as a typed ``dspy.lm15.Response`` (and streamed as the matching events). Every
request the loop sends is kept on :attr:`ScriptedEngine.requests`, so tests assert the
exact wire (system, messages, tools, config).
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import dspy
from dspy.lm15 import (
    Message,
    Request,
    Response,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    Usage,
    response_to_events,
)

MODEL = "scripted/model"


@dataclass(frozen=True)
class Reply:
    """One scripted model reply."""

    text: str = ""
    calls: Sequence[tuple[str, dict[str, Any]]] = ()
    thinking: str = ""
    raises: BaseException | None = None
    call_ids: Sequence[str] = field(default=())
    finish_reason: str | None = None


def calls(*pairs: tuple[str, dict[str, Any]], text: str = "", thinking: str = "") -> Reply:
    """A reply that calls ``pairs`` (name, args) in order."""
    return Reply(text=text, calls=pairs, thinking=thinking)


class ScriptedEngine:
    """Sync engine: replies from the script in order; records every request."""

    supports_function_calling = True
    supports_reasoning = True

    def __init__(self, script: Sequence[Reply]) -> None:
        self.script = list(script)
        self.requests: list[Request] = []
        self._lock = threading.Lock()

    def _next(self, request: Request) -> Response:
        with self._lock:
            index = len(self.requests)
            self.requests.append(request)
        if index >= len(self.script):
            raise AssertionError(
                f"the loop called the model {index + 1} times; script has {len(self.script)}"
            )
        reply = self.script[index]
        if reply.raises is not None:
            raise reply.raises
        parts: list[Any] = []
        if reply.thinking:
            parts.append(ThinkingPart(text=reply.thinking))
        if reply.text:
            parts.append(TextPart(text=reply.text))
        ids = list(reply.call_ids) or [f"call_{index}_{i}" for i in range(len(reply.calls))]
        parts.extend(
            ToolCallPart(id=ids[i], name=name, input=dict(args))
            for i, (name, args) in enumerate(reply.calls)
        )
        return Response(
            id=f"resp_{index}",
            model=request.model,
            message=Message.assistant(parts or [TextPart(text="")]),
            finish_reason=reply.finish_reason or ("tool_call" if reply.calls else "stop"),
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def complete(self, request: Request) -> Response:
        return self._next(request)

    def stream(self, request: Request) -> Iterator[Any]:
        yield from response_to_events(self._next(request))

    def close(self) -> None:
        """Nothing to release."""


class AsyncScriptedEngine:
    """Async face of the same script (shares the sync engine's state)."""

    supports_function_calling = True
    supports_reasoning = True

    def __init__(self, sync: ScriptedEngine) -> None:
        self.sync = sync

    async def complete(self, request: Request) -> Response:
        return self.sync._next(request)

    async def stream(self, request: Request) -> AsyncIterator[Any]:
        for event in response_to_events(self.sync._next(request)):
            yield event

    async def aclose(self) -> None:
        """Nothing to release."""


def scripted_lm(script: Sequence[Reply], **kwargs: Any) -> tuple[dspy.LM, ScriptedEngine]:
    """A ``dspy.LM`` over the script, and the engine that records its requests."""
    engine = ScriptedEngine(script)
    lm = dspy.LM(
        MODEL,
        engine=engine,
        async_engine=AsyncScriptedEngine(engine),
        cache=False,
        num_retries=0,
        **kwargs,
    )
    return lm, engine


def wire(request: Request) -> list[tuple[str, list[Any]]]:
    """A request's messages as ``(role, [part summary...])`` for readable asserts."""
    return summarize(request.messages)


def summarize(messages: Sequence[Message]) -> list[tuple[str, list[Any]]]:
    """Typed messages as ``(role, [part summary...])`` for readable asserts."""
    out: list[tuple[str, list[Any]]] = []
    for message in messages:
        summary: list[Any] = []
        for part in message.parts:
            kind = type(part).__name__
            if kind == "TextPart":
                summary.append(("text", part.text))
            elif kind == "ThinkingPart":
                summary.append(("thinking", part.text))
            elif kind == "ToolCallPart":
                summary.append(("call", part.id, part.name, dict(part.input)))
            elif kind == "ToolResultPart":
                summary.append(("result", part.id, _result_text(part), part.is_error))
            else:
                summary.append((kind,))
        out.append((message.role, summary))
    return out


def _result_text(part: Any) -> str:
    return "".join(getattr(p, "text", "") for p in part.content)
