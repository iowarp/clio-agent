"""Refuse cross-site state changes that arrive without a bearer token.

A request can reach CLIO without a valid bearer token in two ways: its peer is
loopback (:class:`clio_agent.gact.auth.BearerAuthMiddleware` trusts the local
machine), or no token is configured at all. Either way, any web page the user
visits can make the browser send a "simple" cross-origin request to CLIO: a
``POST`` with a ``text/plain`` body and no custom headers skips the CORS
preflight. CORS then only stops the page from *reading* the answer, but the
route has already run. Body-less routes (cancel a turn, install a provider's
runtime support, run sandbox setup, reconnect an MCP server) have nothing that
fails validation, so they act.

:class:`OriginGuardMiddleware` closes that gap for every request that carries no
valid bearer token. A state-changing method (``POST``, ``PUT``, ``PATCH``,
``DELETE``) is refused with a typed ``403 origin_not_allowed`` when:

* it names an ``Origin`` that is not allowed, or
* the browser marks it ``Sec-Fetch-Site: cross-site`` and its ``Origin`` is not
  allowed (or absent).

Allowed origins are the Desktop WebView's own, the configured
``gact.cors.origins``, and the server's own origin when the web UI is served
same-origin (``paths.web_dir``) on a loopback host name. The host-name check
keeps DNS rebinding out: a page on ``evil.example`` resolved to 127.0.0.1 sends
``Host: evil.example``, which is not a loopback name.

A request with no ``Origin`` and no cross-site marker is let through. The
Desktop's native HTTP bridge, curl and SDK clients send none, and a browser
always sends ``Origin`` on a cross-origin ``POST``. Reads (``GET``) are left to
CORS, and a request with a valid token is not inspected.
"""

from __future__ import annotations

import hmac
import logging

from starlette.datastructures import Headers, State
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from clio_agent.gact.cors import gact_cors_origins
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

logger = logging.getLogger(__name__)

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

DESKTOP_WEBVIEW_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
"""The Desktop WebView's own origins (macOS/Linux, Windows, Windows HTTPS scheme)."""

_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _same_loopback_origin(origin: str, scope: Scope, headers: Headers) -> bool:
    """Whether ``origin`` is this server's own origin on a loopback host name."""

    host = headers.get("host", "")
    if not host:
        return False
    host_name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    if host_name.lower() not in _LOOPBACK_HOST_NAMES:
        return False
    scheme = str(scope.get("scheme") or "http")
    return origin == f"{scheme}://{host}"


def browser_origin_allowed(origin: str, scope: Scope, headers: Headers) -> bool:
    """Whether a browser ``Origin`` may change state without a bearer token.

    Args:
        origin: The request's ``Origin`` header.
        scope: The ASGI scope (scheme).
        headers: The request headers (``Host``).

    Returns:
        ``True`` for the Desktop WebView, a configured CORS origin, or this
        server's own loopback origin.
    """

    if origin in DESKTOP_WEBVIEW_ORIGINS:
        return True
    configured = gact_cors_origins()
    if configured == ["*"] or origin in configured:
        return True
    return _same_loopback_origin(origin, scope, headers)


def cross_site_refusal(scope: Scope) -> str | None:
    """Return why a token-less request must be refused, or ``None`` to admit it.

    Args:
        scope: The ASGI HTTP scope of a request that carries no valid token.

    Returns:
        A log detail naming the offending origin and marker, or ``None``.
    """

    method = str(scope.get("method") or "").upper()
    if method not in STATE_CHANGING_METHODS:
        return None
    headers = Headers(scope=scope)
    origin = headers.get("origin")
    fetch_site = headers.get("sec-fetch-site", "").lower()
    allowed = origin is not None and browser_origin_allowed(origin, scope, headers)
    if allowed:
        return None
    if origin is None and fetch_site != "cross-site":
        return None
    return f"origin={origin!r} sec-fetch-site={fetch_site or None!r}"


def _origin_refusal() -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorInfo(
            error="origin_not_allowed",
            message=(
                "This request came from a web page this CLIO does not trust. "
                "Add its origin to gact.cors.origins, or send a bearer token."
            ),
            details={"allowed": "desktop, gact.cors.origins, same-origin web UI"},
            recoverable=False,
        )
    )
    return JSONResponse(status_code=403, content=envelope.model_dump(exclude_none=True))


def _has_valid_token(headers: Headers, expected: str | None) -> bool:
    if expected is None:
        return False
    scheme, separator, token = headers.get("authorization", "").partition(" ")
    if not (separator and scheme.casefold() == "bearer" and token):
        return False
    return hmac.compare_digest(token, expected)


class OriginGuardMiddleware:
    """Refuse token-less, cross-site state changes with a typed 403."""

    def __init__(self, app: ASGIApp, *, state: State) -> None:
        self._app = app
        self._state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        expected = getattr(self._state, "bearer_token", None)
        if not _has_valid_token(Headers(scope=scope), expected):
            detail = cross_site_refusal(scope)
            if detail is not None:
                logger.warning(
                    "gact request refused: reason=origin_not_allowed method=%s path=%s %s",
                    scope.get("method"),
                    scope.get("path"),
                    detail,
                )
                await _origin_refusal()(scope, receive, send)
                return
        await self._app(scope, receive, send)
