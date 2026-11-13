"""The MCP App sandbox's own origin, which carries no API authority.

The MCP Apps specification ("Rendering the MCP App", draft at ext-apps
``ea3e9d0``) gives the view's frame ``allow-scripts`` and ``allow-same-origin``
on an origin different from the host's. CLIO serves the sandbox from a host
name of its own, ``s<digest>.mcp-apps.localhost`` on the server's port, with
one digest per originating server, so:

* a view keeps its own storage and same-origin APIs (a view that reads
  ``localStorage`` works), shared only with views of the same server;
* that origin is never the web UI's or the API's: on a sandbox host CLIO
  serves only the app's sandbox document and its service route (MA23), and
  refuses every other route with ``403 sandbox_origin_refused``;
* a request from the view to the API's own host is cross-origin, so the CORS
  and Origin policy (:mod:`clio_agent.gact.origin_guard`) refuses it.

Browsers resolve every ``*.localhost`` name to loopback, so the names need no
DNS. The sandbox document checks that it is addressed by its app's host name.
"""

from __future__ import annotations

import hashlib
import re

SANDBOX_HOST_SUFFIX = ".mcp-apps.localhost"

_SANDBOX_DOCUMENT_PATH = re.compile(r"^/v1/sessions/[^/]+/mcp-apps/[^/]+/sandbox$")


def sandbox_host_for(namespace: str | None) -> str:
    """The sandbox host name for views of the server ``namespace``."""

    digest = hashlib.sha256((namespace or "").encode("utf-8")).hexdigest()[:16]
    return f"s{digest}{SANDBOX_HOST_SUFFIX}"


def is_sandbox_host(hostname: str) -> bool:
    """Whether ``hostname`` (no port) is an MCP App sandbox host."""

    return hostname.strip().lower().endswith(SANDBOX_HOST_SUFFIX)


def sandbox_route_allowed(path: str, method: str) -> bool:
    """Whether a sandbox host may serve ``path``: the sandbox document or the service route."""

    from clio_agent.gact.mcp_app_service_declaration import is_service_path  # noqa: PLC0415

    if is_service_path(path):
        return True
    return method in {"GET", "HEAD"} and _SANDBOX_DOCUMENT_PATH.match(path) is not None


__all__ = [
    "SANDBOX_HOST_SUFFIX",
    "is_sandbox_host",
    "sandbox_host_for",
    "sandbox_route_allowed",
]
