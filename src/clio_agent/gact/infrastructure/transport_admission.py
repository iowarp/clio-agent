"""Admission for the Desktop SSH transport WebSocket, with typed refusals (#1478).

The Desktop bridges its OpenSSH session to CLIO over
``/v1/infrastructure/targets/{id}/transport``. Before this module, every
refusal closed the socket *before* the WebSocket handshake completed, which an
ASGI server turns into a bare HTTP 403: the close code never reached the
browser, so the Desktop could only say "CLIO rejected the SSH transport
attachment" with no reason, and CLIO logged nothing either.

Refusals are now typed (:class:`TransportRefusal`), always logged, and
delivered to clients that negotiate ``clio.infrastructure.v2`` as a real close
frame (code + machine reason) after the handshake completes. A client that
only offers ``clio.infrastructure.v1`` keeps the old pre-handshake refusal it
knows how to read (an error event), because accepting it first would look like
success to that client.

Admission is checked in this order:

* **Origin** (defense in depth): browsers apply no CORS to WebSockets, so any
  page the user visits could open this socket. A browser always sends
  ``Origin`` on a WebSocket; it must pass the one Host/Origin policy HTTP uses
  (:func:`clio_agent.gact.origin_guard.browser_origin_allowed`: the Desktop
  WebView, ``gact.cors.origins``, or this server's own origin on an allowed
  host). A request with no ``Origin`` comes from a non-browser client, which
  no web page can impersonate. A token-less upgrade to a foreign ``Host`` is
  refused earlier by :class:`~clio_agent.gact.origin_guard.OriginGuardMiddleware`.
* **Target**: it must exist and be an SSH target.
* **Bearer token**: always required when this CLIO enforces one, loopback or
  not. A loopback peer is not trusted here the way HTTP trusts it, because a
  cross-origin page cannot read HTTP responses but could act as the SSH bridge
  (receive CLIO's exec requests and forge their results). The Desktop gets the
  token of a server it did not spawn from that server's credential record
  (:mod:`clio_agent.gact.server_credentials`).
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import WebSocket
from starlette.datastructures import State

from clio_agent.gact.auth import supplied_bearer_token
from clio_agent.gact.infrastructure.models import InfrastructureTarget
from clio_agent.gact.origin_guard import browser_origin_allowed

logger = logging.getLogger(__name__)

INFRASTRUCTURE_TRANSPORT_PROTOCOL = "clio.infrastructure.v1"
"""Legacy bridge protocol: ``open`` means attached; refusals are pre-handshake."""

INFRASTRUCTURE_TRANSPORT_PROTOCOL_V2 = "clio.infrastructure.v2"
"""Bridge protocol with an ``attached`` frame and typed refusal close frames."""

RefusalReason = Literal[
    "origin_not_allowed", "authentication_required", "target_not_found", "target_not_ssh"
]


@dataclass(frozen=True)
class TransportRefusal:
    """Why CLIO will not attach a Desktop SSH transport to a target.

    Attributes:
        code: WebSocket close code (4401, 4403, 4404 or 4409), stable across versions.
        reason: Machine-readable reason sent as the close-frame reason.
        detail: Human-readable explanation for CLIO's own log.
    """

    code: int
    reason: RefusalReason
    detail: str


def negotiate_subprotocol(websocket: WebSocket) -> str | None:
    """Pick the one bridge subprotocol both sides agree on, preferring v2.

    Per RFC 6455 the server echoes exactly one value the client offered, or
    none. The ``clio-bearer.<token>`` entry only carries the bearer token and
    is never selected (#1440).

    Args:
        websocket: The incoming, not yet accepted, WebSocket.

    Returns:
        ``clio.infrastructure.v2`` or ``clio.infrastructure.v1`` when offered,
        otherwise ``None``.
    """

    offered = {
        part.strip()
        for part in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if part.strip()
    }
    for protocol in (INFRASTRUCTURE_TRANSPORT_PROTOCOL_V2, INFRASTRUCTURE_TRANSPORT_PROTOCOL):
        if protocol in offered:
            return protocol
    return None


def transport_refusal(
    websocket: WebSocket,
    target_id: str,
    target: InfrastructureTarget | None,
    app_state: State,
) -> TransportRefusal | None:
    """Decide whether a Desktop transport may attach to ``target``.

    Args:
        websocket: The incoming WebSocket (headers and peer address).
        target_id: The target id from the request path.
        target: The stored target, or ``None`` when no such target exists.
        app_state: The application state carrying ``bearer_token``.

    Returns:
        ``None`` when the attachment is admitted, otherwise the typed refusal.
    """

    origin = websocket.headers.get("origin")
    if origin is not None and not browser_origin_allowed(
        origin, websocket.scope, websocket.headers
    ):
        return TransportRefusal(
            4403, "origin_not_allowed", f"Origin {origin!r} may not attach an SSH transport."
        )
    if target is None:
        return TransportRefusal(
            4404, "target_not_found", f"No infrastructure target has the id {target_id!r}."
        )
    if target.kind != "ssh":
        return TransportRefusal(
            4409,
            "target_not_ssh",
            f"Target {target_id!r} is a {target.kind} target, not an SSH host.",
        )
    expected = getattr(app_state, "bearer_token", None)
    if expected is None:
        return None
    supplied = supplied_bearer_token(websocket.scope)
    if hmac.compare_digest(supplied, expected):
        return None
    return TransportRefusal(
        4401,
        "authentication_required",
        "The client did not present this CLIO's bearer token.",
    )


async def refuse_transport(websocket: WebSocket, target_id: str, refusal: TransportRefusal) -> None:
    """Log a refusal and deliver it in the form the client can read.

    Args:
        websocket: The incoming, not yet accepted, WebSocket.
        target_id: The target id from the request path.
        refusal: The typed refusal to deliver.
    """

    protocol = negotiate_subprotocol(websocket)
    logger.warning(
        "infrastructure transport refused: target=%s reason=%s code=%s protocol=%s: %s",
        target_id,
        refusal.reason,
        refusal.code,
        protocol,
        refusal.detail,
    )
    if protocol == INFRASTRUCTURE_TRANSPORT_PROTOCOL_V2:
        await websocket.accept(subprotocol=protocol)
        await websocket.close(code=refusal.code, reason=refusal.reason)
        return
    # A v1 client treats a completed handshake as "attached"; refuse it before
    # the handshake so it still sees an error (the reason is in the log above).
    await websocket.close(code=refusal.code)
