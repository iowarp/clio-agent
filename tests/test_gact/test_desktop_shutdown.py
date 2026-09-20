"""Desktop-owned server shutdown: private, bearer-authenticated, and ordered.

Covers the route (``gact/routes/lifecycle.py``) and the two lifecycle seams it
drives (``gact/desktop_lifecycle.py``): the route only SIGNALS shutdown, and
the shared clio-core runtime is released exactly once, from the app lifespan,
after the turn drain has settled -- never from the route/request cycle.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.arc import runtime_stop
from clio_agent.gact import app as gact_app
from clio_agent.gact import desktop_lifecycle
from clio_agent.gact.app import build_app
from clio_agent.gact.routes import lifecycle
from tests._config_layer import set_config

_TOKEN = "expected-desktop-token"


def _app(*, bearer_token: str | None = _TOKEN) -> FastAPI:
    app = FastAPI()
    app.state.bearer_token = bearer_token
    lifecycle.register_lifecycle_routes(app)
    return app


@pytest.fixture(autouse=True)
def _reset_runtime_shutdown_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the process-global shutdown latch never leaks across tests.

    Several tests below drive the REAL ``request_desktop_shutdown`` /
    ``prepare_runtime_shutdown`` path (not a mock), which mutates
    ``runtime_stop._runtime_shutdown_requested`` -- a process-global, not a
    per-app one. Capturing the value as monkeypatch's "original" here
    guarantees it is restored after the test regardless of what the test
    mutates it to.
    """
    monkeypatch.setattr(runtime_stop, "_runtime_shutdown_requested", False)


# ---- route: env gate, auth, and the signal-only contract ----


def test_shutdown_route_hidden_when_not_desktop_managed(monkeypatch) -> None:
    monkeypatch.delenv(lifecycle.DESKTOP_MANAGED_ENV, raising=False)

    response = TestClient(_app()).post("/v1/desktop/shutdown")

    assert response.status_code == 404


def test_shutdown_route_503_when_bearer_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv(lifecycle.DESKTOP_MANAGED_ENV, "1")

    response = TestClient(_app(bearer_token=None)).post("/v1/desktop/shutdown")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["error"] == "desktop_lifecycle_unconfigured"
    assert "CLIO_AUTH_TOKEN" in error["message"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
    ],
)
def test_shutdown_route_401_on_wrong_bearer_even_from_loopback(
    tmp_path, monkeypatch, headers: dict[str, str]
) -> None:
    """The route's own bearer check applies even to a peer the trust-socket
    middleware would otherwise wave through -- it never consults peer address."""
    monkeypatch.setenv(lifecycle.DESKTOP_MANAGED_ENV, "1")
    set_config("gact.auth.bearer_token", _TOKEN)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    app.state.peer_address_getter = lambda _scope: "127.0.0.1"

    with TestClient(app) as client:
        # Ordinary routes still trust this loopback peer without a token.
        assert client.get("/v1/capabilities").status_code == 200

        response = client.post("/v1/desktop/shutdown", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["error"] == "authentication_required"


def test_shutdown_route_accepts_bearer_and_requests_server_exit(monkeypatch) -> None:
    monkeypatch.setenv(lifecycle.DESKTOP_MANAGED_ENV, "1")
    released: list[bool] = []
    monkeypatch.setattr(
        "clio_agent.arc.storage.release_runtime_client",
        lambda *a, **k: released.append(True),
    )

    class StubServer:
        should_exit = False

    app = _app()
    server = StubServer()

    with TestClient(app) as client:
        app.state.uvicorn_server = server
        response = client.post(
            "/v1/desktop/shutdown", headers={"Authorization": f"Bearer {_TOKEN}"}
        )
        assert response.status_code == 202
        assert response.json() == {"status": "stopping", "exit_path": "should_exit"}
        assert app.state.desktop_shutdown_requested is True

        # The exit is scheduled via loop.call_later(0.1, ...), not immediate.
        assert server.should_exit is False
        time.sleep(0.25)
        assert server.should_exit is True

    assert released == []


# ---- desktop_lifecycle.request_desktop_shutdown: signal only, never release ----


async def test_request_desktop_shutdown_never_releases_runtime_client(monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(
        "clio_agent.arc.storage.release_runtime_client",
        lambda *a, **k: released.append(True),
    )

    class StubServer:
        should_exit = False

    app = FastAPI()
    app.state.uvicorn_server = StubServer()

    outcome = desktop_lifecycle.request_desktop_shutdown(app)

    assert outcome.exit_path == "should_exit"
    assert app.state.desktop_shutdown_requested is True
    assert runtime_stop._runtime_shutdown_requested is True
    assert released == []


# ---- lifespan: the ONE place the runtime is actually released, after drain ----


def test_lifespan_releases_runtime_after_turn_drain_for_desktop(tmp_path, monkeypatch) -> None:
    order: list[str] = []

    async def fake_drain(app, logger) -> None:  # noqa: ANN001 - test shim
        order.append("drain")

    def fake_release(*args, **kwargs) -> None:  # noqa: ANN002, ANN003 - test shim
        order.append("release")

    monkeypatch.setattr(gact_app, "drain_app_turns", fake_drain)
    monkeypatch.setattr("clio_agent.arc.storage.release_runtime_client", fake_release)
    monkeypatch.setattr(
        desktop_lifecycle,
        "terminate_process_after_cleanup",
        lambda _app: order.append("exit"),
    )

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)

    with TestClient(app):
        app.state.desktop_shutdown_requested = True

    assert order == ["drain", "release", "exit"]


def test_lifespan_releases_runtime_when_not_desktop(tmp_path, monkeypatch) -> None:
    order: list[str] = []

    async def fake_drain(app, logger) -> None:  # noqa: ANN001 - test shim
        order.append("drain")

    def fake_release(*args, **kwargs) -> None:  # noqa: ANN002, ANN003 - test shim
        order.append("release")

    monkeypatch.setattr(gact_app, "drain_app_turns", fake_drain)
    monkeypatch.setattr("clio_agent.arc.storage.release_runtime_client", fake_release)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)

    with TestClient(app):
        pass  # desktop_shutdown_requested never set: a plain (non-desktop) boot

    assert order == ["drain", "release"]


def test_reset_for_boot_clears_runtime_shutdown_latch() -> None:
    runtime_stop.prepare_runtime_shutdown()
    assert runtime_stop._runtime_shutdown_requested is True

    desktop_lifecycle.reset_for_boot()

    assert runtime_stop._runtime_shutdown_requested is False
