"""Unpack the prepared Office tree the installer downloads (on Windows, while it arrives)."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import sys
import tarfile
import time
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, cast

import zstandard

if TYPE_CHECKING or __package__:
    from . import office_compat
    from .process import DocumentError
else:
    import office_compat
    from process import DocumentError

logger = logging.getLogger(__name__)

#: Beside a Windows prepared tree's download: the SHA-256 its release's install plan
#: names, which that release's SHA256SUMS authenticates (``install.ps1``).
PREPARED_DIGEST_SUFFIX = ".sha256"
#: Parallel file writers for a Windows prepared tree. Defender scans each file as it is
#: closed, so one writer leaves the disk and the scanner idle between files.
_WINDOWS_WRITERS = 4
#: Decompressed file bytes held for the writers at once.
_WINDOWS_WRITE_BUDGET = 64 * 1024 * 1024


def unpack_prepared_tree(archive: Path, image: Path, max_unpacked: int) -> None:
    """Unpack the Linux tree (its userland laid in) into ``image``."""
    with (
        archive.open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as stream,
        tarfile.open(fileobj=stream, mode="r|") as contents,
    ):

        def limited() -> Iterator[tarfile.TarInfo]:
            unpacked = 0
            for member in contents:
                unpacked += member.size
                if unpacked > max_unpacked:
                    raise DocumentError("Prepared Office tree exceeds its unpacked size limit")
                yield member

        contents.extractall(image, members=limited(), filter="data")
    if not (image / office_compat.runtime_directory() / office_compat.loader()).is_file():
        raise DocumentError("Prepared Office tree does not hold this release's userland")


def expected_digest(candidate: Path) -> str:
    """The SHA-256 the installer wrote beside ``candidate``, or ``""``."""
    try:
        text = candidate.with_name(candidate.name + PREPARED_DIGEST_SUFFIX).read_text("ascii")
    except (OSError, UnicodeDecodeError):
        return ""
    digest = text.strip().lower()
    return digest if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest) else ""


def _open_shared(path: Path) -> io.FileIO:
    """Open ``path`` for reading without blocking the installer's rename or delete of it."""
    if sys.platform != "win32":
        # Unreachable (only the Windows unpack reads this way); narrows sys.platform so
        # the type-checker admits the Windows-only ctypes/msvcrt API on other platforms.
        raise OSError(f"shared-delete open is Windows-only: {path}")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    generic_read, share_all, open_existing, normal = 0x80000000, 0x7, 3, 0x80
    handle = kernel32.CreateFileW(
        str(path), generic_read, share_all, None, open_existing, normal, None
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    return os.fdopen(descriptor, "rb", buffering=0)


class _GrowingDownload(io.RawIOBase):
    """The installer's download read while it arrives, hashed as it is read.

    Reads ``NAME.part`` (or ``NAME`` once renamed) and waits at its end for more bytes.
    The end is reached when ``NAME`` exists and every byte of it has been read; a
    download that disappears or stops growing for ``stall_s`` is an error.
    """

    def __init__(self, final: Path, stall_s: float) -> None:
        super().__init__()
        self._final = final
        self._partial = final.with_name(final.name + ".part")
        self._stall_s = stall_s
        self._stream: io.FileIO | None = None
        self._read = 0
        self._changed = time.monotonic()
        self.hash = hashlib.sha256()
        #: Seconds spent at the end of the bytes so far, waiting for the download.
        self.waited = 0.0

    def readable(self) -> bool:
        return True

    def _size(self, path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        while True:
            if self._stream is None:
                for path in (self._partial, self._final):
                    try:
                        self._stream = _open_shared(path)
                        break
                    except OSError:
                        continue
            if self._stream is not None:
                count = self._stream.readinto(buffer) or 0
                if count:
                    self.hash.update(buffer[:count])
                    self._read += count
                    self._changed = time.monotonic()
                    return count
            final = self._size(self._final)
            if final is not None and final == self._read and self._size(self._partial) is None:
                return 0
            if final is None and self._size(self._partial) is None:
                raise OSError(f"the installer's download of {self._final.name} was removed")
            if time.monotonic() - self._changed > self._stall_s:
                raise OSError(f"the installer's download of {self._final.name} stalled")
            time.sleep(0.05)
            self.waited += 0.05

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


def _unpack_parallel(contents: tarfile.TarFile, image: Path, max_unpacked: int) -> int:
    """Extract a streamed tar of plain files and folders into ``image`` with parallel writers.

    Folders are made in order; each file's bytes are handed to a writer thread, so the
    antivirus scan on close overlaps the next files. Returns the number of files.
    """

    def write(path: Path, data: bytes) -> None:
        with path.open("wb") as handle:
            handle.write(data)

    destination = str(image)
    made: set[Path] = {image}
    pending: deque[tuple[Future[None], int]] = deque()
    held = unpacked = files = 0
    with ThreadPoolExecutor(_WINDOWS_WRITERS, thread_name_prefix="office-unpack") as writers:
        for member in contents:
            safe = tarfile.data_filter(member, destination)
            target = image.joinpath(*PurePosixPath(safe.name).parts)
            if safe.isdir():
                target.mkdir(parents=True, exist_ok=True)
                made.add(target)
                continue
            if not safe.isfile():
                raise DocumentError(f"Prepared Office tree holds a non-file member: {member.name}")
            unpacked += safe.size
            if unpacked > max_unpacked:
                raise DocumentError("Prepared Office tree exceeds its unpacked size limit")
            if target.parent not in made:
                target.parent.mkdir(parents=True, exist_ok=True)
                made.add(target.parent)
            source = contents.extractfile(member)
            data = source.read() if source is not None else b""
            while pending and (held + len(data) > _WINDOWS_WRITE_BUDGET or pending[0][0].done()):
                future, size = pending.popleft()
                future.result()
                held -= size
            pending.append((writers.submit(write, target, data), len(data)))
            held += len(data)
            files += 1
        for future, _ in pending:
            future.result()
    return files


def unpack_windows_tree(prepared: Path, image: Path, *, max_unpacked: int, stall_s: float) -> float:
    """Unpack the Windows tree as it downloads; raise unless its bytes match the plan.

    Returns the seconds spent waiting for the download's next bytes.
    """
    started = time.monotonic()
    expected = expected_digest(prepared)
    if not expected:
        raise DocumentError("Prepared Office tree has no SHA-256 from the install plan")
    with _GrowingDownload(prepared, stall_s) as raw:
        with (
            zstandard.ZstdDecompressor().stream_reader(
                cast(BinaryIO, raw), closefd=False
            ) as stream,
            tarfile.open(fileobj=stream, mode="r|") as contents,
        ):
            files = _unpack_parallel(contents, image, max_unpacked)
        while raw.read(1 << 20):
            pass
        actual = raw.hash.hexdigest()
        waited = raw.waited
    if actual != expected:
        raise DocumentError(f"Prepared Office tree {prepared.name} does not match its SHA-256")
    logger.info(
        "reason=office_prepared_tree_unpacked name=%s files=%d seconds=%.1f waited=%.1f",
        prepared.name,
        files,
        time.monotonic() - started,
        waited,
    )
    return waited
