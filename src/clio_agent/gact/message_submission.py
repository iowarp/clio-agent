"""Single acceptance path for starting turns and steering active turns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException

from clio_agent.gact.events import Event
from clio_agent.gact.loop_inbox import enqueue_user_steer
from clio_agent.gact.message_intents import DuplicateIntentError, PendingSteer
from clio_agent.gact.messaging import _user_message_parts, raise_on_reserved_metadata
from clio_agent.gact.parts import Part
from clio_agent.gact.providers.config import (
    _active_lm_supports_vision,
    _image_part_error,
    _model_ref_dict,
    _model_ref_is_empty,
    _model_ref_matches_active,
)
from clio_agent.gact.resource_delivery import (
    ResourceDeliveryRecord,
    live_model_modalities,
    plan_resource_delivery,
)
from clio_agent.gact.runtime.globals import _iso_from_epoch, _new_message_id
from clio_agent.gact.turn_runner import session_busy_error_payload
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    Message,
    ModelRef,
    PostMessageRequest,
    PostMessageResponse,
    Session,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _session_not_found(sid: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="not_found",
                message=f"session not found: {sid}",
                details={"session_id": sid},
                recoverable=False,
            )
        ).model_dump(exclude_none=True),
    )


def _effective_model(app: FastAPI, deps: "GactDeps") -> ModelRef:
    raw = deps.active_lm_model_ref(app)
    return ModelRef(
        provider_id=str(raw.get("provider_id", "")),
        model_id=str(raw.get("model_id", "")),
        variant=str(raw.get("variant", "")),
    )


def _selected_model(
    app: FastAPI,
    deps: "GactDeps",
    session: Session,
    request: PostMessageRequest,
) -> ModelRef:
    """Resolve per-message, then session, then active-default model intent."""

    for candidate in (request.model, session.model):
        if candidate is not None and not _model_ref_is_empty(candidate):
            return ModelRef(**_model_ref_dict(candidate))
    return _effective_model(app, deps)


def _idempotency_key(req: PostMessageRequest) -> str:
    return req.idempotency_key.strip() or req.client_message_id.strip()


def _acceptance_replay(response: PostMessageResponse) -> PostMessageResponse:
    return response.model_copy(update={"idempotent_replay": True})


def _identity_conflict(sid: str, message_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="message_identity_conflict",
                message="client message id already names another message",
                details={"session_id": sid, "message_id": message_id},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _plan_resource_parts(
    app: FastAPI,
    *,
    workspace_id: str,
    message_id: str,
    parts: list[Part],
    model: ModelRef,
) -> tuple[list[Part], list[ResourceDeliveryRecord]]:
    """Attach planned delivery provenance without changing the selected route."""

    planned_parts: list[Part] = []
    deliveries: list[ResourceDeliveryRecord] = []
    for part in parts:
        if part.type != "resource_ref":
            planned_parts.append(part)
            continue
        resource = app.state.resource_store.get(workspace_id, part.resource_id)
        if resource is None:  # guarded by _validate_provider_and_payload
            raise RuntimeError("validated resource disappeared before delivery planning")
        delivery = plan_resource_delivery(
            app,
            resource=resource,
            message_id=message_id,
            model=model,
        )
        if resource.detected_mime.startswith("image/") and delivery.representation != "native":
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="unsupported_resource_modality",
                        message=(
                            "The selected model cannot receive this image resource. "
                            "Choose a model with live-verified image input or remove the image."
                        ),
                        details={
                            "workspace_id": workspace_id,
                            "resource_id": resource.id,
                            "media_type": resource.detected_mime,
                            "provider": model.provider_id,
                            "model": model.model_id,
                            "representation": delivery.representation,
                            "evidence_source": delivery.evidence_source,
                            "recovery_actions": [
                                "choose_image_capable_model",
                                "remove_resource",
                                "retry",
                            ],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        deliveries.append(delivery)
        metadata = dict(part.metadata)
        metadata["delivery"] = {
            "representation": delivery.representation,
            "evidence_source": delivery.evidence_source,
            "reason": delivery.reason,
        }
        planned_parts.append(part.model_copy(update={"metadata": metadata}))
    return planned_parts, deliveries


def _commit_resource_deliveries(
    app: FastAPI,
    sid: str,
    deliveries: list[ResourceDeliveryRecord],
) -> None:
    for delivery in deliveries:
        saved = app.state.resource_delivery_store.append(delivery)
        app.state.bus.publish(
            Event(
                type="resource.delivery_resolved",
                session_id=sid,
                payload=saved.model_dump(),
            )
        )


def _validate_provider_and_payload(
    app: FastAPI,
    deps: "GactDeps",
    sid: str,
    req: PostMessageRequest,
) -> tuple[Session, ModelRef, str, list[Part], list[Part]]:
    sess = app.state.sessions.get(sid)
    if sess is None:
        raise _session_not_found(sid)

    raise_on_reserved_metadata(sid, req.metadata)
    lm_status = getattr(app.state, "lm_config_status", {}) or {}
    if lm_status.get("state") == "configuring":
        raise HTTPException(
            status_code=503,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="provider_configuring",
                    message="LM provider configuration is still in progress; retry after it finishes.",
                    details={
                        "session_id": sid,
                        "operation_id": lm_status.get("operation_id", ""),
                        "provider": lm_status.get("provider", ""),
                        "model": lm_status.get("model", ""),
                        "recovery_actions": ["wait", "check_lm_provider_status", "retry"],
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    if app.state.agent is None:
        raise HTTPException(
            status_code=503,
            detail=deps.agent_not_available_error(app, sid).model_dump(exclude_none=True),
        )

    selected_model = _selected_model(app, deps, sess, req)
    if not _model_ref_matches_active(selected_model, app):
        # A selection the ACTIVE global LM does not serve is executable only when
        # the provider catalog holds LIVE handshake evidence for that exact
        # provider/model. Anything weaker is a typed 501 naming which layer asked
        # for it -- a mismatch is explicit and never silently falls back to the
        # active model (the session ref in particular is preserved, not cleared).
        _modalities, evidence, _generated_at = live_model_modalities(app, selected_model)
        if evidence != "live_handshake":
            raise HTTPException(
                status_code=501,
                detail=deps.unsupported_model_ref_error(
                    session_id=sid,
                    source="per_message" if req.model is not None else "session",
                    model_ref=selected_model,
                    active_model=deps.active_lm_model_ref(app),
                ).model_dump(exclude_none=True),
            )

    text = req.extract_text()
    images = req.image_parts()
    resources = req.resource_parts()
    selected_modalities, selected_evidence, _generated_at = live_model_modalities(
        app, selected_model
    )
    selected_image_capable = "image" in selected_modalities and (
        selected_evidence == "live_handshake"
    )
    active_image_capable = _model_ref_matches_active(
        selected_model, app
    ) and _active_lm_supports_vision(app)
    if images and not (selected_image_capable or active_image_capable):
        active_model = deps.active_lm_model_ref(app)
        raise HTTPException(
            status_code=501,
            detail=_image_part_error(
                session_id=sid,
                image_count=len(images),
                provider={
                    "provider_id": selected_model.provider_id,
                    "model_id": selected_model.model_id,
                    "active_model": active_model,
                },
            ).model_dump(exclude_none=True),
        )
    if not text and not images and not resources:
        raise HTTPException(
            status_code=400,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="validation_error",
                    message=(
                        "request body carried no recognizable message parts: expected text, image, "
                        "or resource_ref"
                    ),
                    details={"session_id": sid},
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )
    resolved_parts: list[Part] = []
    for part in req.parts:
        if part.type != "resource_ref":
            resolved_parts.append(part)
            continue
        resource_id = part.resource_id.strip()
        revision = part.resource_revision.strip()
        if not resource_id or not revision:
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_resource_ref",
                        message="resource_ref parts require resource_id and resource_revision",
                        details={"session_id": sid, "resource_id": resource_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        record = app.state.resource_store.get(sess.workspace_id, resource_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="resource_not_found",
                        message="resource is not available in this session workspace",
                        details={"session_id": sid, "resource_id": resource_id},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if str(record.revision) != revision:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="resource_revision_conflict",
                        message="resource reference does not name the current immutable revision",
                        details={
                            "session_id": sid,
                            "resource_id": resource_id,
                            "requested_revision": revision,
                            "current_revision": str(record.revision),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if record.state != "ready":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="resource_not_ready",
                        message="resource must finish upload and validation before submission",
                        details={
                            "session_id": sid,
                            "resource_id": resource_id,
                            "state": record.state,
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        metadata = dict(part.metadata)
        metadata.update(
            {
                "workspace_id": sess.workspace_id,
                "resource_sha256": record.sha256,
                "resource_detected_mime": record.detected_mime,
            }
        )
        resolved_parts.append(
            part.model_copy(
                update={
                    "name": record.name,
                    "media_type": record.detected_mime,
                    "metadata": metadata,
                }
            )
        )
    return sess, selected_model, text, images, resolved_parts


def accept_message(
    app: FastAPI,
    deps: "GactDeps",
    sid: str,
    req: PostMessageRequest,
) -> tuple[PostMessageResponse, int]:
    """Accept one message using explicit start-or-steer semantics.

    Returns the wire response and HTTP status. A pending steer is durable and
    visible in the transcript before this function returns.
    """

    key = _idempotency_key(req)
    prior = app.state.message_intents.acceptance(sid, key)
    if prior is not None:
        return _acceptance_replay(prior), 202 if prior.state == "pending_steer" else 200

    sess, effective_model, user_text, _images, resolved_parts = _validate_provider_and_payload(
        app, deps, sid, req
    )
    message_id = req.client_message_id.strip() or _new_message_id("user")
    resolved_parts, resource_deliveries = _plan_resource_parts(
        app,
        workspace_id=sess.workspace_id,
        message_id=message_id,
        parts=resolved_parts,
        model=effective_model,
    )
    busy_payload = session_busy_error_payload(getattr(app.state, "turn_runner", None), sid)
    busy = busy_payload is not None
    if busy and req.delivery == "start":
        raise HTTPException(status_code=409, detail=busy_payload)
    if not busy and req.delivery == "steer":
        # ``steer`` is HONOURED, not reinterpreted. It used to fall through to the
        # busy check and silently start a turn on an idle session -- the caller
        # asked to steer work in flight and got a brand-new turn instead. There is
        # nothing to steer, so say so and let the caller decide (``auto`` is the
        # value that means "start or steer, whichever fits").
        raise HTTPException(
            status_code=409,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="no_active_turn_to_steer",
                    message=(
                        "delivery=steer requires a turn in flight on this session; "
                        "use delivery=start or delivery=auto to begin one"
                    ),
                    details={
                        "session_id": sid,
                        "recovery_actions": ["retry_with_start", "retry_with_auto"],
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    behavior = req.behavior.model_dump()
    metadata = dict(req.metadata)
    model_selection_source = (
        "per_message"
        if req.model is not None and not _model_ref_is_empty(req.model)
        else "session"
        if not _model_ref_is_empty(sess.model)
        else "global_active"
    )
    metadata.update(
        {
            "delivery": "steer" if busy else "start",
            "behavior": behavior,
            "client_message_id": req.client_message_id,
            "effective_model": effective_model.model_dump(),
        }
    )
    if model_selection_source != "global_active":
        metadata["model_selection_source"] = model_selection_source
    if busy:
        if any(row.id == message_id for row in app.state.messages.get(sid, [])):
            raise _identity_conflict(sid, message_id)
        accepted_at = _iso_from_epoch(datetime.now(timezone.utc).timestamp())
        parts = _user_message_parts(request_parts=resolved_parts, user_text=user_text)
        metadata["pending_steer"] = True
        pending = PendingSteer(
            message_id=message_id,
            session_id=sid,
            parts=parts,
            text=user_text,
            metadata=metadata,
            accepted_at=accepted_at,
            behavior=req.behavior,
            model=effective_model,
        )
        message = Message(
            id=message_id,
            turn_id="",
            session_id=sid,
            role="user",
            created_at=accepted_at,
            updated_at=accepted_at,
            parts=parts,
            metadata=metadata,
        )
        ack = PostMessageResponse(
            message_id=message_id,
            accepted_at=accepted_at,
            delivery="steer",
            state="pending_steer",
            effective_model=effective_model,
            behavior=req.behavior,
        )
        try:
            prior = app.state.message_intents.accept_pending(pending, key, ack)
            if prior is not None:
                return _acceptance_replay(prior), 202
            deps.append_session_message(app, sid, message)
            enqueue_user_steer(
                app,
                sid,
                user_text,
                metadata,
                steer_message_id=message_id,
                steer_created_at=accepted_at,
                steer_parts=parts,
            )
            app.state.bus.publish(
                Event(
                    type="message.accepted",
                    session_id=sid,
                    payload={
                        "message": message.to_wire(),
                        "delivery": "steer",
                        "state": "pending_steer",
                        "effective_model": effective_model.model_dump(),
                        "behavior": behavior,
                    },
                )
            )
            _commit_resource_deliveries(app, sid, resource_deliveries)
        except DuplicateIntentError as exc:
            raise _identity_conflict(sid, message_id) from exc
        except Exception:
            app.state.message_intents.discard_pending(sid, message_id, acceptance_key=key)
            deps.replace_session_messages(
                app,
                sid,
                [row for row in app.state.messages.get(sid, []) if row.id != message_id],
            )
            raise
        return ack, 202

    # Cancellation belongs to the turn that was active when /cancel was requested.
    # An idle cancellation (including recovery after a restart, where the persisted
    # session may still say ``running`` but no executor exists) must not poison this
    # user-authored turn; the busy branch above already preserved genuine mid-turn
    # cancellation/steering races.
    app.state.cancel_flags.discard(sid)
    app.state.cancel_events.pop(sid, None)
    user_msg = deps.start_background_user_turn(
        sid,
        sess,
        user_text,
        request_parts=resolved_parts,
        metadata=metadata,
        prev_status="idle",
        turn_agent_id=req.extract_agent_id().strip(),
        user_msg_id=message_id,
    )
    _commit_resource_deliveries(app, sid, resource_deliveries)
    ack = PostMessageResponse(
        message_id=user_msg.id,
        accepted_at=user_msg.created_at,
        delivery="start",
        state="started",
        effective_model=effective_model,
        behavior=req.behavior,
    )
    app.state.message_intents.record_acceptance(sid, key, ack)
    # Acceptance is UNIFORM across both branches. Publishing this only on the
    # steer branch left a client watching for acceptance seeing nothing at all
    # for a start -- it had to infer acceptance from message.created, a
    # different fact (the transcript row) with a different shape.
    app.state.bus.publish(
        Event(
            type="message.accepted",
            session_id=sid,
            payload={
                "message": user_msg.to_wire(),
                "delivery": "start",
                "state": "started",
                "effective_model": effective_model.model_dump(),
                "behavior": behavior,
            },
        )
    )
    return ack, 200


__all__ = ["accept_message"]
