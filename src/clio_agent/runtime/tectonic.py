"""Pinned official Tectonic, installed privately for LaTeX builds (LaTeX ledger L1, L2).

:func:`tectonic_executable` resolves the binary a build runs: an explicit
``CLIO_DOCUMENT_TECTONIC`` override, then a verified managed install, then a verified
download of the pinned release. Package preparation installs it ahead of time
(:func:`ensure_tectonic`). A host Tectonic or TeX Live is never preferred over the pinned
one, so a paper builds the same way on every machine. When none is possible the build
fails with the typed ``tectonic_missing`` reason; there is no pandoc path for ``.tex``.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import sys
import sysconfig
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

# httpx imports its transport (httpcore, anyio) on the first Client; the installer's worker
# threads doing that at once deadlock on import (_DeadlockError), so load it here.
import httpcore  # noqa: F401
import httpx

from clio_agent import paths

__all__ = [
    "BUNDLE_HOSTS",
    "BUNDLE_URL",
    "TECTONIC_MISSING",
    "VERSION",
    "TectonicMissing",
    "ensure_tectonic",
    "installed_tectonic",
    "package_cache_dir",
    "tectonic_executable",
]

VERSION = "0.17.0"
# Official tectonic-typesetting/tectonic release asset digests, pinned with the version.
ASSETS = {
    ("linux", "amd64"): (
        "x86_64-unknown-linux-musl.tar.gz",
        "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7",
    ),
    ("linux", "arm64"): (
        "aarch64-unknown-linux-musl.tar.gz",
        "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8",
    ),
    ("darwin", "amd64"): (
        "x86_64-apple-darwin.tar.gz",
        "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80",
    ),
    ("darwin", "arm64"): (
        "aarch64-apple-darwin.tar.gz",
        "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667",
    ),
    ("win32", "amd64"): (
        "x86_64-pc-windows-msvc.zip",
        "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f",
    ),
}
# The versioned bundle a build always names (``--bundle``), and the hosts it is served from:
# the relay redirects to the data host (ledger L2, L6).
BUNDLE_URL = "https://relay.fullyjustified.net/default_bundle_v33.tar"
BUNDLE_HOSTS = ("relay.fullyjustified.net", "data1b.fullyjustified.net")

TECTONIC_MISSING = "tectonic_missing"

_LOCK = threading.RLock()
_MAX_DOWNLOAD = 48 * 1024 * 1024
_MAX_BINARY = 96 * 1024 * 1024


class TectonicMissing(RuntimeError):
    """No verified Tectonic is available, with the typed reason ``tectonic_missing``."""

    reason = TECTONIC_MISSING


def _binary(archive: bytes, name: str) -> bytes:
    """Read only the expected executable; never extract archive paths or links."""
    expected = "tectonic.exe" if name.endswith(".zip") else "tectonic"
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_package:
            zip_members = [m for m in zip_package.infolist() if m.filename == expected]
            if len(zip_members) != 1 or zip_members[0].file_size > _MAX_BINARY:
                raise ValueError("Tectonic archive has no unique bounded executable")
            return zip_package.read(zip_members[0])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar_package:
        tar_members = [m for m in tar_package.getmembers() if m.name == expected and m.isfile()]
        if len(tar_members) != 1 or tar_members[0].size > _MAX_BINARY:
            raise ValueError("Tectonic archive has no unique bounded executable")
        stream = tar_package.extractfile(tar_members[0])
        if stream is None:
            raise ValueError("Tectonic executable could not be read")
        return stream.read()


def _installation(cache_root: Path | None) -> tuple[str, str, Path]:
    machine = (platform.machine() or sysconfig.get_platform().rsplit("-", 1)[-1]).lower()
    architecture = (
        "arm64"
        if machine in {"aarch64", "arm64"}
        else "amd64"
        if machine in {"x86_64", "amd64"}
        else machine
    )
    try:
        suffix, digest = ASSETS[(sys.platform, architecture)]
    except KeyError as exc:
        raise TectonicMissing(
            f"Managed Tectonic is unavailable on {sys.platform}/{machine}"
        ) from exc
    root = (
        (cache_root or paths.user_cache_dir() / "tectonic")
        / VERSION
        / suffix.removesuffix(".tar.gz").removesuffix(".zip")
    )
    executable = root / ("tectonic.exe" if sys.platform == "win32" else "tectonic")
    return suffix, digest, executable


def package_cache_dir(*, cache_root: Path | None = None) -> Path:
    """Where Tectonic keeps the bundle index, formats and fetched packages (TECTONIC_CACHE_DIR)."""
    return (cache_root or paths.user_cache_dir() / "tectonic") / "packages"


def installed_tectonic(*, cache_root: Path | None = None) -> Path | None:
    """Find an already verified managed Tectonic without downloading it."""
    try:
        _, _, executable = _installation(cache_root)
    except TectonicMissing:
        return None
    receipt = executable.parent / "executable.sha256"
    with _LOCK:
        try:
            if not executable.is_file() or not receipt.is_file():
                return None
            if not 0 < executable.stat().st_size <= _MAX_BINARY:
                return None
            expected = receipt.read_text(encoding="utf-8").strip()
            if hashlib.sha256(executable.read_bytes()).hexdigest() == expected:
                return executable
        except (OSError, UnicodeError):
            # An absent/unreadable cache is an install to repeat, not a build failure.
            return None
    return None


def ensure_tectonic(*, cache_root: Path | None = None) -> Path:
    """Install the pinned Tectonic from verified official bytes, or reuse those bytes."""
    suffix, digest, executable = _installation(cache_root)
    root = executable.parent
    receipt = root / "executable.sha256"
    with _LOCK:
        if installed_tectonic(cache_root=cache_root) is not None:
            return executable
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"tectonic-{VERSION}-{suffix}"
        url = (
            "https://github.com/tectonic-typesetting/tectonic/releases/download/"
            f"tectonic%40{VERSION}/{name}"
        )
        with httpx.Client(follow_redirects=True, timeout=180) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_DOWNLOAD:
                        raise ValueError("Tectonic download exceeds its size limit")
        if hashlib.sha256(chunks).hexdigest() != digest:
            raise ValueError("Tectonic download failed its pinned SHA-256 check")
        binary = _binary(bytes(chunks), suffix)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
            temporary.write(binary)
            staged = Path(temporary.name)
        try:
            staged.chmod(0o700)
            os.replace(staged, executable)
        finally:
            staged.unlink(missing_ok=True)
        receipt.write_text(hashlib.sha256(binary).hexdigest() + "\n", encoding="utf-8")
        return executable


def tectonic_executable(*, cache_root: Path | None = None) -> Path:
    """The Tectonic a build runs: the explicit override, the managed install, or a download.

    Raises:
        TectonicMissing: no override, no verified install, and the pinned download failed.
    """
    override = os.environ.get("CLIO_DOCUMENT_TECTONIC", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise TectonicMissing("CLIO_DOCUMENT_TECTONIC is not an executable file")
        return candidate
    if (installed := installed_tectonic(cache_root=cache_root)) is not None:
        return installed
    try:
        return ensure_tectonic(cache_root=cache_root)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise TectonicMissing(
            f"the pinned Tectonic {VERSION} could not be installed: {exc}"
        ) from exc


if __name__ == "__main__":
    print(ensure_tectonic())
