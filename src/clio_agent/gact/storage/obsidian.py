"""Vault access with explicit exclusions and no access to Obsidian's private state."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from clio_agent.gact.storage.adapters import LocalSource, file_hash, safe_child
from clio_agent.gact.storage.filesystem import is_link
from clio_agent.gact.storage.linked import FsspecFolder
from clio_agent.gact.storage.models import FileEntry


def vault_path(path: str, excluded: list[str]) -> None:
    """Reject private directories, traversal and explicitly excluded subtrees."""
    FileEntry(path=path, kind="file")
    if any(part.startswith(".") for part in PurePosixPath(path).parts) or any(
        path.casefold() == prefix.casefold()
        or path.casefold().startswith(prefix.rstrip("/").casefold() + "/")
        for prefix in excluded
    ):
        raise PermissionError("This path is outside the approved vault contents")


def vault_root(root: Path) -> Path:
    """Locate the enclosing vault without widening the selected content scope."""
    for candidate in (root, *root.parents):
        marker = candidate / ".obsidian"
        if marker.is_dir() and not is_link(marker):
            relative = root.relative_to(candidate).as_posix()
            if relative != ".":
                vault_path(relative, [])
                safe_child(candidate, relative)
            return candidate
    if root.name.startswith("."):
        raise ValueError("Select a notes folder, not a private settings directory")
    # Markdown folders have the same knowledge semantics without an installed app.
    # Attaching one never creates configuration or changes its existing contents.
    return root


class ObsidianSource(LocalSource):
    """A local vault with safe note and attachment access, including reviewed writes."""

    def __init__(
        self, root: str, *, writable: bool = False, excluded: list[str] | None = None
    ) -> None:
        super().__init__(root, writable=writable)
        self.vault_root = vault_root(self.root)
        self.excluded = excluded or []
        for path in self.excluded:
            FileEntry(path=path, kind="directory")
        self.capabilities = self.capabilities.model_copy(
            update={
                "note_read": True,
                "note_search": True,
                "note_relations": True,
                "note_write": writable,
                "link_folder": True,
                "search": True,
                "conditional_write": True,
            }
        )

    def entries(self) -> list[FileEntry]:
        """Enumerate allowed regular files without reading attachment bodies."""
        from clio_agent.runtime.storage_access import check_storage_access

        check_storage_access(self.root)
        rows: list[FileEntry] = []
        for folder, directories, files in os.walk(self.root, followlinks=False):
            base = Path(folder)
            if base != self.root:
                safe_child(self.root, base.relative_to(self.root).as_posix())
            for names in (directories, files):
                for name in list(names):
                    child = base / name
                    relative = child.relative_to(self.root).as_posix()
                    try:
                        vault_path(relative, self.excluded)
                    except PermissionError:
                        names.remove(name)
                        continue
                    if is_link(child):
                        names.remove(name)
                        continue
                    if len(rows) >= 100_000:
                        raise ValueError("Select a smaller vault (limit 100,000 entries)")
                    info = child.stat()
                    directory = stat.S_ISDIR(info.st_mode)
                    if not directory and not stat.S_ISREG(info.st_mode):
                        raise ValueError("Vaults support regular notes and attachments only")
                    rows.append(
                        FileEntry(
                            path=relative,
                            kind="directory" if directory else "file",
                            size=0 if directory else info.st_size,
                            revision=f"{info.st_mtime_ns}:{info.st_size}:{info.st_ctime_ns}",
                        )
                    )
        return sorted(rows, key=lambda row: row.path)

    def metadata(self, path: str) -> FileEntry | None:
        """Check one current file without rescanning the vault on every note read."""
        vault_path(path, self.excluded)
        child = safe_child(self.root, path)
        try:
            info = child.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return FileEntry(
            path=path,
            kind="file",
            size=info.st_size,
            revision=f"{info.st_mtime_ns}:{info.st_size}:{info.st_ctime_ns}",
        )

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Open only approved vault content."""
        vault_path(entry.path, self.excluded)
        return super().open_read(entry)

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Reuse the storage conflict boundary for approved note writes."""
        vault_path(path, self.excluded)
        if not path.lower().endswith(".md"):
            raise PermissionError("Knowledge edits support Markdown notes only")
        return super().apply(path, content, expected)


class ObsidianFolder(FsspecFolder):
    """Apply the same vault policy to linked reads and the existing edit pipeline."""

    def __init__(self, source: ObsidianSource) -> None:
        from fsspec.implementations.local import LocalFileSystem

        super().__init__(
            LocalFileSystem(auto_mkdir=False), str(source.root), writable=source.writable
        )
        self.source = source
        self.capabilities = source.capabilities

    def entries(
        self,
        folder: str = "",
        *,
        recursive: bool = True,
        progress: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[FileEntry]:
        """Return scoped metadata through the standard linked-folder contract."""
        if folder:
            vault_path(folder, self.source.excluded)
        rows = []
        for entry in self.source.entries():
            if cancelled and cancelled():
                raise InterruptedError("Vault indexing cancelled")
            parent = str(PurePosixPath(entry.path).parent)
            if (not folder or entry.path.startswith(folder + "/")) and (
                recursive or parent == (folder or ".")
            ):
                rows.append(entry)
                if progress:
                    progress(len(rows))
        return rows

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Read a selected allowed file without trusting an upstream locator."""
        return self.source.open_read(entry)

    def metadata(self, path: str) -> FileEntry | None:
        """Read one note's current revision through the vault policy."""
        return self.source.metadata(path)

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Delegate writes to the vault's conditional write implementation."""
        current = self.source.metadata(path)
        if (current.revision if current else None) != expected:
            raise ValueError("The note changed after review")
        digest = file_hash(safe_child(self.source.root, path)) if current else None
        return self.source.apply(path, content, digest)
