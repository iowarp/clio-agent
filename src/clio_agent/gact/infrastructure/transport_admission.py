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

Admission follows the same trust rule as the HTTP surface
(:class:`clio_agent.gact.auth.BearerAuthMiddleware`): a loopback peer is
admitted without a bearer token, any other peer must present the configured
token. The Desktop attaches to an already-running local CLIO with an empty
token by design (``supervisor_attach.rs``), so requiring the token from
loopback here made every remote deploy through such a CLIO fail while all of
its HTTP calls succeeded.
"""

from __future__ import annotations

import base64
import hmac
import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import WebSocket
from starlette.datastructures import State

from clio_agent.gact.auth import (
    PeerAddressGetter,
    _header_bearer_token,
    is_loopback_address,
    peer_address_from_scope,
)
from clio_agent.gact.infrastructure.models import InfrastructureTarget

logger = logging.getLogger(__name__)

INFRASTRUCTURE_TRANSPORT_PROTOCOL = "clio.infrastructure.v1"
"""Legacy bridge protocol: ``open`` means attached; refusals are pre-handshake."""

INFRASTRUCTURE_TRANSPORT_PROTOCOL_V2 = "clio.infrastructure.v2"
"""Bridge protocol with an ``attached`` frame and typed refusal close frames."""

RefusalReason = Literal["authentication_required", "target_not_found", "target_not_ssh"]


@dataclass(frozen=True)
class TransportRefusal:
    """Why CLIO will not attach a Desktop SSH transport to a target.

    Attributes:
        code: WebSocket close code (4401, 4404 or 4409), stable across versions.
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
        app_state: The application state carrying ``bearer_token`` and the
            ``peer_address_getter`` seam shared with the HTTP middleware.

    Returns:
        ``None`` when the attachment is admitted, otherwise the typed refusal.
    """

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
    getter: PeerAddressGetter = getattr(app_state, "peer_address_getter", peer_address_from_scope)
    if is_loopback_address(getter(websocket.scope)):
        return None
    supplied = _header_bearer_token(websocket.scope) or _websocket_protocol_token(websocket)
    if hmac.compare_digest(supplied, expected):
        return None
    return TransportRefusal(
        4401,
        "authentication_required",
        "A non-loopback peer did not present this CLIO's bearer token.",
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


def _websocket_protocol_token(websocket: WebSocket) -> str:
    """Decode a browser-compatible bearer carried as a WebSocket subprotocol."""

    raw = websocket.headers.get("sec-websocket-protocol", "")
    for value in (part.strip() for part in raw.split(",")):
        if not value.startswith("clio-bearer."):
            continue
        encoded = value.removeprefix("clio-bearer.")
        padding = "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return ""
