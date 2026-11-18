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

**A browser on another machine** reaches CLIO by a configured name
(``gact.allowed_hosts``) and cannot resolve ``*.localhost`` to CLIO. For such a
request the sandbox host comes from ``gact.mcp_apps.sandbox_host``, a name that
also reaches CLIO but is never used for the UI or the API: ``*.apps.example.lan``
gives each server its own ``s<digest>.apps.example.lan`` (wildcard DNS), a plain
name gives every server that one host. With no such name configured the
descriptor is refused with ``sandbox_host_unconfigured`` rather than served on
an origin that carries API authority.
"""

from __future__ import annotations

import hashlib
import logging
import re

SANDBOX_HOST_SUFFIX = ".mcp-apps.localhost"

logger = logging.getLogger(__name__)

_SANDBOX_DOCUMENT_PATH = re.compile(r"^/v1/sessions/[^/]+/mcp-apps/[^/]+/sandbox$")


_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class SandboxHostUnavailable(ValueError):
    """No sandbox host can serve a browser that reached CLIO by this name."""

    reason = "sandbox_host_unconfigured"


def configured_sandbox_host() -> str:
    """``gact.mcp_apps.sandbox_host``: the remote browsers' sandbox host name or pattern.

    Config: ``gact.mcp_apps.sandbox_host`` / ``CLIO_MCP_APP_SANDBOX_HOST`` (default
    empty). ``*.apps.example.lan`` gives one host per server under that name.
    """

    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve("gact.mcp_apps.sandbox_host", env="CLIO_MCP_APP_SANDBOX_HOST", default="")
    pattern = str(value or "").strip().lower().rstrip(".")
    from clio_agent.gact.cors import gact_allowed_hosts  # noqa: PLC0415

    if pattern and (pattern in _LOOPBACK_NAMES or pattern in gact_allowed_hosts()):
        # The UI or API answers to that name: a view there would hold API authority.
        logger.warning(
            "mcp app sandbox host ignored reason=sandbox_host_is_an_api_host host=%s", pattern
        )
        return ""
    return pattern


def _digest(namespace: str | None) -> str:
    return "s" + hashlib.sha256((namespace or "").encode("utf-8")).hexdigest()[:16]


def sandbox_host_for(namespace: str | None, request_host: str = "localhost") -> str:
    """The sandbox host name for views of ``namespace``, for a browser on ``request_host``.

    Raises:
        SandboxHostUnavailable: the browser reached CLIO by a non-loopback name
            and no distinct sandbox host is configured for it.
    """

    if request_host.strip().lower() in _LOOPBACK_NAMES:
        return f"{_digest(namespace)}{SANDBOX_HOST_SUFFIX}"
    pattern = configured_sandbox_host()
    if not pattern:
        raise SandboxHostUnavailable(
            "Interactive views need their own host name when CLIO is reached by "
            f"{request_host!r}: set gact.mcp_apps.sandbox_host to a name that reaches "
            "CLIO and is neither the one this page uses nor in gact.allowed_hosts."
        )
    host = f"{_digest(namespace)}.{pattern[2:]}" if pattern.startswith("*.") else pattern
    if host == request_host.strip().lower():
        raise SandboxHostUnavailable(
            "gact.mcp_apps.sandbox_host must differ from the name the page reaches CLIO by."
        )
    return host


def is_sandbox_host(hostname: str) -> bool:
    """Whether ``hostname`` (no port) is an MCP App sandbox host."""

    name = hostname.strip().lower()
    if name.endswith(SANDBOX_HOST_SUFFIX):
        return True
    pattern = configured_sandbox_host()
    if not pattern:
        return False
    if pattern.startswith("*."):
        return name.endswith(pattern[1:])
    return name == pattern


def sandbox_host_matches(hostname: str, namespace: str | None) -> bool:
    """Whether ``hostname`` is the sandbox host of ``namespace``'s views."""

    name = hostname.strip().lower()
    if name == f"{_digest(namespace)}{SANDBOX_HOST_SUFFIX}":
        return True
    pattern = configured_sandbox_host()
    if not pattern:
        return False
    if pattern.startswith("*."):
        return name == f"{_digest(namespace)}.{pattern[2:]}"
    return name == pattern


def sandbox_route_allowed(path: str, method: str) -> bool:
    """Whether a sandbox host may serve ``path``: the sandbox document or the service route."""

    from clio_agent.gact.mcp_app_service_declaration import is_service_path  # noqa: PLC0415

    if is_service_path(path):
        return True
    return method in {"GET", "HEAD"} and _SANDBOX_DOCUMENT_PATH.match(path) is not None


__all__ = [
    "SANDBOX_HOST_SUFFIX",
    "SandboxHostUnavailable",
    "configured_sandbox_host",
    "is_sandbox_host",
    "sandbox_host_for",
    "sandbox_host_matches",
    "sandbox_route_allowed",
]
