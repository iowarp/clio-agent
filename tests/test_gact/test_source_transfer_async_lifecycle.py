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
            yield client, app, f"/v1/workspaces/{wid}/sources/{source_id}", adapter, payload
        finally:
            adapter.release.set()


def _operation(client: TestClient, prefix: str, identifier: str) -> dict[str, Any]:
    rows = client.get(prefix + "/operations").json()["operations"]
    return next(row for row in rows if row["id"] == identifier)


def _wait_terminal(client: TestClient, prefix: str, identifier: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = _operation(client, prefix, identifier)
        if row["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return row
        time.sleep(0.01)
    pytest.fail("the transfer never settled")


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
    client, app, prefix, adapter, payload = transfer_setup
    response = client.post(prefix + "/transfer", json={"access": "read_only"})
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
    finished = _wait_terminal(client, prefix, identifier)
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
    client, app, prefix, adapter, payload = transfer_setup
    response = client.post(prefix + "/transfer", json={"access": "read_only"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert adapter.entered.wait(timeout=5), str(_operation(client, prefix, identifier))
    cancellation = client.post(prefix + f"/operations/{identifier}/cancel")
    assert cancellation.status_code == 200, cancellation.text
    pending = _operation(client, prefix, identifier)
    assert pending["cancel_requested"] is True
    assert pending["state"] == "running"

    with _pause_cleanup(monkeypatch, identifier) as cleanup:
        adapter.release.set()
        assert cleanup.wait(timeout=5), str(_operation(client, prefix, identifier))
        pending = _operation(client, prefix, identifier)
        assert pending["state"] == "running"
        assert pending["cancel_requested"] is True
        assert client.get("/v1/health").status_code == 200
    settled = _wait_terminal(client, prefix, identifier)
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
