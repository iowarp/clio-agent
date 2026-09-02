"""GACT 0.3 A2UI production, snapshot, and action routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.a2ui import (
    SERVER_ACTIONS,
    A2UIValidationError,
    validate_client_action,
)
from clio_agent.gact.events import Event
from clio_agent.gact.permission_gate import GRANTOR_USER, resolve_permission
from clio_agent.gact.protocol_v3 import A2UI_V091, A2UI_V091_WIRE
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.routes.sessions import cancel_session_state
from clio_agent.gact.turn_runner import session_busy_error_payload
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, RetryTurnRequest

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _error(status: int, code: str, message: str, *, recoverable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=recoverable)
        ).model_dump(exclude_none=True),
    )


def register_a2ui_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register A2UI routes against the app's persistent surface store."""

    def require_session(sid: str) -> Any:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _error(404, "not_found", f"session not found: {sid}")
        return sess

    @app.get("/v1/sessions/{sid}/a2ui/surfaces")
    async def list_surfaces(sid: str) -> dict[str, Any]:
        """Return compacted surface snapshots for reconnect reconciliation."""

        require_session(sid)
        degradations = app.state.a2ui_store.projection_degradations(sid)
        if app.state.a2ui_store.load_degradation is not None:
            degradations.insert(0, app.state.a2ui_store.load_degradation)
        return {
            "surfaces": app.state.a2ui_store.list_wire(sid),
            "degradations": degradations,
        }

    @app.post("/v1/sessions/{sid}/a2ui/messages")
    async def produce_messages(sid: str, request: Request) -> dict[str, Any]:
        """Persist and publish ordered official A2UI 0.9.1 messages."""

        require_session(sid)
        if getattr(request.state, "a2ui_protocol_version", None) != A2UI_V091:
            raise _error(
                406,
                "unsupported_protocol",
                f"A2UI {A2UI_V091} must be negotiated",
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/a2ui/messages")
        if set(body) - {"messages", "correlation"}:
            raise _error(422, "validation_error", "A2UI production body contains unknown fields")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise _error(422, "validation_error", "A2UI messages must be a non-empty list")
        correlation = body.get("correlation")
        if correlation is None:
            correlation = {}
        if not isinstance(correlation, Mapping) or set(correlation) - {
            "run_id",
            "message_id",
            "part_id",
        }:
            raise _error(422, "validation_error", "A2UI correlation is invalid")
        try:
            for message in messages:
                if not isinstance(message, Mapping):
                    raise A2UIValidationError("A2UI message must be an object")
            outcome = app.state.a2ui_store.apply_batch_outcome(
                sid,
                messages,
                run_id=str(correlation.get("run_id") or ""),
                message_id=str(correlation.get("message_id") or ""),
                part_id=str(correlation.get("part_id") or ""),
            )
        except A2UIValidationError as exc:
            raise _error(422, "a2ui_validation_failed", str(exc)) from exc
        # Sibling of the model tool's ``created`` flag: the same fold-derived
        # truth about which ids this batch minted. It rides the envelope rather
        # than a surface row because the row shape is the renderer's contract
        # (the frontend decodes surfaces with a non-strict schema that would
        # drop an unknown row key, so a row-level flag would be invisible).
        return {
            "surfaces": [surface.to_wire() for surface in outcome.surfaces],
            "created_surface_ids": list(outcome.created_surface_ids),
        }

    async def dispatch_action(sid: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch a parsed action through the authoritative A2UI owner path."""

        sess = require_session(sid)
        if set(body) - {"message", "correlation"}:
            raise _error(422, "validation_error", "A2UI action body contains unknown fields")
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise _error(422, "validation_error", "A2UI action message is required")
        raw_action = message.get("action")
        surface_id = (
            str(raw_action.get("surfaceId") or "") if isinstance(raw_action, Mapping) else ""
        )
        surface = app.state.a2ui_store.get(sid, surface_id)
        if surface is None or surface.state == "deleted":
            raise _error(404, "not_found", f"A2UI surface not found: {surface_id}")
        try:
            action = validate_client_action(message, surface_id=surface_id)
        except A2UIValidationError as exc:
            raise _error(422, "a2ui_validation_failed", str(exc)) from exc

        name = str(action["name"])
        context = dict(action["context"])
        result: dict[str, Any] = {"name": name, "status": "accepted"}
        if name == "agent.submit":
            prompt = str(context.get("text") or context.get("prompt") or "").strip()
            if not prompt:
                raise _error(422, "validation_error", "agent.submit requires context.text")
            # The canonical within-session gate every other turn producer uses.
            # A status check is not equivalent: a cancelled-but-still-unwinding
            # turn projects a non-running status while its slot is still held,
            # and starting a second turn there orphans the first.
            busy = session_busy_error_payload(getattr(app.state, "turn_runner", None), sid)
            if busy is not None:
                raise HTTPException(status_code=409, detail=busy)
            # The gate reports idle, so this is a fresh turn: a cancellation
            # aimed at the previous one must not poison it (mirrors the POST
            # /messages producer).
            app.state.cancel_flags.discard(sid)
            app.state.cancel_events.pop(sid, None)
            user_message = deps.start_background_user_turn(
                sid,
                sess,
                prompt,
                metadata={"a2ui_action": name, "surface_id": surface_id},
                prev_status=sess.status,
            )
            result["message_id"] = user_message.id
        elif name == "approval.respond":
            permission_id = str(context.get("permission_id") or "")
            decision = str(context.get("action") or "")
            if decision not in {"allow", "deny", "allow_session", "allow_workspace"}:
                raise _error(422, "validation_error", "approval.respond has an invalid action")
            row = resolve_permission(app, permission_id, decision, grantor=GRANTOR_USER)
            if row is None and permission_id not in app.state.permissions:
                raise _error(404, "not_found", f"permission not found: {permission_id}")
            result["permission_id"] = permission_id
            result["resolution"] = decision
        elif name == "run.cancel":
            result["cancellation"] = cancel_session_state(app, deps, sid)
        elif name == "run.retry":
            source_id = str(context.get("message_id") or "")
            attempt = await app.state.retry_turn_action(
                sid,
                source_id,
                RetryTurnRequest(
                    execute=True,
                    notes=str(context.get("notes") or ""),
                    metadata={"a2ui_action": name, "surface_id": surface_id},
                ),
            )
            result["attempt"] = attempt.model_dump(exclude_none=True)
        elif name == "form.submit":
            result["submitted"] = context
        else:  # defensive: validate_client_action already restricts this union
            assert name in SERVER_ACTIONS

        ack = {
            "version": A2UI_V091_WIRE,
            "updateDataModel": {
                "surfaceId": surface_id,
                "path": "/lastAction",
                "value": {
                    "name": name,
                    "status": result["status"],
                    "receivedAt": datetime.now(timezone.utc).isoformat(),
                },
            },
        }
        updated = app.state.a2ui_store.apply(sid, ack)
        result["surface"] = updated.to_wire()
        app.state.bus.publish(
            Event(
                type="a2ui.action.received",
                session_id=sid,
                payload={
                    "surface_id": surface_id,
                    "action": name,
                    "source_component_id": action["sourceComponentId"],
                },
            )
        )
        return result

    @app.post("/v1/sessions/{sid}/a2ui/actions")
    async def handle_action(sid: str, request: Request) -> dict[str, Any]:
        """Validate and dispatch a registered official A2UI client action."""

        if getattr(request.state, "a2ui_protocol_version", None) != A2UI_V091:
            raise _error(
                406,
                "unsupported_protocol",
                f"A2UI {A2UI_V091} must be negotiated",
            )
        body = await json_body(request, route="POST /v1/sessions/{sid}/a2ui/actions")
        return await dispatch_action(sid, body)

    # The normalized interaction responder reuses this exact action dispatcher;
    # protocol negotiation already happened on its own route.
    app.state.dispatch_a2ui_action = dispatch_action


__all__ = ["register_a2ui_routes"]
