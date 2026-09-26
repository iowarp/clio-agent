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

import os
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
    retries: int = 6,
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

    Retries with exponential backoff (``initial_delay_s * 2**attempt``, six
    attempts by default spanning roughly 0.02s-0.64s) before giving up and
    re-raising the last error -- a durable permission problem (a real ACL denial,
    a read-only fence) is never silently swallowed, only a transient one is
    waited out.

    Args:
        source: The staged temp file (already fully written).
        target: The final path being atomically replaced.
        retries: Total attempts before giving up (>= 1).
        initial_delay_s: Delay before the second attempt; doubles each retry.

    Raises:
        OSError: The final attempt's error, when every retry was exhausted (or
            immediately, unmodified, for a non-retryable error).
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
            if not _is_retryable_replace_error(exc) or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    if last_exc is not None:  # pragma: no cover - unreachable (loop always returns/raises)
        raise last_exc


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


__all__ = ["atomic_replace", "short_stage_name", "win_extended_path"]
