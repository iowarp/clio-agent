"""The route that carries a view's service to the browser (MA23).

``/v1/sessions/{sid}/mcp-apps/{app_id}/service/{data_ref}/…`` proxies HTTP and
WebSocket upgrades to the loopback port the app's server declared. The
capability is the ``data_ref`` path segment, so a page served under the route
keeps it in every relative URL and WebSocket it opens; it is checked by the
same registry lookup (:meth:`MCPAppRegistry.get`) as the app's other routes.

On the way through CLIO:

* adds ``Authorization: Bearer <token>``; the browser never sends or sees it;
* drops the browser's ``Authorization``, ``Cookie``, ``Origin`` and ``Referer``
  and the service's ``Set-Cookie``;
* answers a sandboxed view's (``Origin: null``) CORS, including preflights, so
  a view's ``fetch`` works without opening CLIO's other routes to it;
* rewrites a redirect to the service's own address or an absolute path back
  under the route.

Closing the app removes its registry record, so the route answers 404, and
:func:`clio_agent.gact.mcp_app_service.close_app_service` ends its open
WebSockets with close code 4410 and reason ``mcp_app_closed``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketState
from websockets.asyncio.client import connect as connect_websocket
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from clio_agent.gact.mcp_app_service import (
    record_service_reason,
    service_connections,
    service_refusal,
)
from clio_agent.gact.mcp_app_service_declaration import (
    ServiceRefusal,
    is_service_path,
    service_prefix,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.mcp_apps import MCPAppRecord

_ROUTE = "/v1/sessions/{sid}/mcp-apps/{app_id}/service/{data_ref}"
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
_DROP_REQUEST = _HOP_BY_HOP | {"authorization", "cookie", "origin", "referer", "content-length"}
_DROP_RESPONSE = _HOP_BY_HOP | {"set-cookie"}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: The close code an app's open WebSockets get when the app closes.
APP_CLOSED_CODE = 4410
_CONNECT_TIMEOUT_S = 10.0


def _envelope(status: int, reason: str, message: str) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorInfo(error=reason, message=message, recoverable=False))
    return JSONResponse(
        status_code=status,
        content=body.model_dump(exclude_none=True),
        headers={"Cache-Control": "no-store"},
    )


def _refused(denied: ServiceRefusal) -> JSONResponse:
    return _envelope(403, denied.reason, denied.message)


def _upstream_target(scope: Scope, data_ref: str) -> str:
    """The path and query below the route, exactly as the browser encoded them."""

    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if raw else quote(str(scope.get("path") or ""))
    marker = f"/service/{data_ref}"
    index = path.find(marker)
    rest = path[index + len(marker) :] if index >= 0 else "/"
    rest = rest or "/"
    query = (scope.get("query_string") or b"").decode("latin-1")
    return f"{rest}?{query}" if query else rest


def _forward_headers(headers: Headers, token: str, port: int) -> list[tuple[str, str]]:
    forwarded = [
        (name, value) for name, value in headers.items() if name.lower() not in _DROP_REQUEST
    ]
    forwarded.append(("host", f"127.0.0.1:{port}"))
    forwarded.append(("authorization", f"Bearer {token}"))
    return forwarded


def _rewrite_location(value: str, prefix: str, port: int) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        if parsed.hostname in _LOOPBACK_HOSTS and parsed.port == port:
            return prefix + urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
        return value
    if not parsed.scheme and not parsed.netloc and value.startswith("/"):
        return prefix + value
    return value


def _cors_headers(origin: str | None) -> list[tuple[bytes, bytes]]:
    if origin != "null":
        return []
    return [(b"access-control-allow-origin", b"null"), (b"vary", b"Origin")]


class ServicePreflightMiddleware:
    """Answer a sandboxed view's CORS preflight for a service route.

    Installed outermost so CLIO's own CORS policy, which does not trust the
    opaque ``null`` origin for its other routes, never sees these preflights.
    A preflight carries no data and reaches neither the registry nor the
    service.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "OPTIONS"
            and is_service_path(str(scope.get("path") or ""))
        ):
            headers = Headers(scope=scope)
            method = headers.get("access-control-request-method")
            if method is not None:
                raw: list[tuple[bytes, bytes]] = [
                    *_cors_headers(headers.get("origin")),
                    (b"access-control-allow-methods", method.encode("latin-1")),
                    (b"access-control-max-age", b"600"),
                    (b"content-length", b"0"),
                ]
                requested = headers.get("access-control-request-headers")
                if requested:
                    raw.append((b"access-control-allow-headers", requested.encode("latin-1")))
                await send({"type": "http.response.start", "status": 204, "headers": raw})
                await send({"type": "http.response.body", "body": b""})
                return
        await self._app(scope, receive, send)


def _close_code(code: Any) -> int:
    """A close code that may be sent on the wire (1005/1006/1015 may not)."""

    if not isinstance(code, int) or code in {1005, 1006, 1015} or not 1000 <= code <= 4999:
        return 1000
    return code


async def _close_browser(websocket: WebSocket, code: int, reason: str) -> None:
    if (
        websocket.application_state == WebSocketState.CONNECTED
        and websocket.client_state == WebSocketState.CONNECTED
    ):
        try:
            await websocket.close(code=code, reason=reason)
        except RuntimeError:
            pass  # the browser left first; nothing remains to close


