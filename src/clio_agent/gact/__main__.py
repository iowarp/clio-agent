"""Module entry point: ``python -m clio_agent.gact``.

The bundled desktop runtime invokes the GACT server this way (via the
generic ``runtime.json`` manifest, iowarp/gact-tui#311) because
console-script shims embed absolute build-host paths and break when the
runtime is relocated into an installer (#909). ``-m`` needs only a
working interpreter + site-packages, which the portable runtime ships.

Same CLI as ``clio-agent serve`` / the ``clio-agent-gact`` console
script: ``--host``, ``--port``, ``--reload``, ``--no-agent``, ``--cwd``.

The ``app`` import is lazy (inside the guard) per the gact decomposition
no-cycle invariant: siblings never import ``clio_agent.gact.app`` at
module load (``tests/test_gact/test_decomposition_guardrails.py``).
"""

from __future__ import annotations

import os
import sys
import threading
from urllib.error import URLError
from urllib.request import Request, urlopen

_DESKTOP_HEARTBEAT_ENV = "CLIO_DESKTOP_BOOT_HEARTBEAT"
_HEARTBEAT_INTERVAL_SECONDS = 5.0


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


def _start_desktop_boot_heartbeat() -> None:
    """Report live desktop startup progress until the capabilities route responds."""

    if os.environ.get(_DESKTOP_HEARTBEAT_ENV) != "1":
        return

    def _report() -> None:
        interval = threading.Event()
        while True:
            print("sidecar-progress: backend startup is active", file=sys.stderr, flush=True)
            if _capabilities_ready():
                return
            interval.wait(_HEARTBEAT_INTERVAL_SECONDS)

    threading.Thread(target=_report, name="clio-desktop-boot-heartbeat", daemon=True).start()


if __name__ == "__main__":
    _start_desktop_boot_heartbeat()
    from clio_agent.gact.app import main  # noqa: PLC0415 - entry-point-only import

    main()
