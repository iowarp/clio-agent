"""Synthetic stdio MCP server whose view needs a live service behind it (MA23).

Run by CLIO as a stdio connection::

    uv run --no-sync python -B tests/fixtures/mcp_app_service_server.py

``open_counter`` starts (once) a loopback HTTP and WebSocket service on
``CLIO_SERVICE_FIXTURE_PORT`` (default: any free port) that requires a bearer
token (``CLIO_SERVICE_FIXTURE_TOKEN``, default: random), and returns a view. The result declares the service in its private
``_meta["ai.iowarp.clio/service"] = {port, token}``. The view never sees either:
it reads the URL CLIO gives it in ``hostContext["ai.iowarp.clio/service"].url``,
opens a WebSocket there to show a counter that ticks every second, fetches the
service's state over HTTP, posts a bump, and frames the service's own page.

Nothing here is a production provider. The service lives as long as this process.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import sys
import threading
from typing import Any

import mcp.types as mcp_types
import uvicorn
from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools import ToolResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect

SERVICE_META_KEY = "ai.iowarp.clio/service"
VIEW_URI = "ui://service-fixture/counter"

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Counter service page</title>
<style>body{margin:0;padding:10px;font:13px system-ui,sans-serif;color:#1f2937;background:#f3f4f6}
b{font-variant-numeric:tabular-nums}</style></head>
<body><div>Framed service page: count <b id="n">waiting</b>, <span id="s">connecting</span></div>
<script>
const out = document.getElementById('n'), state = document.getElementById('s');
const socket = new WebSocket(new URL('ws', location.href).href.replace(/^http/, 'ws'));
socket.onopen = () => { state.textContent = 'connected over its own WebSocket'; };
socket.onmessage = (event) => { out.textContent = JSON.parse(event.data).count; };
socket.onclose = (event) => { state.textContent = 'closed (' + event.code + ')'; };
</script></body></html>"""

