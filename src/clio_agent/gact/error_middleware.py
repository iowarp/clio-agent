"""Structured 500s that survive the CORS layer.

Starlette installs ``@app.exception_handler(Exception)`` on
``ServerErrorMiddleware``, which sits *outside* every user middleware —
including ``CORSMiddleware``. An unhandled route error therefore produces a
correctly-structured ``ErrorEnvelope`` that carries **no** CORS headers, and a
browser reports the whole thing as an opaque ``net::ERR_FAILED`` /
"Failed to fetch".

That defeats the no-silent-fallback ground rule at the transport layer: the
server does emit a typed reason, and the web client can never read it. Every
5xx looks identical to the backend being down.

This middleware catches the exception *inside* the CORS layer instead, so the
envelope goes back out as an ordinary response and picks up the CORS headers on
the way. The ``ServerErrorMiddleware`` handler stays as the backstop for
anything raised outside this middleware.

Deliberately a pure ASGI middleware, not ``BaseHTTPMiddleware``: the latter
buffers responses and breaks the SSE streams this server is built around.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "EnvelopeErrorMiddleware",
    "install_error_envelope",
    "unhandled_error_envelope",
]

logger = logging.getLogger(__name__)


def unhandled_error_envelope(exc: BaseException) -> ErrorEnvelope:
    """Build the structured envelope for an unexpected route failure.

    Shared with ``app.py``'s ``ServerErrorMiddleware`` handler so both paths
    produce a byte-identical body; a client must not be able to tell which
    layer caught the error.
    """

    return ErrorEnvelope(
        error=ErrorInfo(
            error="internal_error",
            message="Unhandled server error.",
            details={
                "original_error": type(exc).__name__,
                "original_message": str(exc),
            },
            recoverable=False,
        )
    )


class EnvelopeErrorMiddleware:
    """Return the GACT error envelope from inside the CORS layer.

    Install it *before* ``CORSMiddleware`` so that CORS ends up outermost:
    Starlette treats the most recently added middleware as the outer one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if response_started:
                # Headers are already on the wire — typically a streaming SSE
                # response that failed mid-flight. Replacing it is impossible;
                # re-raise so ServerErrorMiddleware records it rather than
                # swallowing the failure here.
                raise
            # The envelope carries the type and message, never the traceback —
            # so without this the only record of WHERE a 500 came from is gone.
            # Catching the exception here also stops ServerErrorMiddleware from
            # ever seeing (and logging) it.
            logger.exception(
                "unhandled error serving %s %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
            )
            envelope = unhandled_error_envelope(exc)
            response = JSONResponse(
                status_code=500,
                content=envelope.model_dump(exclude_none=True),
            )
            await response(scope, receive, send)


def install_error_envelope(app: FastAPI) -> None:
    """Wire both unhandled-error paths onto ``app``.

    Call BEFORE adding ``CORSMiddleware`` so that CORS ends up outermost and
    can stamp the envelope this middleware produces.

    Registers two layers that must agree:

    * ``EnvelopeErrorMiddleware`` — catches route errors inside the CORS layer,
      which is the only place a 5xx body can reach a browser.
    * the ``Exception`` handler on ``ServerErrorMiddleware`` — the backstop for
      anything raised further out, sharing the same envelope builder so the two
      are indistinguishable to a client.

    Both live here rather than in ``app.py`` so the pairing has one owner; they
    are only correct together.
    """

    app.add_middleware(EnvelopeErrorMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled(_request: object, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=unhandled_error_envelope(exc).model_dump(exclude_none=True),
        )
