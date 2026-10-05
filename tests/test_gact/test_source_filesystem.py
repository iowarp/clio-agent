"""Real filesystem acceptance of linked writes, custody and download isolation."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource, Manifest, SourceRecord, TransferOperation
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.source_filesystem import SourceFileSystem


def setup(
    tmp_path: Path, mode: str = "write_enabled"
) -> tuple[SourceFileSystem, StorageService, SourceRecord, Path, Path]:
    """Connect a real source, owned immutable store, workspace and resource custody."""
    original = tmp_path / "original"
    original.mkdir()
    (original / "data.txt").write_bytes(b"original")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = StorageService(tmp_path / "sources", tmp_path / "private" / "credentials.json")
    record = service.create(
        "w", CreateSource(provider="local", root=str(original), label="Data", mode=mode), workspace
    )
    record = link_folder(service, record)
    resources = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)
    return (
        SourceFileSystem(service, "w", record.source.id, resources),
        service,
        record,
        original,
        workspace,
    )


def test_writes_propagate_with_retained_original_and_new_revision(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path)
    before_revision = fs.revision
    assert record.source.local_path is None
    with fs.open("data.txt", "wb") as stream:
        stream.write(b"updated")
        assert (original / "data.txt").read_bytes() == b"original"
    assert (original / "data.txt").read_bytes() == b"updated"
    assert fs.revision != before_revision
    assert (
        service.store.root / record.source.id / before_revision / "data.txt"
    ).read_bytes() == b"original"
    assert fs.cat_file("data.txt") == b"updated"
    evidence = (fs.resources.root / "resources.json").read_text()
    for content in (b"original", b"updated"):
        assert hashlib.sha256(content).hexdigest() in evidence
    operation = service.store.get("operation", fs.last_operation, TransferOperation)
    assert operation.state == "completed" and operation.applied_paths == ["data.txt"]


@pytest.mark.parametrize("operation", ["open", "put", "rm", "pipe"])
def test_read_only_enforced_at_filesystem_boundary(tmp_path: Path, operation: str) -> None:
    fs, _, _, original, _ = setup(tmp_path, "read_only")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"changed")
    with pytest.raises(PermissionError, match="read only"):
        if operation == "open":
            fs.open("data.txt", "wb")
        elif operation == "put":
            fs.put_file(str(replacement), "data.txt")
        elif operation == "pipe":
            fs.pipe_file("data.txt", b"changed")
        else:
            fs.rm_file("data.txt")
    assert (original / "data.txt").read_bytes() == b"original"


def test_cancelled_write_and_revoked_permission_do_not_publish(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path)
    with pytest.raises(RuntimeError, match="cancelled"):
        with fs.open("data.txt", "wb") as stream:
            stream.write(b"cancelled")
            raise RuntimeError("cancelled")
    assert (original / "data.txt").read_bytes() == b"original"
    with pytest.raises(RuntimeError, match="cancelled"):
        with fs.open("data.txt", "wt", encoding="utf-8") as text:
            text.write("cancelled")
            raise RuntimeError("cancelled")
    assert (original / "data.txt").read_bytes() == b"original"
    stream = fs.open("data.txt", "wb")
    stream.write(b"revoked")
    record.source = record.source.model_copy(update={"mode": "read_only"})
    service.store.put("source", record.source.id, record)
    with pytest.raises(PermissionError):
        stream.close()
    assert (original / "data.txt").read_bytes() == b"original"


def test_stale_source_or_link_refuses_overwrite(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path)
    fs.cat_file("data.txt")
    (original / "data.txt").write_bytes(b"someone else's change")
    with pytest.raises(ValueError, match="changed"):
        fs.pipe_file("data.txt", b"overwrite")
    assert (original / "data.txt").read_bytes() == b"someone else's change"
    link_folder(service, record)
    with pytest.raises(ValueError, match="link changed"):
        fs.pipe_file("data.txt", b"overwrite")


def test_create_delete_and_path_confinement(tmp_path: Path) -> None:
    fs, service, _, original, _ = setup(tmp_path)
    fs.pipe_file("nested/new.txt", b"created")
    assert (original / "nested/new.txt").read_bytes() == b"created"
    with pytest.raises(FileExistsError):
        with fs.open("nested/new.txt", "xb") as stream:
            stream.write(b"overwrite")
    revision = fs.revision
    fs.rm_file("nested/new.txt")
    assert not (original / "nested/new.txt").exists()
    assert (
        service.store.root / fs.source_id / revision / "nested/new.txt"
    ).read_bytes() == b"created"
    for path in ("../outside", "/absolute", "nested/../../outside"):
        with pytest.raises(ValueError):
            fs.pipe_file(path, b"escape")


def test_download_from_writable_link_is_independent(tmp_path: Path) -> None:
    fs, service, record, original, workspace = setup(tmp_path)
    operation = service.store.begin_operation(record.source.id, "materialize")
    asyncio.run(service._transfer(record, workspace, operation))
    downloaded = service.get("w", record.source.id)
    copy = Path(downloaded.source.local_path) / "data.txt"
    copy.write_bytes(b"local edit")
    assert (original / "data.txt").read_bytes() == b"original"
    baseline = service.store.get("manifest", downloaded.manifest_id, Manifest)
    assert (
        service.store.root / record.source.id / baseline.id / "data.txt"
    ).read_bytes() == b"original"
    fs.pipe_file("data.txt", b"linked edit")
    assert (original / "data.txt").read_bytes() == b"linked edit"
    assert copy.read_bytes() == b"local edit"


def test_working_copy_adapter_cannot_apply_upstream(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path)
    record.source = record.source.model_copy(update={"mode": "working_copy"})
    service.store.put("source", record.source.id, record)
    with service.adapter(record) as adapter, pytest.raises(PermissionError):
        adapter.apply("data.txt", original / "data.txt", None)
    fs.pipe_file("data.txt", b"local only")
    assert fs.cat_file("data.txt") == b"local only"
    assert (original / "data.txt").read_bytes() == b"original"
