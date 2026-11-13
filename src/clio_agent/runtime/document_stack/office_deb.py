"""Unpack LibreOffice's Linux release: the /opt application of each .deb in its archive."""

from __future__ import annotations

import io
import lzma
import os
import tarfile
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING or __package__:
    from .office_fetch import MAX_DOWNLOAD
    from .process import DocumentError
else:
    from office_fetch import MAX_DOWNLOAD
    from process import DocumentError


def _deb_data(stream: BinaryIO) -> bytes:
    if stream.read(8) != b"!<arch>\n":
        raise DocumentError("Invalid LibreOffice Debian archive")
    while header := stream.read(60):
        if len(header) != 60 or header[58:] != b"`\n":
            raise DocumentError("Invalid Debian archive member header")
        size = int(header[48:58])
        if size < 0 or size > MAX_DOWNLOAD:
            raise DocumentError("Debian member exceeds its size limit")
        payload = stream.read(size)
        if len(payload) != size:
            raise DocumentError("Truncated Debian archive member")
        if size % 2:
            stream.read(1)
        if header[:16].decode("ascii").strip().rstrip("/").startswith("data.tar."):
            return payload
    raise DocumentError("Debian archive contains no data payload")


_XZ_MAGIC = b"\xfd7zXZ\x00"


def _deb_tar(package: Path) -> bytes:
    """A package's data tar, xz-decompressed (lzma releases the GIL, so threads overlap).

    Other compressions are returned as they are; ``tarfile`` opens them itself.
    """
    with package.open("rb") as source:
        payload = _deb_data(source)
    return lzma.decompress(payload) if payload.startswith(_XZ_MAGIC) else payload


def _extract_linux(archive: Path, root: Path) -> None:
    """Unpack the /opt application of every package in the archive into ``root``.

    Decompressing the ~40 packages' xz payloads is most of the time (about 16 of 22 s
    on one core), so a few are decompressed ahead on worker threads while one thread
    writes the files in package order. At most ``2 * workers`` decompressed payloads
    are held at once.
    """
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        packages_root = Path(temporary)
        with tarfile.open(archive) as outer:
            outer.extractall(packages_root, filter="data")
        packages = iter(sorted(packages_root.rglob("*.deb")))
        workers = max(1, min(4, os.cpu_count() or 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="office-unpack") as pool:
            pending: deque[Future[bytes]] = deque()

            def submit_next() -> None:
                package = next(packages, None)
                if package is not None:
                    pending.append(pool.submit(_deb_tar, package))

            for _ in range(2 * workers):
                submit_next()
            while pending:
                payload = pending.popleft().result()
                submit_next()
                with tarfile.open(fileobj=io.BytesIO(payload)) as contents:
                    # Only the self-contained /opt application, not distro integration.
                    members = [
                        member
                        for member in contents
                        if member.name.removeprefix("./").startswith("opt/")
                    ]
                    contents.extractall(root, members=members, filter="data")
