"""Admission authentication for the GACT HTTP surface."""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from starlette.datastructures import Headers, QueryParams, State
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from clio_agent import conf
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

PeerAddressGetter = Callable[[Scope], str | None]


def configured_bearer_token() -> str | None:
    """Resolve the optional GACT bearer token from config, then the environment."""

    raw_value: Any = conf.resolve(
        "gact.auth.bearer_token",
        env="CLIO_GACT_BEARER_TOKEN",
        default=None,
    )
    if raw_value is None:
        return None
    token = conf.as_str(raw_value)
    return token or None


def peer_address_from_scope(scope: Scope) -> str | None:
    """Return the direct ASGI peer address without trusting forwarding headers."""

    client = scope.get("client")
    if not client:
        return None
    return str(client[0])


def is_loopback_address(address: str | None) -> bool:
    """Return whether an address identifies an IPv4 or IPv6 loopback peer."""

    if not address:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    mapped = getattr(parsed, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _is_session_sse_path(path: str) -> bool:
    parts = path.strip("/").split("/")
    return len(parts) == 4 and parts[:2] == ["v1", "sessions"] and parts[3] == "events"


def _header_bearer_token(scope: Scope) -> str | None:
    authorization = Headers(scope=scope).get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.casefold() == "bearer" and token:
        return token
    return None


def _request_bearer_token(scope: Scope) -> str:
    header_token = _header_bearer_token(scope)
    if header_token is not None:
        return header_token
    path = str(scope.get("path") or "")
    if _is_session_sse_path(path):
        return QueryParams(scope.get("query_string", b"")).get("auth_token", "")
    return ""


def _authentication_refusal() -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorInfo(
            error="authentication_required",
            message="A valid bearer token is required for remote access.",
            details={"scheme": "bearer"},
            recoverable=True,
        )
    )
    return JSONResponse(
        status_code=401,
        content=envelope.model_dump(exclude_none=True),
        headers={"WWW-Authenticate": "Bearer"},
    )


class BearerAuthMiddleware:
    """Require the configured bearer token for every non-loopback HTTP peer."""

    def __init__(self, app: ASGIApp, *, token: str, state: State) -> None:
        self._app = app
        self._token = token
        self._state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        getter: PeerAddressGetter = getattr(
            self._state,
            "peer_address_getter",
            peer_address_from_scope,
        )
        if is_loopback_address(getter(scope)):
            await self._app(scope, receive, send)
            return

        supplied_token = _request_bearer_token(scope)
        if hmac.compare_digest(supplied_token, self._token):
            await self._app(scope, receive, send)
            return

        await _authentication_refusal()(scope, receive, send)


def configure_bearer_auth(app: FastAPI) -> None:
    """Configure bearer admission and the overridable direct-peer address seam."""

    token = configured_bearer_token()
    app.state.bearer_token = token
    app.state.peer_address_getter = peer_address_from_scope
    if token is not None:
        app.add_middleware(BearerAuthMiddleware, token=token, state=app.state)
