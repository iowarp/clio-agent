"""Count HTTP requests made by the real fsspec GitHub implementation."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.routes.connected_storage import register_connected_storage_routes
from clio_agent.gact.storage import github_access
from clio_agent.gact.storage.github_access import GitHubAccess
from clio_agent.gact.storage.linked import link_folder, linked_file
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.service import StorageService


def response(url: str, body: dict[str, Any], status: int = 200) -> requests.Response:
    """Return a real requests response for fsspec's normal decoding and error paths."""
    result = requests.Response()
    result.url, result.status_code = url, status
    result._content = json.dumps(body).encode()
    return result


@pytest.fixture
def github_http(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], dict[str, bytes]]:
    """Intercept only the HTTP boundary; exercise actual library caches and traversal."""
    calls: list[str] = []
    files = {"a.txt": b"approved bytes", "nested/b.txt": b"nested bytes"}

    def entry(path: str) -> dict[str, Any]:
        data = files[path]
        return {
            "path": path.rsplit("/", 1)[-1],
            "type": "blob",
            "mode": "100644",
            "size": len(data),
            "sha": hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest(),
        }

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(url)
        if url.endswith("/repos/test/data"):
            body = {"default_branch": "main"}
        elif url.endswith("/git/trees/main"):
            body = {
                "tree": [
                    entry("a.txt"),
                    {"path": "nested", "type": "tree", "mode": "040000", "sha": "nested-tree"},
                ]
            }
        elif url.endswith("/git/trees/nested-tree"):
            body = {"tree": [entry("nested/b.txt")]}
        elif "/contents/" in url:
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            body = {"content": base64.b64encode(files[path]).decode()}
        else:
            pytest.fail(f"Unexpected HTTP call: {url}")
        return response(url, body)

    monkeypatch.setattr("fsspec.implementations.github.requests.get", get)
    return calls, files


def test_browse_navigates_without_repeated_repository_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github_http: tuple[list[str], dict[str, bytes]]
) -> None:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = FastAPI()
    app.state.workspaces = {"w": SimpleNamespace(root_path=str(workspace))}
    register_connected_storage_routes(app)
    calls, _ = github_http
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces/w/sources",
            json={
                "provider": "github",
                "root": "https://github.com/test/data",
                "label": "Public data",
            },
        )
        assert created.status_code == 201, created.text
        prefix = f"/v1/workspaces/w/sources/{created.json()['id']}"
        assert not calls
        for _ in range(3):
            result = client.post(prefix + "/browse", json={})
            assert result.status_code == 200, result.text
            assert len(result.json()["entries"]) == 2
        assert len(calls) == 2  # Default branch and root tree, once each.
        assert not any("nested-tree" in url for url in calls)
        for _ in range(3):
            result = client.post(prefix + "/browse", json={"folder": "nested"})
            assert result.json()["entries"][0]["path"] == "nested/b.txt"
        assert len(calls) == 3
        for query in ("b", "b.txt", "txt"):
            assert client.post(prefix + "/browse", json={"query": query}).status_code == 200
        from tests.test_gact.storage_operation_helpers import complete_indexing

        complete_indexing(client, prefix, client.post(prefix + "/link"))
        assert len(calls) == 3  # Searching and linking reuse the already-read metadata.


def test_linked_file_fetches_only_itself_and_refresh_adopts_changes(
    tmp_path: Path, github_http: tuple[list[str], dict[str, bytes]]
) -> None:
    calls, files = github_http
    service = StorageService(tmp_path / "sources", tmp_path / "auth.json")
    record = service.create(
        "w",
        CreateSource(provider="github", root="https://github.com/test/data", label="Public data"),
    )
    linked = link_folder(service, record)
    assert len(calls) == 3
    assert linked_file(service, linked, "a.txt").read_bytes() == b"approved bytes"
    assert len(calls) == 4  # One content request, no repository scan.
    files["a.txt"] = b"upstream changed"
    assert linked_file(service, linked, "a.txt").read_bytes() == b"approved bytes"
    assert len(calls) == 4  # The approved cached bytes need no network request.
    refreshed = link_folder(service, linked)
    assert len(calls) == 6  # Explicit refresh rereads both trees, not repository metadata.
    assert linked_file(service, refreshed, "a.txt").read_bytes() == b"upstream changed"
    assert len(calls) == 7


def test_restart_read_does_not_scan_unrelated_directories(
    tmp_path: Path, github_http: tuple[list[str], dict[str, bytes]]
) -> None:
    calls, _ = github_http
    root, auth = tmp_path / "sources", tmp_path / "auth.json"
    service = StorageService(root, auth)
    record = link_folder(
        service,
        service.create(
            "w",
            CreateSource(
                provider="github", root="https://github.com/test/data", label="Public data"
            ),
        ),
    )
    calls.clear()
    restarted = StorageService(root, auth)
    assert linked_file(restarted, record, "a.txt").read_bytes() == b"approved bytes"
    assert len(calls) == 3
    assert not any("nested-tree" in url for url in calls)


def test_cached_listing_does_not_approve_changed_file_bytes(
    tmp_path: Path, github_http: tuple[list[str], dict[str, bytes]]
) -> None:
    _, files = github_http
    service = StorageService(tmp_path / "sources", tmp_path / "auth.json")
    record = link_folder(
        service,
        service.create(
            "w",
            CreateSource(
                provider="github", root="https://github.com/test/data", label="Public data"
            ),
        ),
    )
    files["a.txt"] = b"x" * len(files["a.txt"])
    with pytest.raises(ValueError, match="GitHub file changed"):
        linked_file(service, record, "a.txt")
    assert not (
        service.store.root / record.source.id / str(record.linked_manifest_id) / "a.txt"
    ).exists()


@pytest.mark.parametrize(
    "headers,status,delay",
    [
        ({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4600"}, 403, 3600),
        ({"retry-after": "120"}, 429, 120),
        ({}, 403, 60),
    ],
)
def test_rate_limit_pauses_all_repositories_until_allowed(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], status: int, delay: int
) -> None:
    now = [1000.0]
    monkeypatch.setattr(github_access.time, "time", lambda: now[0])
    calls: list[str] = []

    def get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(url)
        result = response(url, {"message": "API rate limit exceeded"}, status)
        result.headers.update(headers)
        return result

    monkeypatch.setattr("fsspec.implementations.github.requests.get", get)
    access = GitHubAccess()
    with pytest.raises(ValueError, match="GitHub is temporarily limiting requests"):
        with access.filesystem("test", "data", ""):
            pytest.fail("A rate-limited filesystem cannot be yielded")
    now[0] += delay - 1
    with pytest.raises(ValueError, match="Try again"):
        with access.filesystem("other", "repo", ""):
            pytest.fail("Other repositories must respect the same limit")
    assert len(calls) == 1
    now[0] += 2
    with pytest.raises(ValueError, match="Try again"):
        with access.filesystem("test", "data", ""):
            pytest.fail("The next rate-limited response still stops the request")
    assert len(calls) == 2
