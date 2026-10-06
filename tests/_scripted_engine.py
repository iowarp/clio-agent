"""A scripted DSPy 3.4 LM engine for the agent-loop tests (no network).

Each scripted reply is one :class:`Reply` -- thinking, visible text, tool calls --
returned as a typed ``dspy.lm15.Response`` (and streamed as the matching events). Every
request the loop sends is kept on :attr:`ScriptedEngine.requests`, so tests assert the
exact wire (system, messages, tools, config).
"""

from __future__ import annotations

import asyncio
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


class RoutedEngine(ScriptedEngine):
    """Replies per conversation: each request is routed by the scope in its prompt-cache
    key (``clio:<session>:<scope>``), so parallel variant tries each get their own script
    whatever order their requests arrive in.

    ``overlap``: the first request of every ``#run`` scope is held until that many are
    in flight -- proof the tries run at once. Every LM call runs on clio's one persistent
    LM loop, so the hold is an ``asyncio`` wait on that loop (a thread barrier would
    block the loop itself); ``overlap_timeout`` only guards the test against a hang.
    """

    def __init__(
        self,
        routes: dict[str, Sequence[Reply]],
        overlap: int = 0,
        overlap_timeout: float = 60.0,
    ) -> None:
        super().__init__([])
        self.routes = {scope: list(script) for scope, script in routes.items()}
        self.by_scope: dict[str, list[Request]] = {scope: [] for scope in routes}
        self.overlap = overlap
        self.overlap_timeout = overlap_timeout
        self.in_flight = 0
        self._all_in: asyncio.Event | None = None

    @staticmethod
    def scope_of(request: Request) -> str:
        """The scope a request belongs to (from its prompt-cache key; ``""``: none)."""
        cache = getattr(request.config, "cache", None) if request.config else None
        key = str(getattr(cache, "key", "") or "")
        return key.rsplit(":", 1)[-1] if key else ""

    def _take(self, request: Request) -> tuple[str, int]:
        scope = self.scope_of(request)
        with self._lock:
            self.requests.append(request)
            seen = self.by_scope.setdefault(scope, [])
            seen.append(request)
            return scope, len(seen) - 1

    def _reply_for(self, scope: str, index: int, request: Request) -> Response:
        script = self.routes.get(scope, [])
        if index >= len(script):
            raise AssertionError(f"scope {scope!r} called the model {index + 1} times")
        return _reply(request, script[index], f"{scope.replace('#', '_')}_{index}")

    def _next(self, request: Request) -> Response:
        scope, index = self._take(request)
        return self._reply_for(scope, index, request)

    async def anext_reply(self, request: Request) -> Response:
        """The async reply, holding a try's first request until ``overlap`` are in."""
        scope, index = self._take(request)
        if self.overlap and "#run" in scope and index == 0:
            if self._all_in is None:
                self._all_in = asyncio.Event()
            self.in_flight += 1
            if self.in_flight >= self.overlap:
                self._all_in.set()
            await asyncio.wait_for(self._all_in.wait(), self.overlap_timeout)
        return self._reply_for(scope, index, request)


class AsyncRoutedEngine(AsyncScriptedEngine):
    """Async face of a :class:`RoutedEngine` (its overlap hold awaits on the LM loop)."""

    sync: RoutedEngine

    async def complete(self, request: Request) -> Response:
        return await self.sync.anext_reply(request)

    async def stream(self, request: Request) -> AsyncIterator[Any]:
        for event in response_to_events(await self.sync.anext_reply(request)):
            yield event


def _reply(request: Request, reply: Reply, tag: str) -> Response:
    if reply.raises is not None:
        raise reply.raises
    parts: list[Any] = []
    if reply.thinking:
        parts.append(ThinkingPart(text=reply.thinking))
    if reply.text:
        parts.append(TextPart(text=reply.text))
    ids = list(reply.call_ids) or [f"call_{tag}_{i}" for i in range(len(reply.calls))]
    parts.extend(
        ToolCallPart(id=ids[i], name=name, input=dict(args))
        for i, (name, args) in enumerate(reply.calls)
    )
    return Response(
        id=f"resp_{tag}",
        model=request.model,
        message=Message.assistant(parts or [TextPart(text="")]),
        finish_reason=reply.finish_reason or ("tool_call" if reply.calls else "stop"),
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def routed_lm(routes: dict[str, Sequence[Reply]], overlap: int = 0) -> tuple[dspy.LM, RoutedEngine]:
    """A ``dspy.LM`` whose replies are routed per scope (it declares a prompt-cache key)."""
    engine = RoutedEngine(routes, overlap)
    lm = dspy.LM(
        MODEL,
        engine=engine,
        async_engine=AsyncRoutedEngine(engine),
        cache=False,
        num_retries=0,
    )
    lm._clio_prompt_cache_key = True
    return lm, engine
