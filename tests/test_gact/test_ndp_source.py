"""Real HTTP catalog/byte integration and credential-routing boundaries for NDP."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.storage_ndp import register_storage_ndp_routes
from clio_agent.gact.storage.linked import linked_file
from clio_agent.gact.storage.models import CreateSource, SourceRecord
from clio_agent.gact.storage.ndp_catalog import NDPCatalog, NDPDataset, NDPEndpoint, NDPResource
from clio_agent.gact.storage.ndp_source import NDPSource, credential_target, resource_route
from clio_agent.gact.storage.service import StorageService


@pytest.fixture
def catalog_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    """A protocol fixture speaks to the real clio-kit NDPClient over TCP."""
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            requests.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    [
                        {
                            "id": "weather",
                            "name": "weather",
                            "title": "Sea observations",
                            "notes": "A catalog fixture",
                            "resources": [
                                {
                                    "id": "stations",
                                    "name": "stations.csv",
                                    "format": "CSV",
                                    "size": 10,
                                    "url": base + "/stations.csv",
                                }
                            ],
                        }
                    ]
                ).encode()
            )

        def do_GET(self) -> None:
            requests.append(self.path)
            assert not self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            self.wfile.write(b"name,x\na,1")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "CLIO_STORAGE_NDP_ENDPOINTS",
        json.dumps([{"id": "weather", "name": "Weather Endpoint", "url": base}]),
    )
    yield base, requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def test_search_attach_restart_then_read_real_bytes(
    tmp_path: Path, catalog_http: tuple[str, list[str]]
) -> None:
    """The actual catalog client, API, ledger and linked-file custody work together."""
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()

    async def run(action: Any) -> Any:
        return await asyncio.to_thread(action)

    def visible(record: SourceRecord) -> dict[str, Any]:
        return record.source.model_dump(mode="json")

    register_storage_ndp_routes(app, service, run, lambda wid: workspace, visible)
    with TestClient(app) as client:
        response = client.post("/v1/storage/ndp/search", json={"query": "sea"})
        assert response.status_code == 200, response.text
        dataset = response.json()["datasets"][0]
        assert dataset["title"] == "Sea observations"
        assert dataset["access"] is None
        assert "url" not in dataset["resources"][0]
        response = client.post(
            "/v1/workspaces/work/sources/ndp",
            json={"endpoint_id": "weather", "dataset_id": "weather", "resource_ids": ["stations"]},
        )
        assert response.status_code == 201, response.text
        source = response.json()
        repeated = client.post(
            "/v1/workspaces/work/sources/ndp",
            json={"endpoint_id": "weather", "dataset_id": "weather", "resource_ids": ["stations"]},
        )
        assert repeated.json()["id"] == source["id"]
        assert source["provider"] == "ndp"
        assert source["capabilities"]["supported_modes"] == ["read_only"]
        assert catalog_http[1] == ["/search"]
        assert not list((tmp_path / "sources").rglob("*.csv"))
        service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
        record = service.get("work", source["id"])
        path = linked_file(service, record, "1-stations.csv")
        assert path.read_bytes() == b"name,x\na,1"
        assert catalog_http[1] == ["/search", "/stations.csv"]
        assert linked_file(service, record, "1-stations.csv") == path
        assert catalog_http[1] == ["/search", "/stations.csv"]
        from clio_agent.gact.storage.linked import link_folder

        previous = record.linked_manifest_id
        link_folder(service, record)
        assert record.linked_manifest_id != previous
        assert catalog_http[1] == ["/search", "/stations.csv", "/search"]
        assert linked_file(service, record, "1-stations.csv").read_bytes() == b"name,x\na,1"
        assert catalog_http[1][-1] == "/stations.csv"


def test_selection_rejects_injected_resource_and_write_mode(
    tmp_path: Path, catalog_http: tuple[str, list[str]]
) -> None:
    catalog = NDPCatalog()
    catalog.search("sea")
    with pytest.raises(ValueError, match="Choose resources"):
        catalog.selected("weather", "weather", ["http://unapproved.test/private"])
    with pytest.raises(ValueError, match="again"):
        catalog.selected("other", "weather", ["stations"])
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    with pytest.raises(ValueError):
        service.create(
            "work",
            CreateSource(provider="ndp", label="Denied", root="dataset", mode="write_enabled"),
        )


def test_expired_account_does_not_block_public_catalog(
    tmp_path: Path, catalog_http: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable refresh service must not turn public discovery into an auth error."""
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    monkeypatch.setattr(service.auth, "connected", lambda record: True)

    def unavailable(record: SourceRecord) -> str:
        raise ValueError("Authorization was rejected")

    monkeypatch.setattr(service.auth, "token", unavailable)
    app = FastAPI()

    async def run(action: Any) -> Any:
        return await asyncio.to_thread(action)

    register_storage_ndp_routes(app, service, run, lambda wid: tmp_path, lambda record: {})
    with TestClient(app) as client:
        response = client.post("/v1/storage/ndp/search", json={"query": "sea"})
    assert response.status_code == 200
    assert response.json()["datasets"][0]["id"] == "weather"
    assert response.json()["datasets"][0]["access"] is None
    assert "could not be refreshed" in response.json()["warnings"][0]


