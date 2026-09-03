"""Composer-lane event projections for GACT 0.3.

The composer lanes (durable pending steers, the editable future-message queue,
and the acceptance envelope that fronts both) publish nine event types. Without
a projector each of them reached a 0.3 client as its raw 0.2 payload — some
with no ``entity_id`` at all — so a client could not correlate an update with
the row it already holds, and the parts inside a queued message never became
0.3 blocks. This module owns those projections; ``protocol.v3.event`` only
registers them.

Two identity decisions are worth naming:

* **A queued message is an entity**, keyed by its own id, and carries the
  server's authoritative ``revision`` so a client can guard its edits.
* **The queue ORDER is its own entity**, keyed ``queue:<session_id>``. A reorder
  is one transaction over the whole queue — it has no single subject row — so
  projecting it per-row would either invent an arbitrary subject or ship an
  entity-less envelope. The order entity carries ``ordered_ids`` plus every
  row's new revision, which is exactly what a client needs to apply it.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from clio_agent.gact.events import Event
from clio_agent.gact.protocol.v3 import Projection
from clio_agent.gact.protocol.v3.message import message_to_v3, part_to_v3_block

QUEUE_ENTITY_PREFIX = "queue:"


def queue_entity_id(session_id: str) -> str:
    """Stable entity id for one session's queue ORDER."""

    return f"{QUEUE_ENTITY_PREFIX}{session_id}"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _blocks(parts: Any) -> list[dict[str, Any]]:
    rows = parts if isinstance(parts, list) else []
    return [part_to_v3_block(part) for part in rows if isinstance(part, Mapping)]


