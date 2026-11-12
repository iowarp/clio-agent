"""Unpack one npm package tarball for the document stack's JavaScript tools."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path


def unpack_package(archive: Path, target: Path) -> None:
    """Unpack one npm tarball's ``package/`` as ``target``, replacing what was there."""
    from clio_agent.runtime.document_runtime import DocumentRuntimeError  # noqa: PLC0415 - cycle

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".unpack-", dir=target.parent) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(staging, filter="data")
        unpacked = staging / "package"
        if not (unpacked / "package.json").is_file():
            raise DocumentRuntimeError(f"{archive.name} has no npm package")
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        unpacked.rename(target)
