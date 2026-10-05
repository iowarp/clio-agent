"""GitHub working copies reuse real fsspec reads and the shared review/apply engine."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.linked import link_folder, linked_adapter
from clio_agent.gact.storage.linked_changes import publish_edits, review_edits
from clio_agent.gact.storage.models import CreateSource, TransferOperation
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.source_filesystem import SourceFileSystem


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


@pytest.fixture
def repository_http(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    files = {"data/edit.txt": b"before", "data/delete.txt": b"delete me"}
    writes: list[dict[str, Any]] = []

    def get(url: str, **kwargs: Any) -> requests.Response:
        assert kwargs["auth"] == ("x-access-token", "test-user-token")
        response = requests.Response()
        response.url, response.status_code = url, 200
        if url.endswith("/repos/example/private"):
            body: Any = {"default_branch": "main"}
        elif "/git/trees/" in url:
            if url.endswith("/main"):
                body = {
                    "tree": [{"path": "data", "type": "tree", "mode": "040000", "sha": "data-tree"}]
                }
            else:
                body = {
                    "tree": [
                        {
                            "path": name.removeprefix("data/"),
                            "type": "blob",
                            "mode": "100644",
                            "size": len(data),
                            "sha": blob(data),
                        }
                        for name, data in files.items()
                    ]
                }
        else:
            path = url.split("/contents/")[1].split("?")[0]
            body = {"content": base64.b64encode(files[path]).decode()}
        response._content = json.dumps(body).encode()
        return response

    blobs: dict[str, bytes] = {}
    pending_tree: list[dict[str, Any]] = []

    def send(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-user-token"
        path = request.url.path
        payload = json.loads(request.content) if request.content else {}
        assert "committer" not in payload and "author" not in payload
        if "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "head"}})
        if request.method == "GET" and "/git/commits/" in path:
            return httpx.Response(200, json={"tree": {"sha": "tree"}})
        if request.method == "GET" and "/git/trees/" in path:
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": name, "sha": blob(data), "mode": "100644"}
                        for name, data in files.items()
                    ]
                },
            )
        if path.endswith("/git/blobs"):
            data = base64.b64decode(payload["content"])
            sha = blob(data)
            blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if path.endswith("/git/trees"):
            pending_tree[:] = payload["tree"]
            return httpx.Response(201, json={"sha": "new-tree"})
        if path.endswith("/git/commits"):
            assert payload["parents"] == ["head"]
            writes.append(payload)
            return httpx.Response(201, json={"sha": "new-commit"})
        if request.method == "PATCH" and "/git/refs/heads/" in path:
            assert payload == {"sha": "new-commit", "force": False}
            for row in pending_tree:
                if row["sha"] is None:
                    files.pop(row["path"])
                else:
                    files[row["path"]] = blobs[row["sha"]]
            return httpx.Response(200, json={"object": {"sha": "new-commit"}})
        raise AssertionError(str(request.url))

    monkeypatch.setattr("fsspec.implementations.github.requests.get", get)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(send), **kwargs)
    )
    return files, writes


def setup_source(tmp_path: Path, mode: str = "working_copy") -> tuple[StorageService, Any, Path]:
    service = StorageService(tmp_path / "store", tmp_path / "auth.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = service.create(
        "ws",
        CreateSource.model_validate(
            {
                "provider": "github",
                "root": "https://github.com/example/private/tree/main/data",
                "mode": mode,
                "label": "Private repository",
            }
        ),
        workspace,
    )
    from clio_agent.gact.storage.accounts import source_binding

    service.auth._vault.write_entries(
        {
            "account": {
                **source_binding(source),
                "kind": "account",
                "tokens": {
                    "access_token": "test-user-token",
                    "non_expiring": True,
                },
            }
        }
    )
    return service, source, workspace


def test_linked_local_edits_publish_as_one_user_attributed_commit(
    tmp_path: Path, repository_http: Any
) -> None:
    files, writes = repository_http
    service, source, _ = setup_source(tmp_path)
    assert not service.download_available(source)
    source = link_folder(service, source)
    fs = SourceFileSystem(
        service,
        "ws",
        source.source.id,
        ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024),
    )
    fs.pipe_file("edit.txt", b"after")
    fs.pipe_file("add.txt", b"new")
    fs.rm_file("delete.txt")
    assert files == {"data/edit.txt": b"before", "data/delete.txt": b"delete me"}
    assert writes == []
    source = service.get("ws", source.source.id)
    review = review_edits(service, source)
    assert {row.kind for row in review.changes} == {"add", "modify", "delete"}
    assert not any(row.conflict for row in review.changes)
    operation_id = publish_edits(service, source, review.id, [row.path for row in review.changes])
    assert files == {"data/edit.txt": b"after", "data/add.txt": b"new"}
    assert len(writes) == 1
    assert service.store.get("operation", operation_id, TransferOperation).state == "completed"


def test_conditional_write_rejects_upstream_change_and_readonly_mode(
    tmp_path: Path, repository_http: Any
) -> None:
    files, writes = repository_http
    service, source, _ = setup_source(tmp_path)
    changed = tmp_path / "updated.txt"
    changed.write_bytes(b"proposed")
    with linked_adapter(service, source) as adapter:
        expected = next(row.revision for row in adapter.entries() if row.path == "edit.txt")
        files["data/edit.txt"] = b"someone else's edit"
        with pytest.raises(ValueError, match="changed"):
            adapter.apply("edit.txt", changed, expected)
    source.source = source.source.model_copy(update={"mode": "read_only"})
    with linked_adapter(service, source) as adapter:
        with pytest.raises(PermissionError, match="does not allow"):
            adapter.apply("edit.txt", changed, blob(files["data/edit.txt"]))
    assert writes == []
    assert files["data/edit.txt"] == b"someone else's edit"
