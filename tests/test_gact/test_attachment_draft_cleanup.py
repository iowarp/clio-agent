"""Removed unsent attachments withdraw their workspace data, never upstream files."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.desktop_upload import UploadedSource, publish_uploaded_source
from clio_agent.gact.storage.drafts import SourceDrafts, retain_message_sources
from clio_agent.gact.storage.linked import link_folder, linked_file
from clio_agent.gact.storage.models import CreateSource, SourceRecord, TransferOperation
from clio_agent.gact.storage.references import source_folder_resource
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.workspace_files import connected_file_entries


def setup(
    tmp_path: Path, mode: str = "read_only"
) -> tuple[StorageService, SourceDrafts, SourceRecord, Path, Path]:
    upstream, workspace = tmp_path / "original", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    (upstream / "a.txt").write_text("original")
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    drafts = SourceDrafts(service, resources)
    service.transfer_settled = drafts.cleanup
    record = service.create(
        "w",
        CreateSource(provider="local", root=str(upstream), label="Selected folder", mode=mode),
        workspace,
    )
    return service, drafts, record, upstream, workspace


def materialize(service: StorageService, record: SourceRecord, workspace: Path) -> SourceRecord:
    operation = service.store.begin_operation(record.source.id, "materialize")
    asyncio.run(service._transfer(record, workspace, operation))
    assert service.store.get("operation", operation.id, TransferOperation).state == "completed"
    return service.get("w", record.source.id)


@pytest.mark.parametrize("kind", ["linked", "read_only", "working_copy"])
def test_remove_unsent_attachment_removes_entire_named_folder(tmp_path: Path, kind: str) -> None:
    service, drafts, record, upstream, workspace = setup(
        tmp_path, "working_copy" if kind == "working_copy" else "read_only"
    )
    lease = drafts.begin(record)
    record = (
        link_folder(service, record)
        if kind == "linked"
        else materialize(service, record, workspace)
    )
    if kind == "linked":
        linked_file(service, record, "a.txt")
    resource = drafts.prepare(
        record.source.id,
        lease,
        lambda: source_folder_resource(
            service.store, drafts.resources, record, linked=kind == "linked"
        ),
    )
    revision = record.linked_manifest_id if kind == "linked" else record.manifest_id
    if kind == "working_copy":
        assert (workspace / "connected-data" / record.source.id / "a.txt").exists()
    else:
        assert connected_file_entries(service, "w", 100)[0]
    drafts.finish(record.source.id, lease)
    assert connected_file_entries(service, "w", 100)[0] == []
    assert not (service.store.root / record.source.id / str(revision)).exists()
    assert not (service.store.root / record.source.id).exists()
    assert not (workspace / "connected-data" / record.source.id).exists()
    assert drafts.resources.get("w", resource.id) is None
    assert (upstream / "a.txt").read_text() == "original"
    assert service.get("w", record.source.id).connected
    with pytest.raises(ValueError, match="removed"):
        drafts.prepare(
            record.source.id,
            lease,
            lambda: source_folder_resource(service.store, drafts.resources, record),
        )


@pytest.mark.parametrize("restart", [False, True])
def test_sent_source_is_retained_when_reused_then_removed(tmp_path: Path, restart: bool) -> None:
    service, drafts, record, _, _ = setup(tmp_path)
    lease = drafts.begin(record)
    record = link_folder(service, record)
    resource = drafts.prepare(
        record.source.id,
        lease,
        lambda: source_folder_resource(service.store, drafts.resources, record, linked=True),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            source_drafts=None if restart else drafts,
            connected_storage=service,
            resource_store=drafts.resources,
        )
    )
    retain_message_sources(
        app,
        SimpleNamespace(
            role="user", parts=[SimpleNamespace(type="resource_ref", resource_id=resource.id)]
        ),
    )
    drafts.finish(record.source.id, lease)
    second = drafts.begin(record)
    drafts.prepare(
        record.source.id,
        second,
        lambda: source_folder_resource(service.store, drafts.resources, record, linked=True),
    )
    drafts.finish(record.source.id, second)
    assert service.get("w", record.source.id).linked_manifest_id == record.linked_manifest_id
    assert drafts.resources.get("w", resource.id) is not None
    assert connected_file_entries(service, "w", 100)[0]


def test_two_drafts_share_a_link_until_the_last_one_is_removed(tmp_path: Path) -> None:
    service, drafts, record, _, _ = setup(tmp_path)
    first = drafts.begin(record)
    record = link_folder(service, record)
    second = drafts.begin(record)
    drafts.finish(record.source.id, first)
    assert connected_file_entries(service, "w", 100)[0]
    restarted = SourceDrafts(service, drafts.resources)
    restarted.finish(record.source.id, second)
    assert connected_file_entries(service, "w", 100)[0] == []


def test_removed_running_download_cannot_publish_a_late_copy(tmp_path: Path) -> None:
    service, drafts, record, upstream, workspace = setup(tmp_path)
    lease = drafts.begin(record)

    async def download() -> None:
        operation = service.start_transfer(record, workspace)
        task = service.tasks[operation.id]
        drafts.finish(record.source.id, lease)
        await task
        assert service.store.get("operation", operation.id, TransferOperation).state == "cancelled"

    asyncio.run(download())
    assert connected_file_entries(service, "w", 100)[0] == []
    assert not (service.store.root / record.source.id).exists()
    assert (upstream / "a.txt").exists()


def test_abandoned_refresh_restores_previous_download(tmp_path: Path) -> None:
    service, drafts, record, upstream, workspace = setup(tmp_path, "working_copy")
    record = materialize(service, record, workspace)
    previous = record.manifest_id
    lease = drafts.begin(record)
    (upstream / "a.txt").write_text("new remote version")
    record = materialize(service, record, workspace)
    temporary = record.manifest_id
    drafts.finish(record.source.id, lease)
    restored = service.get("w", record.source.id)
    assert restored.manifest_id == previous
    assert (Path(restored.source.local_path) / "a.txt").read_text() == "original"
    assert not (service.store.root / record.source.id / str(temporary)).exists()


def test_edited_working_copy_is_not_silently_deleted(tmp_path: Path) -> None:
    service, drafts, record, _, workspace = setup(tmp_path, "working_copy")
    lease = drafts.begin(record)
    record = materialize(service, record, workspace)
    local = Path(record.source.local_path) / "a.txt"
    local.write_text("my edits")
    with pytest.raises(ValueError, match="edited files"):
        drafts.finish(record.source.id, lease)
    assert local.read_text() == "my edits"


def test_uploaded_folder_removal_also_removes_original_upload_receipts(tmp_path: Path) -> None:
    service, drafts, _, _, workspace = setup(tmp_path)
    uploaded, _ = drafts.resources.create_or_resume(workspace_id="w", name="a.txt", declared_size=4)
    uploaded = drafts.resources.append(uploaded.id, offset=0, data=b"data")
    leases = []

    def prepare(record: SourceRecord) -> None:
        lease = drafts.begin(record)
        leases.append(lease)
        drafts.uploaded_resources(record.source.id, lease, [uploaded.id])

    request = UploadedSource(
        label="Uploaded folder",
        draft=True,
        files=[{"path": "folder/a.txt", "resource_id": uploaded.id, "revision": 1}],
    )
    record = publish_uploaded_source(
        service.store,
        drafts.resources,
        "w",
        workspace,
        service.principal,
        request,
        on_prepare=prepare,
    )
    drafts.prepare(
        record.source.id,
        leases[0],
        lambda: source_folder_resource(service.store, drafts.resources, record),
    )
    drafts.finish(record.source.id, leases[0])
    assert drafts.resources.list("w") == []
    assert all(row.source.id != record.source.id for row in service.sources("w"))
    assert connected_file_entries(service, "w", 100)[0] == []
    assert not (service.store.root / record.source.id).exists()


def test_ordinary_upload_removal_cleans_bytes_and_blocks_late_recreation(tmp_path: Path) -> None:
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _ = resources.create_or_resume(
        workspace_id="w",
        name="a.txt",
        declared_size=7,
        client_upload_id="draft-one",
        pending_attachment=True,
    )
    resources.append(record.id, offset=0, data=b"partial")
    for identifier in resources.discard_upload("w", "draft-one"):
        resources.delete("w", identifier, pending_only=True)
    assert resources.list("w") == []
    assert not (resources.root / "w" / record.id).exists()
    restarted = ResourceStore(root=resources.root, max_resource_bytes=1024)
    with pytest.raises(ValueError, match="removed"):
        restarted.create_or_resume(
            workspace_id="w",
            name="a.txt",
            declared_size=7,
            client_upload_id="draft-one",
            pending_attachment=True,
        )
    # Removal can arrive before the create request does.
    assert restarted.discard_upload("w", "draft-late") == []
    with pytest.raises(ValueError, match="removed"):
        restarted.create_or_resume(
            workspace_id="w",
            name="a.txt",
            declared_size=7,
            client_upload_id="draft-late",
            pending_attachment=True,
        )


def test_sent_ordinary_upload_is_not_removed_by_a_stale_draft(tmp_path: Path) -> None:
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024)
    record, _ = resources.create_or_resume(
        workspace_id="w",
        name="a.txt",
        declared_size=4,
        client_upload_id="draft-one",
        pending_attachment=True,
    )
    resources.append(record.id, offset=0, data=b"sent")
    app = SimpleNamespace(state=SimpleNamespace(resource_store=resources))
    retain_message_sources(
        app,
        SimpleNamespace(
            role="user", parts=[SimpleNamespace(type="resource_ref", resource_id=record.id)]
        ),
    )
    assert resources.discard_upload("w", "draft-one") == []
    assert not resources.delete("w", record.id, pending_only=True)
    assert resources.content_path(resources.get("w", record.id)).read_bytes() == b"sent"