_VIEW = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="light dark">
<title>Live counter</title>
<style>
:root{font:14px/1.5 system-ui,sans-serif}
body{margin:0;color:CanvasText;background:Canvas}
main{display:grid;gap:10px;padding:16px}
.count{font-size:40px;font-weight:600;font-variant-numeric:tabular-nums}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:0}
dt{color:GrayText}dd{margin:0;overflow-wrap:anywhere}
iframe{width:100%;height:56px;border:1px solid GrayText;border-radius:6px}
button{justify-self:start;font:inherit;padding:4px 12px}
</style></head>
<body><main>
<div>Live counter</div>
<div class="count" id="count" aria-live="polite">waiting</div>
<dl>
<dt>WebSocket</dt><dd id="socket">waiting for the host</dd>
<dt>HTTP state</dt><dd id="http">not checked</dd>
<dt>Bump</dt><dd id="bump">not sent</dd>
</dl>
<button id="bump-button" type="button" disabled>Bump the counter</button>
<iframe id="page" title="The service's own page"></iframe>
</main>
<script>
const KEY = 'ai.iowarp.clio/service';
let nextId = 1, serviceUrl = '', socket = null;
const pending = new Map();
const $ = (id) => document.getElementById(id);
function request(method, params) {
  const id = nextId++;
  window.parent.postMessage({jsonrpc: '2.0', id, method, params}, '*');
  return new Promise((resolve, reject) => pending.set(id, {resolve, reject}));
}
function notify(method, params) {
  window.parent.postMessage({jsonrpc: '2.0', method, params: params || {}}, '*');
}
function size() { notify('ui/notifications/size-changed', {height: document.body.scrollHeight}); }
function useHostContext(context) {
  const url = context && context[KEY] && context[KEY].url;
  if (!url || url === serviceUrl) return;
  serviceUrl = url;
  connect();
}
function connect() {
  const wsUrl = new URL('ws', serviceUrl).href.replace(/^http/, 'ws');
  socket = new WebSocket(wsUrl);
  $('socket').textContent = 'connecting';
  socket.onopen = () => {
    $('socket').textContent = 'connected through CLIO';
    $('bump-button').disabled = false;
  };
  socket.onmessage = (event) => { $('count').textContent = JSON.parse(event.data).count; };
  socket.onclose = (event) => {
    $('socket').textContent = 'closed: ' + event.code + (event.reason ? ' ' + event.reason : '');
    $('bump-button').disabled = true;
  };
  fetch(new URL('state', serviceUrl)).then((r) => r.json())
    .then((s) => { $('http').textContent = 'answered, count ' + s.count; })
    .catch((e) => { $('http').textContent = 'failed: ' + e; });
  $('page').src = serviceUrl;
  size();
}
$('bump-button').addEventListener('click', () => {
  fetch(new URL('bump', serviceUrl), {method: 'POST'}).then((r) => r.json())
    .then((s) => { $('bump').textContent = 'accepted, count ' + s.count; })
    .catch((e) => { $('bump').textContent = 'failed: ' + e; });
});
window.addEventListener('message', (event) => {
  const message = event.data;
  if (!message || message.jsonrpc !== '2.0') return;
  if (message.id !== undefined && pending.has(message.id)) {
    const handler = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) handler.reject(message.error); else handler.resolve(message.result);
    return;
  }
  if (message.method === 'ui/notifications/host-context-changed') useHostContext(message.params);
});
request('ui/initialize', {
  protocolVersion: '2026-01-26',
  appInfo: {name: 'live-counter-fixture', version: '1.0.0'},
  appCapabilities: {},
}).then((result) => {
  notify('ui/notifications/initialized');
  useHostContext(result.hostContext);
  if (!serviceUrl) $('socket').textContent = 'the host gave no service address';
  size();
  request('ui/request-display-mode', {mode: 'fullscreen'}).catch(() => undefined);
});
</script></body></html>"""


class _BearerGuard:
    """Refuse every HTTP request and WebSocket without the service token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not secrets.compare_digest(supplied, self._expected):
                if scope["type"] == "websocket":
                    await receive()
                    await send({"type": "websocket.close", "code": 4401})
                    return
                response = Response("token required", status_code=401)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class _CounterService:
    """The loopback service: a counter that ticks and accepts bumps."""

    def __init__(self) -> None:
        # A qualification run may fix the token to search the browser's traffic for it.
        self.token = os.environ.get("CLIO_SERVICE_FIXTURE_TOKEN") or secrets.token_urlsafe(24)
        self.count = 0
        self.port = 0
        self._sockets: set[WebSocket] = set()

    async def _broadcast(self) -> None:
        text = json.dumps({"count": self.count})
        for client in list(self._sockets):
            try:
                await client.send_text(text)
            except Exception:  # noqa: BLE001 - a client left
                self._sockets.discard(client)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.count += 1
            await self._broadcast()

    async def page(self, request: Request) -> HTMLResponse:
        del request
        return HTMLResponse(_PAGE)

    async def state(self, request: Request) -> JSONResponse:
        del request
        return JSONResponse({"count": self.count, "clients": len(self._sockets)})

    async def bump(self, request: Request) -> JSONResponse:
        del request
        self.count += 100
        await self._broadcast()
        return JSONResponse({"count": self.count})

    async def socket(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._sockets.add(websocket)
        await websocket.send_text(json.dumps({"count": self.count}))
        try:
            while True:
                if await websocket.receive_text() == "bump":
                    self.count += 100
                    await self._broadcast()
        except WebSocketDisconnect:
            pass
        finally:
            self._sockets.discard(websocket)

    def start(self) -> None:
        """Bind loopback and serve on a daemon thread (stdout stays MCP's)."""

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", int(os.environ.get("CLIO_SERVICE_FIXTURE_PORT", "0"))))
        self.port = sock.getsockname()[1]
        routes: list[Any] = [
            Route("/", self.page),
            Route("/state", self.state),
            Route("/bump", self.bump, methods=["POST"]),
            WebSocketRoute("/ws", self.socket),
        ]
        app = _BearerGuard(Starlette(routes=routes), self.token)
        config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
        server = uvicorn.Server(config)

        async def run() -> None:
            ticker = asyncio.create_task(self._tick())
            try:
                await server.serve(sockets=[sock])
            finally:
                ticker.cancel()

        threading.Thread(target=lambda: asyncio.run(run()), daemon=True).start()
        print(f"counter service on 127.0.0.1:{self.port}", file=sys.stderr, flush=True)


_service: _CounterService | None = None
_lock = threading.Lock()
server = FastMCP("service-fixture")


def _running() -> _CounterService:
    global _service
    with _lock:
        if _service is None:
            _service = _CounterService()
            _service.start()
        return _service


@server.tool(app=AppConfig(resource_uri=VIEW_URI, visibility=["model", "app"]))
def open_counter() -> ToolResult:
    """Open the live counter view. Its counter ticks every second over a WebSocket."""

    service = _running()
    return ToolResult(
        content=[mcp_types.TextContent(type="text", text="Opened the live counter view.")],
        structured_content={"opened": True},
        meta={SERVICE_META_KEY: {"port": service.port, "token": service.token}},
    )


@server.resource(VIEW_URI)
def counter_view() -> str:
    """The view: it reaches its service only through the URL the host gives it."""

    return _VIEW


if __name__ == "__main__":
    server.run()
