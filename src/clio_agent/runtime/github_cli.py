"""Pinned official GitHub CLI, installed privately without installer payload growth."""

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path

import httpx

from clio_agent import paths

VERSION = "2.102.0"
# Official cli/cli release asset digests, pinned with the version.
ASSETS = {
    ("win32", "amd64"): (
        "windows_amd64.zip",
        "ae64e556ecc240b200f7eba60d550e4bb60d78e860e69dd88c449405b86067f4",
    ),
    ("win32", "arm64"): (
        "windows_arm64.zip",
        "5dcf12aa8525eabd0c46ec414f323ab6cf65229fc2cd46543cc705001bbaf223",
    ),
    ("linux", "amd64"): (
        "linux_amd64.tar.gz",
        "bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386",
    ),
    ("linux", "arm64"): (
        "linux_arm64.tar.gz",
        "7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484",
    ),
    ("darwin", "amd64"): (
        "macOS_amd64.zip",
        "b245f24eb2bf5f75b426b4c26da3651a107f8d5b6f4fddfbfccc5679041378b3",
    ),
    ("darwin", "arm64"): (
        "macOS_arm64.zip",
        "da922c20d1792e5b2cbf375593d7a658acf034c12c84e007e71c76ef959c337e",
    ),
}
_LOCK = threading.RLock()
_MAX_BYTES = 64 * 1024 * 1024


def _binary(archive: bytes, name: str) -> bytes:
    """Read only the expected executable; never extract archive paths or links."""
    expected = "gh.exe" if name.startswith("windows") else "gh"
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_package:
            zip_members = [
                m
                for m in zip_package.infolist()
                if m.filename.endswith("/bin/" + expected) or m.filename == "bin/" + expected
            ]
            if len(zip_members) != 1 or zip_members[0].file_size > _MAX_BYTES:
                raise ValueError("GitHub CLI archive has no unique bounded executable")
            return zip_package.read(zip_members[0])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar_package:
        tar_members = [
            m for m in tar_package.getmembers() if m.name.endswith("/bin/gh") and m.isfile()
        ]
        if len(tar_members) != 1 or tar_members[0].size > _MAX_BYTES:
            raise ValueError("GitHub CLI archive has no unique bounded executable")
        stream = tar_package.extractfile(tar_members[0])
        if stream is None:
            raise ValueError("GitHub CLI executable could not be read")
        return stream.read()


def ensure_github_cli(*, cache_root: Path | None = None) -> Path:
    """Install the pinned CLI from verified official bytes, or reuse those bytes."""
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
        raise RuntimeError(
            f"Managed GitHub CLI is unavailable on {sys.platform}/{machine}"
        ) from exc
    root = (
        (cache_root or paths.user_cache_dir() / "github-cli")
        / VERSION
        / suffix.removesuffix(".tar.gz").removesuffix(".zip")
    )
    executable = root / ("gh.exe" if sys.platform == "win32" else "gh")
    receipt = root / "executable.sha256"
    with _LOCK:
        if (
            executable.is_file()
            and receipt.is_file()
            and hashlib.sha256(executable.read_bytes()).hexdigest() == receipt.read_text().strip()
        ):
            return executable
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"gh_{VERSION}_{suffix}"
        url = f"https://github.com/cli/cli/releases/download/v{VERSION}/{name}"
        with httpx.Client(follow_redirects=True, timeout=120) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_BYTES:
                        raise ValueError("GitHub CLI download exceeds its size limit")
        if hashlib.sha256(chunks).hexdigest() != digest:
            raise ValueError("GitHub CLI download failed its pinned SHA-256 check")
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


def run_github_cli(
    arguments: list[str], *, token: str, config_root: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke verified gh with one private account grant and no host CLI credentials."""
    config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
    environment.update(
        {
            "GH_CONFIG_DIR": str(config_root),
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "NO_COLOR": "1",
        }
    )
    if token:
        environment["GH_TOKEN"] = token
    completed = subprocess.run(
        [str(ensure_github_cli()), *arguments],
        env=environment,
        cwd=config_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    # A provider error must never turn a grant into transcript content.
    if token:
        completed.stdout = completed.stdout.replace(token, "[redacted]")
        completed.stderr = completed.stderr.replace(token, "[redacted]")
    return completed


if __name__ == "__main__":
    print(ensure_github_cli())
