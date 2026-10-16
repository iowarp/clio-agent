"""Verified downloads and portable extraction for owned native service installers."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.request import urlopen


def download(url: str, checksum: str, cache: Path) -> Path:
    """Cache one HTTPS artifact, accepting it only after its pinned SHA256 matches."""
    if not url.startswith("https://") or len(checksum) != 64:
        raise ValueError("Native downloads require HTTPS and a pinned SHA256")
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / checksum
    if path.is_symlink():
        raise ValueError("Native download cache cannot contain symlinks")
    if path.is_file() and digest(path) == checksum:
        return path
    temporary = path.with_suffix(".part")
    if temporary.is_symlink():
        raise ValueError("Native download staging cannot be a symlink")
    with urlopen(url, timeout=90) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)
    if digest(temporary) != checksum:
        temporary.unlink()
        raise ValueError("Native download does not match its pinned SHA256")
    temporary.replace(path)
    return path


def digest(path: Path) -> str:
    """Hash the complete artifact without retaining its bytes in memory."""
    with path.open("rb") as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        return checksum.hexdigest()


def _destination(root: Path, name: str) -> Path:
    parts = PurePosixPath(name.replace("\\", "/"))
    if parts.is_absolute() or ".." in parts.parts or any(":" in part for part in parts.parts):
        raise ValueError("Archive member escapes its native service directory")
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Archive member traverses a symlink outside its directory")
    return path


def _write(root: Path, name: str, stream: IO[bytes], mode: int = 0o644) -> None:
    path = _destination(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Archive cannot replace a symlink")
    with path.open("wb") as output:
        shutil.copyfileobj(stream, output)
    path.chmod(mode & 0o777)


def extract(archive: Path, destination: Path, *, compression: str = "") -> Path:
    """Extract regular files and confined links, normalizing every Windows separator."""
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or any(destination.iterdir()):
        raise ValueError("Native extraction requires an empty, owned staging directory")
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if member.is_dir():
                    _destination(destination, member.filename).mkdir(parents=True, exist_ok=True)
                else:
                    mode = member.external_attr >> 16
                    if mode & 0o170000 == 0o120000:
                        raise ValueError("Native ZIP archives cannot contain symlinks")
                    with bundle.open(member) as stream:
                        _write(destination, member.filename, stream, mode or 0o644)
    else:
        # Python's tarfile on extended Windows paths otherwise joins POSIX member
        # names directly to a \\?\ root (WinError 123). Build every path ourselves.
        if compression == "zstd":
            import zstandard  # type: ignore[import-not-found]  # noqa: PLC0415

            with (
                archive.open("rb") as compressed,
                zstandard.ZstdDecompressor().stream_reader(compressed) as stream,
            ):
                _tar(stream, destination, "r|")
        else:
            with archive.open("rb") as stream:
                _tar(stream, destination, "r:*")
    children = list(destination.iterdir())
    return children[0] if len(children) == 1 and children[0].is_dir() else destination


def _tar(stream: IO[bytes], destination: Path, mode: str) -> None:
    links: list[tuple[Path, str, bool]] = []
    with tarfile.open(fileobj=stream, mode=mode) as bundle:  # type: ignore[call-overload]
        for member in bundle:
            path = _destination(destination, member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                content = bundle.extractfile(member)
                if content is None:
                    raise ValueError("Archive file has no content")
                with content:
                    _write(destination, member.name, content, member.mode)
                # Preserve source ordering for generated Autotools files. Giving
                # every extracted file a new mtime triggers unneeded regeneration
                # with whatever automake happens to be installed on the host.
                os.utime(path, (member.mtime, member.mtime))
            elif member.issym() or member.islnk():
                links.append((path, member.linkname, member.issym()))
            else:
                raise ValueError("Native archives cannot contain devices or special files")
    while links:
        deferred = []
        for path, target, symbolic in links:
            relative = PurePosixPath(target)
            if relative.is_absolute():
                raise ValueError("Native archive links must be relative")
            resolved = (
                (path.parent if symbolic else destination).joinpath(*relative.parts).resolve()
            )
            if not resolved.is_relative_to(destination.resolve()):
                raise ValueError("Native archive link escapes its directory")
            if not resolved.exists():
                deferred.append((path, target, symbolic))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if symbolic:
                path.symlink_to(os.path.relpath(resolved, path.parent))
            else:
                os.link(resolved, path)
        if len(deferred) == len(links):
            raise ValueError("Native archive link has no target or contains a cycle")
        links = deferred
