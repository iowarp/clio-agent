"""Shared handles over actual filesystem indexing and approved downloads."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agent_tasks import AgentTaskRegistry
from clio_agent.gact.events import EventBus
from clio_agent.gact.mcp_task_store import SessionMetadataTaskStore, app_task_store
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.storage.indexing import start_index
from clio_agent.gact.storage.linked import FsspecFolder, link_folder
from clio_agent.gact.storage.models import CreateSource, FileEntry, Manifest, TransferOperation
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.task_adapter import storage_handle
from clio_agent.gact.task_controls import cancel_selected, query_snapshot
from clio_agent.gact.task_supervisor import task_supervisor


def setup(tmp_path: Path) -> tuple[Any, str, Any, Path, Path]:
    """Use the real durable owners and local provider with isolated source bytes."""
    source, workspace = tmp_path / "upstream", tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    (source / "original.txt").write_text("original", encoding="utf-8")
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "auth.json")
    record = service.create(
        "workspace", CreateSource(provider="local", label="Owned data", root=str(source)), workspace
    )
    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="workspace", title="owner").id
    app = SimpleNamespace(
        state=SimpleNamespace(
            connected_storage=service,
            sessions=sessions,
            mcp_task_store=SessionMetadataTaskStore(sessions),
            agent_task_registry=AgentTaskRegistry(),
            bus=EventBus(),
            cancel_flags=set(),
        )
    )
    return app, sid, record, source, workspace


def test_index_acceptance_progress_dedupe_and_cancel_preserve_previous_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid, record, upstream, _ = setup(tmp_path)
    service = app.state.connected_storage
    previous = link_folder(service, record)
    old_manifest = previous.linked_manifest_id
    for index in range(70):
        (upstream / f"new-{index}.txt").write_text(str(index), encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = FsspecFolder.entries

    def gated(
        folder: FsspecFolder,
        path: str = "",
        *,
        recursive: bool = True,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[FileEntry]:
        def actual_progress(count: int) -> None:
            if progress is not None:
                progress(count)
            if count == 32:
                entered.set()
                assert release.wait(5), "test index gate was not released"

        return original(
            folder, path, recursive=recursive, progress=actual_progress, cancelled=cancelled
        )

    monkeypatch.setattr(FsspecFolder, "entries", gated)

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        operation = start_index(service, previous)
        receipt = storage_handle(app, sid, operation, "Index owned data", "call-index")
        assert receipt["accepted"] and receipt["kind"] == "Indexing"
        persisted = service.store.get("operation", operation.id, TransferOperation)
        assert persisted.task_handle == receipt["handle"] and persisted.owner_session_id == sid
        assert start_index(service, previous).id == operation.id
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # The application can run another action while the actual enumerator is blocked.
            await asyncio.sleep(0.25)
            snapshot = query_snapshot(app, sid, kind="Indexing")
            assert snapshot["tasks"][0]["progress"]["entries_done"] == 32
            assert snapshot["tasks"][0]["effective_status"] == "running"
            assert service.get("workspace", record.source.id).linked_manifest_id == old_manifest
            ack = await asyncio.to_thread(cancel_selected, app, sid, receipt["handle"])
            assert not ack["errors"] and ack["results"][0]["cancellation_requested"]
            assert query_snapshot(app, sid)["tasks"][0]["effective_status"] == "running"
        finally:
            release.set()
        await service.tasks[operation.id]
        await task_supervisor(app).drivers[receipt["handle"]]
        assert service.store.get("operation", operation.id, TransferOperation).state == "cancelled"
        assert service.get("workspace", record.source.id).linked_manifest_id == old_manifest
        monkeypatch.setattr(FsspecFolder, "entries", original)
        refreshed = start_index(service, service.get("workspace", record.source.id))
        await service.tasks[refreshed.id]
        settled = service.store.get("operation", refreshed.id, TransferOperation)
        assert settled.state == "completed" and settled.entries_done == 71
        current = service.get("workspace", record.source.id)
        assert current.linked_manifest_id == settled.manifest_id != old_manifest
        manifest = service.store.get("manifest", settled.manifest_id, Manifest)
        assert {row.path for row in manifest.entries} == {p.name for p in upstream.iterdir()}
        assert len(service.store.list("manifest", Manifest)) == 2
        await service.shutdown()
        await task_supervisor(app).shutdown()

    asyncio.run(exercise())


def test_download_shared_result_has_actual_selected_bytes_and_hash(tmp_path: Path) -> None:
    app, sid, record, upstream, workspace = setup(tmp_path)
    payload = b"actual selected download\n" * 50_000
    (upstream / "payload.bin").write_bytes(payload)
    service = app.state.connected_storage

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        operation = service.start_transfer(record, workspace, ["payload.bin"])
        receipt = storage_handle(app, sid, operation, "Download selected payload", "call-download")
        assert receipt["accepted"] and receipt["kind"] == "Download"
        assert query_snapshot(app, sid)["tasks"][0]["handle"] == receipt["handle"]
        await service.tasks[operation.id]
        await task_supervisor(app).drivers[receipt["handle"]]
        source = service.get("workspace", record.source.id)
        actual = Path(source.source.local_path, "payload.bin").read_bytes()
        assert actual == payload and not Path(source.source.local_path, "original.txt").exists()
        manifest = service.store.get("manifest", source.manifest_id, Manifest)
        assert manifest.hashes == {"payload.bin": hashlib.sha256(actual).hexdigest()}
        row = next(r for r in app.state.mcp_task_store.list() if r.handle == receipt["handle"])
        assert row.result["operation"]["bytes_done"] == len(payload)
        assert row.result["operation"]["state"] == "completed" and row.notify_pending
        assert row.invocation_id == "call-download"
        await service.shutdown()
        await task_supervisor(app).shutdown()

    asyncio.run(exercise())


def test_failed_handle_persistence_never_acknowledges_or_starts_storage_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real queued enumerator is stopped before execution when custody cannot be saved."""
    app, sid, record, _, _ = setup(tmp_path)
    service = app.state.connected_storage

    def fail_put(*args: Any, **kwargs: Any) -> None:
        raise OSError("test session store unavailable")

    store = app_task_store(app)
    monkeypatch.setattr(store, "put", fail_put)

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        operation = start_index(service, record)
        owner = service.tasks[operation.id]
        with pytest.raises(RuntimeError, match="was not acknowledged"):
            storage_handle(app, sid, operation, "Must not start", "failed-submission")
        await asyncio.gather(owner, return_exceptions=True)
        assert owner.cancelled()
        saved = service.store.get("operation", operation.id, TransferOperation)
        assert saved.state == "failed" and saved.cancel_requested
        assert service.get("workspace", record.source.id).linked_manifest_id is None
        assert service.store.list("manifest", Manifest) == []
        assert store.list() == []
        await service.shutdown()

    asyncio.run(exercise())
