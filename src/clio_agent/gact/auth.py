"""Admission authentication for the GACT HTTP surface."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import os
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
    """Resolve the optional GACT bearer token: config, then env, then the desktop launcher.

    ``CLIO_AUTH_TOKEN`` is the desktop launcher's contract, not a GACT config
    knob: the desktop shell mints a one-use token and exports it to the server
    process it spawns (``external/gact-tui/desktop/sidecar-launcher/main.go``),
    then authenticates its own privileged calls (e.g. desktop shutdown) with
    it. This process must accept that same token as a valid bearer, so it is
    the final fallback here.
    """

    raw_value: Any = conf.resolve(
        "gact.auth.bearer_token",
        env="CLIO_GACT_BEARER_TOKEN",
        default=None,
    )
    if raw_value is not None:
        token = conf.as_str(raw_value)
        if token:
            return token
    return os.environ.get("CLIO_AUTH_TOKEN", "").strip() or None


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


def websocket_protocol_token(scope: Scope) -> str:
    """Decode a browser-compatible bearer carried as a ``clio-bearer.`` subprotocol.

    Browsers cannot set ``Authorization`` on a WebSocket, so the Desktop sends
    its token as an extra ``Sec-WebSocket-Protocol`` entry (urlsafe base64).
    """

    raw = Headers(scope=scope).get("sec-websocket-protocol", "")
    for value in (part.strip() for part in raw.split(",")):
        if not value.startswith("clio-bearer."):
            continue
        encoded = value.removeprefix("clio-bearer.")
        padding = "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""


def supplied_bearer_token(scope: Scope) -> str:
    """The bearer a request presents: header, SSE ``auth_token``, or WS subprotocol."""

    token = _request_bearer_token(scope)
    if not token and scope.get("type") == "websocket":
        token = websocket_protocol_token(scope)
    return token


def has_valid_bearer(scope: Scope, expected: str | None) -> bool:
    """Whether the request presents the configured bearer token."""

    if expected is None:
        return False
    supplied = supplied_bearer_token(scope)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


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

        downloads = getattr(self._state, "session_export_downloads", None)
        if downloads is not None and downloads.admits(scope.get("method"), scope.get("path", "")):
            # The authenticated prepare endpoint mints a one-use capability
            # for one ZIP. Browser downloads need no reusable bearer in URLs.
            await self._app(scope, receive, send)
            return

        supplied_token = _request_bearer_token(scope)
        if hmac.compare_digest(supplied_token, self._token):
            await self._app(scope, receive, send)
            return

        await _authentication_refusal()(scope, receive, send)


def configure_bearer_auth(app: FastAPI) -> None:
    """Configure bearer admission, the cross-site origin guard, and the peer-address seam."""

    token = configured_bearer_token()
    app.state.bearer_token = token
    app.state.peer_address_getter = peer_address_from_scope
    # Inner to the bearer check: a request admitted without a valid token
    # (loopback, or no token configured) still may not change state from an
    # untrusted web page.
    from clio_agent.gact.origin_guard import OriginGuardMiddleware  # noqa: PLC0415

    app.add_middleware(OriginGuardMiddleware, state=app.state)
    if token is not None:
        app.add_middleware(BearerAuthMiddleware, token=token, state=app.state)
