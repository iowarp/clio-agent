"""Normalize clone sources handed to ``git`` at the subprocess boundary.

Agent Blueprint installation may receive a marketplace source as a ``file://``
URI -- notably ``pathlib.Path.as_uri()`` produces ``file:///C:/Users/...`` on
Windows. Older git-for-windows builds reject that shape when it is passed as a
bare ``git clone`` argument (the URL's leading-slash path ``/C:/Users/...`` is
read as a bogus repository: ``fatal: '/C:/Users/...' does not appear to be a
git repository``). The URI only works when an MSYS shell rewrites the argument
first, so a native ``subprocess`` launch on Windows fails (iowarp/clio-agent#903).

The single public helper here converts a ``file://`` URI into a plain local
path that ``git`` accepts natively on every platform, and passes every other
source (``https://``, ``ssh://``, SCP-like remotes, already-local paths)
through untouched. Conversion is deterministic and platform-independent so the
same input yields the same output regardless of the host OS.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

# A URI-encoded Windows drive path: ``/C:/Users/x`` (leading slash is the URI
# authority separator, not part of the filesystem path). Group 1 is the drive.
_URI_DRIVE_RE = re.compile(r"^/([A-Za-z]:)(?:/.*)?$")


def normalize_git_clone_source(source: str) -> str:
    """Return a clone source ``git clone`` accepts natively on any platform.

    A ``file://`` URI is rewritten to a bare local filesystem path so the
    native ``subprocess`` invocation does not depend on an MSYS shell rewriting
    the argument (iowarp/clio-agent#903):

    * ``file:///C:/Users/x`` -> ``C:/Users/x`` (Windows drive path)
    * ``file:///home/x``     -> ``/home/x``    (POSIX absolute path)
    * ``file://server/share/x`` -> ``//server/share/x`` (UNC share)

    Any source whose scheme is not ``file`` -- ``https://...``, ``ssh://...``,
    ``git@host:org/repo.git``, or an already-local path -- is returned
    unchanged.

    Args:
        source: The clone source as supplied to blueprint installation: a URL,
            an SCP-like remote, a ``file://`` URI, or a local path.

    Returns:
        A clone source string ``git`` accepts natively on the current platform.

    Raises:
        ValueError: If ``source`` is a ``file://`` URI that carries no path
            (e.g. ``file://`` or a host with no share). Malformed file URIs are
            surfaced as a structured error rather than silently forwarded to
            ``git`` (no silent fallback).
    """
    parts = urlsplit(source)
    if parts.scheme != "file":
        return source

    path = unquote(parts.path)
    host = parts.netloc
    if host and host.lower() != "localhost":
        # UNC share: file://server/share/path -> //server/share/path.
        if not path:
            raise ValueError(f"malformed file:// URI (host without path): {source!r}")
        return f"//{host}{path}"

    if not path:
        raise ValueError(f"malformed file:// URI (empty path): {source!r}")

    drive = _URI_DRIVE_RE.match(path)
    if drive:
        # Strip the URI authority slash: /C:/Users/x -> C:/Users/x.
        return path[1:]
    return path
