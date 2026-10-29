"""Private Linux userland the pinned LibreOffice runs on, so Office works on any host.

TDF's Linux build links against about forty desktop system libraries (X11, cairo,
cups, fontconfig, NSS, glib, ...) and needs glibc 2.34. A headless server, an HPC
node or a minimal container has few of them and often an older glibc. Like CLIO's
own uv and Node, Office therefore brings everything it runs on: the C runtime and
every library it loads, from pinned Ubuntu 22.04 packages
(``office_userland.lock.json``, written by ``scripts/office_userland_lock.py``,
one package set per architecture: x86_64 and aarch64), run through their own
loader. Nothing from the host is mixed in, so there is no
ABI mismatch on a newer host either. Package notices remain in the private tree.
The agent's environment and the system libraries are never replaced.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import platform
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, NamedTuple

import zstandard

if TYPE_CHECKING or __package__:
    from .process import DocumentError
else:
    from process import DocumentError

logger = logging.getLogger(__name__)
LOCK_FILE = Path(__file__).with_name("office_userland.lock.json")


class _Architecture(NamedTuple):
    """One pinned package set (its key in the lock) and the loader it runs through."""

    key: str
    loader: str


_ARCHITECTURES = {
    "x86_64": _Architecture("amd64", "ld-linux-x86-64.so.2"),
    "amd64": _Architecture("amd64", "ld-linux-x86-64.so.2"),
    "aarch64": _Architecture("arm64", "ld-linux-aarch64.so.1"),
    "arm64": _Architecture("arm64", "ld-linux-aarch64.so.1"),
}
UNSUPPORTED = "office_userland_unsupported_architecture"
LOCK_UNREADABLE = "office_userland_lock_unreadable"
_REPORTED: set[str] = set()
WRAPPER = "soffice-clio"
MAX_UNPACKED = 128 * 1024 * 1024
#: fontconfig reads LibreOffice's own fonts; its cache goes to the user's cache directory.
FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir prefix="relative">../opt/libreoffice26.2/share/fonts</dir>
  <cachedir prefix="xdg">clio-office-fontconfig</cachedir>
</fontconfig>
"""


class UnsupportedArchitecture(DocumentError):
    """No private userland is pinned for this host's machine architecture."""

    reason = UNSUPPORTED

    def __init__(self, machine: str) -> None:
        self.machine = machine
        pinned = ", ".join(sorted({a.key for a in _ARCHITECTURES.values()}))
        super().__init__(
            f"reason={UNSUPPORTED} machine={machine or 'unknown'}: Office's private userland "
            f"is pinned for {pinned} only; configure CLIO_DOCUMENT_SOFFICE"
        )


def _lock() -> dict[str, Any]:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def _architecture() -> _Architecture:
    """This host's pinned architecture, or the typed reason there is none."""
    machine = platform.machine().lower()
    architecture = _ARCHITECTURES.get(machine)
    if architecture is None:
        raise UnsupportedArchitecture(machine)
    return architecture


def _entry() -> dict[str, Any]:
    architecture = _architecture()
    entry = _lock()["architectures"].get(architecture.key)
    if entry is None:
        raise UnsupportedArchitecture(platform.machine().lower())
    return entry


def packages() -> list[tuple[str, str]]:
    """This host's pinned packages as (URL, SHA-256)."""
    entry = _entry()
    return [(f"{entry['base']}/{item['path']}", item["sha256"]) for item in entry["packages"]]


def _triplet() -> str:
    return _entry()["triplet"]


def loader() -> Path:
    """The private ELF loader, relative to the userland directory."""
    return Path("lib") / _triplet() / _architecture().loader


def runtime_directory() -> str:
    """The userland's directory name, which changes whenever its pins change."""
    return "userland-" + hashlib.sha256(repr(packages()).encode()).hexdigest()[:12]


