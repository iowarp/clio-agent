"""Protocol-backed lazy links, selected downloads, and capability separation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.storage.linked import github_location, link_folder, linked_file
from clio_agent.gact.storage.models import CreateSource, Manifest, SourceRecord
from clio_agent.gact.storage.service import StorageService, provider_capabilities
from clio_agent.gact.storage.workspace_files import connected_file_entries, resolve_connected_input


def local_source(tmp_path: Path) -> tuple[StorageService, SourceRecord, Path, Path]:
    """Create real local files for the fsspec and existing transfer paths."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "a.txt").write_bytes(b"first file")
    (upstream / "nested").mkdir()
    (upstream / "nested" / "b.txt").write_bytes(b"second file")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = StorageService(tmp_path / "source-store", tmp_path / "private" / "auth.json")
    record = service.create(
        "w", CreateSource(provider="local", label="Data", root=str(upstream)), workspace
    )
    return service, record, upstream, workspace


def test_link_indexes_without_copying_and_reads_only_selected_file(tmp_path: Path) -> None:
    service, record, upstream, _ = local_source(tmp_path)
    linked = link_folder(service, record)
    assert linked.linked_manifest_id
    assert linked.manifest_id is None
    assert linked.source.local_path is None
    cache = service.store.root / linked.source.id / linked.linked_manifest_id
    assert not cache.exists()
    entries, truncated = connected_file_entries(service, "w", 100)
    assert not truncated
    selected = next(row for row in entries if row["path"].endswith("/a.txt"))
    opened = resolve_connected_input(service, "w", selected["path"])
    assert opened is not None and opened.read_bytes() == b"first file"
    assert not (cache / "nested" / "b.txt").exists()
    assert (upstream / "nested" / "b.txt").read_bytes() == b"second file"
    assert service.get("w", record.source.id).source.local_path is None
    with pytest.raises(KeyError):
        resolve_connected_input(service, "other", selected["path"])
    service.disconnect(linked)
    with pytest.raises(ValueError, match="disconnected"):
        resolve_connected_input(service, "w", selected["path"])


def test_changed_linked_file_requires_explicit_refresh(tmp_path: Path) -> None:
    service, record, upstream, _ = local_source(tmp_path)
    linked = link_folder(service, record)
    (upstream / "a.txt").write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="changed"):
        linked_file(service, linked, "a.txt")
    refreshed = link_folder(service, linked)
    assert linked_file(service, refreshed, "a.txt").read_bytes() == b"changed bytes"


def test_selected_download_retains_prior_copies_and_does_not_download_siblings(
    tmp_path: Path,
) -> None:
    service, record, _, workspace = local_source(tmp_path)
    first = service.store.begin_operation(record.source.id, "materialize")
    first = service.store.update_operation(first.id, selected_paths=["a.txt"])
    asyncio.run(service._transfer(record, workspace, first))
    record = service.get("w", record.source.id)
    assert Path(record.source.local_path, "a.txt").read_bytes() == b"first file"
    assert not Path(record.source.local_path, "nested", "b.txt").exists()
    second = service.store.begin_operation(record.source.id, "materialize")
    second = service.store.update_operation(second.id, selected_paths=["nested"])
    asyncio.run(service._transfer(record, workspace, second))
    record = service.get("w", record.source.id)
    assert Path(record.source.local_path, "a.txt").read_bytes() == b"first file"
    assert Path(record.source.local_path, "nested", "b.txt").read_bytes() == b"second file"
    assert record.linked_manifest_id is None
    manifest = service.store.get("manifest", record.manifest_id, Manifest)
    assert set(manifest.hashes) == {"a.txt", "nested/b.txt"}


def test_github_link_only_and_human_folder_links(tmp_path: Path) -> None:
    assert github_location("https://github.com/fsspec/gdrive-fsspec/tree/main/tests") == (
        "fsspec",
        "gdrive-fsspec",
        "main",
        "tests",
    )
    capabilities = provider_capabilities("github")
    assert capabilities.link_folder and not capabilities.download
    assert capabilities.supported_modes == ["read_only", "working_copy", "write_enabled"]
    service = StorageService(tmp_path / "sources", tmp_path / "auth.json")
    record = service.create(
        "w",
        CreateSource(
            provider="github", label="Code", root="https://github.com/fsspec/gdrive-fsspec"
        ),
    )
    assert not service.download_available(record)
    with pytest.raises(ValueError, match="transfer backend"):
        service.start_transfer(record, tmp_path)
    for invalid in (
        "https://example.com/repo",
        "https://github.com/a/b/blob/main/code.py",
        "https://github.com/a/b/tree/main/../secrets",
    ):
        with pytest.raises(ValueError):
            github_location(invalid)


def test_drive_reuses_library_reads_and_existing_export_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gdrive_fsspec.core

    from clio_agent.gact.storage.linked import FsspecFolder, _ApprovedDrive

    calls: list[str] = []

    class Files:
        def list(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                execute=lambda: {
                    "files": [
                        {
                            "id": "text",
                            "name": "data.txt",
                            "size": "4",
                            "version": "1",
                            "mimeType": "text/plain",
                        },
                        {
                            "id": "doc",
                            "name": "Report",
                            "version": "2",
                            "mimeType": "application/vnd.google-apps.document",
                        },
                    ]
                }
            )

        def get_media(self, **kwargs: Any) -> Any:
            calls.append("read:" + kwargs["fileId"])
            return SimpleNamespace(headers={}, execute=lambda: b"data")

        def export(self, *, fileId: str, mimeType: str) -> Any:
            assert (fileId, mimeType) == ("doc", "application/pdf")
            calls.append("export:" + fileId)
            return SimpleNamespace(execute=lambda: b"%PDF-test")

    def build(*args: Any, **kwargs: Any) -> Any:
        assert kwargs["credentials"].token == "approved-test-token"
        return SimpleNamespace(files=lambda: Files())

    monkeypatch.setattr(gdrive_fsspec.core, "build", build)
    folder = FsspecFolder(_ApprovedDrive("approved-root", "approved-test-token"), "")
    entries = {row.path: row for row in folder.entries()}
    assert not calls
    assert set(entries) == {"data.txt", "Report.pdf"}
    with folder.open_read(entries["data.txt"]) as reader:
        assert reader.read() == b"data"
    with folder.open_read(entries["Report.pdf"]) as reader:
        assert reader.read() == b"%PDF-test"
    assert calls == ["read:text", "export:doc"]
