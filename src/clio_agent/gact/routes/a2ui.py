"""GACT 0.3 A2UI production, snapshot, and action routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.a2ui import (
    A2UICatalogNotProducibleError,
    A2UICatalogUnknownError,
    A2UIFunctionNotInCatalogError,
    A2UIValidationError,
)
from clio_agent.gact.a2ui_actions.dispatcher import dispatch_action
from clio_agent.gact.a2ui_catalogs.routes.a2ui_capabilities import register_a2ui_capabilities_routes
from clio_agent.gact.a2ui_catalogs.routes.a2ui_catalogs import register_a2ui_catalog_routes
from clio_agent.gact.protocol_v3 import A2UI_V091
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

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

    del deps  # S5: dispatch_action moved to the owner package; no route needs deps directly.

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
        except A2UICatalogUnknownError as exc:
            app.state.a2ui_catalogs.record_session_reason(
                sid, "a2ui_catalog_unknown", catalog_id=exc.catalog_id
            )
            raise _error(422, "a2ui_catalog_unknown", str(exc)) from exc
        except A2UICatalogNotProducibleError as exc:
            app.state.a2ui_catalogs.record_session_reason(
                sid, "a2ui_catalog_not_producible", catalog_id=exc.catalog_id
            )
            raise _error(422, "a2ui_catalog_not_producible", str(exc)) from exc
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
        # Sibling of the model tool's ``created`` flag: the same fold-derived
        # truth about which ids this batch minted. It rides the envelope rather
        # than a surface row because the row shape is the renderer's contract
        # (the frontend decodes surfaces with a non-strict schema that would
        # drop an unknown row key, so a row-level flag would be invisible).
        return {
            "surfaces": [surface.to_wire() for surface in outcome.surfaces],
            "created_surface_ids": list(outcome.created_surface_ids),
        }

    async def dispatch_action_negotiated(
        sid: str,
        body: Mapping[str, Any],
        *,
        protocol_version: str | None,
    ) -> dict[str, Any]:
        """Negotiate + parse the request body, then call the owner dispatcher.

        ``protocol_version`` is the caller's negotiated ``x-a2ui-version``. The
        check lives HERE, not on one route, so every door into the dispatcher
        enforces the same negotiation: the normalized interaction responder
        reaches this function with no negotiation of its own, while the
        canonical ``/a2ui/actions`` route 406's without it.
        """

        if protocol_version != A2UI_V091:
            raise _error(
                406,
                "unsupported_protocol",
                f"A2UI {A2UI_V091} must be negotiated",
            )
        require_session(sid)
        if set(body) - {"message", "correlation", "metadata"}:
            raise _error(422, "validation_error", "A2UI action body contains unknown fields")
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise _error(422, "validation_error", "A2UI action message is required")
        return await dispatch_action(
            app, sid, message, body.get("correlation"), body.get("metadata")
        )

    @app.post("/v1/sessions/{sid}/a2ui/actions")
    async def handle_action(sid: str, request: Request) -> dict[str, Any]:
        """Validate and dispatch a registered official A2UI client action."""

        body = await json_body(request, route="POST /v1/sessions/{sid}/a2ui/actions")
        return await dispatch_action_negotiated(
            sid,
            body,
            protocol_version=getattr(request.state, "a2ui_protocol_version", None),
        )

    # The normalized interaction responder reuses this exact negotiation gate
    # + dispatcher: it passes its own request's negotiated version through, so
    # both doors refuse an un-negotiated A2UI action identically.
    app.state.dispatch_a2ui_action = dispatch_action_negotiated
    # Catalog discovery is a sibling concern of A2UI production (S2's client
    # registry source): registered here so app.py needs no separate import.
    register_a2ui_catalog_routes(app)
    # S3: the per-session capability-negotiation route, same reasoning.
    register_a2ui_capabilities_routes(app)


__all__ = ["register_a2ui_routes"]
