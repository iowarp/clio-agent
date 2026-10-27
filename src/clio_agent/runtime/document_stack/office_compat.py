"""Private Linux userland the pinned LibreOffice runs on, so Office works on any host.

TDF's Linux build links against about forty desktop system libraries (X11, cairo,
cups, fontconfig, NSS, glib, ...) and needs glibc 2.34. A headless server, an HPC
node or a minimal container has few of them and often an older glibc. Like CLIO's
own uv and Node, Office therefore brings everything it runs on: the C runtime and
every library it loads, from pinned Ubuntu 22.04 packages
(``office_userland.lock.json``, written by ``scripts/office_userland_lock.py``),
run through their own loader. Nothing from the host is mixed in, so there is no
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
from typing import TYPE_CHECKING, Any, BinaryIO

import zstandard

if TYPE_CHECKING or __package__:
    from .process import DocumentError
else:
    from process import DocumentError

logger = logging.getLogger(__name__)
LOCK_FILE = Path(__file__).with_name("office_userland.lock.json")
_ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64"}
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


def _lock() -> dict[str, Any]:
    return json.loads(LOCK_FILE.read_text(encoding="utf-8"))


def _architecture() -> str | None:
    return _ARCHITECTURES.get(platform.machine().lower())


def packages() -> list[tuple[str, str]]:
    """This host's pinned packages as (URL, SHA-256)."""
    lock = _lock()
    entry = lock["architectures"][_architecture() or ""]
    base = f"{lock['base']}/{lock['snapshot']}"
    return [(f"{base}/{item['path']}", item["sha256"]) for item in entry["packages"]]


def _triplet() -> str:
    return _lock()["architectures"][_architecture() or ""]["triplet"]


def runtime_directory() -> str:
    """The userland's directory name, which changes whenever its pins change."""
    return "userland-" + hashlib.sha256(repr(packages()).encode()).hexdigest()[:12]


def supported() -> bool:
    """Return whether Office runs on its private userland on this host.

    Every Linux host whose architecture is pinned does, whatever its glibc and
    whatever it has installed.
    """
    if platform.system() != "Linux":
        return False
    architecture = _architecture()
    try:
        return architecture is not None and architecture in _lock()["architectures"]
    except (OSError, ValueError, KeyError):
        return False


def _extract(payload: bytes, root: Path) -> None:
    if payload[:4] == b"\x28\xb5\x2f\xfd":
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(payload)) as stream:
            payload = stream.read(MAX_UNPACKED + 1)
    if len(payload) > MAX_UNPACKED:
        raise DocumentError("Office compatibility package exceeds its unpacked size limit")
    with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
        # /lib64's absolute loader alias is unnecessary: invoke the real loader
        # inside lib/x86_64-linux-gnu. Never publish an absolute/escaping link.
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
    loader = (
        Path("lib")
        / triplet
        / ("ld-linux-x86-64.so.2" if triplet.startswith("x86_64") else "ld-linux-aarch64.so.1")
    )
    userland = root / directory
    if not (userland / loader).is_file():
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
            if not (image / loader).is_file():
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
  "$userland/{loader.as_posix()}" \\
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
