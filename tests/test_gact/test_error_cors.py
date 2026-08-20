"""A 5xx must reach a browser as a readable error, not an opaque failure.

The defect: Starlette installs the ``Exception`` handler on
``ServerErrorMiddleware``, outside ``CORSMiddleware``. The envelope was
therefore correct and completely unreadable from a browser — every server
error became ``net::ERR_FAILED`` / "Failed to fetch", indistinguishable from
the backend being down.

Found by driving the live server: ``GET /v1/sessions/{id}/messages`` 500s when
the message blob is gone, and the web client could only report "Failed to
fetch".
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from clio_agent.gact.error_middleware import (
    EnvelopeErrorMiddleware,
    unhandled_error_envelope,
)

ORIGIN = "http://127.0.0.1:4191"


def _app(*, with_middleware: bool) -> FastAPI:
    """A minimal app with the production middleware ordering."""

    app = FastAPI()
    if with_middleware:
        app.add_middleware(EnvelopeErrorMiddleware)
    # Added last so CORS is outermost, exactly as build_app does.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ORIGIN],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    @app.get("/boom")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("GetBlob operation failed")

    @app.get("/fine")
    async def _fine() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_unhandled_error_carries_cors_headers() -> None:
    """The fix: a 500 is readable by the browser that asked for it."""

    client = TestClient(_app(with_middleware=True), raise_server_exceptions=False)
    response = client.get("/boom", headers={"Origin": ORIGIN})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == ORIGIN

    body = response.json()
    assert body["error"]["error"] == "internal_error"
    # The cause survives, so the client can say something true about it.
    assert body["error"]["details"]["original_error"] == "RuntimeError"
    assert "GetBlob" in body["error"]["details"]["original_message"]


def test_without_the_middleware_the_header_is_missing() -> None:
    """Sabotage: prove the assertion above is actually load-bearing.

    Without EnvelopeErrorMiddleware the envelope is still produced — by
    ServerErrorMiddleware, outside CORS — and the header is gone. A test that
    passed in both arrangements would be proving nothing.
    """

    client = TestClient(_app(with_middleware=False), raise_server_exceptions=False)
    response = client.get("/boom", headers={"Origin": ORIGIN})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") is None


def test_success_responses_are_unaffected() -> None:
    """The middleware must be invisible on the happy path."""

    client = TestClient(_app(with_middleware=True))
    response = client.get("/fine", headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert response.json() == {"ok": "yes"}
    assert response.headers.get("access-control-allow-origin") == ORIGIN


def test_streaming_failure_after_headers_is_reraised_not_swallowed() -> None:
    """A response that already started cannot be replaced.

    SSE is the server's main surface; if a stream dies mid-flight the headers
    are already on the wire. Silently truncating it would be exactly the
    unlogged degradation the ground rules forbid, so the error propagates.
    """

    from fastapi.responses import StreamingResponse

    app = FastAPI()
    app.add_middleware(EnvelopeErrorMiddleware)

    async def _chunks():
        yield b"first"
        raise RuntimeError("stream died")

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        return StreamingResponse(_chunks(), media_type="text/plain")

    client = TestClient(app)
    with pytest.raises(RuntimeError, match="stream died"):
        client.get("/stream")


def test_both_paths_build_the_same_envelope() -> None:
    """The client must not be able to tell which layer caught the error."""

    envelope = unhandled_error_envelope(RuntimeError("GetBlob operation failed"))
    payload = envelope.model_dump(exclude_none=True)

    assert payload["error"]["error"] == "internal_error"
    assert payload["error"]["message"] == "Unhandled server error."
    assert payload["error"]["recoverable"] is False


def test_real_app_returns_cors_headers_on_an_unhandled_error(tmp_path) -> None:
    """The same guarantee on the real build_app middleware stack.

    Uses an origin from the shipped default allow-list; 4191 above is only
    trusted when a launcher passes CLIO_GACT_CORS_ORIGINS.
    """

    from clio_agent.gact.app import build_app

    default_origin = "http://127.0.0.1:4173"
    app = build_app(sessions_path=tmp_path / "sessions.json")

    @app.get("/_test_boom")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("GetBlob operation failed")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/_test_boom", headers={"Origin": default_origin})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == default_origin
    assert response.json()["error"]["error"] == "internal_error"


def test_the_traceback_is_logged(caplog) -> None:
    """The envelope carries no traceback, so the log must.

    Catching the error here also prevents ServerErrorMiddleware from logging
    it, so without this the origin of a 500 would vanish entirely — the live
    server logged only the access line for the GetBlob failure.
    """

    client = TestClient(_app(with_middleware=True), raise_server_exceptions=False)
    with caplog.at_level("ERROR", logger="clio_agent.gact.error_middleware"):
        client.get("/boom", headers={"Origin": ORIGIN})

    assert any("GetBlob operation failed" in r.getMessage() or r.exc_info for r in caplog.records)
    assert any("/boom" in r.getMessage() for r in caplog.records)
