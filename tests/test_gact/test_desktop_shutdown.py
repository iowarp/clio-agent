"""Desktop-owned server shutdown must remain private and graceful."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes import lifecycle


def _app() -> FastAPI:
    app = FastAPI()
    lifecycle.register_lifecycle_routes(app)
    return app


def test_shutdown_route_is_hidden_from_non_desktop_servers(monkeypatch) -> None:
    monkeypatch.delenv(lifecycle.DESKTOP_MANAGED_ENV, raising=False)
    called: list[bool] = []
    monkeypatch.setattr(lifecycle, "schedule_desktop_shutdown", lambda _app: called.append(True))

    response = TestClient(_app()).post("/v1/desktop/shutdown")

    assert response.status_code == 404
    assert called == []


def test_managed_desktop_shutdown_schedules_clean_exit(monkeypatch) -> None:
    monkeypatch.setenv(lifecycle.DESKTOP_MANAGED_ENV, "1")
    called: list[bool] = []
    monkeypatch.setattr(lifecycle, "schedule_desktop_shutdown", lambda _app: called.append(True))

    app = _app()
    response = TestClient(app).post("/v1/desktop/shutdown")

    assert response.status_code == 202
    assert response.json() == {"status": "stopping"}
    assert called == [True]
    assert app.state.desktop_shutdown_requested is True


def test_shutdown_callback_sets_uvicorn_should_exit(monkeypatch) -> None:
    monkeypatch.setattr(lifecycle.signal, "raise_signal", lambda _signal: None)
    app = _app()

    class Server:
        should_exit = False

    server = Server()
    app.state.uvicorn_server = server

    lifecycle._request_server_exit(app)

    assert server.should_exit is True


@pytest.mark.asyncio
async def test_runtime_is_released_before_server_exit(monkeypatch) -> None:
    app = _app()
    order: list[str] = []
    monkeypatch.setattr(
        "clio_agent.arc.storage.release_runtime_client",
        lambda: order.append("runtime released"),
    )
    monkeypatch.setattr(
        lifecycle,
        "_request_server_exit",
        lambda _app: order.append("server exit"),
    )

    await lifecycle._release_runtime_then_exit(app)
    await asyncio.sleep(0)

    assert order == ["runtime released", "server exit"]
