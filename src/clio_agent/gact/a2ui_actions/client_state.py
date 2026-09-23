"""A2UI client-reported data-model and error ingestion (S5).

Two distinct client-authored objects this module owns:

1. **``a2uiClientDataModel`` per-surface filtering.** S3's
   ``apply_client_metadata_guards`` already refuses the WHOLE request when no
   LIVE ``sendDataModel`` surface exists in the session; this module narrows
   further, per surface KEY, once the request has otherwise been accepted —
   an offending surface entry is dropped with a typed reason, the rest of
   the (still validated) data model is carried through.
2. **Client error reports** (the official ``{version, error: {...}}``
   envelope, S1 ``A2UIClientMessage``). Routed BEFORE any surface lookup
   (issue #1372 S6-review comment): the client reporting its OWN rejection
   is accepted (200, an error record id) even when the named ``surfaceId``
   cannot be resolved -- but an UNKNOWN surface (one this session never
   created) is a persisted dead end (``a2ui_error_surface_unknown``), NEVER
   a re-drive target (adversarial review finding #3, BLOCKING: an unknown
   surface used to look like "first VALIDATION_FAILED" on every submission,
   re-driving the agent unboundedly). For a KNOWN surface, ``VALIDATION_
   FAILED`` gets exactly ONE repair delivery per surface revision -- counted
   only over repairs that actually reached ``delivered``/``consumed``
   (finding #4: a repair REFUSED, e.g. an uncorrelated ``waiting_user``, does
   not burn the budget) -- a second DELIVERED repair's report for the same
   revision marks the surface ``state="failed"`` (``gact/a2ui_actions/
   record.py::fold_action_records`` applies that projection rule); a generic
   error code is persisted for the record but never delivered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_schemas.a2ui.v0_9_1.data_model import A2UIClientDataModel
from clio_schemas.a2ui.v0_9_1.messages import A2UIGenericError
from clio_schemas.a2ui.v0_9_1.messages import A2UIValidationError as A2UIValidationFailedReport
from fastapi import HTTPException

from clio_agent.gact.a2ui_actions.delivery import deliver_to_agent
from clio_agent.gact.a2ui_actions.narration import repair_narration
from clio_agent.gact.a2ui_actions.record import (
    ActionRecord,
    lifecycle_event_payload,
    new_record_id,
    persist_new_record,
    persist_transition,
    prior_error_count_for_revision,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=recoverable)
        ).model_dump(exclude_none=True),
    )


def filter_owned_data_model(
    app: "FastAPI", session_id: str, data_model: A2UIClientDataModel
) -> A2UIClientDataModel:
    """Keep only surfaces this session owns, is live, and requested ``sendDataModel``.

    Every dropped entry is recorded through the S2 per-session ledger (never
    silently dropped) with one of two typed reasons: ``a2ui_data_model_
    foreign_surface`` (no such LIVE surface in this session at all) or
    ``a2ui_data_model_not_requested`` (the surface exists but was not created
    with ``sendDataModel: true``, or has since been deleted). The rest of the
    request still proceeds — S3's session-level gate already proved at least
    one surface in this data model qualifies.
    """

    store = getattr(app.state, "a2ui_store", None)
    registry = getattr(app.state, "a2ui_catalogs", None)
    if store is None:
        return data_model
    live = {str(row.get("id") or ""): row for row in store.list_wire(session_id)}
    kept: dict[str, Any] = {}
    for surface_id, value in data_model.surfaces.items():
        row = live.get(surface_id)
        if row is None:
            if registry is not None:
                registry.record_session_reason(
                    session_id, "a2ui_data_model_foreign_surface", surface_id=surface_id
                )
            continue
        sends_data_model = row.get("state") != "deleted" and any(
            isinstance(message, Mapping)
            and isinstance(message.get("createSurface"), Mapping)
            and bool(message["createSurface"].get("sendDataModel"))
            for message in row.get("messages") or []
        )
        if not sends_data_model:
            if registry is not None:
                registry.record_session_reason(
                    session_id, "a2ui_data_model_not_requested", surface_id=surface_id
                )
            continue
        kept[surface_id] = value
    return data_model.model_copy(update={"surfaces": kept})


def is_error_envelope(client_message: Mapping[str, Any]) -> bool:
    """Return whether a client->server envelope reports an error, not an action."""

    return "error" in client_message and client_message.get("error") is not None


async def ingest_client_error(
    app: "FastAPI",
    session_id: str,
    client_message: Mapping[str, Any],
    correlation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Ingest one official client error report and return the HTTP result body.

    Never gates on the named ``surfaceId`` resolving to a live surface — a
    renderer reporting its OWN rejection is accepted regardless (issue #1372
    S6-review comment: "route error envelopes BEFORE the surface lookup").
    """

    from clio_schemas import A2UIClientMessage  # noqa: PLC0415
    from pydantic import ValidationError as _PydanticValidationError  # noqa: PLC0415

    from clio_agent.gact.a2ui import A2UIValidationError as _EnvelopeError  # noqa: PLC0415

    try:
        parsed = A2UIClientMessage.model_validate(client_message)
    except _PydanticValidationError as exc:
        raise _EnvelopeError(f"A2UI client error report is malformed: {exc.errors()}") from exc
    error = parsed.error
    if error is None:
        raise _EnvelopeError("A2UI client message carries no error to ingest")

    store = getattr(app.state, "a2ui_store", None)
    registry = getattr(app.state, "a2ui_catalogs", None)
    surface = store.get(session_id, error.surfaceId) if store is not None else None
    revision = surface.revision if surface is not None else 0
    existing_actions = surface.actions if surface is not None else []
    correlation_fields: dict[str, Any] = {**dict(correlation or {}), "revision": revision}
    if surface is not None and surface.part_id:
        correlation_fields.setdefault("part_id", surface.part_id)

    record = ActionRecord(
        id=new_record_id(),
        session_id=session_id,
        surface_id=error.surfaceId,
        catalog_id=surface.catalog_id if surface is not None else "",
        kind="error",
        envelope=dict(client_message),
        # Adversarial review finding #9: the error's own code, not "", so a
        # consumer reading action_name off the lifecycle event/record never
        # sees a blank field for an error report.
        action_name=str(error.code),
        source_component_id="",
        correlation=correlation_fields,
    )
    # Mirrors the main dispatcher's order: persist the ``received`` snapshot
    # first, unconditionally, so an error record's full lifecycle (including
    # a terminal one) is auditable on the transcript exactly like an action's.
    persist_new_record(app, record)
    app.state.bus.publish(_event(session_id, record))

    # Adversarial review finding #3 (BLOCKING): a surface this session does
    # not own/know is a dead end, never a re-drive target -- reported once,
    # typed, never delivered. Without this an attacker (or a stale/forged
    # surfaceId) could re-drive the agent unboundedly, since an unknown
    # surface always looked like "first VALIDATION_FAILED at revision 0".
    if surface is None:
        if registry is not None:
            registry.record_session_reason(
                session_id, "a2ui_error_surface_unknown", surface_id=error.surfaceId
            )
        failed = record.transition(
            state="failed", delivery="rejected", reason="a2ui_error_surface_unknown"
        )
        persist_transition(app, failed)
        app.state.bus.publish(_event(session_id, failed))
        return _wire(failed)

    if isinstance(error, A2UIGenericError):
        failed = record.transition(
            state="failed", delivery="rejected", reason="a2ui_client_error_unhandled"
        )
        persist_transition(app, failed)
        app.state.bus.publish(_event(session_id, failed))
        return _wire(failed)

    assert isinstance(error, A2UIValidationFailedReport)
    prior = prior_error_count_for_revision(existing_actions, revision)
    if prior >= 1:
        failed = record.transition(
            state="failed", delivery="rejected", reason="a2ui_repair_exhausted"
        )
        persist_transition(app, failed)
        app.state.bus.publish(_event(session_id, failed))
        return _wire(failed)

    narration = repair_narration(error.surfaceId, error.path, error.message)
    delivered = await deliver_to_agent(
        app,
        session_id,
        record.transition(narration=narration),
        narration=narration,
        context={"surface_id": error.surfaceId, "path": error.path, "message": error.message},
    )
    if delivered.state == "failed" and delivered.reason == "a2ui_waiting_user_uncorrelated":
        # Adversarial review finding #4: consistency with the action door --
        # the SAME typed 409, and (since this record's state stayed
        # "failed", never "delivered"/"consumed") prior_error_count_for_revision
        # does not count it, so the repair budget is not burned.
        raise _error(
            409,
            "a2ui_waiting_user_uncorrelated",
            "session is waiting_user and this error report does not correlate to the "
            "pending question",
            recoverable=True,
        )
    return _wire(delivered)


def _event(session_id: str, record: ActionRecord) -> Any:
    from clio_agent.gact.events import Event  # noqa: PLC0415

    return Event(
        type=f"a2ui.action.{record.state}",
        session_id=session_id,
        payload=lifecycle_event_payload(record),
    )


def _wire(record: ActionRecord) -> dict[str, Any]:
    return {
        "status": "accepted",
        "action_id": record.id,
        "state": record.state,
        "delivery": record.delivery,
        "reason": record.reason,
        "surface_id": record.surface_id,
    }


__all__ = ["filter_owned_data_model", "ingest_client_error", "is_error_envelope"]
