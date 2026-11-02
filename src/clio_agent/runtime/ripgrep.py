"""Pinned official ripgrep, installed privately for the file search and glob tools.

:func:`ripgrep_executable` resolves the binary the fs tools run: a verified managed
install, then an ``rg`` on the host PATH, then a verified download of the pinned
release. Package preparation installs it ahead of time (:func:`ensure_ripgrep`). When
none is possible the tools fail with the typed ``ripgrep_missing`` reason; there is no
slower Python search behind it.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import sys
import sysconfig
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

import httpx

from clio_agent import conf, paths

__all__ = [
    "RIPGREP_MISSING",
    "VERSION",
    "RipgrepMissing",
    "ensure_ripgrep",
    "installed_ripgrep",
    "max_bytes",
    "ripgrep_executable",
]

VERSION = "14.1.1"
# Official BurntSushi/ripgrep release asset digests, pinned with the version.
ASSETS = {
    ("win32", "amd64"): (
        "x86_64-pc-windows-msvc.zip",
        "d0f534024c42afd6cb4d38907c25cd2b249b79bbe6cc1dbee8e3e37c2b6e25a1",
    ),
    ("linux", "amd64"): (
        "x86_64-unknown-linux-musl.tar.gz",
        "4cf9f2741e6c465ffdb7c26f38056a59e2a2544b51f7cc128ef28337eeae4d8e",
    ),
    ("linux", "arm64"): (
        "aarch64-unknown-linux-gnu.tar.gz",
        "c827481c4ff4ea10c9dc7a4022c8de5db34a5737cb74484d62eb94a95841ab2f",
    ),
    ("darwin", "amd64"): (
        "x86_64-apple-darwin.tar.gz",
        "fc87e78f7cb3fea12d69072e7ef3b21509754717b746368fd40d88963630e2b3",
    ),
    ("darwin", "arm64"): (
        "aarch64-apple-darwin.tar.gz",
        "24ad76777745fbff131c8fbc466742b011f925bfa4fffa2ded6def23b5b937be",
    ),
}
#: Typed reason when no ripgrep can be found or installed.
RIPGREP_MISSING = "ripgrep_missing"
_LOCK = threading.RLock()


def max_bytes() -> int:
    """Largest ripgrep download, and executable in it, CLIO accepts (``fs.search_max_bytes``).

    Raises:
        ValueError: The configured size is below 1.
    """
    value = conf.resolve(
        "fs.search_max_bytes",
        env="CLIO_FS_SEARCH_MAX_BYTES",
        default=32 * 1024 * 1024,
        cast=conf.as_int,
    )
    if value < 1:
        raise ValueError(f"fs.search_max_bytes must be at least 1, not {value}")
    return value


class RipgrepMissing(RuntimeError):
    """No verified or host ripgrep is available (reason :data:`RIPGREP_MISSING`)."""

    reason = RIPGREP_MISSING


def _binary(archive: bytes, name: str) -> bytes:
    """Read only the expected executable; never extract archive paths or links."""
    expected = "rg.exe" if name.endswith(".zip") else "rg"
    limit = max_bytes()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            members = [m for m in package.infolist() if m.filename.endswith("/" + expected)]
            if len(members) != 1 or members[0].file_size > limit:
                raise ValueError("ripgrep archive has no unique bounded executable")
            return package.read(members[0])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        found = [m for m in package.getmembers() if m.name.endswith("/rg") and m.isfile()]
        if len(found) != 1 or found[0].size > limit:
            raise ValueError("ripgrep archive has no unique bounded executable")
        stream = package.extractfile(found[0])
        if stream is None:
            raise ValueError("ripgrep executable could not be read")
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
        raise RuntimeError(f"Managed ripgrep is unavailable on {sys.platform}/{machine}") from exc
    root = (cache_root or paths.user_cache_dir() / "ripgrep") / VERSION / suffix.split(".")[0]
    executable = root / ("rg.exe" if sys.platform == "win32" else "rg")
    return suffix, digest, executable


def installed_ripgrep(*, cache_root: Path | None = None) -> Path | None:
    """Find an already verified managed ripgrep without downloading."""
    try:
        _, _, executable = _installation(cache_root)
    except RuntimeError:
        return None
    receipt = executable.parent / "executable.sha256"
    with _LOCK:
        try:
            if not executable.is_file() or not receipt.is_file():
                return None
            expected = receipt.read_text(encoding="utf-8").strip()
            if hashlib.sha256(executable.read_bytes()).hexdigest() == expected:
                return executable
        except (OSError, UnicodeError):
            return None
    return None


def ensure_ripgrep(*, cache_root: Path | None = None) -> Path:
    """Install the pinned ripgrep from verified official bytes, or reuse those bytes."""
    suffix, digest, executable = _installation(cache_root)
    root = executable.parent
    with _LOCK:
        if installed_ripgrep(cache_root=cache_root) is not None:
            return executable
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"ripgrep-{VERSION}-{suffix}"
        url = f"https://github.com/BurntSushi/ripgrep/releases/download/{VERSION}/{name}"
        chunks, limit = bytearray(), max_bytes()
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ValueError("ripgrep download exceeds its size limit")
        if hashlib.sha256(chunks).hexdigest() != digest:
            raise ValueError("ripgrep download failed its pinned SHA-256 check")
        binary = _binary(bytes(chunks), suffix)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as temporary:
            temporary.write(binary)
            staged = Path(temporary.name)
        try:
            staged.chmod(0o700)
            os.replace(staged, executable)
        finally:
            staged.unlink(missing_ok=True)
        (root / "executable.sha256").write_text(
            hashlib.sha256(binary).hexdigest() + "\n", encoding="utf-8"
        )
        return executable


def ripgrep_executable() -> Path:
    """The ripgrep the fs tools run: managed, then host, then a verified download."""
    managed = installed_ripgrep()
    if managed is not None:
        return managed
    host = shutil.which("rg")
    if host:
        return Path(host)
    try:
        return ensure_ripgrep()
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        raise RipgrepMissing(
            f"{RIPGREP_MISSING}: no ripgrep is installed and the pinned release could not be "
            f"installed ({type(exc).__name__}: {exc})"
        ) from exc


if __name__ == "__main__":
    print(ensure_ripgrep())
