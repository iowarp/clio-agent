"""WebSocket transport with delta continuation (A.6).

The default transport; :mod:`clio_agent.providers.codex.transport_sse` is
the automatic fallback. One socket is kept open per CLIO session and reused
across turns (:mod:`clio_agent.providers.codex.sessions`); it is closed
after roughly 5 minutes idle and recycled before 55 minutes of total age.

Delta continuation: when every request-body field except ``input`` is
unchanged from the previous turn AND the new ``input`` starts with
``previous input + previous output items``, only the new suffix is sent, with
``previous_response_id`` pointing at the last turn's response. This works
even with ``store: false`` because the state lives on the connection, not on
the backend.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.errors import (
    CodexAuthError,
    CodexTransportError,
    raise_for_backend_error,
)
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.sessions import CodexSessionState
from clio_agent.providers.codex.stream_events import (
    Completed,
    Failed,
    ResponseEventParser,
    StreamEvent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DeltaSnapshot",
    "WsConnection",
    "WsPreStreamFailure",
    "build_ws_headers",
    "close_connections",
    "compute_delta",
    "drop_connection_after_cancel",
    "stream_ws_turn",
]


class WsPreStreamFailure(CodexTransportError):
    """A WebSocket failure before any event arrived (A.6).

    The caller (:mod:`clio_agent.providers.codex.litellm_adapter`) catches
    this and retries the SAME turn over SSE; the session is left marked
    ``sse_only`` so later turns skip straight to SSE.
    """


class _PreviousResponseNotFoundSignal(Exception):
    """Internal control-flow signal: resend once with the full body, same socket."""


class _WsConnectionLimitSignal(Exception):
    """Internal control-flow signal: open a new connection and retry once."""


def build_ws_headers(credential: CodexCredential, *, session_id: str) -> dict[str, str]:
    """A.6 headers: SSE's auth headers minus ``accept``/``content-type``/the SSE
    ``OpenAI-Beta`` value, plus the WebSocket beta flag."""

    return {
        "Authorization": f"Bearer {credential.access_token}",
        "chatgpt-account-id": credential.account_id,
        "originator": c.ORIGINATOR,
        "User-Agent": "clio-agent",
        "OpenAI-Beta": c.OPENAI_BETA_WEBSOCKETS,
        "session-id": session_id,
        "x-client-request-id": session_id,
    }


@dataclass
class DeltaSnapshot:
    """The previous turn's full body, response id, and output items (A.6)."""

    body: dict[str, Any] | None = None
    response_id: str = ""
    output_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WsConnection:
    """One session's pooled WebSocket connection plus its delta snapshot."""

    socket: Any
    created_at: float
    last_used_at: float
    delta: DeltaSnapshot = field(default_factory=DeltaSnapshot)

    def is_expired(self, now: float) -> bool:
        return (now - self.created_at) >= c.WS_MAX_AGE_S or (
            now - self.last_used_at
        ) >= c.WS_IDLE_CLOSE_S


def _strip_tool_outputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Output items minus tool outputs -- CLIO's own ``function_call_output``
    items belong to the NEXT turn's input, not to what the model produced."""

    return [item for item in items if item.get("type") != "function_call_output"]


