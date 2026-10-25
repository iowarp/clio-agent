"""Normalized pending-interaction projection and exact-owner response routing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from clio_agent import conf
from clio_agent.arc.task_types import QuestionReply
from clio_agent.errors import ClioError
from clio_agent.gact.a2ui_actions.record import last_action_wire
from clio_agent.gact.human_controls import intervene
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.permission_gate import GRANTOR_USER, resolve_permission
from clio_agent.gact.question_surface import question_row
from clio_agent.gact.routes.interaction_surface_projection import (
    _latest_action_record as _latest_action_record,
)
from clio_agent.gact.routes.interaction_surface_projection import (
    _question_correlation as _question_correlation,
)
from clio_agent.gact.routes.interaction_surface_projection import (
    _surface_actions as _surface_actions,
)
from clio_agent.gact.routes.interaction_surface_projection import (
    _task_id_for_owner as _task_id_for_owner,
)
from clio_agent.gact.routes.plan_interactions import is_plan_exit_question, plan_exit_payload
from clio_agent.gact.routes.session_question_helpers import (
    answer_question_response,
    question_surface_ids,
)
from clio_agent.gact.session_descendants import descendant_session_ids
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    PendingInteraction,
    PendingInteractionSource,
    RespondInteractionRequest,
    UserQuestion,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

__all__ = [
    "InteractionProjection",
    "interactions_projection_limit",
    "project_pending_interactions",
    "register_permission_and_interaction_routes",
]


def register_permission_and_interaction_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register authoritative permission routes and their interaction projection."""

    from clio_agent.gact.routes.permissions import register_permissions_routes  # noqa: PLC0415

    register_permissions_routes(app, deps)
    register_interaction_routes(app, deps)


def _error(status_code: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error=code,
                message=message,
                details=details,
                recoverable=status_code in {409, 422},
            )
        ).model_dump(exclude_none=True),
    )


