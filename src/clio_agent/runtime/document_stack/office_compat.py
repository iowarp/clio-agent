"""Private Linux C/C++ runtime for the pinned LibreOffice application.

The loader is invoked explicitly for Office only. Neither system libraries nor
the agent's environment are replaced. Package notices remain in the private tree.
"""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import zstandard

if TYPE_CHECKING or __package__:
    from .process import DocumentError
else:
    from process import DocumentError

# Ubuntu 22.04 updates: retained Launchpad files and published Packages SHA256 values.
# glibc 2.35 / GCC 12 cover TDF 26.2's GLIBC_2.34 / GLIBCXX_3.4.29 imports.
PACKAGES = (
    (
        "https://launchpadlibrarian.net/843559020/gcc-12-base_12.3.0-1ubuntu1~22.04.3_amd64.deb",
        "7f9253b7e0976f0526fc21346c73ee006e185a7b2f2f865048ea78a2af55bc8d",
    ),
    (
        "https://launchpadlibrarian.net/875852022/libc6_2.35-0ubuntu3.15_amd64.deb",
        "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d",
    ),
    (
        "https://launchpadlibrarian.net/843559184/libgcc-s1_12.3.0-1ubuntu1~22.04.3_amd64.deb",
        "d383e642d83263147f7b4b69fb92cf1036e5246c6be3accf10b77f986684589a",
    ),
    (
        "https://launchpadlibrarian.net/843559197/libstdc++6_12.3.0-1ubuntu1~22.04.3_amd64.deb",
        "29ef3d289b272704b75141906c8f683db0f2bdb0942061366691333bfb289bdc",
    ),
)
LOADER = Path("lib/x86_64-linux-gnu/ld-linux-x86-64.so.2")
RUNTIME_DIRECTORY = "compat-" + hashlib.sha256(repr(PACKAGES).encode()).hexdigest()[:12]
WRAPPER = "soffice-clio"
MAX_UNPACKED = 128 * 1024 * 1024


def supported() -> bool:
    """Return whether this host needs the qualified private ABI runtime.

    Newer hosts use their own libc: mixing a private older libc with newer host
    graphics libraries would introduce a second ABI mismatch.
    """
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        return False
    name, version = platform.libc_ver()
    try:
        return name == "glibc" and tuple(int(part) for part in version.split(".")[:2]) < (2, 34)
    except ValueError:
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
    """Publish a relocatable Office-only loader after all pinned libraries exist.

    The caller holds the renderer's interprocess lock. An interrupted preparation
    can be retried; the executable wrapper is published last.
    """
    compatibility = root / RUNTIME_DIRECTORY
    if not (compatibility / LOADER).is_file():
        with tempfile.TemporaryDirectory(prefix="office-abi-", dir=root.parent) as temporary:
            staging = Path(temporary)
            image = staging / "image"
            image.mkdir()
            for filename, checksum in PACKAGES:
                archive = staging / Path(filename).name
                download(filename, archive, checksum)
                with archive.open("rb") as source:
                    _extract(deb_data(source), image)
            if not (image / LOADER).is_file():
                raise DocumentError("Office compatibility runtime contains no ELF loader")
            image.rename(compatibility)
    wrapper = root / WRAPPER
    text = """#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
program="$root/opt/libreoffice26.2/program"
# LibreOffice's first profile setup requests EXITHELPER_NORMAL_RESTART (81).
# Honor that launcher protocol, bounded; crashes and other errors stay errors.
attempt=0
while :; do
  status=0
  "$root/COMPAT_DIRECTORY/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2" \\
    --library-path "$root/COMPAT_DIRECTORY/lib/x86_64-linux-gnu:$root/COMPAT_DIRECTORY/usr/lib/x86_64-linux-gnu:$program" \\
    "$program/soffice.bin" "$@" || status=$?
  if [ "$status" -ne 81 ]; then exit "$status"; fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 3 ]; then exit 81; fi
done
""".replace("COMPAT_DIRECTORY", RUNTIME_DIRECTORY)
    temporary_wrapper = wrapper.with_suffix(".part")
    temporary_wrapper.write_text(text, encoding="utf-8", newline="\n")
    temporary_wrapper.chmod(0o755)
    temporary_wrapper.replace(wrapper)
