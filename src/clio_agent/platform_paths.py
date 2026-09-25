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

import sys
import uuid
from pathlib import Path

_EXTENDED_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


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


__all__ = ["short_stage_name", "win_extended_path"]
