"""Short-lived, single-download capabilities for streamed session ZIPs."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExportDownload:
    """One prepared archive, independent of the service's bearer credential."""

    path: Path
    filename: str
    expires_at: float


class ExportDownloads:
    """Own temporary archives until download or expiry; never retain workspace bytes in RAM."""

    def __init__(self, *, lifetime_seconds: float = 1200, max_pending: int = 8) -> None:
        self._lifetime = lifetime_seconds
        self._max_pending = max_pending
        self._items: dict[str, ExportDownload] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def add(self, path: Path, filename: str) -> str:
        """Register a completed ZIP and schedule cleanup even if the browser closes."""
        with self._lock:
            if len(self._items) >= self._max_pending:
                path.unlink(missing_ok=True)
                raise ValueError("Too many pending exports; download an existing archive first.")
            ticket = secrets.token_hex(32)
            self._items[ticket] = ExportDownload(path, filename, time.monotonic() + self._lifetime)
            timer = threading.Timer(self._lifetime, self._expire, args=(ticket,))
            timer.daemon = True
            self._timers[ticket] = timer
        timer.start()
        return f"/v1/session-export-downloads/{ticket}"

    def admits(self, method: str, path: str) -> bool:
        """Authorize only GET of this particular unexpired archive capability."""
        if method != "GET" or not path.startswith("/v1/session-export-downloads/"):
            return False
        ticket = path.removeprefix("/v1/session-export-downloads/")
        with self._lock:
            item = self._items.get(ticket)
            return item is not None and item.expires_at > time.monotonic()

    def take(self, ticket: str) -> ExportDownload | None:
        """Consume a ticket atomically; a second request cannot reuse it."""
        with self._lock:
            item = self._items.pop(ticket, None)
            timer = self._timers.pop(ticket, None)
            if timer is not None:
                timer.cancel()
        if item is not None and item.expires_at <= time.monotonic():
            item.path.unlink(missing_ok=True)
            return None
        return item

    def _expire(self, ticket: str) -> None:
        with self._lock:
            item = self._items.pop(ticket, None)
            self._timers.pop(ticket, None)
        if item is not None:
            item.path.unlink(missing_ok=True)