def compute_delta(previous: DeltaSnapshot, new_body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return ``(request_to_send, is_delta)`` (A.6).

    A delta applies when every body field except ``input`` is unchanged from
    ``previous`` AND the new ``input`` starts with
    ``previous input + previous output items``. Otherwise the full body is
    sent and continuation resets from this turn.
    """

    if previous.body is None or not previous.response_id:
        return new_body, False
    previous_non_input = {k: v for k, v in previous.body.items() if k != "input"}
    new_non_input = {k: v for k, v in new_body.items() if k != "input"}
    if previous_non_input != new_non_input:
        return new_body, False
    expected_prefix = list(previous.body.get("input") or []) + _strip_tool_outputs(
        previous.output_items
    )
    new_input = list(new_body.get("input") or [])
    if (
        len(new_input) < len(expected_prefix)
        or new_input[: len(expected_prefix)] != expected_prefix
    ):
        return new_body, False
    suffix = new_input[len(expected_prefix) :]
    delta_body = {**new_non_input, "input": suffix, "previous_response_id": previous.response_id}
    return delta_body, True


def _handshake_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


async def _open_connection(credential: CodexCredential, *, session_id: str) -> Any:
    headers = build_ws_headers(credential, session_id=session_id)
    try:
        return await websockets.connect(
            c.CODEX_WS_URL, additional_headers=headers, open_timeout=c.WS_CONNECT_TIMEOUT_S
        )
    except CodexAuthError:
        raise
    except Exception as exc:
        if _handshake_status_code(exc) == 401:
            raise CodexAuthError(f"WebSocket handshake rejected the access token: {exc}") from exc
        raise WsPreStreamFailure(f"could not open the WebSocket: {exc}") from exc


async def _close_connection(connection: WsConnection) -> None:
    try:
        await connection.socket.close()
    except Exception:  # noqa: BLE001 - teardown must never raise
        logger.debug("codex ws: socket close failed", exc_info=True)


async def drop_connection_after_cancel(state: CodexSessionState) -> None:
    """Drop (never reuse) a session's socket after an aborted turn (A.7)."""

    if state.ws is not None:
        await _close_connection(state.ws)
        state.ws = None


async def close_connections(connections: list[WsConnection]) -> None:
    """Close every given connection (clean server shutdown, ``runtime.process_tree``)."""

    for connection in connections:
        await _close_connection(connection)


async def _run_over_socket(socket: Any, body: dict[str, Any]) -> AsyncIterator[StreamEvent]:
    """Send one ``response.create`` frame and yield the normalized events it produces.

    Returns as soon as a terminal event (``Completed``) has been yielded --
    the connection is kept OPEN for reuse (A.6), so this must stop listening
    for this turn's messages itself rather than waiting for the peer to close
    the socket, which it never does between turns.
    """

    parser = ResponseEventParser()
    first_event_seen = False
    try:
        await socket.send(json.dumps({"type": "response.create", **body}))
    except Exception as exc:
        raise WsPreStreamFailure(f"could not send over the WebSocket: {exc}") from exc
    try:
        async for raw in socket:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            for normalized in parser.feed(event):
                if isinstance(normalized, Failed):
                    if normalized.code == c.PREVIOUS_RESPONSE_NOT_FOUND_CODE:
                        raise _PreviousResponseNotFoundSignal()
                    if (
                        normalized.code == c.WEBSOCKET_CONNECTION_LIMIT_REACHED_CODE
                        and not first_event_seen
                    ):
                        raise _WsConnectionLimitSignal()
                    raise_for_backend_error(code=normalized.code, message=normalized.message)
                first_event_seen = True
                yield normalized
            if parser.completed:
                return
    except (_PreviousResponseNotFoundSignal, _WsConnectionLimitSignal):
        raise
    except WsPreStreamFailure:
        raise
    except Exception as exc:
        if not first_event_seen:
            raise WsPreStreamFailure(f"WebSocket failed before any event arrived: {exc}") from exc
        raise CodexTransportError(f"WebSocket failed mid-stream: {exc}") from exc
    raise CodexTransportError(
        "the Codex backend's WebSocket stream ended without a completion event"
    )


async def _drive_turn(
    state: CodexSessionState,
    credential: CodexCredential,
    session_id: str,
    original_body: dict[str, Any],
    request_body: dict[str, Any],
    *,
    retried_connection_limit: bool,
) -> AsyncIterator[StreamEvent]:
    connection = state.ws
    assert connection is not None  # noqa: S101 - stream_ws_turn always sets this first
    try:
        async for event in _run_over_socket(connection.socket, request_body):
            if isinstance(event, Completed):
                connection.delta = DeltaSnapshot(
                    body=original_body,
                    response_id=event.response_id,
                    output_items=event.output_items,
                )
            yield event
        return
    except _PreviousResponseNotFoundSignal:
        # A.6: resend once with the full body, same socket.
        connection.delta = DeltaSnapshot()
        state.counters.full_requests += 1
        async for event in _run_over_socket(connection.socket, original_body):
            if isinstance(event, Completed):
                connection.delta = DeltaSnapshot(
                    body=original_body,
                    response_id=event.response_id,
                    output_items=event.output_items,
                )
            yield event
        return
    except _WsConnectionLimitSignal:
        # A.6: open a new connection and retry once.
        await _close_connection(connection)
        if retried_connection_limit:
            raise WsPreStreamFailure("WebSocket connection limit reached twice") from None
        now = time.monotonic()
        state.ws = WsConnection(
            socket=await _open_connection(credential, session_id=session_id),
            created_at=now,
            last_used_at=now,
        )
        state.counters.connections_created += 1
        async for event in _drive_turn(
            state,
            credential,
            session_id,
            original_body,
            original_body,
            retried_connection_limit=True,
        ):
            yield event
        return
    except WsPreStreamFailure:
        raise
    except Exception as exc:
        await _close_connection(connection)
        state.ws = None
        raise CodexTransportError(f"WebSocket turn failed: {exc}") from exc


async def stream_ws_turn(
    *,
    credential: CodexCredential,
    body: dict[str, Any],
    session_id: str,
    state: CodexSessionState,
) -> AsyncIterator[StreamEvent]:
    """Stream one turn over the session's pooled WebSocket, with delta continuation.

    Raises:
        WsPreStreamFailure: No event arrived before the failure. The session
            is marked ``sse_only`` and its socket dropped; the caller should
            retry this turn over SSE (A.6).
        CodexTransportError / a typed backend error: A failure after events
            had started streaming, or the stream never completed. Never
            replayed (A.6/A.7).
    """

    try:
        now = time.monotonic()
        if state.ws is not None and state.ws.is_expired(now):
            await _close_connection(state.ws)
            state.ws = None

        if state.ws is None:
            socket = await _open_connection(credential, session_id=session_id)
            state.ws = WsConnection(socket=socket, created_at=now, last_used_at=now)
            state.counters.connections_created += 1
        else:
            state.counters.connections_reused += 1
        state.ws.last_used_at = time.monotonic()

        request_body, is_delta = compute_delta(state.ws.delta, body)
        if is_delta:
            state.counters.delta_requests += 1
        else:
            state.counters.full_requests += 1

        async for event in _drive_turn(
            state, credential, session_id, body, request_body, retried_connection_limit=False
        ):
            yield event
    except WsPreStreamFailure:
        state.sse_only = True
        state.counters.fallbacks += 1
        if state.ws is not None:
            await _close_connection(state.ws)
            state.ws = None
        raise
