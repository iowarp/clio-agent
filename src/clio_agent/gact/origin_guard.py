"""Host and Origin policy for requests that arrive without a bearer token.

A request can reach CLIO without a valid bearer token in two ways: its peer is
loopback (:class:`clio_agent.gact.auth.BearerAuthMiddleware` trusts the local
machine), or no token is configured at all. Two browser attacks target exactly
those requests:

* **Cross-site state changes.** Any web page the user visits can make the
  browser send a "simple" cross-origin request: a ``POST`` with a
  ``text/plain`` body and no custom headers skips the CORS preflight. CORS then
  only stops the page from *reading* the answer; the route has already run.
* **DNS rebinding.** A foreign domain re-pointed at 127.0.0.1 makes its page
  same-origin with CLIO, so CORS does not apply at all and the page can read
  sessions, messages and files. The browser still sends the foreign name in
  ``Host``.

:class:`OriginGuardMiddleware` therefore checks every token-less HTTP request
and WebSocket upgrade:

1. ``Host`` must name ``localhost``, ``127.0.0.1`` or ``[::1]`` (any port) or a
   host configured in ``gact.allowed_hosts`` for a LAN or container deployment.
   Otherwise: typed ``403 host_not_allowed``.
2. A state-changing method (``POST``, ``PUT``, ``PATCH``, ``DELETE``) is refused
   with a typed ``403 origin_not_allowed`` when it names an ``Origin`` that is
   not allowed, or is marked ``Sec-Fetch-Site: cross-site`` without an allowed
   ``Origin``.

Allowed origins (:func:`browser_origin_allowed`, also used by the Desktop SSH
transport socket) are the Desktop WebView's own, the configured
``gact.cors.origins``, and the server's own origin (``Origin`` equal to
``scheme://Host`` on an allowed host) for the same-origin web UI.

A request with no ``Origin`` and no cross-site marker passes step 2: the
Desktop's native HTTP bridge, curl and SDK clients send none, and a browser
always sends ``Origin`` on a cross-origin ``POST``. A request with a valid token
is not inspected. Every refusal is logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from starlette.datastructures import Headers, State
from starlette.types import ASGIApp, Receive, Scope, Send

from clio_agent.gact.auth import has_valid_bearer
from clio_agent.gact.cors import gact_allowed_hosts, gact_cors_origins
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

logger = logging.getLogger(__name__)

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

DESKTOP_WEBVIEW_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
"""The Desktop WebView's own origins (macOS/Linux, Windows, Windows HTTPS scheme)."""

TrustReason = Literal["host_not_allowed", "origin_not_allowed"]

_MESSAGES: dict[TrustReason, str] = {
    "host_not_allowed": (
        "This request addressed CLIO by a host name it does not answer to without a "
        "bearer token. Use localhost, add the name to gact.allowed_hosts, or send a "
        "bearer token."
    ),
    "origin_not_allowed": (
        "This request came from a web page this CLIO does not trust. Add its origin to "
        "gact.cors.origins, or send a bearer token."
    ),
}


@dataclass(frozen=True)
class TrustRefusal:
    """Why a token-less request is refused.

    Attributes:
        reason: The typed reason, also the error code on the wire.
        detail: What was seen, for CLIO's log.
    """

    reason: TrustReason
    detail: str


def host_name(host: str) -> str:
    """The lowercase host name of a ``Host`` header value, without its port."""

    host = host.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_allowed(headers: Headers) -> bool:
    """Whether a token-less request's ``Host`` is loopback or configured.

    A request without ``Host`` cannot come from a browser and is allowed.
    """

    host = headers.get("host")
    if host is None:
        return True
    allowed = gact_allowed_hosts()
    return "*" in allowed or host_name(host) in allowed


def browser_origin_allowed(origin: str, scope: Scope, headers: Headers) -> bool:
    """Whether a browser ``Origin`` may act on CLIO without a bearer token.

    Args:
        origin: The request's ``Origin`` header.
        scope: The ASGI scope (scheme).
        headers: The request headers (``Host``).

    Returns:
        ``True`` for the Desktop WebView, a configured CORS origin, or this
        server's own origin on an allowed host.
    """

    if origin in DESKTOP_WEBVIEW_ORIGINS:
        return True
    configured = gact_cors_origins()
    if configured == ["*"] or origin in configured:
        return True
    host = headers.get("host", "")
    if not host or not host_allowed(headers):
        return False
    scheme = str(scope.get("scheme") or "http")
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    return origin == f"{scheme}://{host}"


def trust_refusal(scope: Scope) -> TrustRefusal | None:
    """Return why a token-less request must be refused, or ``None`` to admit it.

    Args:
        scope: The ASGI HTTP or WebSocket scope of a request without a valid token.

    Returns:
        The typed refusal, or ``None``.
    """

    headers = Headers(scope=scope)
    if not host_allowed(headers):
        return TrustRefusal("host_not_allowed", f"host={headers.get('host')!r}")
    method = "GET" if scope["type"] == "websocket" else str(scope.get("method") or "").upper()
    if method not in STATE_CHANGING_METHODS:
        return None
    origin = headers.get("origin")
    fetch_site = headers.get("sec-fetch-site", "").lower()
    if origin is not None and browser_origin_allowed(origin, scope, headers):
        return None
    if origin is None and fetch_site != "cross-site":
        return None
    return TrustRefusal(
        "origin_not_allowed", f"origin={origin!r} sec-fetch-site={fetch_site or None!r}"
    )


def _refusal_body(refusal: TrustRefusal) -> bytes:
    envelope = ErrorEnvelope(
        error=ErrorInfo(
            error=refusal.reason,
            message=_MESSAGES[refusal.reason],
            details={"allowed": "loopback or gact.allowed_hosts; desktop or gact.cors.origins"},
            recoverable=False,
        )
    )
    return json.dumps(envelope.model_dump(exclude_none=True)).encode("utf-8")


async def _refuse(scope: Scope, receive: Receive, send: Send, refusal: TrustRefusal) -> None:
    body = _refusal_body(refusal)
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if scope["type"] == "http":
        await send({"type": "http.response.start", "status": 403, "headers": headers})
        await send({"type": "http.response.body", "body": body})
        return
    await receive()  # websocket.connect
    if "websocket.http.response" in scope.get("extensions", {}):
        await send({"type": "websocket.http.response.start", "status": 403, "headers": headers})
        await send({"type": "websocket.http.response.body", "body": body})
        return
    await send({"type": "websocket.close", "code": 4403, "reason": refusal.reason})


class OriginGuardMiddleware:
    """Refuse token-less requests with a foreign ``Host`` or cross-site state changes."""

    def __init__(self, app: ASGIApp, *, state: State) -> None:
        self._app = app
        self._state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        if not has_valid_bearer(scope, getattr(self._state, "bearer_token", None)):
            refusal = trust_refusal(scope)
            if refusal is not None:
                logger.warning(
                    "gact request refused: reason=%s type=%s method=%s path=%s %s",
                    refusal.reason,
                    scope["type"],
                    scope.get("method"),
                    scope.get("path"),
                    refusal.detail,
                )
                await _refuse(scope, receive, send, refusal)
                return
        await self._app(scope, receive, send)
