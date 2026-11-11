"""Open append handles for JSONL logs that are written one line at a time.

Reopening a log (and recreating its directory) for every line cost about a tenth of
a busy server's active time in the stream audit and again in the semantic trace
writer (P7, 2026-11-02: one audit row per store put, one trace event per record).
:class:`AppendHandles` keeps one handle per path, flushed per line so readers see
each line at once. A path is re-checked at most once a second, so a log that was
rotated or removed (a deleted session's trace) is reopened rather than written into
an unlinked file. At most ``max_open`` handles stay open; the least recently written
is closed first. On Windows an open handle blocks deleting or renaming the file, so
there each line still closes its handle (``keep_open``).
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TextIO

from clio_agent.platform_paths import win_extended_path

__all__ = ["AppendHandles"]


class AppendHandles:
    """Append lines to files through cached handles (thread-safe).

    Args:
        recheck_s: How long a handle is trusted before the path is checked to still
            name the same file.
        max_open: The most handles kept open at once.
        keep_open: Keep handles between lines (default: not on Windows).
    """

    def __init__(
        self, *, recheck_s: float = 1.0, max_open: int = 64, keep_open: bool = os.name != "nt"
    ) -> None:
        self.recheck_s = recheck_s
        self.max_open = max_open
        self.keep_open = keep_open
        self._lock = threading.Lock()
        self._open: OrderedDict[str, tuple[TextIO, tuple[int, int], float]] = OrderedDict()

    def write(self, path: Path | str, line: str) -> None:
        """Append ``line`` and a newline to ``path``, creating it and its directory.

        Raises:
            OSError: The file could not be opened or written; its handle is dropped.
        """
        key = str(path)
        with self._lock:
            try:
                handle = self._handle(key, time.monotonic())
                handle.write(line + "\n")
                handle.flush()
                if not self.keep_open:
                    self._drop(key)
            except OSError:
                self._drop(key)
                raise

    def close(self) -> None:
        """Close every open handle."""
        with self._lock:
            for key in list(self._open):
                self._drop(key)

    def _handle(self, key: str, now: float) -> TextIO:
        cached = self._open.get(key)
        if cached is not None:
            handle, identity, checked = cached
            self._open.move_to_end(key)
            if now - checked < self.recheck_s:
                return handle
            if _identity(Path(win_extended_path(os.path.abspath(key)))) == identity:
                self._open[key] = (handle, identity, now)
                return handle
            self._drop(key)
        path = Path(win_extended_path(os.path.abspath(key)))  # a deep home passes MAX_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
        self._open[key] = (handle, _identity(path) or (0, 0), now)
        while len(self._open) > self.max_open:
            self._drop(next(iter(self._open)))
        return handle

    def _drop(self, key: str) -> None:
        cached = self._open.pop(key, None)
        if cached is not None:
            try:
                cached[0].close()
            except OSError:
                pass  # closing a handle whose file is gone; it is dropped either way


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)
