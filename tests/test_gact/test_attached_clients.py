"""The attached-client count: register, release, prune, and the last-one-out stop."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import attached_clients, desktop_lifecycle


@pytest.fixture
def server(tmp_path: Path) -> Iterator[TestClient]:
    from clio_agent.gact.app import build_app

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        yield client


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(
        desktop_lifecycle, "request_managed_shutdown", lambda _app, owner: calls.append(owner)
    )
    return calls


def test_attach_release_counts_clients(server: TestClient, stops: list[str]) -> None:
    first = server.post("/v1/server/clients", json={"kind": "sdk", "pid": os.getpid()})
    assert first.status_code == 201 and first.json()["attached"] == 1
    second = server.post("/v1/server/clients", json={"kind": "desktop"}).json()
    assert second["attached"] == 2
    view = server.get("/v1/server/clients").json()
    assert view["attached"] == 2 and view["stop_with_last_client"] is False
    assert server.delete(f"/v1/server/clients/{first.json()['client_id']}").json() == {
        "attached": 1,
        "stopping": False,
    }
    last = server.delete(f"/v1/server/clients/{second['client_id']}").json()
    assert last == {"attached": 0, "stopping": False}  # a server nobody scripted keeps running
    assert stops == []
    assert server.delete("/v1/server/clients/client_missing").status_code == 404


def test_script_started_server_stops_with_its_last_client(
    server: TestClient, stops: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(attached_clients.STOP_WITH_LAST_CLIENT_ENV, "1")
    one = server.post("/v1/server/clients", json={"pid": os.getpid()}).json()
    two = server.post("/v1/server/clients", json={"pid": os.getpid()}).json()
    assert server.delete(f"/v1/server/clients/{one['client_id']}").json()["stopping"] is False
    assert stops == []
    assert server.delete(f"/v1/server/clients/{two['client_id']}").json()["stopping"] is True
    assert stops == ["serve"]
    refused = server.post("/v1/server/clients", json={"pid": os.getpid()})
    assert refused.status_code == 503 and refused.json()["error"]["error"] == "server_stopping"


def test_a_client_that_exits_without_releasing_is_pruned(
    server: TestClient, stops: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(attached_clients.STOP_WITH_LAST_CLIENT_ENV, "1")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        attached = server.post("/v1/server/clients", json={"pid": child.pid})
        assert attached.status_code == 201
    finally:
        child.kill()
        child.wait()
    registry: Any = attached_clients.registry(server.app)
    gone = registry.dead()
    assert gone == [attached.json()["client_id"]]
    registry.prune(gone)
    assert registry.rows == {}
    assert stops == ["serve"]


def test_an_unknown_local_process_is_refused(server: TestClient) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    refused = server.post("/v1/server/clients", json={"pid": child.pid})
    assert refused.status_code == 422
    assert refused.json()["error"]["error"] == "client_process_unknown"
