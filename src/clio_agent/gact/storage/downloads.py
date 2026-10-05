"""Selection and copy retention shared by connector download backends."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import BinaryIO

from clio_agent.gact.storage.adapters import LocalSource, SourceAdapter
from clio_agent.gact.storage.models import FileEntry, Manifest, SourceRecord
from clio_agent.gact.storage.sftp import SftpSource
from clio_agent.gact.storage.store import SourceStore


def selected(path: str, paths: list[str]) -> bool:
    """Match complete path segments, never a sibling sharing a name prefix."""
    return any(path == root or path.startswith(root + "/") for root in paths)


class SelectedDownload:
    """Copy selected remote entries while retaining previously downloaded siblings."""

    def __init__(
        self, store: SourceStore, record: SourceRecord, upstream: SourceAdapter, paths: list[str]
    ) -> None:
        if not paths:
            raise ValueError("Select at least one file or folder to download")
        for path in paths:
            FileEntry(path=path, kind="file")
        self.upstream, self.paths = upstream, paths
        self.capabilities = upstream.capabilities
        self.previous: LocalSource | None = None
        self.retained: list[FileEntry] = []
        if record.manifest_id:
            previous = store.get("manifest", record.manifest_id, Manifest)
            if previous.source_id != record.source.id:
                raise ValueError("Downloaded source ownership mismatch")
            self.previous = LocalSource(str(store.root / record.source.id / previous.id))
            self.retained = [entry for entry in previous.entries if not selected(entry.path, paths)]

    def entries(self) -> list[FileEntry]:
        """List the selected revision and retained copies without downloading siblings."""
        upstream = (
            self.upstream.entries(metadata_only=True)
            if isinstance(self.upstream, SftpSource)
            else self.upstream.entries()
        )
        for path in self.paths:
            if not any(row.path == path or row.path.startswith(path + "/") for row in upstream):
                raise ValueError("The selected download no longer exists")
        chosen = [entry for entry in upstream if selected(entry.path, self.paths)]
        rows = {entry.path: entry for entry in self.retained}
        rows.update({entry.path: entry for entry in chosen})
        # Files in a selected nested folder need their parent directories too.
        for entry in list(rows.values()):
            for parent in PurePosixPath(entry.path).parents:
                if str(parent) != ".":
                    rows.setdefault(str(parent), FileEntry(path=str(parent), kind="directory"))
        return sorted(rows.values(), key=lambda entry: entry.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Read only chosen paths upstream; other bytes come from retained local evidence."""
        if selected(entry.path, self.paths):
            return self.upstream.open_read(entry)
        if self.previous is None:
            raise ValueError("The retained download is missing")
        return self.previous.open_read(entry)

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Downloads never change the source."""
        raise PermissionError("Downloads cannot update upstream files")
