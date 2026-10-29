"""Copy a folder file by file, cloning copy-on-write where the filesystem allows.

A fork in an isolated workflow run starts with a copy of its forker's folder
(:mod:`clio_agent.gact.node_folders`). Large files are cloned where the filesystem
supports it, so the copy is cheap, and copied otherwise. A clone shares blocks until
either side writes, so an edit through one name never changes the other. A hard link
is never used: it is the same file under two names.

* Linux: the ``FICLONE`` ioctl (btrfs, XFS with reflink, bcachefs).
* macOS: ``clonefile(2)`` (APFS).
* Windows: ``FSCTL_DUPLICATE_EXTENTS_TO_FILE`` (ReFS, Dev Drive).

Any other filesystem (ext4, NTFS, tmpfs, ...) refuses the clone call and the file is
copied. The method used for each file is counted, so the fork records what happened.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from pathlib import Path
from typing import Any

#: ``_IOW(0x94, 9, int)``: clone a whole file's extents (``linux/fs.h``).
FICLONE = 0x40049409
#: ``CTL_CODE(FILE_DEVICE_FILE_SYSTEM, 209, METHOD_BUFFERED, FILE_WRITE_ACCESS)``.
FSCTL_DUPLICATE_EXTENTS_TO_FILE = 0x98344
#: ``clonefile`` flag: do not follow a symlink at the source.
CLONE_NOFOLLOW = 0x0001


def _clone_linux(source: Path, target: Path) -> None:
    import fcntl  # noqa: PLC0415 - POSIX only

    with open(source, "rb") as src, open(target, "xb") as dst:
        try:
            fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
        except OSError:
            dst.close()
            target.unlink(missing_ok=True)
            raise


def _clone_macos(source: Path, target: Path) -> None:
    import ctypes.util  # noqa: PLC0415

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    if libc.clonefile(os.fsencode(source), os.fsencode(target), CLONE_NOFOLLOW) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(source))


class _DuplicateExtents(ctypes.Structure):
    """``DUPLICATE_EXTENTS_DATA``: the source handle and the byte range to clone."""

    _fields_ = [  # noqa: RUF012 - ctypes layout
        ("FileHandle", ctypes.c_void_p),
        ("SourceFileOffset", ctypes.c_longlong),
        ("TargetFileOffset", ctypes.c_longlong),
        ("ByteCount", ctypes.c_longlong),
    ]


def _windows_cluster_size(path: Path) -> int:
    sectors, size, free, total = (ctypes.c_ulong() for _ in range(4))
    drive = os.path.splitdrive(str(path.resolve()))[0] + "\\"
    if not ctypes.windll.kernel32.GetDiskFreeSpaceW(  # type: ignore[attr-defined]
        drive, ctypes.byref(sectors), ctypes.byref(size), ctypes.byref(free), ctypes.byref(total)
    ):
        raise ctypes.WinError()  # type: ignore[attr-defined]
    return int(sectors.value * size.value)


def _duplicate_extents(source: Any, target: Any, length: int) -> None:
    """``DeviceIoControl(FSCTL_DUPLICATE_EXTENTS_TO_FILE)`` from ``source`` into ``target``."""

    import msvcrt  # noqa: PLC0415 - Windows only

    request = _DuplicateExtents(msvcrt.get_osfhandle(source.fileno()), 0, 0, length)  # type: ignore[attr-defined]
    returned = ctypes.c_ulong()
    if not ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
        ctypes.c_void_p(msvcrt.get_osfhandle(target.fileno())),  # type: ignore[attr-defined]
        FSCTL_DUPLICATE_EXTENTS_TO_FILE,
        ctypes.byref(request),
        ctypes.sizeof(request),
        None,
        0,
        ctypes.byref(returned),
        None,
    ):
        raise ctypes.WinError()  # type: ignore[attr-defined]


def _clone_windows(source: Path, target: Path) -> None:
    size = source.stat().st_size
    cluster = _windows_cluster_size(target.parent)
    # Block cloning works in whole clusters: extend the target to a cluster boundary,
    # clone, then trim it back to the source's size.
    rounded = -(-size // cluster) * cluster
    with open(source, "rb") as src, open(target, "xb") as dst:
        try:
            dst.truncate(rounded)
            if rounded:
                _duplicate_extents(src, dst, rounded)
            dst.truncate(size)
        except OSError:
            dst.close()
            target.unlink(missing_ok=True)
            raise


def _platform_clone(platform: str) -> Any:
    if platform.startswith("linux"):
        return _clone_linux
    if platform == "darwin":
        return _clone_macos
    if platform == "win32":
        return _clone_windows
    return None


def clone_or_copy_file(source: Path, target: Path, *, platform: str = "") -> str:
    """Clone ``source`` to the new file ``target``, else copy it; return the method."""

    clone = _platform_clone(platform or sys.platform)
    if clone is not None:
        try:
            clone(source, target)
            shutil.copystat(source, target)
            return "clone"
        except (OSError, AttributeError, ValueError):
            target.unlink(missing_ok=True)
    shutil.copy2(source, target)
    return "copy"


def clone_or_copy_tree(source: Path, target: Path, *, platform: str = "") -> dict[str, Any]:
    """Copy the folder ``source`` into ``target`` (created), cloning files where possible.

    Symlinks are recreated as symlinks, never followed. Returns the method (``clone``,
    ``copy``, ``mixed`` when the filesystem cloned some files and not others, or
    ``empty``) with the file counts.
    """

    counts = {"clone": 0, "copy": 0}
    target.mkdir(parents=True, exist_ok=True)
    for folder, directories, files in os.walk(source, followlinks=False):
        here = Path(folder)
        destination = target / here.relative_to(source)
        for name in directories:
            path = here / name
            if path.is_symlink():
                (destination / name).symlink_to(os.readlink(path))
            else:
                (destination / name).mkdir(exist_ok=True)
        for name in files:
            path = here / name
            if path.is_symlink():
                (destination / name).symlink_to(os.readlink(path))
                continue
            counts[clone_or_copy_file(path, destination / name, platform=platform)] += 1
    if counts["clone"] and counts["copy"]:
        method = "mixed"
    elif counts["clone"]:
        method = "clone"
    elif counts["copy"]:
        method = "copy"
    else:
        method = "empty"
    return {"method": method, "cloned": counts["clone"], "copied": counts["copy"]}


__all__ = ["clone_or_copy_file", "clone_or_copy_tree"]
