r"""Windows extended-length path support (the MAX_PATH workaround).

Windows limits an ordinary ("legacy") path to 260 characters unless either the
caller passes the **extended-length** form (``\\?\<drive>:\...`` for a local
drive, ``\\?\UNC\<server>\<share>\...`` for a network path) or the
system-wide ``LongPathsEnabled`` registry policy is on. CLIO cannot assume that
policy is set on a user's machine: observed live (2026-09-24, Windows 11,
``LongPathsEnabled=0``), a real workspace root plus the resource-upload staging
name for :mod:`clio_agent.gact.resource_materialization` landed at roughly 270
characters and every write under it raised ``FileNotFoundError`` even though
every parent directory existed.

A workspace root is user-chosen and arbitrarily deep; CLIO composes further
subdirectories and filenames on top of it (``.clio/agent/...``,
``.clio/inputs/<resource_id>/<filename>``, CAS shards, working-copy ids). None
of that composition can assume it stays under 260 characters, so every
OS-level filesystem call that touches a COMPOSED path -- open, mkdir/makedirs,
rename/replace, unlink, rmtree -- routes its path through
:func:`win_extended_path` first.

This is deliberately a plain string transform, not a new path type: callers
keep ordinary :class:`pathlib.Path` objects for everything else (joining,
``.name``, ``.parent``, user-facing display, records persisted to disk) and
only wrap the path at the actual syscall. ``pathlib``'s own Windows path
parser does not reliably round-trip an already-prefixed ``\\?\`` string, so
the prefixed form is used with the plain :mod:`os` / builtin ``open`` calls,
never re-wrapped in :class:`pathlib.Path`.

No-op on non-win32 platforms (Linux CI, macOS) and for a path that is not
fully qualified (relative, or drive-relative like ``C:foo``) -- the ``\\?\``
prefix only applies to an absolute path, and every call site here already
resolves to an absolute path (``Path.resolve()``) before reaching this helper.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

_EXTENDED_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"

#: Windows ``WinError 5`` (ERROR_ACCESS_DENIED) and ``WinError 32``
#: (ERROR_SHARING_VIOLATION) both surface to Python as a bare ``PermissionError``
#: from ``os.replace``/``Path.replace`` -- there is no distinct exception type
#: for "a virus scanner / search indexer / another CLIO process had the target
#: momentarily open for read" vs. a real, durable permission problem. Retried;
#: see :func:`atomic_replace`.
_RETRYABLE_WINERRORS = (5, 32)

#: Longest single wait between replace attempts. With the default 14 attempts
#: the whole wait is about 4.6 s: long enough for an antivirus scan or an
#: indexer that opened the file for read, short enough that a real problem is
#: reported instead of hanging the write.
_MAX_RETRY_DELAY_S = 0.5

logger = logging.getLogger(__name__)


class ReplaceBlockedError(PermissionError):
    """A tmp-file replace kept failing because another process held the target open.

    Attributes:
        reason: ``replace_blocked_by_open_handle`` -- the typed reason logged
            and carried to the caller (the operation, the API error).
        target: The file that could not be replaced; it still holds its
            previous, complete contents.
    """

    reason = "replace_blocked_by_open_handle"

    def __init__(self, target: str, attempts: int, cause: OSError) -> None:
        super().__init__(
            f"{self.reason}: {target} stayed open in another process for {attempts} attempts "
            f"({cause}); it still holds its previous contents"
        )
        self.target = target


def win_extended_path(path: str | Path) -> str:
    r"""Return the win32 extended-length form of ``path`` for an OS-level call.

    No-op (returns ``str(path)`` unchanged) off win32, for a path already in
    extended form, and for a path that is not fully qualified. A UNC path
    (``\\server\share\...``) becomes ``\\?\UNC\server\share\...``; a
    drive-absolute path (``C:\...``) becomes ``\\?\C:\...``.
    """
    text = str(path)
    if sys.platform != "win32":
        return text
    if text.startswith(_EXTENDED_PREFIX):
        return text
    normalized = text.replace("/", "\\")
    if normalized.startswith("\\\\"):
        return _UNC_PREFIX + normalized[2:]
    if len(normalized) >= 3 and normalized[0].isalpha() and normalized[1:3] == ":\\":
        return _EXTENDED_PREFIX + normalized
    # Relative or drive-relative ("C:foo") -- cannot be extended; leave as-is.
    return text


def _is_retryable_replace_error(exc: OSError) -> bool:
    """Whether ``exc`` from a tmp-file replace is a transient Windows race, not a real failure.

    Off win32 (or for a genuine, durable permission problem) this is ``False`` and
    the caller re-raises immediately -- retrying a real access-control failure would
    just burn the whole retry budget for nothing.
    """
    if sys.platform != "win32" or not isinstance(exc, PermissionError):
        return False
    winerror = getattr(exc, "winerror", None)
    return winerror in _RETRYABLE_WINERRORS


def atomic_replace(
    source: str | Path,
    target: str | Path,
    *,
    retries: int = 14,
    initial_delay_s: float = 0.02,
) -> None:
    """``os.replace(source, target)`` with retry on a transient Windows sharing race.

    A crash-safe write is staged as ``tmp`` + rename, which is atomic on every
    platform CLIO ships on -- but on Windows a rename-onto-an-existing-file can
    transiently fail with ``PermissionError`` (``WinError 5``/``32``) when a virus
    scanner, a search indexer, or another CLIO process (e.g. a concurrent reader
    of the very file being replaced) has the target momentarily open. Observed
    live (2026-09-24): P4a's ``model_discovery/overlay.py`` writer hit exactly
    this intermittently. POSIX ``rename(2)`` has no such failure mode (an open
    file handle does not block a rename), so this is a genuine platform
    difference, not a bug to paper over uniformly -- the retry loop is a no-op
    everywhere except win32.

    Retries with exponential backoff capped at ``_MAX_RETRY_DELAY_S`` (14
    attempts by default, about 4.6 s in all -- an antivirus scan or a
    monitoring script reading the file can hold it longer than a blink; seen
    live 2026-09-27 failing a deploy on ``infrastructure.json``) before giving
    up with :class:`ReplaceBlockedError` (a typed, logged reason) -- a durable
    permission problem (a real ACL denial, a read-only fence) is never
    silently swallowed, and is re-raised at once, unretried.

    Args:
        source: The staged temp file (already fully written).
        target: The final path being atomically replaced.
        retries: Total attempts before giving up (>= 1).
        initial_delay_s: Delay before the second attempt; doubles each retry.

    Raises:
        ReplaceBlockedError: Every attempt hit the transient sharing race.
        OSError: Immediately, unmodified, for a non-retryable error.
    """
    source_text = win_extended_path(source)
    target_text = win_extended_path(target)
    delay = initial_delay_s
    last_exc: OSError | None = None
    for attempt in range(max(1, retries)):
        try:
            os.replace(source_text, target_text)
            return
        except OSError as exc:
            last_exc = exc
            if not _is_retryable_replace_error(exc):
                raise
            if attempt == retries - 1:
                logger.warning(
                    "atomic replace: reason=%s target=%s attempts=%d: %s",
                    ReplaceBlockedError.reason,
                    target,
                    retries,
                    exc,
                )
                raise ReplaceBlockedError(str(target), retries, exc) from exc
            time.sleep(delay)
            delay = min(delay * 2, _MAX_RETRY_DELAY_S)
    if last_exc is not None:  # pragma: no cover - unreachable (loop always returns/raises)
        raise last_exc


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
    file_mode: int | None = None,
) -> None:
    """Write ``text`` to ``path`` crash-safely: stage, fsync, then :func:`atomic_replace`.

    The one tmp+replace write for small state files (the infrastructure
    store, saved servers, session defaults, ...). ``path`` always holds either
    its previous contents or the new ones, never a partial write; the staged
    file is removed whenever the write does not complete, so a failure leaves
    nothing behind. ``file_mode`` creates the staged file with that mode from
    the start (a credential file is never readable more widely, even briefly).

    Raises:
        ReplaceBlockedError: Another process kept ``path`` open (typed reason).
        OSError: Any other failure to stage or replace.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / short_stage_name(prefix=target.name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        # Like open(): 0o666 filtered by the umask unless a mode is required.
        fd = os.open(win_extended_path(staged), flags, 0o666 if file_mode is None else file_mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode(encoding))
            stream.flush()
            os.fsync(stream.fileno())
        atomic_replace(staged, target)
    except BaseException:
        try:
            os.unlink(win_extended_path(staged))
        except FileNotFoundError:
            pass
        raise


def short_stage_name(*, prefix: str = "part", suffix: str = ".tmp") -> str:
    """A short, same-directory staging filename for an atomic tmp+rename write.

    Earlier staging names embedded the real target filename plus a full
    32-hex ``uuid4`` (``.<name>.<32 hex>.tmp``), which repeats a
    potentially-long user filename right at the point the path is already
    deepest -- observed live pushing a resource-materialization path past 260
    characters. This carries only enough entropy (8 hex characters) to avoid
    a same-directory collision between concurrent writers targeting the same
    final name; the rename into place stays atomic within the same directory
    either way.
    """
    return f".{prefix}-{uuid.uuid4().hex[:8]}{suffix}"


def _extended_absolute(path: str | Path) -> str:
    return win_extended_path(Path(path).absolute())


def copytree_extended(source: str | Path, destination: str | Path) -> None:
    """``shutil.copytree`` with both trees addressed in extended-length form.

    A pack or skill tree copied under a long user dir (a long user name, a packaged
    app's ``LocalCache`` redirection, a staging name) crosses 260 characters deep in
    the tree, where a legacy path raises ``WinError 206``. The extended form lifts the
    limit for every ``scandir`` / ``makedirs`` / ``copy2`` the copy makes.
    """
    shutil.copytree(_extended_absolute(source), _extended_absolute(destination))


def rmtree_extended(path: str | Path, *, ignore_errors: bool = False) -> None:
    """``shutil.rmtree`` on the extended-length form (a tree deeper than MAX_PATH)."""
    shutil.rmtree(_extended_absolute(path), ignore_errors=ignore_errors)


def rename_extended(source: str | Path, destination: str | Path) -> None:
    """``os.rename`` with both paths in extended-length form."""
    os.rename(_extended_absolute(source), _extended_absolute(destination))


def tree_files(root: str | Path) -> list[tuple[Path, str]]:
    """Every file under ``root`` as ``(path relative to root, OS path to open)``.

    Sorted by the relative :class:`~pathlib.Path` (the order ``sorted(root.rglob(...))``
    gives), and walked in extended-length form so a file deeper than MAX_PATH is
    listed and openable. Directory symlinks are not followed.
    """
    base = _extended_absolute(root)
    rows: list[tuple[Path, str]] = []
    for directory, _subdirs, names in os.walk(base):
        for name in names:
            full = os.path.join(directory, name)
            if os.path.isfile(full):
                rows.append((Path(os.path.relpath(full, base)), full))
    return sorted(rows, key=lambda row: row[0])


__all__ = [
    "atomic_replace",
    "copytree_extended",
    "rename_extended",
    "rmtree_extended",
    "short_stage_name",
    "tree_files",
    "win_extended_path",
]
