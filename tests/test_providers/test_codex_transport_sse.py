"""Integration tests for the HTTP/SSE transport against a mocked Codex backend (A.9)."""

from __future__ import annotations

import httpx
import pytest

from clio_agent.providers.codex.errors import (
    CodexAuthError,
    CodexPlanLimitError,
    CodexTransportError,
)
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.stream_events import Completed, TextDelta
from clio_agent.providers.codex.transport_sse import build_headers, stream_sse_turn

_CREDENTIAL = CodexCredential(
    access_token="at", refresh_token="rt", expires_at_ms=0, account_id="acct_1"
)


def _sse_body(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def test_build_headers_carries_auth_and_ids() -> None:
    headers = build_headers(_CREDENTIAL, session_id="sess_1")
    assert headers["Authorization"] == "Bearer at"
    assert headers["codex-account-id"] == "acct_1"
    assert headers["session-id"] == "sess_1"
    assert headers["x-client-request-id"] == "sess_1"
    assert headers["accept"] == "text/event-stream"


@pytest.mark.asyncio
async def test_stream_sse_turn_maps_events_to_normalized_stream() -> None:
    import json

    body = _sse_body(
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'Hello'})}",
        "",
        f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'resp_1', 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}})}",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["session-id"] == "sess_1"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = [
        event
        async for event in stream_sse_turn(
            credential=_CREDENTIAL, body={"model": "m"}, session_id="sess_1", client=client
        )
    ]
    assert TextDelta("Hello") in events
    assert any(isinstance(e, Completed) and e.response_id == "resp_1" for e in events)


@pytest.mark.asyncio
async def test_stream_sse_turn_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(CodexAuthError):
        async for _ in stream_sse_turn(
            credential=_CREDENTIAL, body={}, session_id="s", client=client
        ):
            pass


@pytest.mark.asyncio
async def test_stream_sse_turn_terminal_429_raises_plan_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"usage limit reached for this plan")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(CodexPlanLimitError):
        async for _ in stream_sse_turn(
            credential=_CREDENTIAL, body={}, session_id="s", client=client, max_attempts=1
        ):
            pass


@pytest.mark.asyncio
async def test_stream_sse_turn_retries_retryable_status_then_succeeds() -> None:
    import json

    calls = {"n": 0}
    completed_line = (
        f"data: {json.dumps({'type': 'response.completed', 'response': {'id': 'r', 'usage': {}}})}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, content=b"service unavailable")
        return httpx.Response(200, content=_sse_body(completed_line))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = [
        event
        async for event in stream_sse_turn(
            credential=_CREDENTIAL, body={}, session_id="s", client=client, max_attempts=3
        )
    ]
    assert calls["n"] == 2
    assert any(isinstance(e, Completed) for e in events)


@pytest.mark.asyncio
async def test_stream_sse_turn_without_completion_event_is_an_error() -> None:
    import json

    body = _sse_body(
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': 'partial'})}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(CodexTransportError):
        async for _ in stream_sse_turn(
            credential=_CREDENTIAL, body={}, session_id="s", client=client
        ):
            pass
