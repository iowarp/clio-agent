"""A view's service carried through CLIO's capability-bound route (MA23)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_sandbox import _csp_header
from clio_agent.gact.mcp_app_sandbox_origin import sandbox_host_for
from clio_agent.gact.mcp_app_service import recorded_service_reasons
from clio_agent.gact.mcp_app_service_declaration import (
    SERVICE_META_KEY,
    is_loopback_source,
    is_service_path,
    parse_service_declaration,
    split_service_declaration,
)
from clio_agent.gact.mcp_apps import MCP_APP_MIME_TYPE, call_tool_result_to_observer

TOKEN = "service-secret-token-value"


def test_declaration_is_parsed_and_taken_off_the_result() -> None:
    result = {
        "content": [{"type": "text", "text": "opened"}],
        "_meta": {SERVICE_META_KEY: {"port": 8123, "token": TOKEN}, "other": {"keep": 1}},
    }
    stripped, service = split_service_declaration(result)
    assert service is not None and service.port == 8123 and service.token == TOKEN
    assert service.refusal is None
    assert stripped["_meta"] == {"other": {"keep": 1}}
    assert TOKEN not in repr(service)
    only, _ = split_service_declaration({"_meta": {SERVICE_META_KEY: {"port": 1, "token": "t"}}})
    assert "_meta" not in only
    untouched, none = split_service_declaration({"content": []})
    assert none is None and untouched == {"content": []}


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"port": 0, "token": "t"},
        {"port": 70000, "token": "t"},
        {"port": True, "token": "t"},
        {"port": 80, "token": ""},
        {"port": 80, "token": "a\r\nInjected: 1"},
        {"port": "80", "token": "t"},
    ],
)
def test_a_malformed_declaration_is_refused_with_a_reason(value: Any) -> None:
    service = parse_service_declaration(value)
    assert service.refusal is not None
    assert service.refusal.reason == "service_declaration_invalid"
    assert service.checked


@pytest.mark.parametrize(
    ("source", "loopback"),
    [
        ("http://127.0.0.1:*", True),
        ("ws://localhost:9000", True),
        ("https://[::1]:443", True),
        ("127.0.0.5", True),
        ("*.localhost", True),
        ("http://0.0.0.0:80", True),
        ("blob:", False),
        ("https://cdn.example.org", False),
        ("*", False),
        ("wss://example.org/socket", False),
    ],
)
def test_loopback_sources(source: str, loopback: bool) -> None:
    assert is_loopback_source(source) is loopback


def test_csp_refuses_loopback_and_allows_only_the_service_route() -> None:
    header = _csp_header(
        {"connectDomains": ["http://127.0.0.1:*", "https://api.example.org"]},
        "http://127.0.0.1:5173",
        ["http://localhost:8100/v1/x/service/cap/", "ws://localhost:8100/v1/x/service/cap/"],
    )
    connect = next(part for part in header.split("; ") if part.startswith("connect-src"))
    frame = next(part for part in header.split("; ") if part.startswith("frame-src"))
    assert "127.0.0.1" not in connect
    assert "https://api.example.org" in connect
    assert "ws://localhost:8100/v1/x/service/cap/" in connect
    assert "http://localhost:8100/v1/x/service/cap/" in frame


def test_service_path_shape() -> None:
    assert is_service_path("/v1/sessions/s/mcp-apps/a/service/cap/")
    assert is_service_path("/v1/sessions/s/mcp-apps/a/service/cap")
    assert not is_service_path("/v1/sessions/s/mcp-apps/a/sandbox")


class _Upstream:
    """A loopback service that requires the token and records what reached it."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.socket_messages: list[str] = []
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self._sock = sock

        async def check(scope_headers: dict[str, str]) -> bool:
            self.seen.append(scope_headers)
            return scope_headers.get("authorization") == f"Bearer {TOKEN}"

        async def page(request: Request) -> Response:
            if not await check(dict(request.headers)):
                return Response("no", status_code=401)
            return JSONResponse(
                {"path": request.url.path, "query": request.url.query},
                headers={"set-cookie": "leak=1", "access-control-allow-origin": "*"},
            )

        async def moved(request: Request) -> Response:
            await check(dict(request.headers))
            return RedirectResponse(f"http://127.0.0.1:{self.port}/state", status_code=302)

        async def bump(request: Request) -> Response:
            if not await check(dict(request.headers)):
                return Response("no", status_code=401)
            return JSONResponse({"bumped": (await request.body()).decode()})

        async def ws(websocket: WebSocket) -> None:
            if not await check(dict(websocket.headers)):
                await websocket.close(code=4401)
                return
            await websocket.accept()
            try:
                while True:
                    text = await websocket.receive_text()
                    self.socket_messages.append(text)
                    await websocket.send_text(f"echo:{text}")
            except WebSocketDisconnect:
                return

        app = Starlette(
            routes=[
                Route("/state", page),
                Route("/moved", moved),
                Route("/bump", bump, methods=["POST"]),
                WebSocketRoute("/ws", ws),
            ]
        )
        self._server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
        )
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._server.serve(sockets=[sock])), daemon=True
        )

    def __enter__(self) -> _Upstream:
        self._thread.start()
        deadline = time.monotonic() + 20
        while not self._server.started:
            assert time.monotonic() < deadline, "the upstream service never started"
            time.sleep(0.01)
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()