def supported() -> bool:
    """Return whether Office runs on its private userland on this host.

    Every Linux host whose architecture is pinned does, whatever its glibc and
    whatever it has installed. Any other Linux host is reported once with a typed
    reason (``UNSUPPORTED`` or ``LOCK_UNREADABLE``), never passed over silently.
    """
    if platform.system() != "Linux":
        return False
    try:
        _entry()
    except UnsupportedArchitecture as exc:
        _report(str(exc))
        return False
    except (OSError, ValueError, KeyError) as exc:
        _report(f"reason={LOCK_UNREADABLE} file={LOCK_FILE} error={exc!r}")
        return False
    return True


def _report(message: str) -> None:
    if message not in _REPORTED:
        _REPORTED.add(message)
        logger.warning("%s", message)


def _extract(payload: bytes, root: Path) -> None:
    if payload[:4] == b"\x28\xb5\x2f\xfd":
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload)) as stream:
            payload = stream.read(MAX_UNPACKED + 1)
    if len(payload) > MAX_UNPACKED:
        raise DocumentError("Office compatibility package exceeds its unpacked size limit")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        # The absolute loader alias (/lib64, /lib) is unnecessary: invoke the real
        # loader inside lib/<triplet>. Never publish an absolute/escaping link.
        members = [
            member for member in archive if not (member.issym() and member.linkname.startswith("/"))
        ]
        archive.extractall(root, members=members, filter="data")


def prepare(
    root: Path,
    *,
    download: Callable[[str, Path, str], None],
    deb_data: Callable[[BinaryIO], bytes],
) -> None:
    """Publish the private userland and Office's launcher after all pinned packages exist.

    The caller holds the renderer's interprocess lock. An interrupted preparation
    can be retried; the executable wrapper is published last.
    """
    directory = runtime_directory()
    triplet = _triplet()
    elf_loader = loader()
    userland = root / directory
    if not (userland / elf_loader).is_file():
        with tempfile.TemporaryDirectory(prefix="office-userland-", dir=root.parent) as temporary:
            staging = Path(temporary)
            image = staging / "image"
            image.mkdir()
            pinned = packages()

            def fetch(item: tuple[str, str]) -> Path:
                url, checksum = item
                archive = staging / Path(url).name
                download(url, archive, checksum)
                return archive

            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="office-userland") as pool:
                archives = list(pool.map(fetch, pinned))
            for archive in archives:
                with archive.open("rb") as source:
                    _extract(deb_data(source), image)
                archive.unlink()
            if not (image / elf_loader).is_file():
                raise DocumentError("Office's private userland contains no ELF loader")
            (image / "fonts.conf").write_text(FONTS_CONF, encoding="utf-8")
            image.rename(userland)
    wrapper = root / WRAPPER
    text = f"""#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
program="$root/opt/libreoffice26.2/program"
userland="$root/{directory}"
# Office runs only on its own userland: no host library is loaded, whatever the host has.
export FONTCONFIG_FILE="$userland/fonts.conf" SAL_USE_VCLPLUGIN=svp
# LibreOffice's first profile setup requests EXITHELPER_NORMAL_RESTART (81).
# Honor that launcher protocol, bounded; crashes and other errors stay errors.
attempt=0
while :; do
  status=0
  "$userland/{elf_loader.as_posix()}" \\
    --library-path "$userland/lib/{triplet}:$userland/usr/lib/{triplet}:$program" \\
    "$program/soffice.bin" "$@" || status=$?
  if [ "$status" -ne 81 ]; then exit "$status"; fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 3 ]; then exit 81; fi
done
"""
    temporary_wrapper = wrapper.with_suffix(".part")
    temporary_wrapper.write_text(text, encoding="utf-8", newline="\n")
    temporary_wrapper.chmod(0o755)
    temporary_wrapper.replace(wrapper)
    # The launcher now names this userland; earlier ones (and the C-runtime-only
    # "compat-" directory before them) are no longer referenced.
    for stale in [*root.glob("userland-*"), *root.glob("compat-*")]:
        if stale.name != directory and stale.is_dir():
            try:
                shutil.rmtree(stale)
            except OSError as exc:
                logger.warning(
                    "reason=office_userland_cleanup_failed directory=%s error=%s", stale, exc
                )
