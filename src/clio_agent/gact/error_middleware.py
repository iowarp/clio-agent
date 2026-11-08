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

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "EnvelopeErrorMiddleware",
    "error_code_for_status",
    "install_error_envelope",
    "unhandled_error_envelope",
]

logger = logging.getLogger(__name__)


def error_code_for_status(status_code: int) -> str:
    """Map one HTTP status onto the envelope's ``error`` vocabulary.

    Lives beside the envelope it fills rather than as a closure inside
    ``build_app`` -- it closes over nothing.
    """

    if status_code == 404:
        return "not_found"
    if status_code == 405:
        return "unsupported"
    if status_code in {400, 422}:
        return "validation_error"
    if status_code in {401, 403}:
        return "permission_error"
    return "internal_error" if status_code >= 500 else "request_error"


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


# Readable before the task runtime is up: readiness, identity and shutdown.
_AVAILABLE_WHILE_STARTING = frozenset(
    {"/v1/health", "/v1/capabilities", "/v1/server/shutdown", "/v1/desktop/shutdown"}
)
STARTUP_RETRY_AFTER_S = 1


def _startup_refusal(app: object, failed: bool) -> JSONResponse:
    """The 503 for an API call that needs the task runtime before it is ready.

    While starting it says so unambiguously: ``details.readiness`` matches
    ``/v1/health``'s ``readiness``, ``details.starting`` lists what is still coming up,
    and ``Retry-After`` tells a client when to ask again.
    """
    if failed:
        info = ErrorInfo(
            error="task_runtime_unavailable",
            message="Durable task services are unavailable.",
            details={"readiness": "failed"},
            recoverable=True,
        )
        return JSONResponse(
            status_code=503, content=ErrorEnvelope(error=info).model_dump(exclude_none=True)
        )
    from clio_agent.gact import boot_timing  # noqa: PLC0415 - keep the middleware import light

    _, pending = boot_timing.readiness(app)
    info = ErrorInfo(
        error="task_runtime_starting",
        message="Durable task services are starting.",
        details={"readiness": "starting", "starting": pending or ["task_runtime"]},
        recoverable=True,
        retry_after_s=STARTUP_RETRY_AFTER_S,
    )
    return JSONResponse(
        status_code=503,
        content=ErrorEnvelope(error=info).model_dump(exclude_none=True),
        headers={"Retry-After": str(STARTUP_RETRY_AFTER_S)},
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

        app = scope.get("app")
        starting = getattr(getattr(app, "state", None), "task_runtime_start", None)
        path = str(scope.get("path") or "")
        # Only the API waits for the task runtime: the web UI's shell and assets load at
        # once, so it can show the startup state instead of a raw JSON 503.
        if (
            starting is not None
            and path.startswith("/v1/")
            and path not in _AVAILABLE_WHILE_STARTING
        ):
            failed = starting.done() and (starting.cancelled() or starting.exception() is not None)
            if failed or not starting.done():
                await _startup_refusal(app, failed)(scope, receive, send)
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


def install_typed_error_handlers(app: FastAPI) -> None:
    """Register HTTP and request-validation errors in the GACT envelope."""
    from clio_agent.gact.task_history import RecordingUnconfirmed

    @app.exception_handler(RecordingUnconfirmed)
    async def _recording_unconfirmed(_request: object, exc: RecordingUnconfirmed) -> JSONResponse:
        """Classify the existing durable-admission hold as a retryable service refusal."""
        return JSONResponse(
            status_code=503,
            content=ErrorEnvelope(
                error=ErrorInfo(
                    error="core_recording_unconfirmed",
                    message="Conversation history is recovering. Try again when the service is ready.",
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: object, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error=error_code_for_status(exc.status_code),
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: object, exc: RequestValidationError
    ) -> JSONResponse:
        """Wrap FastAPI request validation failures in the GACT envelope."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error",
                message="Request validation failed.",
                # Validation can fail on another field while its input still contains
                # the entire credential-bearing body. Never echo raw inputs/context.
                details={
                    "errors": jsonable_encoder(
                        [
                            {key: row[key] for key in ("type", "loc", "msg") if key in row}
                            for row in exc.errors()
                        ]
                    )
                },
                recoverable=True,
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(exclude_none=True),
        )
