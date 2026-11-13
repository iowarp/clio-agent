"""Unpack npm packages for the document stack's JavaScript tools."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

from filelock import FileLock

NPM_ARCHIVE = "npm.tar"


def managed_npm(node_root: Path) -> Path:
    """The managed Node.js package's ``npm-cli.js``, laid out from its archive on first use.

    A Windows runtime pack carries npm's ~1,400 files as one ``lib/node_modules/npm.tar``
    (``install/prune_documents.py``): only the pnpm fallback and the Obsidian client's
    install run npm, so an installation does not write them and Defender does not scan
    them. A package without the archive (another OS, a wheel install) is returned as is.
    """
    modules = node_root / "lib" / "node_modules"
    npm = modules / "npm" / "bin" / "npm-cli.js"
    archive = modules / NPM_ARCHIVE
    if npm.is_file() or not archive.is_file():
        return npm
    with FileLock(str(modules / "npm.lock"), timeout=600):
        if npm.is_file():
            return npm
        target = modules / "npm"
        with tempfile.TemporaryDirectory(prefix=".npm-", dir=modules) as temporary:
            staging = Path(temporary)
            with tarfile.open(archive) as source:
                source.extractall(staging, filter="data")
            if target.exists():
                shutil.rmtree(target)
            (staging / "npm").rename(target)
    return npm


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
