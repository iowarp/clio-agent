"""HTTP/SSE transport for the Codex backend (A.5). The automatic fallback for
the WebSocket transport (A.6), and the only transport when WS pre-stream fails
or a session has been demoted to SSE-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.errors import (
    CodexAuthError,
    CodexTransportError,
    is_retryable_status,
    next_retry_delay_ms,
    raise_for_backend_error,
)
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.stream_events import Failed, ResponseEventParser, StreamEvent

logger = logging.getLogger(__name__)

__all__ = ["build_headers", "stream_sse_turn"]


def build_headers(credential: CodexCredential, *, session_id: str) -> dict[str, str]:
    """A.5 headers. Callers must never log these verbatim -- Authorization and
    chatgpt-account-id are secrets/identity and are redacted at every log site."""

    return {
        "Authorization": f"Bearer {credential.access_token}",
        "chatgpt-account-id": credential.account_id,
        "originator": c.ORIGINATOR,
        "User-Agent": "clio-agent",
        "OpenAI-Beta": c.OPENAI_BETA_SSE,
        "accept": "text/event-stream",
        "content-type": "application/json",
        "session-id": session_id,
        "x-client-request-id": session_id,
    }


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse ``data:`` lines out of an SSE body, ignoring comments/heartbeats.

    Only the JSON payload's own ``type`` field is trusted; a stray ``event:``
    line (if the backend ever sends one) is not needed to classify an event.
    """

    data_lines: list[str] = []

    def _flush() -> dict[str, Any] | None:
        if not data_lines:
            return None
        payload = "\n".join(data_lines)
        data_lines.clear()
        if payload == "[DONE]":
            return None
        try:
            event = json.loads(payload)
        except ValueError:
            logger.warning("codex sse: undecodable event payload (len=%d)", len(payload))
            return None
        return event if isinstance(event, dict) else None

    async for line in response.aiter_lines():
        if line == "":
            event = _flush()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    event = _flush()
    if event is not None:
        yield event


async def _sleep_ms(milliseconds: float) -> None:
    await asyncio.sleep(max(0.0, milliseconds) / 1000.0)


async def stream_sse_turn(
    *,
    credential: CodexCredential,
    body: dict[str, Any],
    session_id: str,
    client: httpx.AsyncClient | None = None,
    max_attempts: int = c.DEFAULT_MAX_RETRIES,
) -> AsyncIterator[StreamEvent]:
    """Stream one turn over HTTP/SSE.

    Retries the CONNECTION attempt (a transport error, or a retryable status
    before any bytes of the stream were read) with backoff (A.7). Once the
    stream itself has started delivering events, any failure is surfaced
    immediately -- never replayed, matching A.6's WebSocket rule applied
    symmetrically here.

    Raises:
        CodexPlanLimitError: A terminal 429 (the account's plan window).
        CodexResponseError: The backend reported ``error``/``response.failed``.
        CodexTransportError: Retries were exhausted, or the stream ended
            without a completion event.
    """

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))
    headers = build_headers(credential, session_id=session_id)
    attempt = 0
    try:
        while True:
            try:
                request = http_client.build_request(
                    "POST", c.CODEX_HTTP_URL, headers=headers, json=body
                )
                response = await http_client.send(request, stream=True)
            except httpx.HTTPError as exc:
                decision = next_retry_delay_ms(attempt=attempt)
                if attempt + 1 >= max_attempts or not decision.should_retry:
                    raise CodexTransportError(f"could not reach the Codex backend: {exc}") from exc
                attempt += 1
                await _sleep_ms(decision.delay_ms)
                continue

            if response.status_code >= 400:
                raw = await response.aread()
                await response.aclose()
                text = raw.decode("utf-8", errors="replace")
                if response.status_code == 401:
                    # A.7: refresh once and retry -- the caller (litellm_adapter)
                    # catches this, refreshes the credential, and retries the turn.
                    raise CodexAuthError(f"access token rejected (401): {text}")
                if is_retryable_status(response.status_code, text):
                    decision = next_retry_delay_ms(attempt=attempt, headers=dict(response.headers))
                    if attempt + 1 < max_attempts and decision.should_retry:
                        attempt += 1
                        await _sleep_ms(decision.delay_ms)
                        continue
                raise_for_backend_error(
                    code=str(response.status_code), message=text, status_code=response.status_code
                )

            parser = ResponseEventParser()
            try:
                async for raw_event in _iter_sse_events(response):
                    for normalized in parser.feed(raw_event):
                        if isinstance(normalized, Failed):
                            raise_for_backend_error(
                                code=normalized.code, message=normalized.message
                            )
                        yield normalized
            finally:
                await response.aclose()
            if not parser.completed:
                raise CodexTransportError(
                    "the Codex backend's stream ended without a completion event"
                )
            return
    finally:
        if owns_client:
            await http_client.aclose()