class _Executor:
    def __init__(self, transport: str) -> None:
        self._clio_namespace_specs = {"svc": SimpleNamespace(transport=transport)}
        self.definitions = {
            "svc_open": SimpleNamespace(meta={"ui": {"resourceUri": "ui://svc/view"}}),
            "svc_poke": SimpleNamespace(meta={"ui": {"visibility": ["app"]}}),
        }

    def get_all_tool_definitions(self) -> dict[str, Any]:
        return self.definitions

    def call_tool_result(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        del name, args
        return {"content": [], "_meta": {SERVICE_META_KEY: {"port": 1, "token": TOKEN}}}

    def read_resource(self, namespace: str | None, uri: str) -> list[dict[str, Any]]:
        del namespace
        return [
            {
                "uri": uri,
                "mimeType": MCP_APP_MIME_TYPE,
                "text": "<!doctype html><title>view</title>",
                "_meta": {"ui": {"csp": {"connectDomains": ["http://127.0.0.1:*"]}}},
            }
        ]


@pytest.fixture
def upstream() -> Iterator[_Upstream]:
    with _Upstream() as service:
        yield service


def _open(app: Any, sid: str, port: int) -> Any:
    return app.state.mcp_app_registry.register(
        session_id=sid,
        source_namespace="svc",
        tool_name="svc_open",
        resource_uri="ui://svc/view",
        tool_input={},
        tool_result={
            "content": [{"type": "text", "text": "opened"}],
            "_meta": {SERVICE_META_KEY: {"port": port, "token": TOKEN}},
        },
    )


def _client(tmp_path: Path, transport: str = "stdio") -> tuple[Any, TestClient]:
    executor = _Executor(transport)
    agent = SimpleNamespace(_active_tool_executor=lambda: executor)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=agent)
    return app, TestClient(app, base_url="http://127.0.0.1:8100")


def test_descriptor_gives_the_service_url_and_never_the_token(
    tmp_path: Path, upstream: _Upstream
) -> None:
    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        record = _open(app, sid, upstream.port)
        prefix = f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}"
        descriptor = client.get(
            prefix,
            params={"data_ref": record.data_ref},
            headers={"referer": "http://127.0.0.1:8100/session"},
        )
        body = descriptor.json()
        assert TOKEN not in descriptor.text
        assert str(upstream.port) not in json.dumps(body["tool_result"])
        assert SERVICE_META_KEY not in json.dumps(body["tool_result"])
        url = urlparse(body["service_url"])
        sandbox_netloc = f"{sandbox_host_for('svc')}:8100"
        assert url.netloc == urlparse(body["sandbox_url"]).netloc == sandbox_netloc
        assert url.path == f"{prefix}/service/{record.data_ref}/"
        assert body["service_refusal"] is None
        assert body["csp_refused"] == [
            {
                "directive": "connectDomains",
                "source": "http://127.0.0.1:*",
                "reason": "csp_loopback_source_refused",
            }
        ]
        sandbox = client.get(
            f"http://{sandbox_netloc}{prefix}/sandbox",
            params={"data_ref": record.data_ref},
            headers={"referer": "http://127.0.0.1:5173/session"},
        )
        csp = sandbox.headers["content-security-policy"]
        assert f"ws://{sandbox_netloc}{prefix}/service/{record.data_ref}/" in csp
        assert "http://127.0.0.1:*" not in csp
        assert TOKEN not in json.dumps(call_tool_result_to_observer(record.tool_result))


