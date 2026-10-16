"""The preview bootstrap is public static code, never a public data/API lane."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import State

from clio_agent.gact.auth import BearerAuthMiddleware
from clio_agent.gact.html_renderer import RENDERER_HTML, register_html_renderer
from clio_agent.gact.origin_guard import OriginGuardMiddleware


def test_renderer_needs_no_bearer_but_does_not_open_other_routes() -> None:
    app = FastAPI()
    register_html_renderer(app)
    state = State(
        {"bearer_token": "private-token", "peer_address_getter": lambda _scope: "198.51.100.2"}
    )
    app.add_middleware(OriginGuardMiddleware, state=state)
    app.add_middleware(BearerAuthMiddleware, state=state, token="private-token")
    with TestClient(app) as client:
        response = client.get("/v1/html-renderer", headers={"Host": "foreign.example"})
        assert response.status_code == 200 and response.text == RENDERER_HTML
        assert "event.source !== parent" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert "connect-src 'none'" in response.headers["content-security-policy"]
        for method, path in (
            ("POST", "/v1/html-renderer"),
            ("GET", "/v1/sessions"),
            ("GET", "/v1/html-renderer/private"),
        ):
            assert client.request(method, path).status_code == 401
