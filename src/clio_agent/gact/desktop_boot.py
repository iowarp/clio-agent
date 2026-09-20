"""Progress heartbeat for desktop-managed CLIO backend startup."""

from __future__ import annotations

import os
import sys
import threading
from urllib.error import URLError
from urllib.request import Request, urlopen

_DESKTOP_HEARTBEAT_ENV = "CLIO_DESKTOP_BOOT_HEARTBEAT"
_HEARTBEAT_INTERVAL_SECONDS = 5.0
_heartbeat_lock = threading.Lock()
_heartbeat_started = False


def _argument_value(name: str, default: str) -> str:
    """Return a command-line option value without constructing the full CLI."""

    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return default


def _capabilities_ready() -> bool:
    """Return whether this desktop-managed server is already accepting API calls."""

    host = _argument_value("--host", "127.0.0.1")
    port = _argument_value("--port", "8100")
    request = Request(f"http://{host}:{port}/v1/capabilities")
    if token := os.environ.get("CLIO_AUTH_TOKEN", "").strip():
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=0.5) as response:  # noqa: S310 - loopback URL only
            return response.status == 200
    except (OSError, URLError):
        return False


def start_desktop_boot_heartbeat() -> bool:
    """Start one progress reporter for a desktop-managed backend process.

    The bundled ``python -m clio_agent.gact`` path can start this before the
    heavier application import, while the legacy ``clio-agent-gact`` console
    entry point calls it from :func:`clio_agent.gact.app.main`. The process-wide
    guard keeps those two compatible paths from emitting duplicate reporters.

    Returns:
        ``True`` when this call started the reporter, otherwise ``False``.
    """

    if os.environ.get(_DESKTOP_HEARTBEAT_ENV) != "1":
        return False

    global _heartbeat_started  # noqa: PLW0603
    with _heartbeat_lock:
        if _heartbeat_started:
            return False
        _heartbeat_started = True

    def _report() -> None:
        interval = threading.Event()
        while True:
            print("sidecar-progress: backend startup is active", file=sys.stderr, flush=True)
            if _capabilities_ready():
                return
            interval.wait(_HEARTBEAT_INTERVAL_SECONDS)

    threading.Thread(
        target=_report,
        name="clio-desktop-boot-heartbeat",
        daemon=True,
    ).start()
    return True