def test_http_through_the_route_carries_the_token_and_strips_the_browser(
    tmp_path: Path, upstream: _Upstream
) -> None:
    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        record = _open(app, sid, upstream.port)
        base = f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}/service/{record.data_ref}"
        answer = client.get(
            f"{base}/state?x=1",
            headers={"origin": "null", "authorization": "Bearer browser", "cookie": "c=1"},
        )
        assert answer.status_code == 200
        assert answer.json() == {"path": "/state", "query": "x=1"}
        assert answer.headers["access-control-allow-origin"] == "null"
        assert "set-cookie" not in answer.headers
        seen = upstream.seen[-1]
        assert seen["authorization"] == f"Bearer {TOKEN}"
        assert "cookie" not in seen and "origin" not in seen and "referer" not in seen
        moved = client.get(f"{base}/moved", follow_redirects=False)
        assert moved.headers["location"] == f"{base}/state"
        posted = client.post(f"{base}/bump", headers={"origin": "null"}, content=b"one")
        assert posted.status_code == 200 and posted.json() == {"bumped": "one"}
        preflight = client.options(
            f"{base}/bump",
            headers={
                "origin": "null",
                "access-control-request-method": "PUT",
                "access-control-request-headers": "content-type",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == "null"
        root = client.get(base, follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"].endswith("/")


def test_route_is_bound_to_its_own_app_and_closes_with_it(
    tmp_path: Path, upstream: _Upstream
) -> None:
    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        first = _open(app, sid, upstream.port)
        second = _open(app, sid, upstream.port)
        prefix = f"/v1/sessions/{sid}/mcp-apps/{first.app_instance_id}"
        crossed = client.get(f"{prefix}/service/{second.data_ref}/state")
        assert crossed.status_code == 404
        assert len(upstream.seen) == 0
        with client.websocket_connect(f"{prefix}/service/{first.data_ref}/ws") as socket_:
            socket_.send_text("hello")
            assert socket_.receive_text() == "echo:hello"
            closed = client.delete(prefix, params={"data_ref": first.data_ref})
            assert closed.status_code == 204
            message = socket_.receive()
            assert message["type"] == "websocket.close"
            assert message["code"] == 4410
            assert message["reason"] == "mcp_app_closed"
        after = client.get(f"{prefix}/service/{first.data_ref}/state")
        assert after.status_code == 404
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"{prefix}/service/{first.data_ref}/ws") as socket_:
                socket_.receive_text()
        assert any(row["reason"] == "mcp_app_closed" for row in recorded_service_reasons())


def test_a_server_clio_did_not_launch_gets_no_service(tmp_path: Path, upstream: _Upstream) -> None:
    app, client = _client(tmp_path, transport="http")
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        record = _open(app, sid, upstream.port)
        prefix = f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}"
        body = client.get(prefix, params={"data_ref": record.data_ref}).json()
        assert body["service_url"] is None
        assert body["service_refusal"]["reason"] == "service_server_not_launched"
        refused = client.get(f"{prefix}/service/{record.data_ref}/state")
        assert refused.status_code == 403
        assert refused.json()["error"]["error"] == "service_server_not_launched"
        assert upstream.seen == []


def test_unreachable_service_and_clio_port_are_typed(tmp_path: Path) -> None:
    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        record = _open(app, sid, port)
        base = f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}/service/{record.data_ref}"
        down = client.get(f"{base}/state")
        assert down.status_code == 502
        assert down.json()["error"]["error"] == "service_unreachable"
        # A page load (the view's frame navigating) gets a readable page, not JSON.
        page = client.get(f"{base}/", headers={"sec-fetch-mode": "navigate"})
        assert page.status_code == 502
        assert page.headers["content-type"].startswith("text/html")
        assert "Service unavailable" in page.text and "reopen the view" in page.text
        # The descriptor says so too, so the tab does not read "Ready" (fix stream).
        descriptor = client.get(
            f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}",
            params={"data_ref": record.data_ref},
        ).json()
        assert descriptor["service_url"] is None
        assert descriptor["service_refusal"]["reason"] == "service_unreachable"
        own = _open(app, sid, 8100)
        own_base = f"/v1/sessions/{sid}/mcp-apps/{own.app_instance_id}/service/{own.data_ref}"
        refused = client.get(f"{own_base}/state")
        assert refused.status_code == 403
        assert refused.json()["error"]["error"] == "service_port_is_clio"


def test_a_view_called_tool_cannot_hand_the_view_a_declaration(tmp_path: Path) -> None:
    app, client = _client(tmp_path)
    with client:
        sid = client.post("/v1/sessions", json={"title": "svc"}).json()["id"]
        record = _open(app, sid, 1)
        answer = client.post(
            f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}/tools/call",
            params={"data_ref": record.data_ref},
            json={"name": "poke", "arguments": {}},
        )
        assert answer.status_code == 200
        assert TOKEN not in answer.text and SERVICE_META_KEY not in answer.text