def _permission_intercept(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read the P2.6 approve-with-modify/synthesize payload out of the response.

    Same shape ``POST /v1/permissions/{pid}`` accepts, carried under the
    kind-neutral ``metadata`` this route already forwards: ``input`` runs the tool
    with modified arguments, ``result`` skips the real call with a synthesized
    result. ``resolve_permission`` honours it only on an allow.
    """

    if isinstance(metadata.get("input"), dict):
        return {"input": metadata["input"]}
    if "result" in metadata:
        return {"result": metadata["result"]}
    return None


def _session_scope(app: FastAPI, root_session_id: str, include_children: bool) -> set[str]:
    """Every session whose interactions this root is answerable for.

    The lineage walk includes conversation forks, while this interaction scope
    stops at independent branches. Delegated children still attend their owning
    conversation, even when their task registry row has been retired.
    """

    scope = {root_session_id}
    if include_children:
        attended = attended_session_id(app, root_session_id)
        scope.update(
            child
            for child in descendant_session_ids(app, root_session_id)
            if attended_session_id(app, child) == attended
        )
    return scope


def _stamp_request_sequence(rows: list[PendingInteraction]) -> list[PendingInteraction]:
    """Number MCP requests inside their durable invocation/task branch.

    This is deliberately called a *request* sequence rather than a protocol
    round: one MCP task input round may request multiple keys. The stable
    invocation id is preferred; detached asynchronous task inputs fall back to
    their task id, so they retain the same causal branch after the initiating
    tool call has left the transcript.
    """

    groups: dict[str, list[PendingInteraction]] = {}
    for row in rows:
        key = row.source.invocation_id or row.task_id
        if row.source.protocol != "mcp" or not key:
            continue
        groups.setdefault(key, []).append(row)
    positions: dict[str, tuple[int, int]] = {}
    for grouped in groups.values():
        ordered = sorted(grouped, key=lambda row: (row.created_at, row.id))
        total = len(ordered)
        positions.update({row.id: (index, total) for index, row in enumerate(ordered, start=1)})
    stamped: list[PendingInteraction] = []
    for row in rows:
        position = positions.get(row.id)
        if position is None:
            stamped.append(row)
            continue
        payload = dict(row.payload)
        payload["request_index"], payload["request_count"] = position
        stamped.append(row.model_copy(update={"payload": payload}))
    return stamped


def _permission_interaction(app: FastAPI, row: Mapping[str, Any]) -> PendingInteraction:
    owner = str(row.get("session_id") or "")
    tool_call = row.get("tool_call")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
    import platform

    sessions = getattr(app.state, "sessions", None)
    session = sessions.get(owner) if sessions is not None else None
    workspaces = getattr(app.state, "workspaces", None)
    workspace = (
        workspaces.get(getattr(session, "workspace_id", ""))
        if session is not None and workspaces is not None
        else None
    )
    tool_input = tool_call.get("input")
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    execution = {
        "computer": platform.node(),
        "cwd": str(tool_input.get("cwd") or getattr(workspace, "root_path", "") or ""),
    }
    status: Literal["pending", "answered", "cancelled"] = "pending"
    if row.get("status") != "pending":
        status = (
            "answered"
            if row.get("action") in {"allow", "allow_session", "allow_workspace"}
            else "cancelled"
        )
    return PendingInteraction(
        id=f"permission:{row.get('id', '')}",
        kind="permission",
        owner_session_id=owner,
        attended_session_id=attended_session_id(app, owner),
        task_id=str(
            row.get("task_id") or tool_call.get("task_id") or _task_id_for_owner(app, owner) or ""
        ),
        status=status,
        title=str(row.get("summary") or "Permission required"),
        requires_human_response=status == "pending",
        source=PendingInteractionSource(
            protocol="mcp" if row.get("kind") == "external_mcp" else "native",
            tool_name=str(tool_call.get("tool_name") or ""),
            invocation_id=str(
                row.get("invocation_id")
                or tool_call.get("invocation_id")
                or tool_call.get("call_id")
                or ""
            ),
        ),
        created_at=str(row.get("created_at") or ""),
        revision=str(
            row.get("updated_at") or row.get("resolved_at") or row.get("created_at") or ""
        ),
        payload={
            "permission_id": str(row.get("id") or ""),
            "tool_call": dict(tool_call),
            "execution": execution,
            **({"access": row["access"]} if isinstance(row.get("access"), Mapping) else {}),
        },
        actions=(
            list(row.get("actions") or ["allow", "deny", "allow_session", "allow_workspace"])
            if status == "pending"
            else []
        ),
    )


def _a2ui_interactions(
    app: FastAPI, owner: str
) -> tuple[list[PendingInteraction], list[dict[str, str]]]:
    """Project one owner's actionable surfaces, plus the store's quarantine rows.

    ONE derivation per owner: ``list_wire`` and ``projection_degradations`` each
    re-walk the owner's whole message ledger, and ``task_id`` / ``attended`` used
    to be recomputed per SURFACE (a full registry snapshot + sort each time), so a
    poll cost O(surfaces x tasks) on top of two ledger walks.
    """

    try:
        surfaces, quarantined = app.state.a2ui_store.list_wire_with_degradations(owner)
    except ClioError as error:
        return [], [{"reason": error.error_type, "owner_session_id": owner, "detail": str(error)}]
    rows: list[PendingInteraction] = []
    if not surfaces:
        return rows, list(quarantined)
    question_surfaces = question_surface_ids(app, owner)
    owner_task_id = _task_id_for_owner(app, owner)
    owner_attended = attended_session_id(app, owner)
    for surface in surfaces:
        actions = _surface_actions(surface)
        if surface.get("state") == "deleted" or not actions:
            continue
        surface_id = str(surface.get("id") or "")
        if surface_id in question_surfaces:
            continue  # the question owns its presentation and response lifecycle
        latest_record = _latest_action_record(surface)
        # A responded surface is SETTLED once its latest record has actually
        # DELIVERED or been CONSUMED by a turn (S5) -- a ``received`` or
        # ``failed`` record is not a real answer, so the row stays pending.
        # It used to keep projecting ``pending`` forever once settled, so an
        # attention lane showed a surface the user had already submitted,
        # indefinitely and unboundedly. The action list is NOT cleared: a
        # surface can legitimately be acted on again (a chat-like
        # agent.submit), so the row states what happened rather than
        # foreclosing what is offered.
        settled = latest_record is not None and latest_record.get("state") in {
            "delivered",
            "consumed",
        }
        payload: dict[str, Any] = {"revision": surface.get("revision", 0)}
        if latest_record is not None:
            payload["last_action"] = last_action_wire(latest_record)
        rows.append(
            PendingInteraction(
                id=f"a2ui:{owner}:{surface_id}",
                kind="a2ui",
                owner_session_id=owner,
                attended_session_id=owner_attended,
                task_id=owner_task_id,
                status="answered" if settled else "pending",
                title="Interactive surface",
                # An offered action is not a request for an answer. Only an
                # explicit question owns human attention; retain this row for
                # action routing/history without moving the surface into it.
                requires_human_response=False,
                source=PendingInteractionSource(
                    protocol="native",
                    tool_name="create_a2ui_surface",
                    invocation_id=str(surface.get("run_id") or surface.get("message_id") or ""),
                    surface_id=surface_id,
                ),
                created_at=str(surface.get("created_at") or ""),
                revision=str(surface.get("revision", "")),
                payload=payload,
                actions=actions,
            )
        )
    return rows, list(quarantined)


def interactions_projection_limit() -> int:
    """Bound returned rows and expensive per-owner surface projections.

    Zero leaves the projection unlimited. Config:
    ``gact.interactions.projection_limit`` / ``CLIO_INTERACTIONS_PROJECTION_LIMIT``.
    """

    return conf.resolve(
        "gact.interactions.projection_limit",
        env="CLIO_INTERACTIONS_PROJECTION_LIMIT",
        default=200,
        cast=conf.as_int,
    )


def _owners_newest_first(app: FastAPI, owners: set[str]) -> list[str]:
    """Order a scope by session creation time, newest first, ties by id."""

    def key(owner: str) -> tuple[str, str]:
        session = app.state.sessions.get(owner)
        return (str(getattr(session, "created_at", "") or ""), owner)

    return sorted(owners, key=key, reverse=True)


@dataclass(frozen=True)
class InteractionProjection:
    """The projected rows plus every typed reason they are partial."""

    rows: list[PendingInteraction] = field(default_factory=list)
    degradations: list[dict[str, str]] = field(default_factory=list)


def _question_from_interaction_id(app: FastAPI, identity: str) -> UserQuestion | None:
    if not identity.startswith("question:"):
        return None
    handle = identity.removeprefix("question:")
    task = app.state.task_history.task_snapshot(handle)
    return (
        question_row(app, handle)
        if task is not None and task.kind == "Question" and task.details and task.raw_status
        else None
    )


def _question_interaction(app: FastAPI, question: UserQuestion) -> PendingInteraction:
    """Render a common Question through the shipped interaction contract."""
    bundle = question.metadata.get("question_bundle")
    return PendingInteraction(
        id="question:" + question.id,
        kind="question",
        owner_session_id=question.owner_session_id,
        attended_session_id=question.attended_session_id,
        task_id=question.task_handle or question.id,
        status=question.status,
        title="Review the plan" if is_plan_exit_question(question) else "Question",
        prompt=question.prompt,
        requires_human_response=question.status == "pending",
        audience=question.audience,
        routing_state=question.agent_elicitation_routing,
        answered_by=question.answered_by,
        source=PendingInteractionSource(
            protocol="mcp" if question.source.startswith("mcp") else "native",
            tool_name=question.source,
            invocation_id=str(question.metadata.get("invocation_id") or ""),
            surface_id=str(question.metadata.get("a2ui_surface_id") or ""),
        ),
        created_at=question.created_at,
        revision=question.updated_at,
        payload={
            "question_id": question.id,
            "question_kind": question.kind,
            "options": [option.model_dump() for option in question.options],
            "allow_freeform": question.allow_freeform,
            "answer": question.answer,
            "selected_options": question.selected_options,
            "metadata": question.metadata,
            "answer_metadata": question.answer_metadata,
            **(question.metadata.get("elicitation") or {}),
            **(
                {"mode": "form", "fields": bundle["fields"], "additional_properties": False}
                if isinstance(bundle, dict)
                else {}
            ),
            **(plan_exit_payload(question) if is_plan_exit_question(question) else {}),
            "task_handle": question.task_handle,
            "response_mode": question.response_mode,
        },
        actions=["answer", "cancel"] if question.status == "pending" else [],
    )


def project_pending_interactions(
    app: FastAPI,
    root_session_id: str,
    *,
    include_children: bool = False,
    include_recent_resolved: bool = False,
    resolved_limit: int = 20,
) -> InteractionProjection:
    """Combine presentation kinds; Question lifecycle stays in the common Core row."""
    history = app.state.task_history
    caller = TaskCaller(root_session_id, app.state.human_principal, human_action=True)
    access = TaskAccess(history, caller)
    owners = _session_scope(app, root_session_id, include_children)
    handles = set().union(
        *(
            history.task_handles(kind="Question", human_id=caller.human_id, session_id=owner)
            for owner in owners
        )
    )
    pending: list[PendingInteraction] = []
    resolved: list[PendingInteraction] = []
    degradations: list[dict[str, str]] = []
    for handle in handles:
        task = access.require(handle, "read")
        if task.details is None or not task.raw_status:
            degradations.append(
                {
                    "reason": "question_content_unconfirmed"
                    if task.details is None
                    else "question_posting_unconfirmed",
                    "owner_session_id": task.owner.session_id,
                }
            )
            continue
        row = _question_interaction(app, question_row(app, handle))
        (pending if row.status == "pending" else resolved).append(row)
    for permission in app.state.permissions.values():
        if str(permission.get("session_id") or "") in owners:
            row = _permission_interaction(app, permission)
            (pending if row.status == "pending" else resolved).append(row)
    ordered_owners = _owners_newest_first(app, owners)
    limit = interactions_projection_limit()
    if limit > 0 and len(ordered_owners) > limit:
        degradations.append(
            {
                "reason": "interaction_projection_truncated",
                "owner_session_id": root_session_id,
                "detail": f"Interactive surfaces include {limit} of {len(ordered_owners)} authorized sessions; narrow the session scope for the remainder.",
            }
        )
        ordered_owners = ordered_owners[:limit]
    for owner in ordered_owners:
        surfaces, reasons = _a2ui_interactions(app, owner)
        degradations.extend(reasons)
        for row in surfaces:
            (pending if row.status == "pending" else resolved).append(row)
    if include_recent_resolved:
        resolved.sort(key=lambda row: (row.revision, row.id), reverse=True)
        pending.extend(resolved[:resolved_limit])
    pending.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    if limit > 0 and len(pending) > limit:
        degradations.append({"reason": "projection_limit", "owner_session_id": root_session_id})
        pending = pending[:limit]
    return InteractionProjection(_stamp_request_sequence(pending), degradations)


def _interaction_wire(row: PendingInteraction) -> dict[str, Any]:
    """Serialize a projection while retaining required fields with defaults."""

    wire = row.model_dump(exclude_defaults=True)
    wire["status"] = row.status
    wire["requires_human_response"] = row.requires_human_response
    wire["actions"] = row.actions
    return wire


def recorded_session_interactions(app: FastAPI, session_id: str) -> list[dict[str, Any]]:
    """Export retained interactions without the live UI's count or recency limit."""
    history = app.state.task_history
    caller = TaskCaller(session_id, app.state.human_principal, human_action=True)
    access = TaskAccess(history, caller)
    rows = [
        _question_interaction(app, question_row(app, handle))
        for handle in history.session_handles(session_id)
        if (task := access.require(handle, "read")).kind == "Question"
        and task.details is not None
        and task.raw_status
    ]
    rows.extend(
        _permission_interaction(app, permission)
        for permission in app.state.permissions.values()
        if str(permission.get("session_id") or "") == session_id
    )
    surfaces, _ = _a2ui_interactions(app, session_id)
    rows.extend(surfaces)
    rows.sort(key=lambda row: (row.created_at, row.id))
    return [_interaction_wire(row) for row in _stamp_request_sequence(rows)]


def register_interaction_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register normalized list/respond routes without introducing a state store."""

    del deps

    @app.get("/v1/sessions/{root_session_id}/interactions")
    async def list_interactions(
        root_session_id: str,
        include_children: bool = False,
        include_recent_resolved: bool = False,
        resolved_limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        if app.state.sessions.get(root_session_id) is None:
            raise _error(404, "not_found", f"session not found: {root_session_id}")
        # Restoring retained ledgers can perform synchronous CTE reads/writes.
        # Keep that work off the ASGI event loop so attention hydration cannot
        # freeze tool deltas, prompt submission, or unrelated health requests.
        projection = await run_in_threadpool(
            project_pending_interactions,
            app,
            root_session_id,
            include_children=include_children,
            include_recent_resolved=include_recent_resolved,
            resolved_limit=resolved_limit,
        )
        return {
            "interactions": [_interaction_wire(row) for row in projection.rows],
            "include_children": include_children,
            "include_recent_resolved": include_recent_resolved,
            # Typed reasons this lane is partial (quarantined surfaces, an owner
            # or row cap). A shorter attention lane is never served as complete.
            "degradations": projection.degradations,
        }

    @app.post("/v1/sessions/{root_session_id}/interactions/{interaction_id}/respond")
    async def respond_interaction(
        root_session_id: str,
        interaction_id: str,
        request: RespondInteractionRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        if app.state.sessions.get(root_session_id) is None:
            raise _error(404, "not_found", f"session not found: {root_session_id}")
        scope = await run_in_threadpool(_session_scope, app, root_session_id, True)
        question = await run_in_threadpool(_question_from_interaction_id, app, interaction_id)
        if question is not None:
            if (
                question.session_id not in scope
                and question.owner_session_id not in scope
                and question.attended_session_id != root_session_id
            ):
                raise _error(404, "not_found", f"interaction not found: {interaction_id}")
            if question.status != "pending":
                raise _error(
                    409,
                    "interaction_resolved",
                    "interaction is already resolved",
                    interaction_id=interaction_id,
                    status=question.status,
                )
            interaction = _question_interaction(app, question)
            if not interaction.requires_human_response:
                raise _error(
                    409,
                    "interaction_not_human_addressed",
                    "this interaction is currently being answered by the agent",
                    interaction_id=interaction_id,
                    routing_state=interaction.routing_state,
                )
            action = request.action or "answer"
            if action == "cancel":
                await intervene(app, question.id, "cancel")
            elif action == "answer":
                await run_in_threadpool(
                    answer_question_response,
                    app,
                    TaskCaller(root_session_id, app.state.human_principal, human_action=True),
                    question.id,
                    QuestionReply(
                        answer=request.answer,
                        selected_options=tuple(request.selected_options),
                        # User-submitted form fields remain an exact field map.
                        # Ownership and correlation stay on the authoritative
                        # question/interaction instead of overwriting equally
                        # named schema fields inside answer metadata.
                        metadata=dict(request.metadata),
                        responder_human=app.state.human_principal,
                    ),
                )
            else:
                raise _error(422, "validation_error", "question action must be answer or cancel")
            updated = await run_in_threadpool(question_row, app, question.id)
            return {"interaction": _interaction_wire(_question_interaction(app, updated))}

        permission_id = interaction_id.removeprefix("permission:")
        permission = app.state.permissions.get(permission_id)
        if permission is not None:
            if str(permission.get("session_id") or "") not in scope:
                raise _error(404, "not_found", f"interaction not found: {interaction_id}")
            if permission.get("status") != "pending":
                raise _error(409, "interaction_resolved", "interaction is already resolved")
            action = request.action
            if action not in {"allow", "deny", "allow_session", "allow_workspace"}:
                raise _error(422, "validation_error", "permission response action is invalid")
            if permission.get("kind") == "folder_access" and action not in permission["actions"]:
                raise _error(422, "validation_error", "Choose Deny or Allow for workspace")
            # P2.6 parity with ``POST /v1/permissions/{pid}``: an approved PreToolUse
            # defer may carry the modify/synthesize the approval decides. Dropping it
            # here made the normalized responder a strictly weaker door for the same
            # decision — an approve-with-modified-args arrived as a plain allow.
            updated_permission = await run_off_loop(
                lambda: resolve_permission(
                    app,
                    permission_id,
                    action,
                    grantor=GRANTOR_USER,
                    intercept=_permission_intercept(request.metadata),
                )
            )
            if updated_permission is None:
                raise _error(409, "interaction_resolved", "interaction is already resolved")
            return {
                "interaction": _interaction_wire(_permission_interaction(app, updated_permission))
            }

        for owner in scope:
            prefix = f"a2ui:{owner}:"
            if not interaction_id.startswith(prefix):
                continue
            surface_id = interaction_id.removeprefix(prefix)
            surface = app.state.a2ui_store.get(owner, surface_id)
            if surface is None or surface.state == "deleted":
                break
            if not request.message:
                raise _error(422, "validation_error", "A2UI response requires message")
            action_payload = request.message.get("action")
            action_payload = action_payload if isinstance(action_payload, Mapping) else {}
            if str(action_payload.get("surfaceId") or "") != surface_id:
                raise _error(
                    422,
                    "validation_error",
                    "A2UI action surface does not match the interaction destination",
                )
            result = await app.state.dispatch_a2ui_action(
                owner,
                {
                    "message": request.message,
                    "correlation": {
                        **request.metadata,
                        "interaction_id": interaction_id,
                        "owner_session_id": owner,
                        "attended_session_id": root_session_id,
                        "task_id": _task_id_for_owner(app, owner),
                        "surface_id": surface_id,
                    },
                },
                protocol_version=getattr(http_request.state, "a2ui_protocol_version", None),
            )
            return {"interaction_id": interaction_id, "result": result}

        if interaction_id.startswith("mcp_task_input:"):
            raise _error(
                409,
                "interaction_not_ready",
                "MCP task input has not published its correlated question yet",
            )
        raise _error(404, "not_found", f"interaction not found: {interaction_id}")
