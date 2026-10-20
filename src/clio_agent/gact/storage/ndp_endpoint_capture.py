"""Finite response collection with an absolute socket deadline."""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time

import httpx
from pydantic import ValidationError

from clio_agent.gact.storage.ndp_endpoint_models import EndpointContinuation, EndpointRequest

logger = logging.getLogger(__name__)
MAX_RESPONSE = 2 * 1024 * 1024


def stream_continuation(
    raw: bytes, reason: str, request: EndpointRequest
) -> EndpointContinuation | None:
    """Separate observed control metadata from a potentially truncated text preview.

    A partial body, malformed batch or another stream's response must never
    advance the interface. The complete original response remains in custody.
    """
    if reason != "completed" or not request.parameters.get("stream_id"):
        return None
    try:
        body = json.loads(raw)
        if (
            not isinstance(body, dict)
            or body.get("complete") is not True
            or body.get("stream_id") != request.parameters["stream_id"]
            or not isinstance(body.get("records"), list)
            or body.get("count") != len(body["records"])
        ):
            return None
        return EndpointContinuation.model_validate(
            {key: body[key] for key in EndpointContinuation.model_fields}, strict=True
        )
    except (ValueError, KeyError, ValidationError):
        return None


def capture_response(
    response: httpx.Response, request: EndpointRequest, deadline: float
) -> tuple[bytes, str, int]:
    """Close even a silent HTTP/SSH socket at the limit and preserve received bytes."""
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        try:
            channel = response.extensions.get("ndp_ssh_channel")
            if channel is not None:
                channel.close()
            else:
                stream = response.extensions.get("network_stream")
                sock = stream.get_extra_info("socket") if stream is not None else None
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            response.close()
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            logger.debug("NDP capture deadline reached an already closed connection: %s", exc)

    timer = threading.Timer(max(0.01, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    raw, reason, count = bytearray(), "completed", 0
    sse = "text/event-stream" in response.headers.get("content-type", "")
    try:
        try:
            for chunk in response.iter_bytes():
                remaining = MAX_RESPONSE - len(raw)
                raw.extend(chunk[:remaining])
                if sse:
                    endings = [match.end() for match in re.finditer(rb"\r?\n\r?\n", raw)]
                    count = len(endings)
                    if count >= request.max_events:
                        raw = raw[: endings[request.max_events - 1]]
                        count = request.max_events
                if len(chunk) >= remaining:
                    reason = "byte_limit"
                    break
                if sse and count >= request.max_events:
                    reason = "event_limit"
                    break
                if time.monotonic() >= deadline:
                    reason = "time_limit"
                    break
        except (httpx.TimeoutException, TimeoutError):
            reason = "time_limit"
        except (httpx.HTTPError, OSError):
            if not expired.is_set():
                raise
        if expired.is_set():
            reason = "time_limit"
        if reason == "time_limit" and not raw:
            raise TimeoutError("Endpoint produced no bytes within the capture time limit")
        return bytes(raw), reason, count
    finally:
        timer.cancel()
        timer.join(timeout=2)