def dataset(url: str) -> NDPDataset:
    """An explicitly approved protected Endpoint and one selected object."""
    return NDPDataset(
        id="d",
        name="d",
        title="Dataset",
        endpoint=NDPEndpoint(
            id="ep", name="Endpoint", url="https://endpoint.test/api", authenticated=True
        ),
        resources=[NDPResource(id="r", path="data.csv", name="data.csv", url=url)],
    )


@pytest.mark.parametrize(
    ("url", "path", "params"),
    [
        (
            "pelican://osg-htc.org/science/a.csv",
            "/api/pelican/download",
            {"path": "/science/a.csv", "federation": "osdf", "stream": "true"},
        ),
        (
            "pelican://path-cc.io/science/a.csv",
            "/api/pelican/download",
            {"path": "/science/a.csv", "federation": "path-cc", "stream": "true"},
        ),
        ("s3://bucket/science/a.csv", "/api/s3/objects/bucket/science/a.csv", {}),
    ],
)
def test_documented_read_routes(url: str, path: str, params: dict[str, str]) -> None:
    selected = dataset(url)
    target, query = resource_route(selected.endpoint, selected.resources[0])
    assert target == "https://endpoint.test" + path
    assert query == params


def test_authenticated_redirect_does_not_leak_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []
    original = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "endpoint.test":
            assert request.headers["Authorization"] == "Bearer private-access"
            return httpx.Response(302, headers={"Location": "https://data.test/bytes"})
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"science")

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    source = NDPSource(dataset("pelican://osg-htc.org/science/a.csv"), lambda: "private-access")
    with source.open_read(source.entries()[0]) as reader:
        assert reader.read() == b"science"
    assert len(seen) == 2
    assert not credential_target(source.dataset.endpoint, "https://endpoint.test/api-other/private")
    assert not credential_target(source.dataset.endpoint, "https://endpoint.test.evil/api/private")
    with pytest.raises(PermissionError, match="read-only"):
        source.apply("data.csv", None, None)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_role_outage_and_read_denial_are_explicit(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    original = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"detail": "Role query failed"})
            ),
            **kwargs,
        ),
    )
    selected = dataset("s3://bucket/data.csv")
    access, warning = NDPCatalog.access(selected.endpoint, "private")
    assert access == ("request_access" if status == 403 else None)
    if status != 403:
        assert warning
    source = NDPSource(selected, lambda: "private")
    with pytest.raises(PermissionError if status in {401, 403} else ValueError):
        source.open_read(source.entries()[0])


@pytest.mark.parametrize(
    "url",
    [
        "file:///secret",
        "pelican://unknown.test/file",
        "s3://bucket/../secret",
        "https://user:secret@data.test/file",
    ],
)
def test_invalid_or_unknown_transports_do_not_get_credentials(url: str) -> None:
    selected = dataset(url)
    with pytest.raises(ValueError):
        resource_route(selected.endpoint, selected.resources[0])