def register_mcp_app_service_routes(
    app: FastAPI, resolve: Callable[[str, str, str], MCPAppRecord]
) -> None:
    """Register the HTTP and WebSocket service routes.

    Args:
        app: The GACT application.
        resolve: The app routes' capability check; raises a 404 for a wrong
            session, app, capability, or a closed app.
    """

    app.add_middleware(ServicePreflightMiddleware)

    async def admit(request: Request | WebSocket, record: MCPAppRecord) -> ServiceRefusal | None:
        server = request.scope.get("server")
        port = server[1] if isinstance(server, (tuple, list)) and len(server) == 2 else None
        return await service_refusal(app, record, port if isinstance(port, int) else None)

    @app.api_route(_ROUTE, methods=["GET", "HEAD"])
    async def service_root(sid: str, app_id: str, data_ref: str) -> Response:
        resolve(sid, app_id, data_ref)
        return RedirectResponse(f"{data_ref}/", status_code=307)

    @app.api_route(_ROUTE + "/{path:path}", methods=_METHODS)
    async def service_http(
        sid: str, app_id: str, data_ref: str, path: str, request: Request
    ) -> Response:
        del path  # the raw path below the route is forwarded unchanged
        record = resolve(sid, app_id, data_ref)
        denied = await admit(request, record)
        if denied is not None:
            return _refused(denied)
        service = record.service
        target = f"http://127.0.0.1:{service.port}{_upstream_target(request.scope, data_ref)}"
        body = None if request.method in {"GET", "HEAD"} else await request.body()
        client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(_CONNECT_TIMEOUT_S, read=None, write=None, pool=None),
        )
        outgoing = client.build_request(
            request.method,
            target,
            headers=_forward_headers(request.headers, service.token, service.port),
            content=body,
        )
        try:
            upstream = await client.send(outgoing, stream=True)
        except httpx.TransportError as exc:
            await client.aclose()
            record_service_reason(
                "service_unreachable", app_instance_id=app_id, error=type(exc).__name__
            )
            return _envelope(
                502,
                "service_unreachable",
                "This view's live service did not answer. Its server may have stopped.",
            )

        connections = service_connections(app)

        async def close_upstream() -> None:
            await upstream.aclose()

        connections.add(app_id, close_upstream)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            except (httpx.StreamError, httpx.TransportError):
                return  # the app closed mid-response; the body ends here

        async def finish() -> None:
            connections.discard(app_id, close_upstream)
            await upstream.aclose()
            await client.aclose()

        prefix = service_prefix(sid, app_id, data_ref)
        raw: list[tuple[bytes, bytes]] = []
        for name, value in upstream.headers.multi_items():
            lowered = name.lower()
            if lowered in _DROP_RESPONSE or lowered.startswith("access-control-"):
                continue
            if lowered == "location":
                value = _rewrite_location(value, prefix, service.port)
            raw.append((lowered.encode("latin-1"), value.encode("latin-1")))
        raw.extend(_cors_headers(request.headers.get("origin")))
        response = StreamingResponse(
            stream(), status_code=upstream.status_code, background=BackgroundTask(finish)
        )
        response.raw_headers = raw
        return response

    @app.websocket(_ROUTE + "/{path:path}")
    async def service_websocket(
        websocket: WebSocket, sid: str, app_id: str, data_ref: str, path: str
    ) -> None:
        del path
        try:
            record = resolve(sid, app_id, data_ref)
        except HTTPException:
            await websocket.close(code=4404, reason="not_found")
            return
        denied = await admit(websocket, record)
        if denied is not None:
            await websocket.close(code=4403, reason=denied.reason)
            return
        service = record.service
        offered = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        ]
        target = f"ws://127.0.0.1:{service.port}{_upstream_target(websocket.scope, data_ref)}"
        try:
            upstream = await connect_websocket(
                target,
                additional_headers=[("Authorization", f"Bearer {service.token}")],
                subprotocols=offered or None,  # type: ignore[arg-type]
                proxy=None,
                max_size=None,
                ping_interval=None,
                open_timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            record_service_reason(
                "service_unreachable", app_instance_id=app_id, error=type(exc).__name__
            )
            await websocket.accept()
            await websocket.close(code=1011, reason="service_unreachable")
            return
        await websocket.accept(subprotocol=upstream.subprotocol)

        def alive() -> bool:
            try:
                resolve(sid, app_id, data_ref)
            except HTTPException:
                return False
            return True

        async def browser_to_service() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    await upstream.close(code=_close_code(message.get("code")))
                    return
                if not alive():
                    return
                text = message.get("text")
                await upstream.send(text if text is not None else message.get("bytes") or b"")

        async def service_to_browser() -> None:
            try:
                async for data in upstream:
                    if not alive():
                        return
                    if isinstance(data, str):
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)
            except ConnectionClosed:
                pass
            await _close_browser(
                websocket, _close_code(upstream.close_code), upstream.close_reason or ""
            )

        tasks = [
            asyncio.create_task(browser_to_service()),
            asyncio.create_task(service_to_browser()),
        ]

        async def close_for_app() -> None:
            for task in tasks:
                task.cancel()
            await _close_browser(websocket, APP_CLOSED_CODE, "mcp_app_closed")
            await upstream.close()

        connections = service_connections(app)
        connections.add(app_id, close_for_app)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not alive():
                await _close_browser(websocket, APP_CLOSED_CODE, "mcp_app_closed")
        finally:
            connections.discard(app_id, close_for_app)
            await upstream.close()
            await _close_browser(websocket, 1000, "")


__all__ = [
    "APP_CLOSED_CODE",
    "ServicePreflightMiddleware",
    "register_mcp_app_service_routes",
]
