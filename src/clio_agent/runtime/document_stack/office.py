"""Provision a pinned, private LibreOffice without a machine-wide installation."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import ssl
import sysconfig
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import truststore
from filelock import FileLock

if TYPE_CHECKING or __package__:
    from .process import DocumentError, run
else:
    from process import DocumentError, run

VERSION = "26.2.6"
# Published by The Document Foundation at each download URL's .sha256 endpoint.
ASSETS = {
    ("Windows", "x86_64"): (
        "win/x86_64",
        "Win_x86-64.msi",
        "f9877032fd908beb9c0ddf06df4af5c2e85f419c42e14876c4cce5aae5fb2660",
    ),
    ("Windows", "aarch64"): (
        "win/aarch64",
        "Win_aarch64.msi",
        "13be874da62665756b6d7c8a6afbc50e6465382a885ad90acf4dbe806d51db24",
    ),
    ("Linux", "x86_64"): (
        "deb/x86_64",
        "Linux_x86-64_deb.tar.gz",
        "fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc",
    ),
    ("Linux", "aarch64"): (
        "deb/aarch64",
        "Linux_aarch64_deb.tar.gz",
        "f8e8b1d30abde0d530d727ce1b26909ccbedc3d76bcf75d93b3dd5fcd5b8d278",
    ),
    ("Darwin", "x86_64"): (
        "mac/x86_64",
        "MacOS_x86-64.dmg",
        "135b8a95b8133396d54bf8e726dbc0066145efa0d785963fc3d4592acbfcfe5b",
    ),
    ("Darwin", "aarch64"): (
        "mac/aarch64",
        "MacOS_aarch64.dmg",
        "94bb3248df074c225490a8a6d1d9dc87c7d6783dbb7a8e9f0d0c3d94348552af",
    ),
}
MAX_DOWNLOAD = 600 * 1024 * 1024


def asset() -> tuple[str, str, str]:
    """Return this host's fixed official download URL, filename and SHA-256."""
    machine = platform.machine().lower() or sysconfig.get_platform().split("-")[-1]
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    key = (platform.system(), architecture)
    if key not in ASSETS:
        raise DocumentError(
            f"No private LibreOffice build for {key}; configure CLIO_DOCUMENT_SOFFICE"
        )
    directory, suffix, checksum = ASSETS[key]
    name = f"LibreOffice_{VERSION}_{suffix}"
    url = f"https://download.documentfoundation.org/libreoffice/stable/{VERSION}/{directory}/{name}"
    return url, name, checksum


def executable(root: Path) -> Path | None:
    """Find an executable only inside a private renderer tree."""
    candidates = [
        root / "program" / "soffice.com",
        root / "LibreOffice" / "program" / "soffice.com",
        root / "LibreOffice.app" / "Contents" / "MacOS" / "soffice",
        root / "opt" / "libreoffice26.2" / "program" / "soffice",
    ]
    return next((path for path in candidates if path.is_file()), None)


def _download(url: str, target: Path, checksum: str) -> None:
    digest = hashlib.sha256()
    count = 0
    started = time.monotonic()
    try:
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            response = urllib.request.urlopen(url, timeout=30, context=context)
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 410}:
                raise
            archive = url.replace(
                "download.documentfoundation.org", "downloadarchive.documentfoundation.org"
            ).replace(f"/stable/{VERSION}/", f"/old/{VERSION}.2/")
            response = urllib.request.urlopen(archive, timeout=30, context=context)
        with response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_DOWNLOAD:
                    raise DocumentError("LibreOffice download exceeds its size limit")
                if time.monotonic() - started > 600:
                    raise DocumentError("LibreOffice download exceeded 600 seconds")
                digest.update(chunk)
                output.write(chunk)
    except OSError as exc:
        raise DocumentError(f"LibreOffice download failed: {exc}") from exc
    if digest.hexdigest() != checksum:
        raise DocumentError("LibreOffice download failed its pinned SHA-256 check")


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


def _extract_linux(archive: Path, root: Path) -> None:
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        packages = Path(temporary)
        with tarfile.open(archive) as outer:
            outer.extractall(packages, filter="data")
        for package in sorted(packages.rglob("*.deb")):
            with package.open("rb") as source:
                payload = _deb_data(source)
            with tarfile.open(fileobj=io.BytesIO(payload)) as contents:
                # Only the self-contained /opt application, not distro integration.
                members = [
                    member
                    for member in contents
                    if member.name.removeprefix("./").startswith("opt/")
                ]
                contents.extractall(root, members=members, filter="data")


def _extract(archive: Path, root: Path) -> None:
    if platform.system() == "Windows":
        msiexec = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "msiexec.exe"
        # /a builds an administrative IMAGE; /qn neither installs nor opens UI.
        run([str(msiexec), "/a", str(archive), "/qn", f"TARGETDIR={root}"], cwd=root, timeout=300)
    elif platform.system() == "Darwin":
        with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
            mount = Path(temporary)
            run(
                [
                    "/usr/bin/hdiutil",
                    "attach",
                    str(archive),
                    "-readonly",
                    "-nobrowse",
                    "-mountpoint",
                    str(mount),
                ],
                cwd=root,
                timeout=120,
            )
            try:
                shutil.copytree(mount / "LibreOffice.app", root / "LibreOffice.app", symlinks=True)
            finally:
                run(["/usr/bin/hdiutil", "detach", str(mount)], cwd=root, timeout=30)
    else:
        _extract_linux(archive, root)


def ensure_office(root: Path) -> str:
    """Download, verify and extract the renderer once under an owned cache root.

    The tree includes LibreOffice's license/notices. Linux still needs the OS
    runtime libraries required by LibreOffice; a missing library is a typed error.
    """
    root = root.resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root.parent / f"{root.name}.lock"), timeout=600):
        existing = executable(root)
        if existing:
            run([str(existing), "--version"], cwd=root, timeout=30)
            return str(existing)
        url, name, checksum = asset()
        downloads = root.parent / "downloads"
        downloads.mkdir(exist_ok=True)
        archive = downloads / name
        cached = False
        if archive.is_file():
            with archive.open("rb") as stream:
                cached = hashlib.file_digest(stream, "sha256").hexdigest() == checksum
        if not cached:
            partial = archive.with_suffix(archive.suffix + ".part")
            _download(url, partial, checksum)
            partial.replace(archive)
        with tempfile.TemporaryDirectory(prefix="office-extract-", dir=root.parent) as temporary:
            staging = Path(temporary)
            image = staging / "image"
            image.mkdir()
            try:
                _extract(archive, image)
                candidate = executable(image)
                if candidate is None:
                    raise DocumentError("LibreOffice image contains no supported executable")
                run([str(candidate), "--version"], cwd=image, timeout=30)
                if root.exists():
                    raise DocumentError(
                        f"Incomplete renderer cache at {root}; remove it before retrying"
                    )
                image.rename(root)
            except (OSError, ValueError, tarfile.TarError) as exc:
                raise DocumentError(f"LibreOffice extraction failed: {exc}") from exc
        candidate = executable(root)
        if candidate is None:
            raise DocumentError("LibreOffice renderer disappeared after provisioning")
        archive.unlink(missing_ok=True)
        return str(candidate)
