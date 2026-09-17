"""The A2UI action dispatcher: durable, idempotent, destination-routed (S5).

Moved out of ``routes/a2ui.py`` (docs/design/a2ui-compat-campaign-2026-09.md
S5), which keeps only the routes (negotiation header check, body parsing) and
calls :func:`dispatch_action` with the parsed pieces. Order, every call:

1. **Route an error envelope FIRST**, before any surface lookup
   (``client_state.ingest_client_error`` — issue #1372's S6-review comment).
2. Resolve the surface + its catalog, validate the client action (S2).
3. **Idempotency lookup**, cheap peek first (BEFORE the data-model guards --
   adversarial review finding #6, so a REPLAY never re-records a per-surface
   data-model reason), then the AUTHORITATIVE atomic check-and-persist inside
   ``A2UIStore.persist_action_part`` (finding #1, BLOCKING: the lookup and
   the persist happen under the SAME per-session lock, closing the
   check-then-persist race). A duplicate of a ``failed`` record re-raises
   the SAME typed refusal (finding #5) rather than returning 200.
4. **Deliver by destination**: ``agent`` (idle/running/waiting_user, via
   :mod:`clio_agent.gact.a2ui_actions.delivery`), ``permission`` (the
   existing session-scoped ``resolve_permission`` path), ``run`` (the
   sidecar's declared ``cancel``/``retry`` operation). Every owner call is
   wrapped so an unexpected raise durably fails the record BEFORE the same
   exception propagates (finding #2, BLOCKING).

⚑ No deterministic decision-making in core (.claude/CLAUDE.md #1): every
branch below routes on the sidecar's DECLARED ``destination``/``operation``
and the session's own STATE — never on the action's name or its prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from fastapi import HTTPException

from clio_agent.gact.a2ui import (
    A2UIEventContextInvalidError,
    A2UIFunctionNotInCatalogError,
    A2UIValidationError,
    validate_client_action,
)
from clio_agent.gact.a2ui_actions.client_state import (
    filter_owned_data_model,
    ingest_client_error,
    is_error_envelope,
)
from clio_agent.gact.a2ui_actions.delivery import deliver_to_agent
from clio_agent.gact.a2ui_actions.narration import narration_for
from clio_agent.gact.a2ui_actions.record import (
    ActionRecord,
    compute_idempotency_key,
    fail_and_publish,
    find_by_idempotency_key,
    lifecycle_event_payload,
    new_record_id,
    persist_new_record,
    persist_transition,
)
from clio_agent.gact.a2ui_capabilities import A2UICapabilitiesError, apply_client_metadata_guards
from clio_agent.gact.events import Event
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.permission_gate import GRANTOR_USER, resolve_permission
from clio_agent.gact.session_descendants import descendant_session_ids
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, RetryTurnRequest

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.routes.deps import GactDeps


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=recoverable)
        ).model_dump(exclude_none=True),
    )


def _publish(app: "FastAPI", session_id: str, record: ActionRecord) -> None:
    app.state.bus.publish(
        Event(
            type=f"a2ui.action.{record.state}",
            session_id=session_id,
            payload=lifecycle_event_payload(record),
        )
    )


def _duplicate_result(
    app: "FastAPI",
    sid: str,
    existing: Mapping[str, Any],
    *,
    name: str,
    destination: str,
    surface: Any,
) -> dict[str, Any]:
    """Handle a resubmission of an already-recorded idempotency key.

    Adversarial review finding #5: a duplicate of a ``failed`` record
    re-raises the SAME typed refusal (status + reason) the FIRST submission
    raised — it is never silently downgraded to a 200 with ``state:
    failed``. Every other duplicate is a 200 echoing the existing record; no
    new snapshot is persisted and nothing is re-delivered either way.
    """

    if str(existing.get("state") or "") == "failed":
        status = int(existing.get("http_status") or 0) or 500
        reason = str(existing.get("reason") or "a2ui_delivery_error")
        raise _error(
            status,
            reason,
            f"duplicate submission of a previously refused action (reason={reason})",
            recoverable=status in (409, 422),
        )
    app.state.a2ui_catalogs.record_session_reason(
        sid, "a2ui_action_duplicate", surface_id=surface.id
    )
    duplicate = ActionRecord.from_wire(existing)
    if duplicate is not None:
        # No new persisted snapshot -- "duplicate, no re-delivery" means
        # exactly that -- but the lifecycle event still fires, its OWN
        # type (not the record's unchanged state) carrying the reason.
        app.state.bus.publish(
            Event(
                type="a2ui.action.duplicate",
                session_id=sid,
                payload={**lifecycle_event_payload(duplicate), "reason": "a2ui_action_duplicate"},
            )
        )
    return {
        "action_id": existing.get("id", ""),
        "state": existing.get("state", ""),
        "delivery": existing.get("delivery", ""),
        "reason": existing.get("reason", ""),
        "surface_id": existing.get("surface_id", ""),
        "status": "accepted",
        "name": name,
        "destination": destination,
        "surface": surface.to_wire(),
    }


async def dispatch_action(
    app: "FastAPI",
    sid: str,
    client_message: Mapping[str, Any],
    correlation: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    deps: "GactDeps",
) -> dict[str, Any]:
    """Dispatch one parsed A2UI client envelope through the owner path.

    Args:
        app: The FastAPI app (``app.state.a2ui_store``/``a2ui_catalogs``/etc.).
        sid: The session the envelope targets.
        client_message: The raw client->server envelope, verbatim
            (``{version, action}`` or ``{version, error}``).
        correlation: The request's ``correlation`` object, read (never
            ignored): ``interaction_id``/``question_id``/``message_id``/
            ``part_id``/``task_id``/``run_id``.
        metadata: The request's top-level ``metadata`` (S3 renderer transport
            metadata: ``a2uiClientCapabilities``/``a2uiClientDataModel``).
        deps: The real ``GactDeps`` bag (threaded from ``routes/a2ui.py``,
            which already has it) -- the ``run.cancel`` destination hands it
            to ``cancel_session_state`` verbatim (adversarial review finding
            #7: no more re-derived shim).

    Returns:
        The HTTP-facing result body (200). Every refusal is a typed
        ``HTTPException`` — never a silently-swallowed reason.
    """

    sess = app.state.sessions.get(sid)
    if sess is None:
        raise _error(404, "not_found", f"session not found: {sid}")

    try:
        raw_data_model = apply_client_metadata_guards(app, sid, metadata)
    except A2UICapabilitiesError as exc:
        raise _error(422, exc.reason, str(exc)) from exc

    # S6-review deliverable 2b: an ``error`` envelope routes BEFORE any
    # surface lookup — the renderer reporting its own rejection is accepted
    # even when the named surfaceId cannot be resolved.
    if is_error_envelope(client_message):
        try:
            return await ingest_client_error(app, sid, client_message, correlation)
        except A2UIValidationError as exc:
            raise _error(422, "a2ui_validation_failed", str(exc)) from exc

    raw_action = client_message.get("action")
    surface_id = str(raw_action.get("surfaceId") or "") if isinstance(raw_action, Mapping) else ""
    surface = app.state.a2ui_store.get(sid, surface_id)
    if surface is None or surface.state == "deleted":
        raise _error(404, "not_found", f"A2UI surface not found: {surface_id}")

    from clio_agent.gact.a2ui_catalogs.activation import (  # noqa: PLC0415
        session_catalog_resolver,
    )

    catalog_entry = session_catalog_resolver(app, sid).get(surface.catalog_id)
    try:
        action = validate_client_action(
            client_message, surface_id=surface_id, catalog_entry=catalog_entry
        )
    except A2UIEventContextInvalidError as exc:
        # Finding #12: unlike an ordinary validation failure (never
        # persisted), a context_schema mismatch IS durably recorded --
        # replaying the SAME invalid context is deduped/re-raised by the
        # SAME idempotent-duplicate path every other action uses.
        raw = raw_action if isinstance(raw_action, Mapping) else {}
        raw_context_value = raw.get("context")
        raw_context: Mapping[str, Any] = (
            raw_context_value if isinstance(raw_context_value, Mapping) else {}
        )
        name = str(raw.get("name") or "")
        bad_key = compute_idempotency_key(
            surface_id,
            str(raw.get("sourceComponentId") or ""),
            str(raw.get("timestamp") or ""),
            raw_context,
        )
        failed_record = ActionRecord(
            id=new_record_id(),
            session_id=sid,
            surface_id=surface_id,
            catalog_id=surface.catalog_id,
            kind="action",
            envelope=dict(client_message),
            action_name=name,
            source_component_id=str(raw.get("sourceComponentId") or ""),
            idempotency_key=bad_key,
            correlation=dict(correlation or {}),
            state="failed",
            delivery="rejected",
            reason="a2ui_event_context_invalid",
            http_status=422,
        )
        existing = persist_new_record(app, failed_record)
        if existing is not None:
            return _duplicate_result(
                app, sid, existing, name=name, destination="agent", surface=surface
            )
        app.state.a2ui_catalogs.record_session_reason(
            sid, "a2ui_event_context_invalid", action=name, pointer=exc.pointer
        )
        _publish(app, sid, failed_record)
        raise _error(422, "a2ui_event_context_invalid", str(exc)) from exc
    except A2UIFunctionNotInCatalogError as exc:
        app.state.a2ui_catalogs.record_session_reason(
            sid,
            "a2ui_function_not_in_catalog",
            function_name=exc.function_name,
            catalog_id=exc.catalog_id,
        )
        raise _error(422, "a2ui_function_not_in_catalog", str(exc)) from exc
    except A2UIValidationError as exc:
        raise _error(422, "a2ui_validation_failed", str(exc)) from exc

    name = str(action["name"])
    context = dict(action["context"])
    source_component_id = str(action.get("sourceComponentId") or "")
    timestamp = str(action.get("timestamp") or "")
    destination = str(action.get("destination") or "agent")
    if not action.get("declared"):
        app.state.a2ui_catalogs.record_session_reason(
            sid, "a2ui_event_destination_undeclared", action=name
        )

    idempotency_key = compute_idempotency_key(surface_id, source_component_id, timestamp, context)

    # Finding #6: a CHEAP, unlocked peek -- purely to skip the data-model
    # guards' typed-reason recording on an obvious replay. The store's own
    # atomic check (below, via persist_new_record) is the AUTHORITATIVE
    # race-safe decision; this peek can only ever produce a false negative
    # (a genuine race), never a false positive, so it never weakens finding
    # #1's guarantee -- it only avoids needless reason-ledger noise on the
    # common sequential replay.
    existing_peek = find_by_idempotency_key(surface.actions, idempotency_key)
    if existing_peek is not None:
        return _duplicate_result(
            app, sid, existing_peek, name=name, destination=destination, surface=surface
        )

    action_data_model = (
        filter_owned_data_model(app, sid, raw_data_model) if raw_data_model is not None else None
    )

    correlation_fields = dict(correlation or {})
    record = ActionRecord(
        id=new_record_id(),
        session_id=sid,
        surface_id=surface_id,
        catalog_id=surface.catalog_id,
        kind="action",
        envelope=dict(client_message),
        action_name=name,
        source_component_id=source_component_id,
        idempotency_key=idempotency_key,
        correlation=correlation_fields,
        client_data_model=(
            action_data_model.model_dump(mode="json", by_alias=True, exclude_none=True)
            if action_data_model is not None
            else None
        ),
        narration=narration_for(name, context),
    )
    # Finding #1 (BLOCKING): the REAL idempotency decision. Persist and check
    # atomically, under the store's per-session lock -- a concurrent
    # duplicate racing the peek above is still caught here.
    existing = persist_new_record(app, record)
    if existing is not None:
        return _duplicate_result(
            app, sid, existing, name=name, destination=destination, surface=surface
        )
    _publish(app, sid, record)

    if destination == "permission":
        delivered = await _deliver_permission(app, sid, record, context)
    elif destination == "run":
        operation = str(action.get("operation") or "")
        delivered = await _deliver_run(app, sid, record, context, operation, deps)
    else:
        delivered = await deliver_to_agent(
            app, sid, record, narration=record.narration, context=context
        )

    if delivered.state == "failed" and delivered.reason == "a2ui_waiting_user_uncorrelated":
        raise _error(
            409,
            "a2ui_waiting_user_uncorrelated",
            "session is waiting_user and this action does not correlate to the pending question",
            recoverable=True,
        )

    result: dict[str, Any] = {
        "name": name,
        "status": "accepted",
        "action_id": delivered.id,
        "state": delivered.state,
        "delivery": delivered.delivery,
        "destination": destination,
        "surface": surface.to_wire(),
    }
    if delivered.reason:
        result["reason"] = delivered.reason
    message_id = delivered.correlation.get("message_id")
    if message_id:
        result["message_id"] = message_id
    if action_data_model is not None:
        result["a2ui_client_data_model"] = record.client_data_model
    return result


async def _deliver_permission(
    app: "FastAPI", sid: str, record: ActionRecord, context: Mapping[str, Any]
) -> ActionRecord:
    """Resolve an in-scope permission; the EXACT-OWNER scope check moved verbatim."""

    permission_id = str(context.get("permission_id") or "")
    decision = str(context.get("action") or "")
    if decision not in {"allow", "deny", "allow_session", "allow_workspace"}:
        failed = record.transition(
            state="failed", delivery="rejected", reason="validation_error", http_status=422
        )
        persist_transition(app, failed)
        _publish(app, sid, failed)
        raise _error(422, "validation_error", "approval.respond has an invalid action")
    pending = app.state.permissions.get(permission_id)
    scope = {sid, *descendant_session_ids(app, sid)}
    if pending is not None and str(pending.get("session_id") or "") not in scope:
        app.state.a2ui_catalogs.record_session_reason(
            sid, "a2ui_permission_out_of_scope", permission_id=permission_id
        )
        failed = record.transition(
            state="failed",
            delivery="rejected",
            reason="a2ui_permission_out_of_scope",
            http_status=404,
        )
        persist_transition(app, failed)
        _publish(app, sid, failed)
        raise _error(404, "not_found", f"permission not found: {permission_id}")
    try:
        row = await run_off_loop(
            lambda: resolve_permission(app, permission_id, decision, grantor=GRANTOR_USER)
        )
    except Exception as exc:  # noqa: BLE001 - captured on the record, then re-raised verbatim
        fail_and_publish(app, sid, record, exc)
        raise
    if row is None and pending is None:
        failed = record.transition(
            state="failed", delivery="rejected", reason="not_found", http_status=404
        )
        persist_transition(app, failed)
        _publish(app, sid, failed)
        raise _error(404, "not_found", f"permission not found: {permission_id}")
    delivered = record.transition(
        state="delivered",
        delivery="permission",
        correlation={"permission_id": permission_id, "decision": decision},
    )
    persist_transition(app, delivered)
    _publish(app, sid, delivered)
    return delivered


async def _deliver_run(
    app: "FastAPI",
    sid: str,
    record: ActionRecord,
    context: Mapping[str, Any],
    operation: str,
    deps: "GactDeps",
) -> ActionRecord:
    """Route to the two existing run owners by the sidecar's declared operation."""

    if operation == "cancel":
        from clio_agent.gact.routes.session_cancellation import (
            cancel_session_state,  # noqa: PLC0415
        )

        try:
            cancel_session_state(app, deps, sid)
        except Exception as exc:  # noqa: BLE001 - captured on the record, then re-raised verbatim
            fail_and_publish(app, sid, record, exc)
            raise
        delivered = record.transition(state="delivered", delivery="run_cancel")
    elif operation == "retry":
        source_id = str(context.get("message_id") or "")
        try:
            attempt = await app.state.retry_turn_action(
                sid,
                source_id,
                RetryTurnRequest(
                    execute=True,
                    notes=str(context.get("notes") or ""),
                    metadata={"a2ui_action": record.id, "surface_id": record.surface_id},
                ),
            )
        except Exception as exc:  # noqa: BLE001 - captured on the record, then re-raised verbatim
            fail_and_publish(app, sid, record, exc)
            raise
        delivered = record.transition(
            state="delivered", delivery="run_retry", correlation={"attempt_id": attempt.id}
        )
    else:
        delivered = record.transition(
            state="failed", delivery="rejected", reason="a2ui_validation_failed", http_status=422
        )
        persist_transition(app, delivered)
        _publish(app, sid, delivered)
        raise _error(422, "validation_error", "run destination requires a declared operation")
    persist_transition(app, delivered)
    _publish(app, sid, delivered)
    return delivered


__all__ = ["dispatch_action"]
