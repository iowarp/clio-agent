"""The startup gate: API calls wait for the task runtime, the web UI shell does not."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.error_middleware import STARTUP_RETRY_AFTER_S, install_error_envelope


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    created = asyncio.new_event_loop()
    try:
        yield created
    finally:
        created.close()


def _app(start: asyncio.Future[None]) -> FastAPI:
    app = FastAPI()
    install_error_envelope(app)

    @app.get("/v1/sessions")
    def sessions() -> list[str]:
        return []

    @app.get("/v1/health")
    def health() -> dict[str, bool]:
        return {"healthy": True}

    @app.get("/")
    def shell() -> str:
        return "shell"

    app.state.task_runtime_start = start
    return app


def test_api_call_while_starting_is_an_unambiguous_retryable_503(
    loop: asyncio.AbstractEventLoop,
) -> None:
    client = TestClient(_app(loop.create_future()))

    response = client.get("/v1/sessions")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(STARTUP_RETRY_AFTER_S)
    error = response.json()["error"]
    assert error["error"] == "task_runtime_starting"
    assert error["recoverable"] is True
    assert error["retry_after_s"] == STARTUP_RETRY_AFTER_S
    assert error["details"] == {"readiness": "starting", "starting": ["task_runtime"]}


def test_health_and_web_shell_answer_while_starting(loop: asyncio.AbstractEventLoop) -> None:
    client = TestClient(_app(loop.create_future()))

    assert client.get("/v1/health").status_code == 200
    assert client.get("/").json() == "shell"


def test_failed_runtime_is_not_retried(loop: asyncio.AbstractEventLoop) -> None:
    start: asyncio.Future[None] = loop.create_future()
    start.set_exception(RuntimeError("boom"))
    client = TestClient(_app(start))

    response = client.get("/v1/sessions")

    assert response.status_code == 503
    assert "Retry-After" not in response.headers
    error = response.json()["error"]
    assert error["error"] == "task_runtime_unavailable"
    assert error["details"] == {"readiness": "failed"}
    start.exception()  # retrieved, so the loop does not log it as never retrieved


def test_ready_runtime_passes_through(loop: asyncio.AbstractEventLoop) -> None:
    start: asyncio.Future[None] = loop.create_future()
    start.set_result(None)

    assert TestClient(_app(start)).get("/v1/sessions").json() == []


def test_web_ui_unknown_api_path_is_json_404_and_routes_fall_back_to_spa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.gact.app import build_app

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    monkeypatch.setenv("CLIO_WEB_DIR", str(web))
    client = TestClient(build_app(sessions_path=tmp_path / "s.json", arc=None))

    missing = client.get("/v1/definitely-not-a-route")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/json")

    page = client.get("/sessions/abc")
    assert page.status_code == 200
    assert "spa" in page.text
