"""Actual transfer bytes/progress with a deliberately paused test-only reader."""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.storage import transfers
from clio_agent.gact.storage.adapters import LocalSource, SourceAdapter
from clio_agent.gact.storage.models import FileEntry, SourceRecord
from clio_agent.platform_paths import win_extended_path
from tests.test_gact.test_source_connect_runtime import _collect
from tests.test_gact.test_source_connect_runtime import bound as storage_runtime_fixture

bound = storage_runtime_fixture


class _PausedReader(io.BufferedReader):
    """Read actual file bytes, pausing the second chunk for deterministic observation."""

    def __init__(self, path: Path, entered: threading.Event, release: threading.Event) -> None:
        super().__init__(io.FileIO(win_extended_path(path), "r"))
        self.entered, self.release = entered, release
        self.reads = 0

    def read(self, size: int | None = -1, /) -> bytes:
        """Make one real chunk visible before pausing the next read."""
        if self.reads == 1:
            self.entered.set()
            assert self.release.wait(timeout=10), "the paused transfer reader was never released"
        self.reads += 1
        return super().read(size)


class _PausedLocalSource(LocalSource):
    """Retain production listing, hashing and custody behavior; gate only test reads."""

    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.entered = threading.Event()
        self.release = threading.Event()

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Return the real file reader with the controlled pause."""
        return _PausedReader(self.root / entry.path, self.entered, self.release)


@pytest.fixture
def transfer_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, ...]]:
    """Build the actual API/storage owner with isolated files and no provider credentials."""
    # Make the staging path exceed Windows MAX_PATH even in short CI checkouts.
    agent_home = tmp_path / ("agent-" + "h" * max(0, 170 - len(str(tmp_path))))
    monkeypatch.setenv("CLIO_AGENT_HOME", str(agent_home))
    upstream, workspace = tmp_path / "upstream", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    payload = b"x" * (2 * 1024 * 1024 + 17)
    (upstream / "payload.bin").write_bytes(payload)
    adapter = _PausedLocalSource(upstream)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces", json={"name": "Transfer fixture", "root_path": str(workspace)}
        )
        assert created.status_code == 201, created.text
        wid = created.json()["id"]
        owner = client.post("/v1/sessions", json={"workspace_id": wid, "title": "Transfer owner"})
        assert owner.status_code == 200, owner.text
        sid = owner.json()["id"]
        source = client.post(
            f"/v1/workspaces/{wid}/sources",
            json={"provider": "local", "root": str(upstream), "label": "Transfer fixture"},
        )
        assert source.status_code == 201, source.text
        source_id = source.json()["id"]

        @contextmanager
        def source_adapter(record: SourceRecord) -> Iterator[SourceAdapter]:
            assert record.source.id == source_id
            yield adapter

        monkeypatch.setattr(app.state.connected_storage, "adapter", source_adapter)
        try:
            yield client, app, f"/v1/workspaces/{wid}/sources/{source_id}", adapter, payload, sid
        finally:
            adapter.release.set()


def _operation(client: TestClient, prefix: str, identifier: str) -> dict[str, Any]:
    rows = client.get(prefix + "/operations").json()["operations"]
    return next(row for row in rows if row["id"] == identifier)


def _wait_cancel_request(client: TestClient, prefix: str, identifier: str) -> dict[str, Any]:
    """Observe deferred intent confirmation while the actual worker is still held."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = _operation(client, prefix, identifier)
        if row["cancel_requested"]:
            assert row["state"] == "cancellation_requested", row
            return row
        time.sleep(0.01)
    pytest.fail("the cancellation request was not durably recorded")


@contextmanager
def _pause_cleanup(monkeypatch: pytest.MonkeyPatch, identifier: str) -> Iterator[threading.Event]:
    """Gate the real staging cleanup so terminal publication has a deterministic boundary."""
    entered, release = threading.Event(), threading.Event()
    remove = transfers.remove_owned_tree

    def paused_remove(path: Path, owner_root: Path) -> None:
        if path.name == "stage-" + identifier:
            entered.set()
            assert release.wait(timeout=10), "the paused cleanup was never released"
        remove(path, owner_root)

    with monkeypatch.context() as patch:
        patch.setattr(transfers, "remove_owned_tree", paused_remove)
        try:
            yield entered
        finally:
            release.set()


