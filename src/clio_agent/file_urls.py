"""``file:`` URLs to local paths, correctly on every OS.

``Path(unquote(urlsplit(url).path))`` is wrong on Windows: ``file:///C:/w/a.html`` has
the path ``/C:/w/a.html``, which Windows reads as drive-relative (``\\C:\\w\\a.html`` on
the current drive), and ``file://server/share/a.html`` loses its host. ``url2pathname``
maps the drive form; a host on Windows names a UNC share. ``Path.from_uri`` does both but
needs Python 3.13, and this package supports 3.12.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

__all__ = ["file_url_path"]


def file_url_path(url: str, *, shares: bool = True) -> Path | None:
    """The absolute local path a ``file:`` URL names, or ``None``.

    ``file:///C:/w/a%20b.html`` is ``C:\\w\\a b.html`` on Windows and ``file:///srv/a`` is
    ``/srv/a`` on POSIX. A host other than ``localhost`` names a UNC share on Windows
    (``file://server/share/a`` is ``\\\\server\\share\\a``); ``shares=False`` refuses every
    share form, so the caller never connects to one.
    ``None`` for another scheme, a relative or malformed path, or a host the OS cannot
    open as a local path (every host on POSIX).
    """

    parts = urlsplit(url)
    if parts.scheme.lower() != "file" or not parts.path:
        return None
    host = parts.netloc
    if host and host.lower() != "localhost":
        # A colon would let the drive parser take "//host/C:/x" as local "C:\x".
        if not shares or os.name != "nt" or ":" in parts.path:
            return None
        raw = f"//{host}{parts.path}"
    else:
        raw = parts.path
    try:
        path = Path(url2pathname(raw))
    except OSError:  # Windows: a malformed drive ("/a:b:c")
        return None
    if not shares and path.drive.startswith(("\\\\", "//")):
        return None  # the four-slash form, file:////server/share/a
    return path if path.is_absolute() else None
