"""The workspace h5grove route resolves files exactly as GET /files/read does.

The real router lives in clio-kit (``hdf5-mcp[data-routes]``), which this
repository does not depend on, so a stand-in module plays its part: its one
endpoint calls the mounted resolver and reports the path it was given.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from clio_agent.gact.app import build_app

META = "/v1/workspaces/ws_default/h5grove/meta/"


def _stand_in_router_module() -> types.ModuleType:
    def create_router(resolve_file):  # type: ignore[no-untyped-def]
        router = APIRouter()

        @router.get("/meta/")
        async def meta(file: str, request: Request) -> dict[str, str]:
            return {"resolved": str(await resolve_file(file, request))}

        return router

    module = types.ModuleType("hdf5_mcp.provider.routes")
    module.create_router = create_router  # type: ignore[attr-defined]
    return module


@pytest.fixture
def client(tmp_path: Path, monkeypatch: MonkeyPatch) -> TestClient:
    monkeypatch.setitem(sys.modules, "hdf5_mcp.provider.routes", _stand_in_router_module())
    client = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    project = tmp_path / "project"
    project.mkdir()
    client.app.state.workspaces.update("ws_default", root_path=str(project))
    return client


def _project(client: TestClient) -> Path:
    return Path(client.app.state.workspaces.get("ws_default").root_path)


def test_resolves_a_workspace_file(client: TestClient) -> None:
    target = _project(client) / "data" / "run.h5"
    target.parent.mkdir()
    target.write_bytes(b"\x89HDF\r\n\x1a\n")

    response = client.get(META, params={"file": "data/run.h5"})

    assert response.status_code == 200
    assert response.json() == {"resolved": str(target.resolve())}


def test_refuses_a_path_outside_the_workspace(client: TestClient) -> None:
    (_project(client).parent / "outside.h5").write_bytes(b"\x89HDF\r\n\x1a\n")

    response = client.get(META, params={"file": "../outside.h5"})

    assert response.status_code == 403
    assert response.json()["error"]["error"] == "path_outside_workspace"


def test_refuses_redacted_service_storage(client: TestClient) -> None:
    cache = _project(client) / ".clio-child-cache"
    cache.mkdir()
    (cache / "run.h5").write_bytes(b"\x89HDF\r\n\x1a\n")

    response = client.get(META, params={"file": ".clio-child-cache/run.h5"})

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "sandbox_child_cache"


def test_reports_a_missing_file_and_an_unknown_workspace(client: TestClient) -> None:
    missing = client.get(META, params={"file": "absent.h5"})
    unknown = client.get("/v1/workspaces/ws_nope/h5grove/meta/", params={"file": "x.h5"})

    assert missing.status_code == 404
    assert missing.json()["error"]["error"] == "not_found"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["error"] == "not_found"


def test_refuses_a_file_over_the_policy_size_cap(
    client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    (_project(client) / "big.h5").write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 64)
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", "16")

    response = client.get(META, params={"file": "big.h5"})

    assert response.status_code == 413
    assert response.json()["error"]["error"] == "file_too_large"


def test_mounts_nothing_without_the_data_routes_extra(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # A None entry makes the import raise ImportError, as if hdf5-mcp were absent.
    monkeypatch.setitem(sys.modules, "hdf5_mcp.provider.routes", None)
    app = build_app(sessions_path=tmp_path / "s.json")

    assert not any("/h5grove" in getattr(route, "path", "") for route in app.routes)
