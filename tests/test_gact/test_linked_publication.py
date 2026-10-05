"""End-to-end local fsspec staging, restart, review, conflict and isolation checks."""

import asyncio
from pathlib import Path

import pytest

from clio_agent.gact.storage.linked import FsspecFolder, link_folder
from clio_agent.gact.storage.linked_changes import (
    discard_edits,
    pending_edits,
    publish_edits,
    review_edits,
)
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.storage.source_filesystem import SourceFileSystem
from tests.test_gact.test_source_filesystem import setup


def test_restart_and_selected_publication_preserve_unpublished_edits(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path, "working_copy")
    fs.pipe_file("data.txt", b"updated")
    fs.pipe_file("new.txt", b"new")
    assert (original / "data.txt").read_bytes() == b"original"
    assert not (original / "new.txt").exists()
    service = StorageService(service.store.root, tmp_path / "private" / "credentials.json")
    record = service.get("w", record.source.id)
    fs = SourceFileSystem(service, "w", record.source.id, fs.resources)
    assert fs.cat_file("data.txt") == b"updated"
    review = review_edits(service, record)
    assert len(review.changes) == 2 and not any(row.conflict for row in review.changes)
    publish_edits(service, record, review.id, ["data.txt"])
    assert (original / "data.txt").read_bytes() == b"updated"
    assert not (original / "new.txt").exists()
    record = service.get("w", record.source.id)
    fs = SourceFileSystem(service, "w", record.source.id, fs.resources)
    assert fs.cat_file("data.txt") == b"updated"
    assert fs.cat_file("new.txt") == b"new"
    assert list(pending_edits(service, record).changes) == ["new.txt"]
    review = review_edits(service, record)
    publish_edits(service, record, review.id, ["new.txt"])
    assert (original / "new.txt").read_bytes() == b"new"
    assert not pending_edits(service, record).changes


def test_discard_restores_view_and_never_changes_originals(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path, "working_copy")
    fs.rm_file("data.txt")
    fs.pipe_file("new.txt", b"new")
    with pytest.raises(ValueError, match="Publish or discard"):
        link_folder(service, service.get("w", record.source.id))
    record = discard_edits(service, record)
    current = SourceFileSystem(service, "w", record.source.id, fs.resources)
    assert current.cat_file("data.txt") == b"original"
    assert current.ls("", detail=False) == ["data.txt"]
    assert list(original.iterdir()) == [original / "data.txt"]
    assert not pending_edits(service, record).changes


def test_stale_review_and_upstream_change_cannot_overwrite(tmp_path: Path) -> None:
    fs, service, record, original, _ = setup(tmp_path, "working_copy")
    fs.pipe_file("data.txt", b"first")
    record = service.get("w", record.source.id)
    review = review_edits(service, record)
    fs.pipe_file("data.txt", b"second")
    with pytest.raises(ValueError, match="review again"):
        publish_edits(service, record, review.id, ["data.txt"])
    record = service.get("w", record.source.id)
    review = review_edits(service, record)
    (original / "data.txt").write_bytes(b"other person's edit")
    with pytest.raises(ValueError, match="review again"):
        publish_edits(service, record, review.id, ["data.txt"])
    assert (original / "data.txt").read_bytes() == b"other person's edit"
    assert review_edits(service, record).changes[0].conflict


def test_download_without_link_cannot_be_published(tmp_path: Path) -> None:
    _, service, record, _, _ = setup(tmp_path, "working_copy")
    record.linked_manifest_id = None
    with pytest.raises(ValueError, match="Only linked folders"):
        review_edits(service, record)


def test_partial_publish_keeps_remaining_edits_and_refreshes_published_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs, service, record, original, _ = setup(tmp_path, "working_copy")
    fs.pipe_file("data.txt", b"updated")
    fs.pipe_file("new.txt", b"not published")
    record = service.get("w", record.source.id)
    review = review_edits(service, record)
    apply = FsspecFolder.apply

    def fail_second(
        adapter: FsspecFolder, name: str, content: Path | None, revision: str | None
    ) -> None:
        if name == "new.txt":
            raise OSError("Second file was denied")
        apply(adapter, name, content, revision)

    monkeypatch.setattr(FsspecFolder, "apply", fail_second)
    with pytest.raises(OSError, match="Second file was denied"):
        publish_edits(service, record, review.id, ["data.txt", "new.txt"])
    current = service.get("w", record.source.id)
    assert list(pending_edits(service, current).changes) == ["new.txt"]
    assert (original / "data.txt").read_bytes() == b"updated"
    assert not (original / "new.txt").exists()
    reopened = SourceFileSystem(service, "w", record.source.id, fs.resources)
    assert reopened.cat_file("data.txt") == b"updated"
    assert reopened.cat_file("new.txt") == b"not published"


@pytest.mark.parametrize("editable", [True, False])
def test_download_permission_is_independent_of_link(tmp_path: Path, editable: bool) -> None:
    fs, service, record, original, workspace = setup(tmp_path, "read_only")
    record.download_access = "editable" if editable else "read_only"
    service.store.put("source", record.source.id, record)
    operation = service.store.begin_operation(record.source.id, "materialize")
    asyncio.run(service._transfer(record, workspace, operation))
    downloaded = service.get("w", record.source.id)
    assert downloaded.linked_access == "read_only"
    copy = Path(downloaded.source.local_path) / "data.txt"
    if editable:
        assert copy.is_relative_to(workspace)
        copy.write_bytes(b"local changes")
        assert (original / "data.txt").read_bytes() == b"original"
        assert fs.cat_file("data.txt") == b"original"
    else:
        assert copy.is_relative_to(service.store.root)
        assert not copy.stat().st_mode & 0o222
    with pytest.raises(PermissionError):
        fs.pipe_file("data.txt", b"cannot change original")
