"""Permission + permission-policy CRUD routes for the GACT server (#714).

This concern owns two related vendor surfaces the gact-tui drives:

* **Permissions** (SPEC BBB23) -- the human-in-the-loop ledger of tool-call
  permission requests. ``GET /v1/permissions`` lists/filters them;
  ``POST /v1/permissions/{pid}`` resolves a pending one
  (``allow`` | ``deny`` | ``allow_session`` | ``allow_workspace``), wakes any
  ``MCPToolBridge`` thread blocked on the request's event, and -- for the
  ``allow_session``/``allow_workspace`` actions -- derives a sticky policy.
* **Policies** (SPEC §6.11.b) -- the declarative ``allow``/``deny``/``ask``
  rules consulted at the permission boundary.
  ``GET /v1/policies`` lists them; ``PUT /v1/policies`` atomically replaces the
  whole list (matching the gact-tui ``PutPolicies`` shape), validating every row
  before persisting so a typoed deny rule can never be silently dropped.

The data layer (validation, load/flush, resolution-derived policy) lives in
:mod:`clio_agent.gact.runtime.permission_policies` so this module and the
``build_app`` startup path share one implementation. Handlers close over the
``app`` argument (FastAPI's decorators need it) and read/write the live ledger +
policy list via ``app.state``. The permission-resolution body is shared with the
in-process reviewer path via :func:`clio_agent.gact.permission_gate.resolve_permission`
(#1044) — a sibling ``gact`` module, NOT ``clio_agent.gact.app``; the route→gate
import direction is safe (``permission_gate`` never imports routes), so the no-cycle
invariant holds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from clio_agent.gact.permission_gate import resolve_permission
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.runtime.grant_resolver import migrate_priorities
from clio_agent.gact.runtime.grants import GRANTOR_USER
from clio_agent.gact.runtime.permission_policies import (
    _PERMISSION_POLICY_ACTIONS,
    _PERMISSION_POLICY_SCOPES,
    _flush_permission_policies,
    _validate_permission_policies,
)
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def register_permissions_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the permission-request + permission-policy CRUD routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the live state through ``app.state.permissions`` /
    ``app.state.permission_policies`` / ``app.state.permission_events`` /
    ``app.state.bus``; the permission-policy data machinery comes from
    :mod:`clio_agent.gact.runtime.permission_policies`. ``deps`` is accepted to
    match the uniform ``register_<concern>_routes(app, deps)`` factory signature.
    """

    # ---- /v1/permissions (BBB23) --------------------------------------

    @app.get("/v1/permissions")
    async def list_permissions(
        session_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        """List permission requests.

        ?session_id=<sid> narrows to a session; ?status=pending
        hides resolved rows; ?status=all returns the audit ledger.
        """

        rows = list(app.state.permissions.values())
        total_before_filters = len(rows)
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        total_after_session_filter = len(rows)
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
        total_after_status_filter = len(rows)
        rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        if limit <= 0:
            limit = 100
        limit = min(limit, 500)
        return {
            "permissions": rows[:limit],
            "metadata": {
                "session_id": session_id,
                "status": status or "all",
                "limit": limit,
                "total": total_after_status_filter,
                "returned": min(total_after_status_filter, limit),
                "truncated": total_after_status_filter > limit,
                "total_before_filters": total_before_filters,
                "total_after_session_filter": total_after_session_filter,
            },
        }

    @app.post("/v1/permissions/{pid}")
    async def respond_permission(pid: str, request: Request) -> Response:
        """Resolve a pending permission. Body: ``{action}`` where
        action is ``allow | deny | allow_session | allow_workspace``.
        Idempotent when the row is already resolved (returns the
        existing resolution rather than erroring).
        """

        row = app.state.permissions.get(pid)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"permission not found: {pid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = await json_body(request, route="POST /v1/permissions/{pid}")
        action = body.get("action") or ""
        if action not in {"allow", "deny", "allow_session", "allow_workspace"}:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "action must be one of allow, deny, allow_session, allow_workspace"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # #1044: the resolution core (status flip, sticky-policy derivation, bridge-thread
        # wake, permission.resolved emit) lives in ``permission_gate.resolve_permission`` so
        # the HTTP route and the in-process ai-review reviewer share ONE path. The route
        # resolves as ``GRANTOR_USER`` — byte-identical to the prior inline body, now with the
        # grantor stamped on the audit row + both resolved payloads.
        resolve_permission(app, pid, action, grantor=GRANTOR_USER)
        return Response(status_code=204)

    # ---- /v1/policies (SPEC §6.11.b permission policies) -------------
    #
    # Declarative allow/deny/ask rules consulted at the permission
    # boundary. PUT replaces the whole list (matches the
    # gact-tui client's PutPolicies shape) and persists it locally.

    @app.get("/v1/policies")
    async def list_policies() -> dict[str, Any]:
        return {"policies": list(app.state.permission_policies)}

    @app.put("/v1/policies")
    async def put_policies(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="PUT /v1/policies")
        policies = body.get("policies")
        if not isinstance(policies, list):
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="body must be {'policies': [...]}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        clean, errors = _validate_permission_policies(policies)
        if errors:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message=("invalid permission policies; no policy changes were applied"),
                        details={
                            "policy_errors": errors,
                            "allowed_scopes": sorted(_PERMISSION_POLICY_SCOPES),
                            "allowed_actions": sorted(_PERMISSION_POLICY_ACTIONS),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Materialize the priority band on the whole list before persisting (P0.1 #1059): rows
        # that omit ``priority`` gain a unique DESCENDING priority by insertion index (first row
        # highest), so the stored order is the resolver's highest-wins band order and reload is a
        # no-op. Explicit priorities the client sent are preserved verbatim.
        migrate_priorities(clean)
        app.state.permission_policies = clean
        _flush_permission_policies(app)
        return {"policies": clean}
