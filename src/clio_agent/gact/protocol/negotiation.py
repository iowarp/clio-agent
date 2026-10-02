"""Request middleware for GACT and A2UI protocol negotiation."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from clio_agent.gact.protocol.constants import A2UI_V091, GACT_V2, GACT_V3


class ProtocolNegotiationMiddleware:
    """Reject unsupported versions without buffering or rewriting streamed bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        gact_version = headers.get("x-gact-version", "").strip()
        a2ui_version = headers.get("x-a2ui-version", "").strip()
        state = scope.setdefault("state", {})
        state["protocol_version"] = gact_version or GACT_V2
        state["a2ui_protocol_version"] = a2ui_version or None
        if gact_version and gact_version not in {GACT_V2, GACT_V3}:
            await _unsupported("GACT", gact_version, [GACT_V3, GACT_V2])(scope, receive, send)
            return
        if a2ui_version and a2ui_version != A2UI_V091:
            await _unsupported("A2UI", a2ui_version, [A2UI_V091])(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_protocol_negotiation(app: FastAPI) -> None:
    """Install protocol negotiation as pure ASGI middleware."""

    app.add_middleware(ProtocolNegotiationMiddleware)


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
