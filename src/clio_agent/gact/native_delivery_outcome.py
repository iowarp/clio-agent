"""Planned vs DELIVERED for native resource attachments.

``plan_resource_delivery`` writes a ledger row saying a resource will ride the
request as ``native``. Nothing then checked whether it did. The attach step could
decline for any of half a dozen reasons — the record vanished, its revision moved,
it was not ready, its media type did not match, it was over the byte ceiling —
and each of those returned ``None`` with no reason recorded anywhere, leaving a
ledger that reported a delivery that never happened.

This module is the seam that closes that gap. The attach helpers note a typed
reason when they decline; the turn settles those notes against the planned rows,
stamping each with ``delivery_confirmed`` and, when false, the reason. The ledger
then records what HAPPENED, not what was planned.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Typed reasons a planned native attachment did not ride the request, in the
#: ``stream_fallback`` reason-catalog style: the code is the queryable fact, the
#: sentence is what a human reads.
NATIVE_ATTACHMENT_SKIP_REASONS: dict[str, str] = {
    "delivery_not_native": (
        "the delivery plan for this resource did not select native, so its bytes were "
        "never eligible to ride the request"
    ),
    "no_workspace_context": (
        "the attach step ran without an app/workspace to resolve the resource against"
    ),
    "resource_missing": (
        "the referenced resource no longer exists in this workspace; it may have been "
        "deleted between planning and dispatch"
    ),
    "resource_revision_mismatch": (
        "the resource advanced to a newer revision after the plan was made; the planned "
        "revision's bytes were not substituted with different ones"
    ),
    "resource_not_ready": (
        "the resource is not in the ready state (still uploading, quarantined, or failed), "
        "so its bytes are not safe to deliver"
    ),
    "resource_media_type_mismatch": (
        "the resource's detected media type does not match the native lane the plan chose"
    ),
    "resource_over_attachment_bound": (
        "the resource is larger than the configured per-attachment byte ceiling; refused "
        "before it was read and base64-expanded"
    ),
    "image_part_undecodable": ("an inline image part could not be decoded into a model input"),
}


#: Reasons that describe the plan working as designed rather than a decline. A
#: non-native plan means the resource was never eligible for the native lane, so
#: there is no promise to settle and nothing to buffer.
_NOT_A_DECLINE: frozenset[str] = frozenset({"delivery_not_native"})

#: Cap on unsettled notes. Settling pops only the keys its own turn planned, so a
#: note for a part no turn ever settles (no workspace bound, a plan that never
#: reached the ledger) would otherwise accumulate for the life of the process.
#: Keys are popped in insertion order; the newest notes are the ones a settle is
#: about to ask for.
_MAX_PENDING_NOTES = 256


def _pending(app: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the per-app buffer of decline notes, creating it on first use."""

    buffer = getattr(app.state, "native_delivery_outcomes", None)
    if not isinstance(buffer, dict):
        buffer = {}
        app.state.native_delivery_outcomes = buffer
    return buffer


def note_delivery_outcome(
    app: Any,
    *,
    resource_id: str,
    revision: str,
    kind: str,
    reason: str,
    detail: str = "",
    resource_name: str = "",
) -> None:
    """Record that one planned native attachment declined to ride the request.

    Buffered by ``(resource_id, revision)`` so :func:`settle_native_deliveries`
    can stamp the matching ledger row once the whole attach pass is done. An
    uncatalogued reason raises rather than entering the ledger as an ad-hoc
    string.
    """

    if reason not in NATIVE_ATTACHMENT_SKIP_REASONS:
        raise ValueError(f"Unknown native attachment skip reason: {reason}")
    if reason in _NOT_A_DECLINE:
        return
    if app is None or getattr(app, "state", None) is None or not resource_id:
        return
    buffer = _pending(app)
    buffer[(str(resource_id), str(revision))] = {
        "reason": reason,
        "detail": detail or NATIVE_ATTACHMENT_SKIP_REASONS[reason],
        "kind": kind,
    }
    while len(buffer) > _MAX_PENDING_NOTES:
        oldest = next(iter(buffer))
        dropped = buffer.pop(oldest)
        logger.warning(
            "dropped an unsettled native-attachment note reason=native_note_buffer_full "
            "resource_id=%s revision=%s note=%s",
            oldest[0],
            oldest[1],
            dropped.get("reason", ""),
        )
    logger.warning(
        "planned native attachment not delivered reason=%s resource_id=%s revision=%s name=%s",
        reason,
        resource_id,
        revision,
        resource_name,
    )


def settle_native_deliveries(
    app: Any, *, workspace_id: str, message_id: str, parts: list[Any]
) -> None:
    """Stamp every native-planned resource part with its real delivery outcome.

    Called once, after the attach pass has built the turn's model inputs, so the
    ledger records the delivery that actually happened. A part with no buffered
    decline note DID ride the request; one with a note carries
    ``delivery_confirmed=False`` plus the typed reason.
    """

    if app is None or getattr(app, "state", None) is None or not workspace_id:
        return
    store = getattr(app.state, "resource_delivery_store", None)
    if store is None:
        return
    buffer = _pending(app)
    for part in parts:
        if getattr(part, "type", "") != "resource_ref":
            continue
        metadata = getattr(part, "metadata", None) or {}
        delivery = metadata.get("delivery") if isinstance(metadata, dict) else None
        if not isinstance(delivery, dict) or delivery.get("representation") != "native":
            continue
        key = (str(part.resource_id), str(part.resource_revision))
        note = buffer.pop(key, None)
        store.record_outcome(
            workspace_id=workspace_id,
            message_id=message_id,
            resource_id=str(part.resource_id),
            resource_revision=str(part.resource_revision),
            delivered=note is None,
            reason_code=str((note or {}).get("reason") or ""),
            reason=str((note or {}).get("detail") or ""),
        )


__all__ = [
    "NATIVE_ATTACHMENT_SKIP_REASONS",
    "note_delivery_outcome",
    "settle_native_deliveries",
]
