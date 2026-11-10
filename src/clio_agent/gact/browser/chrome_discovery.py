"""Find the DevTools endpoint of the person's own running Chrome or Edge.

Chrome 136 and later refuse remote debugging on the default profile when it is asked for
with a launch flag, and no program can read that profile's cookies or saved passwords
(they are sealed by the operating system, and on Windows bound to Chrome itself). Since
Chrome 144 the person can allow it from inside Chrome instead: at
``chrome://inspect/#remote-debugging`` they turn on "Allow remote debugging for this
browser instance", Chrome writes the endpoint to ``DevToolsActivePort`` in its user data
folder, and it asks them each time a client attaches. ``browser.cdp_url = "chrome"`` (or
``"msedge"``) reads that file, so CLIO works in the person's real browser, sign-ins and
all, with their consent each session.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from clio_agent.gact.browser.settings import BrowserError

__all__ = ["PRODUCTS", "devtools_endpoint", "user_data_dir"]

#: The products a ``browser.cdp_url`` may name instead of an endpoint.
PRODUCTS = ("chrome", "msedge")

_FOLDERS = {
    "chrome": {
        "win32": ("Google", "Chrome", "User Data"),
        "darwin": ("Google", "Chrome"),
        "linux": ("google-chrome",),
    },
    "msedge": {
        "win32": ("Microsoft", "Edge", "User Data"),
        "darwin": ("Microsoft Edge",),
        "linux": ("microsoft-edge",),
    },
}


def user_data_dir(product: str, *, platform: str = sys.platform, home: Path | None = None) -> Path:
    """The product's default user data folder on this operating system."""
    home = home or Path.home()
    kind = "win32" if platform.startswith("win") else "darwin" if platform == "darwin" else "linux"
    parts = _FOLDERS[product][kind]
    if kind == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif kind == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return base.joinpath(*parts)


def devtools_endpoint(product: str, *, folder: Path | None = None) -> str:
    """The WebSocket endpoint the person's running ``product`` allows CLIO to attach to."""
    if product not in PRODUCTS:
        raise BrowserError("browser_cdp_url_invalid", f"browser.cdp_url names {product!r}")
    marker = (folder or user_data_dir(product)) / "DevToolsActivePort"
    name = "Chrome" if product == "chrome" else "Edge"
    page = "chrome" if product == "chrome" else "edge"
    try:
        lines = marker.read_text(encoding="utf-8").split()
    except OSError as exc:
        raise BrowserError(
            "browser_cdp_not_enabled",
            f"{name} is not running with remote debugging allowed. In {name} 144 or later, "
            f"open {page}://inspect/#remote-debugging and turn on 'Allow remote debugging for "
            "this browser instance', then try again.",
            path=str(marker),
        ) from exc
    if len(lines) < 2 or not lines[0].isdigit():
        raise BrowserError(
            "browser_cdp_not_enabled",
            f"{marker} does not name a DevTools endpoint",
            path=str(marker),
        )
    return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
