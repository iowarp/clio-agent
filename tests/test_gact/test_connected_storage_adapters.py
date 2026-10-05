"""Provider wire behavior without credentials or external network dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import globus_sdk
import httpx
import pytest
from clio_schemas.connected_resources import ConnectedSource, ResourceOwner

from clio_agent.gact.storage.drive import DriveSource
from clio_agent.gact.storage.globus import GlobusSource
from clio_agent.gact.storage.models import SourceConfiguration, SourceRecord, TransferOperation
from clio_agent.gact.storage.store import SourceStore


def test_drive_exports_keep_original_identity_and_cannot_write_back(tmp_path: Path) -> None:
    calls = []

    def request(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        assert req.headers["Authorization"] == "Bearer private-test-token"
        if req.url.path.endswith("/files/folder"):
            return httpx.Response(
                200, json={"id": "folder", "mimeType": "application/vnd.google-apps.folder"}
            )
        if req.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "doc",
                            "name": "Study",
                            "mimeType": "application/vnd.google-apps.document",
                            "version": "7",
                        }
                    ]
                },
            )
        assert req.url.path.endswith("/files/doc/export")
        assert req.url.params["mimeType"] == "application/pdf"
        return httpx.Response(200, content=b"%PDF-1.7\nfixture")

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        adapter = DriveSource("folder", "private-test-token", writable=True, client=client)
        entry = adapter.entries()[0]
        assert (entry.path, entry.locator, entry.revision) == ("Study.pdf", "doc", "7")
        with adapter.open_read(entry) as stream:
            assert stream.read() == b"%PDF-1.7\nfixture"
        proposed = tmp_path / "edited.pdf"
        proposed.write_bytes(b"changed")
        with pytest.raises(ValueError, match="exports are read-only"):
            adapter.apply(entry.path, proposed, "7")
    assert all(req.method == "GET" for req in calls)


def test_drive_upload_destination_never_receives_token_on_untrusted_origin(tmp_path: Path) -> None:
    calls = []

    def request(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/files/root"):
            return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.folder"})
        if req.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, headers={"Location": "https://attacker.invalid/upload"})

    proposed = tmp_path / "new.txt"
    proposed.write_text("approved")
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        adapter = DriveSource("root", "test-token", writable=True, client=client)
        with pytest.raises(ValueError, match="invalid upload destination"):
            adapter.apply("new.txt", proposed, None)
    assert all(req.url.host == "www.googleapis.com" for req in calls)


class GlobusFixture:
    """A test transport preserving the real SDK's transfer payload and native task semantics."""

    def __init__(self, mapped_root: Path) -> None:
        self.mapped_root = mapped_root
        self.requests: list[dict[str, Any]] = []
        self.fail_first = True
        self.status = "ACTIVE"
        self.cancelled: list[str] = []

    def operation_ls(self, endpoint: str, *, path: str) -> list[dict[str, str]]:
        assert endpoint == "22222222-2222-4222-8222-222222222222"
        local = self.mapped_root / path.removeprefix("/mapped/")
        return [{"name": item.name, "type": "file"} for item in local.iterdir()]

    def get_submission_id(self) -> dict[str, str]:
        return {"value": "unique-submission"}

    def submit_transfer(self, body: dict[str, Any]) -> dict[str, str]:
        self.requests.append(body)
        if self.fail_first:
            self.fail_first = False
            raise TimeoutError("Response lost after submission")
        return {"task_id": "native-task"}

    def get_task(self, identifier: str) -> dict[str, Any]:
        assert identifier == "native-task"
        return {"status": self.status, "bytes_transferred": 6}

    def cancel_task(self, identifier: str) -> None:
        self.cancelled.append(identifier)


def test_globus_lost_submission_and_restart_reuse_the_native_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(globus_sdk.LocalGlobusConnectPersonal, "endpoint_id", None)
    store = SourceStore(tmp_path / "source-data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = SourceRecord(
        source=ConnectedSource(
            id="source_globus",
            provider="globus",
            label="Collection",
            root="/opal",
            owner=ResourceOwner(clio_id=store.clio_id, host_id="local"),
            workspace_id="w",
            capabilities=GlobusSource.capabilities,
        ),
        principal="test-owner",
        configuration=SourceConfiguration(
            collection_id="11111111-1111-4111-8111-111111111111",
            destination_collection_id="22222222-2222-4222-8222-222222222222",
            destination_collection_root="/mapped",
            destination_local_root=str(tmp_path),
        ),
    )
    store.put("source", record.source.id, record)
    operation = store.begin_operation(record.source.id, "materialize")
    client = GlobusFixture(tmp_path)
    adapter = GlobusSource(record, "private-test-token", client=client)
    with pytest.raises(TimeoutError):
        adapter.submit(store, operation)
    persisted = store.get("operation", operation.id, TransferOperation)
    assert persisted.native_request["submission_id"] == "unique-submission"
    assert persisted.native_job_id is None
    restarted = SourceStore(store.root)
    restarted.recover()
    persisted = restarted.get("operation", operation.id, TransferOperation)
    assert persisted.state == "interrupted"
    operation = adapter.submit(restarted, persisted)
    assert client.requests[0] == client.requests[1]
    assert operation.native_job_id == "native-task"
    assert adapter.poll(restarted, operation, workspace).state == "running"
    # Native success alone is insufficient: the declared mapping must expose
    # actual bytes on this CLIO, and those bytes enter the normal baseline owner.
    stage = store.root / record.source.id / ("native-" + operation.id)
    stage.mkdir()
    (stage / "a.txt").write_text("inputs")
    client.status = "SUCCEEDED"
    assert adapter.poll(restarted, operation, workspace).state == "completed"
    saved = restarted.get("source", record.source.id, SourceRecord)
    assert Path(saved.source.local_path).joinpath("a.txt").read_text() == "inputs"
    assert not stage.exists()
    assert len(client.requests) == 2