def queued_message_to_v3(payload: Mapping[str, Any], session_id: str) -> dict[str, Any]:
    """Project one persisted queued message into the 0.3 entity shape."""

    projected: dict[str, Any] = {
        "id": str(payload.get("id") or ""),
        "session_id": str(payload.get("session_id") or session_id),
        "revision": int(payload.get("revision") or 1),
        "position": int(payload.get("position") or 0),
        "blocks": _blocks(payload.get("parts")),
        "created_at": str(payload.get("created_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "behavior": dict(_mapping(payload.get("behavior"))),
        "model": dict(_mapping(payload.get("model"))),
    }
    for key in ("client_message_id", "idempotency_key"):
        value = str(payload.get(key) or "")
        if value:
            projected[key] = value
    return projected


def message_accepted(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """Acceptance of a user message — uniform across the start and steer branches."""

    del session
    message = _mapping(payload.get("message"))
    entity_id = str(message.get("id") or payload.get("message_id") or "")
    projected: dict[str, Any] = {
        "id": entity_id,
        "session_id": str(message.get("session_id") or event.session_id),
        "delivery": str(payload.get("delivery") or ""),
        "state": str(payload.get("state") or ""),
        "accepted_at": str(message.get("created_at") or event.occurred_at),
        "effective_model": dict(_mapping(payload.get("effective_model"))),
        "behavior": dict(_mapping(payload.get("behavior"))),
    }
    if message:
        projected["message"] = message_to_v3(message)
    return Projection("message.accepted", projected, entity_id)


def message_cancelled(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """A user message withdrawn before the model ever saw it."""

    del session
    entity_id = str(payload.get("message_id") or "")
    projected = {
        "id": entity_id,
        "session_id": str(payload.get("session_id") or event.session_id),
        "cancelled_at": event.occurred_at,
    }
    return Projection("message.cancelled", projected, entity_id)


def pending_steer_cancelled(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """The steer INTENT behind a cancelled message reaching its terminal state."""

    del session
    entity_id = str(payload.get("message_id") or "")
    projected = {
        "id": entity_id,
        "session_id": str(payload.get("session_id") or event.session_id),
        "state": "cancelled",
        "cancelled_at": event.occurred_at,
    }
    return Projection("pending_steer.cancelled", projected, entity_id)


def queued_message_upserted(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """Create + edit collapse onto one upsert, as everywhere else in 0.3."""

    del session
    projected = queued_message_to_v3(payload, event.session_id)
    return Projection("queued_message.upserted", projected, projected["id"])


def queued_message_deleted(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """A future message removed before it was ever promoted."""

    del session
    entity_id = str(payload.get("id") or "")
    projected = {
        "id": entity_id,
        "session_id": str(payload.get("session_id") or event.session_id),
        "revision": int(payload.get("revision") or 1),
        "deleted_at": event.occurred_at,
    }
    return Projection("queued_message.deleted", projected, entity_id)


def queued_message_reordered(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """One transaction over the whole queue — see the module docstring."""

    del session
    rows = [
        queued_message_to_v3(row, event.session_id)
        for row in (payload.get("queued_messages") or [])
        if isinstance(row, Mapping)
    ]
    entity_id = queue_entity_id(event.session_id)
    projected = {
        "id": entity_id,
        "session_id": event.session_id,
        "ordered_ids": [row["id"] for row in rows],
        "queued_messages": rows,
        "reordered_at": event.occurred_at,
    }
    return Projection("queued_message.reordered", projected, entity_id)


def queued_message_promoted(event: Event, payload: dict[str, Any], session: Any) -> Projection:
    """A future message leaving the queue and becoming an accepted message."""

    del session
    entity_id = str(payload.get("queued_message_id") or "")
    acceptance = _mapping(payload.get("acceptance"))
    projected = {
        "id": entity_id,
        "session_id": event.session_id,
        "message_id": str(acceptance.get("message_id") or ""),
        "delivery": str(acceptance.get("delivery") or ""),
        "state": str(acceptance.get("state") or ""),
        "automatic": bool(payload.get("automatic", False)),
        "status_code": int(payload.get("status_code") or 0),
        "promoted_at": event.occurred_at,
    }
    return Projection("queued_message.promoted", projected, entity_id)


def queued_message_promotion_failed(
    event: Event, payload: dict[str, Any], session: Any
) -> Projection:
    """A promotion that did not take. The row stays durable; ``cause`` says why.

    ``error`` names the EVENT; the machine-readable acceptance reason rides
    ``cause`` (status code, typed error, message, details). This projector used to
    whitelist ``error``/``recoverable``/``retry_on`` only, so the typed cause the
    server had already unwrapped never reached the client -- the client saw the
    generic ``queue_auto_promotion_failed`` the unwrap exists to replace. A
    TERMINAL failure also carries ``blocks_queue`` + ``recovery_actions`` (and no
    ``retry_on``), because the queue behind that head is frozen until it is edited.
    """

    del session
    entity_id = str(payload.get("queued_message_id") or "")
    projected: dict[str, Any] = {
        "id": entity_id,
        "session_id": event.session_id,
        "error": str(payload.get("error") or "queue_auto_promotion_failed"),
        "cause": dict(_mapping(payload.get("cause"))),
        "recoverable": bool(payload.get("recoverable", True)),
        "failed_at": event.occurred_at,
    }
    if payload.get("blocks_queue"):
        projected["blocks_queue"] = True
        projected["recovery_actions"] = list(payload.get("recovery_actions") or [])
    else:
        projected["retry_on"] = list(payload.get("retry_on") or [])
    return Projection("queued_message.promotion_failed", projected, entity_id)


# Annotated rather than inferred: ``protocol.v3.event`` imports this table while
# this module imports ``protocol.v3`` (for ``Projection``), and across that cycle
# mypy cannot resolve an inferred dict type at the import site
# (``has-type``). The annotation is the same shape ``event._Projector`` declares.
COMPOSER_PROJECTORS: dict[str, Callable[[Event, dict[str, Any], Any], Projection]] = {
    "message.accepted": message_accepted,
    "message.cancelled": message_cancelled,
    "pending_steer.cancelled": pending_steer_cancelled,
    "queued_message.created": queued_message_upserted,
    "queued_message.updated": queued_message_upserted,
    "queued_message.deleted": queued_message_deleted,
    "queued_message.reordered": queued_message_reordered,
    "queued_message.promoted": queued_message_promoted,
    "queued_message.promotion_failed": queued_message_promotion_failed,
}

__all__ = [
    "COMPOSER_PROJECTORS",
    "QUEUE_ENTITY_PREFIX",
    "queue_entity_id",
    "queued_message_to_v3",
]
