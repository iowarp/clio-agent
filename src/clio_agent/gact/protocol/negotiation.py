"""Request middleware for GACT and A2UI protocol negotiation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from clio_agent.gact.protocol.constants import A2UI_V091, GACT_V2, GACT_V3


def install_protocol_negotiation(app: FastAPI) -> None:
    """Reject explicit protocol versions the server cannot honor."""

    @app.middleware("http")
    async def negotiate_protocol(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        gact_version = request.headers.get("x-gact-version", "").strip()
        a2ui_version = request.headers.get("x-a2ui-version", "").strip()
        request.state.protocol_version = gact_version or GACT_V2
        request.state.a2ui_protocol_version = a2ui_version or None
        if gact_version and gact_version not in {GACT_V2, GACT_V3}:
            return _unsupported("GACT", gact_version, [GACT_V3, GACT_V2])
        if a2ui_version and a2ui_version != A2UI_V091:
            return _unsupported("A2UI", a2ui_version, [A2UI_V091])
        return await call_next(request)


def _unsupported(protocol: str, requested: str, supported: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=406,
        content={
            "error": {
                "error": "unsupported_protocol",
                "message": f"Unsupported {protocol} version: {requested}",
                "details": {"supported": supported},
                "recoverable": False,
            }
        },
    )
