"""Additional collection consent stays source-bound and uses the existing sign-in UI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse

import globus_sdk
import httpx
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.globus_consent import TRANSFER_SCOPE, collection_scopes
from clio_agent.gact.storage.models import SourceRecord

COLLECTION = "6c54cade-bde5-45c1-bdea-f4bd71dba2cc"
DEPENDENCY = f"https://auth.globus.org/scopes/{COLLECTION}/data_access"
REQUIRED = f"{TRANSFER_SCOPE}[*{DEPENDENCY}]"


def test_collection_consent_roundtrip_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real API reports a consent step, retains tokens, and removes both on discard."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    for suffix in ("CLIENT_ID", "REDIRECT_URI", "CLIENT_SECRET"):
        monkeypatch.delenv(f"CLIO_STORAGE_GLOBUS_{suffix}", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    service = app.state.connected_storage
    consented = False

    def listing(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if consented:
            return [{"name": "file1.txt", "type": "file", "size": 4}]
        response = requests.Response()
        response.status_code = 403
        response.request = requests.Request(
            "GET", "https://transfer.api.globus.org/v0.10/operation/endpoint/ls"
        ).prepare()
        response.url = response.request.url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {"code": "ConsentRequired", "required_scopes": [REQUIRED]}
        ).encode()
        raise globus_sdk.TransferAPIError(response)

    monkeypatch.setattr(globus_sdk.TransferClient, "operation_ls", listing)

    def token_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resource_server": "transfer.api.globus.org",
                "access_token": "private-test-token",
                "refresh_token": "private-test-refresh",
            },
        )

    with (
        TestClient(app) as client,
        httpx.Client(transport=httpx.MockTransport(token_response)) as oauth,
    ):
        created = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "globus",
                "label": "Tutorial",
                "root": "/home/share/godata",
                "configuration": {"collection_id": COLLECTION},
            },
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["id"]
        prefix = f"/v1/workspaces/w/sources/{identifier}"
        record = service.store.get("source", identifier, SourceRecord)
        started = client.post(prefix + "/auth/start").json()
        service.auth.complete(record, started["flow_id"], "fixture-code", client=oauth)
        assert service.auth.connected(record)
        result = client.post(prefix + "/browse", json={})
        assert result.status_code == 403, result.text
        assert "Authorize this Globus collection" in result.text
        assert not service.auth.connected(record)
        assert service.auth.token(record) == "private-test-token"
        assert not client.get("/v1/workspaces/w/sources").json()["sources"][0]["authenticated"]
        started = client.post(prefix + "/auth/start").json()
        query = parse_qs(urlparse(started["authorization_url"]).query)
        assert query["scope"] == [REQUIRED + " offline_access"]
        service.auth.complete(record, started["flow_id"], "fixture-code-2", client=oauth)
        consented = True
        assert service.auth.connected(record)
        result = client.post(prefix + "/browse", json={})
        assert result.status_code == 200, result.text
        assert result.json()["entries"][0]["path"] == "file1.txt"
        pending = client.post(prefix + "/auth/start").json()
        assert client.delete(prefix).status_code == 200
        assert not service.auth.connected(record)
        with pytest.raises(PermissionError):
            service.auth.token(record)
        with pytest.raises(ValueError, match="expired"):
            service.auth.complete(record, pending["flow_id"], "fixture-code-3", client=oauth)
        assert workspace.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "openid",
        TRANSFER_SCOPE,
        f"{TRANSFER_SCOPE}[https://auth.globus.org/scopes/another/data_access]",
        f"{TRANSFER_SCOPE}[{DEPENDENCY}[openid]]",
        "broken[[scope]",
        "x" * 4097,
    ],
)
def test_consent_cannot_expand_beyond_selected_collection(invalid: str) -> None:
    record = SourceRecord.model_validate(
        {
            "principal": "owner",
            "source": {
                "id": "s1",
                "provider": "globus",
                "label": "Tutorial",
                "root": "/",
                "capabilities": {},
                "owner": {"clio_id": "c1", "host_id": "local"},
            },
            "configuration": {"collection_id": COLLECTION},
        }
    )
    with pytest.raises(ValueError):
        collection_scopes(record, [invalid])
    assert collection_scopes(record, [REQUIRED, REQUIRED]) == [REQUIRED]


def test_folder_preview_does_not_scan_unopened_private_directories() -> None:
    record = SourceRecord.model_validate(
        {
            "principal": "owner",
            "source": {
                "id": "s1",
                "provider": "globus",
                "label": "Tutorial",
                "root": "/",
                "capabilities": {},
                "owner": {"clio_id": "c1", "host_id": "local"},
            },
            "configuration": {"collection_id": COLLECTION},
        }
    )
    requested: list[str] = []

    def listing(endpoint: str, *, path: str, limit: int, offset: int) -> list[dict[str, Any]]:
        requested.append(path)
        if path == "/home/share/godata":
            return [{"name": "file1.txt", "type": "file", "size": 4}]
        if path == "/":
            return [{"name": "private", "type": "dir"}]
        raise AssertionError("A preview must not enter unopened directories")

    adapter = GlobusSource(record, "test-token", client=SimpleNamespace(operation_ls=listing))
    assert adapter.entries(recursive=False)[0].path == "private"
    assert (
        adapter.entries(folder="home/share/godata", recursive=False)[0].path
        == "home/share/godata/file1.txt"
    )
    assert requested == ["/", "/home/share/godata"]
