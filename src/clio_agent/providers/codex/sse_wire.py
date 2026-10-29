"""Codex's native lm15 HTTP wire with CLIO's explicit SSE size policy.

DSPy 3.4's clients hardcode the parser defaults. This narrow override preserves
their request building, response parsing, coalescing and error metadata. The
private lm15 hooks used here are covered by the pinned dependency contract tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from importlib import import_module
from typing import TypeAlias

from dspy.lm15 import (
    AsyncOpenAICodexLM,
    ProviderError,
    Request,
    StreamDeltaEvent,
    StreamEndEvent,
    StreamErrorEvent,
    StreamStartEvent,
    TransportError,
)

from clio_agent import conf
from clio_agent.providers.usage.capture import record_codex_headers

StreamEvent: TypeAlias = StreamStartEvent | StreamDeltaEvent | StreamEndEvent | StreamErrorEvent
# Keep private SDK hooks in this one pinned boundary, rather than importing
# private subpackages into DSPy's untyped public package graph during analysis.
_sdk = import_module("dspy._vendor.lm15.providers.async_base")


def sse_limit() -> int:
    """Resolve the Codex HTTP line ceiling; a larger line also raises the event ceiling."""
    limit = conf.resolve(
        "lm15.sse_limit", env="CLIO_LM15_SSE_LIMIT", default=1_048_576, cast=conf.as_int
    )
    if limit < 1:
        raise ValueError("lm15.sse_limit must be a positive byte count")
    return limit


class CodexSSEWire(AsyncOpenAICodexLM):
    """Use lm15's native SSE parser with an operator-visible byte ceiling."""

    async def _stream_raw(
        self, request: Request, req: object | None = None
    ) -> AsyncIterator[StreamEvent]:
        limit = sse_limit()  # Reject an invalid policy before transport submission.
        if req is None:
            req = await self._build(self._inner.build_request, request, stream=True)
        try:
            async with self.transport.stream(req) as response:
                record_codex_headers(response.headers)
                if response.status >= 400:
                    body = await response.read()
                    error = self._inner.normalize_error(
                        response.status, body.decode("utf-8", errors="replace")
                    )
                    _sdk._attach_error_metadata(error, response.headers)
                    raise error
                try:
                    async for raw in _sdk.aparse_sse(
                        _sdk._aiter_lines(response),
                        max_line_bytes=limit,
                        max_event_bytes=max(limit, 1024 * 1024),
                    ):
                        for event in self._inner.parse_stream_events(request, raw):
                            if event is not None:
                                yield _sdk._stream_error_metadata(event, response.headers)
                except ProviderError as error:
                    _sdk._attach_error_metadata(error, response.headers)
                    raise
        except _sdk.NetworkTransportError as error:
            raise TransportError(str(error)) from error
