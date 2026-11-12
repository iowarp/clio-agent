"""Provider interface and bounded local-folder access. Adapters never imply an OS mount."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from clio_schemas.connected_resources import SourceCapabilities

from clio_agent.gact.storage.filesystem import is_link, walk
from clio_agent.gact.storage.models import FileEntry
from clio_agent.gact.storage.names import (
    UNSAFE_PATH,
    check_remote_relative,
    listed_child,
    resolve_remote,
)
from clio_agent.platform_paths import win_extended_path


def file_hash(path: Path) -> str:
    """Hash a regular file using a bounded read buffer."""
    with open(win_extended_path(path), "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    """Resolve a validated name beneath its root and reject symlink aliases."""
    FileEntry(path=relative, kind="file")
    return source_child(root, relative)


def source_child(root: Path, remote: str) -> Path:
    """Resolve an existing provider name beneath its root, even when it is not portable.

    Use only to read or replace a name the host itself listed; CLIO-owned copies
    are always written under portable names through ``safe_child``.
    """
    check_remote_relative(remote)
    parts = remote.split("/")
    if os.name == "nt" and any("\\" in part or ":" in part for part in parts):
        raise ValueError(UNSAFE_PATH)
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current /= part
        if is_link(current):
            raise ValueError("Symbolic links and junctions require an explicit source selection")
    candidate.resolve().relative_to(root.resolve())
    return candidate


class SourceAdapter(Protocol):
    """A source implementation provides revisions and explicit file operations."""

    capabilities: SourceCapabilities

    def entries(self) -> list[FileEntry]:
        """Inspect a complete bounded source snapshot."""
        ...

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Open the selected file for streaming."""
        ...

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Apply one reviewed change, refusing an observed upstream conflict."""
        ...


class LocalSource:
    """A selected directory on the connected CLIO, without following nested links."""

    def __init__(self, root: str, *, writable: bool = False) -> None:
        path = Path(root).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError("Select an existing absolute directory on the connected CLIO")
        self.root = path.resolve(strict=True)
        self.writable = writable and os.access(self.root, os.W_OK)
        modes = ["read_only", "working_copy"]
        if self.writable:
            modes.append("write_enabled")
        self.capabilities = SourceCapabilities.model_validate(
            {
                "search": True,
                "revision_check": True,
                "conditional_write": False,
                "writable_folder": self.writable,
                "supported_modes": modes,
                "unavailable_reasons": {}
                if self.writable
                else {"write_enabled": "This folder is not writable on the connected CLIO."},
            }
        )

    def entries(self) -> list[FileEntry]:
        """Inspect regular files and directories, bounded to 100,000 entries.

        Names that are not portable are listed under their ``storage.names``
        alias; a name that cannot be represented is skipped and counted.
        """
        from clio_agent.runtime.storage_access import check_storage_access  # noqa: PLC0415

        rows: list[FileEntry] = []
        self.skipped = 0
        aliases = {"": ""}
        for directory, names, files in walk(self.root):
            remote_parent = Path(directory).relative_to(self.root).as_posix()
            remote_parent = "" if remote_parent == "." else remote_parent
            parent = aliases[remote_parent]
            for name in sorted(names + files):
                listed = listed_child(parent, remote_parent, name)
                if listed is None:
                    self.skipped += 1
                    if name in names:
                        names.remove(name)
                    continue
                relative, remote = listed
                path = source_child(self.root, remote)
                check_storage_access(path)
                if len(rows) >= 100_000:
                    raise ValueError("Select a smaller source folder (limit 100,000 entries)")
                if os.path.isdir(win_extended_path(path)):
                    rows.append(FileEntry(path=relative, remote_path=remote, kind="directory"))
                    aliases[remote] = relative
                elif os.path.isfile(win_extended_path(path)):
                    digest = file_hash(path)
                    rows.append(
                        FileEntry(
                            path=relative,
                            remote_path=remote,
                            kind="file",
                            size=os.stat(win_extended_path(path)).st_size,
                            revision=digest,
                            sha256=digest,
                        )
                    )
                else:
                    raise ValueError("Sources may contain only regular files and directories")
        return sorted(rows, key=lambda row: row.path)

    def local(self, path: str) -> Path:
        """Locate a listed portable path among this folder's own names."""
        FileEntry(path=path, kind="file")
        return source_child(
            self.root,
            resolve_remote(path, lambda folder: os.listdir(win_extended_path(self.root / folder))),
        )

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Open a selected regular file; reject changes from file to link."""
        return open(win_extended_path(source_child(self.root, entry.source_path)), "rb")

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Check the current hash immediately before a per-file atomic replacement.

        This is optimistic conflict detection, not filesystem compare-and-swap or
        directory-wide atomicity; another application can still race this check.
        """
        if not self.writable:
            raise PermissionError("This source has not been approved for writes")
        destination = self.local(path)
        actual = file_hash(destination) if os.path.isfile(win_extended_path(destination)) else None
        if actual != expected:
            raise ValueError(f"Upstream changed after review: {path}")
        if content is None:
            if os.path.exists(win_extended_path(destination)):
                os.unlink(win_extended_path(destination))
            return None
        os.makedirs(win_extended_path(destination.parent), exist_ok=True)
        with staged_file(destination) as stage:
            with (
                open(win_extended_path(content), "rb") as reader,
                open(win_extended_path(stage), "xb") as writer,
            ):
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            # Recheck after copying a potentially large file into staging.
            actual = (
                file_hash(destination) if os.path.isfile(win_extended_path(destination)) else None
            )
            if actual != expected:
                raise ValueError(f"Upstream changed after review: {path}")
            os.replace(win_extended_path(stage), win_extended_path(destination))
        return file_hash(destination)


@contextmanager
def staged_file(destination: Path) -> Iterator[Path]:
    """Use a unique sibling staging file; cleanup only this operation's file."""
    stage = destination.with_name(".clio-transfer-" + uuid.uuid4().hex)
    try:
        yield stage
    finally:
        if os.path.exists(win_extended_path(stage)):
            os.unlink(win_extended_path(stage))
