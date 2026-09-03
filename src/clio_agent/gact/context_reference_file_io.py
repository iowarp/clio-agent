"""Bounded file reads used by structured context-reference delivery."""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from clio_agent.gact.context_reference_domain import BoundedFileSnapshot
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES

_FILE_CHUNK_BYTES = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Hash one file without retaining it in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_FILE_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def read_bounded_file(path: Path) -> BoundedFileSnapshot:
    """Read at most the context limit while hashing the same file-handle stream."""

    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_FILE_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
            remaining = _CTX_MAX_BYTES - len(prefix)
            if remaining > 0:
                prefix.extend(chunk[:remaining])
    return BoundedFileSnapshot(
        data=bytes(prefix),
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def file_media_type(path: Path) -> str:
    """Return the best available media type for a workspace file."""

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"
