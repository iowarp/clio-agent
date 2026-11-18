"""Cleanup of what an Office preparation leaves when it is ended mid-way.

On macOS the LibreOffice image is attached inside a staging directory beside the
renderer root while it is copied out (``office._extract``). A stop must not leave it
attached until reboot, and an ended preparation must not leave its partial tree.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import plistlib
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .process import DocumentError, run
else:
    from process import DocumentError, run

# Scratch directories beside the root while a renderer is laid out.
STAGING_PREFIX = "office-extract-"
logger = logging.getLogger(__name__)


def _detach_attached_at_exit() -> None:
    """Detach the images this process attached and has not detached yet.

    A desktop quit sends SIGINT, which unwinds the main thread into interpreter
    shutdown to wait for the extracting worker, then SIGTERM, whose exit
    (``process.end_on_termination``) skips that wait: the worker's ``finally``
    never runs, but this does.
    """
    for mount in sorted(ATTACHED):
        try:
            run(["/usr/bin/hdiutil", "detach", mount, "-force"], cwd=Path(mount).parent, timeout=30)
        except DocumentError as error:
            logger.warning("reason=office_mount_kept_at_exit mount=%s error=%s", mount, error)


# The macOS images attached by this process and not yet detached.
ATTACHED: set[str] = set()


def track_attached(mount: Path) -> None:
    """Record an attached image so an exit that skips its ``finally`` still detaches it.

    Registered again at each attach because exit handlers run last-in first-out: the
    mount point's ``TemporaryDirectory`` finalizer is registered before this, so the
    detach runs first and that finalizer can then remove the emptied staging directory.
    """
    ATTACHED.add(str(mount))
    atexit.unregister(_detach_attached_at_exit)
    atexit.register(_detach_attached_at_exit)


def staging_prefix(version: str) -> str:
    """The name prefix of a staging directory for one renderer version."""
    return f"{STAGING_PREFIX}{version}-"


def clear_stale_staging(parent: Path, version: str) -> None:
    """Remove what an earlier preparation left behind when it was ended mid-way.

    Called holding the lock of ``version``'s root, so its staging directories here are
    abandoned; those of another version (an update in progress) are left alone. A
    preparation ended by SIGTERM or SIGKILL (a desktop quit, a power loss) skips its
    ``finally``: on macOS its LibreOffice image stays attached inside the staging
    directory until reboot, and on every OS the partial tree stays on disk.
    """
    # Names without a version come from releases before staging names carried one.
    stale = [
        path
        for path in parent.glob(f"{STAGING_PREFIX}*")
        if path.is_dir()
        and (
            path.name.startswith(staging_prefix(version))
            or "-" not in path.name[len(STAGING_PREFIX) :]
        )
    ]
    if not stale:
        return
    if platform.system() == "Darwin":
        _detach_images_under(stale, cwd=parent, version=version)
    for path in stale:
        try:
            shutil.rmtree(path, onexc=_retry_writable)
        except OSError as error:
            logger.warning("reason=office_stale_staging_kept path=%s error=%s", path, error)
            continue
        logger.info("reason=office_stale_staging_removed path=%s", path)


def _retry_writable(function: Callable[[str], object], path: str, error: BaseException) -> None:
    """Make a read-only entry writable and remove it again, as Windows requires."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    function(path)


def _detach_images_under(directories: list[Path], *, cwd: Path, version: str) -> None:
    """Detach every image mounted inside ``directories`` or backed by ``version``'s download.

    The lock is held, so an image from this cache is a leftover. One stopped while it
    was being detached keeps its device attached with no mount point: detach that device.
    """
    try:
        listing = plistlib.loads(
            run(["/usr/bin/hdiutil", "info", "-plist"], cwd=cwd, timeout=30).encode()
        )
    except (DocumentError, ValueError) as error:
        logger.warning("reason=office_stale_mounts_unknown error=%s", error)
        return
    owned = [directory.resolve() for directory in directories]
    cache = cwd.resolve()
    for image in listing.get("images", []):
        entities = image.get("system-entities", [])
        mounts = [entity["mount-point"] for entity in entities if entity.get("mount-point")]
        devices = sorted((entity.get("dev-entry", "") for entity in entities), key=len)
        if any(Path(mount).resolve().is_relative_to(d) for mount in mounts for d in owned):
            target = mounts[0]
        elif not mounts and devices and _cached_image(image.get("image-path", "/"), cache, version):
            target = devices[0]
        else:
            continue
        try:
            run(["/usr/bin/hdiutil", "detach", target, "-force"], cwd=cwd, timeout=30)
        except DocumentError as error:
            logger.warning("reason=office_stale_mount_kept mount=%s error=%s", target, error)
            continue
        logger.info("reason=office_stale_mount_detached mount=%s", target)


def _cached_image(image_path: str, cache: Path, version: str) -> bool:
    """Whether an attached image is backed by ``version``'s download in ``cache``."""
    path = Path(image_path)
    return version in path.name and path.resolve().is_relative_to(cache)
