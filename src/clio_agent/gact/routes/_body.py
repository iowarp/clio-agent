"""Shared request-body parsing for gact route handlers (iowarp/clio-agent#772).

Centralizes the ``body = await request.json()`` idiom the route handlers use to
read an *optional* JSON object, replacing the scattered ``try``/``except`` blocks
that silently swallowed a malformed or non-object body to ``{}``. A parse failure
is no longer invisible: it emits a structured ``reason=`` warning in the
``gact/streaming.py`` stream-fallback house style (through the standard logging
channel, so it is assertable via ``caplog`` and visible in the trace) while
preserving the behavior every caller already relied on -- an unparseable or
non-object body is treated as an empty mapping so downstream ``body.get(...)``
access keeps working.

Callers guarding destructive actions (session undo/rewind) instead pass
``non_object="raise"``: a body that parses to valid-JSON-but-not-an-object
raises :class:`NonObjectBodyError` so the handler can reject it with its
route-specific 422 envelope rather than proceed on coerced defaults.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import Request

__all__ = ["NonObjectBodyError", "REQUEST_BODY_UNPARSEABLE_REASON", "json_body"]


class NonObjectBodyError(Exception):
    """The request body parsed as valid JSON but is not a JSON object.

    Raised only when ``json_body`` is called with ``non_object="raise"`` so
    handlers guarding destructive actions (session undo/rewind) can reject a
    wrong-shaped payload with their route-specific 422 envelope instead of
    silently treating it as ``{}`` and proceeding.
    """

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.payload_type = type(payload).__name__
        super().__init__(f"request body is valid JSON but not an object: {self.payload_type}")


_LOGGER = logging.getLogger("clio_agent.gact.routes.body")

# Structured reason for an unparseable/non-object request body, mirroring the
# closed-set ``_STREAM_FALLBACK_REASON_DEFINITIONS`` house style in
# ``gact/streaming.py``: a typed reason with a category, recovery actions, and a
# human description so the degraded path stays queryable rather than silent.
REQUEST_BODY_UNPARSEABLE_REASON: dict[str, Any] = {
    "reason": "request_body_unparseable",
    "category": "request_validation",
    "recovery_actions": ["treat_body_as_empty"],
    "description": (
        "The request body could not be parsed as a JSON object; the handler "
        "treats a malformed or non-object body as an empty mapping."
    ),
}

# Exceptions raised while reading/parsing a JSON request body.
# ``json.JSONDecodeError`` is a subclass of ``ValueError`` (malformed JSON);
# ``UnicodeDecodeError`` covers a mis-encoded payload; Starlette can raise a
# bare ``RuntimeError`` when the receive stream was already consumed. Caught
# explicitly so this stays a targeted parse guard, never a blind ``except``.
_BODY_PARSE_ERRORS: tuple[type[Exception], ...] = (
    json.JSONDecodeError,
    ValueError,
    UnicodeDecodeError,
    RuntimeError,
)


async def json_body(
    request: Request,
    *,
    route: str,
    logger: logging.Logger = _LOGGER,
    non_object: Literal["empty", "raise"] = "empty",
    null_is_empty: bool = True,
) -> dict[str, Any]:
    """Return the request's JSON object body, or ``{}`` if it is unusable.

    Behavior-preserving replacement for the
    ``try: body = await request.json() except ...: body = {}`` idiom scattered
    across the gact route handlers: a malformed body, a non-object body
    (list/scalar/``null``), or an absent body all resolve to an empty mapping so
    downstream ``body.get(...)`` access keeps working. Unlike the silent
    originals, a parse failure emits a structured ``request_body_unparseable``
    warning (stream-fallback house style) carrying the ``route`` and the
    exception/payload type, so the degraded path is visible in the logs/trace
    instead of vanishing.

    A *successfully parsed* non-object body (list/scalar) is a different case
    from a parse failure: the client sent deliberate-but-wrong-shaped JSON.
    Handlers that go on to perform destructive actions (session undo/rewind)
    must reject it rather than proceed on defaults; they pass
    ``non_object="raise"`` and convert :class:`NonObjectBodyError` into their
    route-specific 422 envelope. No fallback warning is emitted on that path --
    an explicit rejection is surfaced to the client, not a silent degradation.

    Args:
        request: The incoming request whose JSON body should be read.
        route: A stable identifier for the calling endpoint (typically its HTTP
            method and path template, e.g. ``"POST /v1/hooks"``), recorded on
            the structured warning so a miss can be attributed to a site.
        logger: Logger to emit the structured warning through; defaults to this
            module's logger (assertable via ``caplog``).
        non_object: What to do when the body parses to a non-object value:
            ``"empty"`` (default) logs the structured reason and returns ``{}``;
            ``"raise"`` raises :class:`NonObjectBodyError` for the caller to
            surface as its own validation error.
        null_is_empty: Whether a JSON ``null`` body is treated like an absent
            body (logged, coerced to ``{}``) even under ``non_object="raise"``.
            Pass ``False`` to have ``null`` handled by ``non_object`` as well.

    Returns:
        The parsed JSON object as a ``dict``. An unparseable body -- and, under
        the defaults, a non-object body -- yields an empty ``dict``.

    Raises:
        NonObjectBodyError: The body parsed to a non-object value and
            ``non_object="raise"`` was requested.
    """

    try:
        payload: Any = await request.json()
    except _BODY_PARSE_ERRORS as exc:
        logger.warning(
            "stream_fallback reason=request_body_unparseable route=%s exception=%s",
            route,
            type(exc).__name__,
            extra={
                "structured_reason": {
                    **REQUEST_BODY_UNPARSEABLE_REASON,
                    "route": route,
                    "exception_type": type(exc).__name__,
                }
            },
        )
        return {}
    if isinstance(payload, dict):
        return payload
    if non_object == "raise" and not (payload is None and null_is_empty):
        raise NonObjectBodyError(payload)
    logger.warning(
        "stream_fallback reason=request_body_unparseable route=%s payload_type=%s",
        route,
        type(payload).__name__,
        extra={
            "structured_reason": {
                **REQUEST_BODY_UNPARSEABLE_REASON,
                "route": route,
                "payload_type": type(payload).__name__,
            }
        },
    )
    return {}
