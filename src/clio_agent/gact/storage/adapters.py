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
from clio_agent.platform_paths import win_extended_path


def file_hash(path: Path) -> str:
    """Hash a regular file using a bounded read buffer."""
    with open(win_extended_path(path), "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_child(root: Path, relative: str) -> Path:
    """Resolve a validated name beneath its root and reject symlink aliases."""
    FileEntry(path=relative, kind="file")
    candidate = root / relative
    current = root
    for part in Path(relative).parts:
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
        """Inspect regular files and directories, bounded to 100,000 entries."""
        rows: list[FileEntry] = []
        for directory, names, files in walk(self.root):
            for name in sorted(names + files):
                path = Path(directory) / name
                relative = path.relative_to(self.root).as_posix()
                path = safe_child(self.root, relative)
                from clio_agent.runtime.storage_access import check_storage_access  # noqa: PLC0415

                check_storage_access(path)
                if len(rows) >= 100_000:
                    raise ValueError("Select a smaller source folder (limit 100,000 entries)")
                if os.path.isdir(win_extended_path(path)):
                    rows.append(FileEntry(path=relative, kind="directory"))
                elif os.path.isfile(win_extended_path(path)):
                    digest = file_hash(path)
                    rows.append(
                        FileEntry(
                            path=relative,
                            kind="file",
                            size=os.stat(win_extended_path(path)).st_size,
                            revision=digest,
                            sha256=digest,
                        )
                    )
                else:
                    raise ValueError("Sources may contain only regular files and directories")
        return sorted(rows, key=lambda row: row.path)

    def open_read(self, entry: FileEntry) -> BinaryIO:
        """Open a selected regular file; reject changes from file to link."""
        return open(win_extended_path(safe_child(self.root, entry.path)), "rb")

    def apply(self, path: str, content: Path | None, expected: str | None) -> str | None:
        """Check the current hash immediately before a per-file atomic replacement.

        This is optimistic conflict detection, not filesystem compare-and-swap or
        directory-wide atomicity; another application can still race this check.
        """
        if not self.writable:
            raise PermissionError("This source has not been approved for writes")
        destination = safe_child(self.root, path)
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