def test_download_ack_progress_and_result_do_not_block_control_plane(
    transfer_setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real 202/progress/result path stays responsive while file I/O is paused."""
    client, app, prefix, adapter, payload, sid = transfer_setup
    response = client.post(prefix + "/transfer", json={"access": "read_only", "session_id": sid})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    source_id = prefix.rsplit("/", 1)[-1]
    staging = app.state.connected_storage.store.root / source_id / ("stage-" + identifier)
    assert len(str(staging)) > 260
    assert adapter.entered.wait(timeout=5), str(_operation(client, prefix, identifier))

    assert client.get("/v1/health").status_code == 200
    live = _operation(client, prefix, identifier)
    assert live["state"] == "running"
    assert live["bytes_done"] == 1024 * 1024
    assert live["bytes_total"] == len(payload)

    with _pause_cleanup(monkeypatch, identifier) as cleanup:
        adapter.release.set()
        assert cleanup.wait(timeout=5), str(_operation(client, prefix, identifier))
        assert _operation(client, prefix, identifier)["state"] == "running"
        assert client.get("/v1/health").status_code == 200
    # Join the native owner and confirm its stored result before checking the
    # HTTP projection, as the abandoned-attachment case below does already.
    outcome, _reply = _collect(app, response.json()["acceptance"], session_id=sid, kind="Download")
    assert outcome["status"] == "completed", outcome
    finished = _operation(client, prefix, identifier)
    assert finished["state"] == "completed", finished
    assert finished["bytes_done"] == finished["bytes_total"] == len(payload)
    record = app.state.connected_storage.store.get("source", source_id, SourceRecord)
    assert record.source.local_path is not None
    copied = Path(record.source.local_path) / "payload.bin"
    with open(win_extended_path(copied), "rb") as reader:
        assert hashlib.sha256(reader.read()).digest() == hashlib.sha256(payload).digest()
    assert (adapter.root / "payload.bin").read_bytes() == payload


def test_cancel_is_requested_then_settled_without_publishing_partial_data(
    transfer_setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An acknowledged cancellation becomes terminal only when the transfer owner settles."""
    client, app, prefix, adapter, payload, sid = transfer_setup
    response = client.post(prefix + "/transfer", json={"access": "read_only", "session_id": sid})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert adapter.entered.wait(timeout=5), str(_operation(client, prefix, identifier))
    cancellation = client.post(prefix + f"/operations/{identifier}/cancel")
    assert cancellation.status_code == 200, cancellation.text
    assert cancellation.json()["cancel_requested"] is True
    pending = _wait_cancel_request(client, prefix, identifier)
    assert pending["cancel_requested"] is True
    assert pending["state"] == "cancellation_requested"

    with _pause_cleanup(monkeypatch, identifier) as cleanup:
        adapter.release.set()
        assert cleanup.wait(timeout=5), str(_operation(client, prefix, identifier))
        pending = _operation(client, prefix, identifier)
        assert pending["state"] == "cancellation_requested"
        assert pending["cancel_requested"] is True
        assert client.get("/v1/health").status_code == 200
    outcome, _reply = _collect(app, response.json()["acceptance"], session_id=sid, kind="Download")
    assert outcome["status"] == "cancelled", outcome
    settled = _operation(client, prefix, identifier)
    assert settled["state"] == "cancelled", settled
    assert settled["bytes_done"] == 1024 * 1024
    source_id = prefix.rsplit("/", 1)[-1]
    record = app.state.connected_storage.store.get("source", source_id, SourceRecord)
    assert record.manifest_id is None
    assert record.source.local_path is None
    assert (adapter.root / "payload.bin").read_bytes() == payload
    owner_root = app.state.connected_storage.store.root / source_id
    with os.scandir(win_extended_path(owner_root)) as entries:
        assert not any(entry.name.startswith("stage-") for entry in entries)


def test_native_download_settles_abandoned_attachment_cleanup(bound: Any, monkeypatch: Any) -> None:
    """Real native cleanup finishes before publishing the terminal task observation."""
    import asyncio
    import threading

    from clio_agent.gact.storage import download_tool
    from clio_agent.gact.storage.drafts import SourceDraft, SourceDrafts
    from clio_agent.gact.storage.models import TransferOperation
    from tests.test_gact.test_source_connect_runtime import _body, _connect

    service, app, upstream = bound
    first = _connect(app, "local", str(upstream), "Draft source", configuration={})
    outcome, reply = _collect(app, first)
    assert outcome["status"] == "completed"
    source_id = _body(reply)["source_id"]
    record = service.get("ws_default", source_id)
    drafts = SourceDrafts(service, app.state.resource_store)
    app.state.source_drafts = drafts
    lease = drafts.begin(record)
    entered, release = threading.Event(), threading.Event()
    original = service.execute_transfer

    async def held(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.to_thread(release.wait)
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "execute_transfer", held)
    tool = app.state.builtin_boundary.bind(download_tool.build_connected_data_download_tool())
    receipt = asyncio.run(tool.acall(source_id=source_id, paths=["measurements.csv"]))
    try:
        assert entered.wait(5)
        drafts.finish(source_id, lease)
        [operation] = [
            row
            for row in service.store.list("operation", TransferOperation)
            if row.id == receipt["handle"]
        ]
        assert operation.cancel_requested
    finally:
        release.set()
    outcome, reply = _collect(app, receipt, kind="Download")
    assert outcome["status"] == "cancelled" and _body(reply)["state"] == "cancelled"
    settled = app.state.task_history.task_snapshot(receipt["handle"])
    assert settled is not None and settled.settlement == "confirmed"
    assert settled.cancellation_requested_at is None  # the draft owner requested cancellation
    assert (upstream / "measurements.csv").read_text() == "x,y\n1,2\n"
    restored = service.get("ws_default", source_id)
    assert restored.manifest_id == record.manifest_id
    assert restored.source.local_path == record.source.local_path
    assert not (Path(record.workspace_root) / "connected-data" / source_id).exists()
    [draft] = service.store.list("source-draft", SourceDraft)
    assert draft.state == "removed" and not draft.leases
