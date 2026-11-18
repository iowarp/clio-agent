"""A view's service as a server declares it (MA23), with no runtime dependencies.

A server CLIO launched may give the view it opens a live service behind it: a
loopback HTTP and WebSocket listener on the machine CLIO runs on. It declares
the service in the private ``_meta`` of the result that opens the view::

    _meta["ai.iowarp.clio/service"] = {"port": 8123, "token": "..."}

CLIO takes the declaration off the result before anything else sees it, so the
view, the transcript and the model never receive the port or the token, and
serves the port under the app's own capability-bound route (see
:mod:`clio_agent.gact.mcp_app_service_proxy`). This module holds the pure parts
both the registry and the request guards need: parsing and stripping the
declaration, the route's path shape, and the loopback test the sandbox CSP uses
to refuse direct loopback access from a view.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: The private ``_meta`` key a server declares its view's service under.
SERVICE_META_KEY = "ai.iowarp.clio/service"

#: The ``hostContext`` key the view reads the service URL from.
SERVICE_HOST_CONTEXT_KEY = SERVICE_META_KEY

_MAX_TOKEN_CHARS = 4096

#: ``/v1/sessions/{sid}/mcp-apps/{app_id}/service/{data_ref}[/rest]``. The
#: capability (``data_ref``) is a path segment, so a page served under it keeps
#: the capability in every relative URL and WebSocket it opens.
SERVICE_PATH = re.compile(
    r"^/v1/sessions/(?P<sid>[^/]+)/mcp-apps/(?P<app_id>[^/]+)/service/(?P<data_ref>[^/]+)(?P<rest>/.*)?$"
)


@dataclass(frozen=True)
class ServiceRefusal:
    """Why CLIO does not carry a declared service.

    Attributes:
        reason: The typed reason, from
            :data:`clio_agent.gact.mcp_app_service.SERVICE_REASONS`.
        message: The person-facing sentence.
    """

    reason: str
    message: str

    def to_wire(self) -> dict[str, str]:
        """Return the descriptor shape ``{reason, message}``."""

        return {"reason": self.reason, "message": self.message}


@dataclass
class ViewService:
    """One app's declared service. The token never leaves this object.

    Attributes:
        port: The loopback port on CLIO's machine.
        token: Sent to the service on every proxied request; never serialized.
        refusal: Set once CLIO has decided not to carry the service.
        checked: Whether the launched-server check has run.
    """

    port: int
    token: str = field(repr=False)
    refusal: ServiceRefusal | None = None
    checked: bool = False


def _invalid(detail: str) -> ViewService:
    return ViewService(
        port=0,
        token="",
        refusal=ServiceRefusal(
            "service_declaration_invalid",
            f"The server declared its view's service incorrectly: {detail}.",
        ),
        checked=True,
    )


def parse_service_declaration(value: Any) -> ViewService:
    """Validate one ``{port, token}`` declaration.

    A malformed declaration still yields a :class:`ViewService`, refused with
    ``service_declaration_invalid``, so the view's descriptor says why its
    service is missing instead of dropping it silently.
    """

    if not isinstance(value, Mapping):
        return _invalid("it is not an object with a port and a token")
    port = value.get("port")
    token = value.get("token")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return _invalid("the port is not a number from 1 to 65535")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > _MAX_TOKEN_CHARS
        or any(ch in token for ch in "\r\n\0")
    ):
        return _invalid("the token is not a single-line string")
    return ViewService(port=port, token=token)


def _meta_of(result: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    for key in ("_meta", "meta"):
        meta = result.get(key)
        if isinstance(meta, Mapping):
            return key, meta
    return None


def split_service_declaration(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], ViewService | None]:
    """Return the result without its service declaration, and the declaration."""

    found = _meta_of(result)
    if found is None or SERVICE_META_KEY not in found[1]:
        return dict(result), None
    key, meta = found
    service = parse_service_declaration(meta[SERVICE_META_KEY])
    stripped = dict(result)
    rest = {name: value for name, value in meta.items() if name != SERVICE_META_KEY}
    if rest:
        stripped[key] = rest
    else:
        stripped.pop(key, None)
    return stripped, service


def strip_service_declaration(result: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Remove a declaration from a result that does not open a view.

    Returns the stripped result and whether a declaration was removed.
    """

    stripped, service = split_service_declaration(result)
    return stripped, service is not None


def service_prefix(sid: str, app_id: str, data_ref: str) -> str:
    """The route prefix of one app's service, without a trailing slash."""

    return f"/v1/sessions/{sid}/mcp-apps/{app_id}/service/{data_ref}"


def is_service_path(path: str) -> bool:
    """Whether ``path`` is under an app's capability-bound service route."""

    return SERVICE_PATH.match(path) is not None


def _source_host(source: str) -> str:
    text = source.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    elif text.endswith(":"):
        return ""  # a bare scheme such as ``blob:`` or ``data:``
    text = text.split("/", 1)[0]
    if text.startswith("["):
        return text[1:].split("]", 1)[0]
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def is_loopback_source(source: str) -> bool:
    """Whether a CSP source expression names this machine's loopback.

    A view never reaches a loopback address directly (MA23): its service comes
    through CLIO's route, so declared ``127.0.0.1``/``localhost``/``::1``
    sources are refused.
    """

    host = _source_host(source)
    if not host:
        return False
    if host in {"localhost", "0.0.0.0"} or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = [
    "SERVICE_HOST_CONTEXT_KEY",
    "SERVICE_META_KEY",
    "SERVICE_PATH",
    "ServiceRefusal",
    "ViewService",
    "is_loopback_source",
    "is_service_path",
    "parse_service_declaration",
    "service_prefix",
    "split_service_declaration",
    "strip_service_declaration",
]
