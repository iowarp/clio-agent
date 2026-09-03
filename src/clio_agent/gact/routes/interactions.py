"""Normalized pending-interaction projection and exact-owner response routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Request

from clio_agent import conf
from clio_agent.gact.a2ui import SERVER_ACTIONS
from clio_agent.gact.agent_tasks import descendant_session_ids
from clio_agent.gact.ask_user_tool import restore_pending_ask_user_questions
from clio_agent.gact.mcp_task_store import app_task_store
from clio_agent.gact.permission_delivery import attended_session_id
from clio_agent.gact.permission_gate import GRANTOR_USER, resolve_permission
from clio_agent.gact.types import (
    AnswerUserQuestionRequest,
    ErrorEnvelope,
    ErrorInfo,
    PendingInteraction,
    PendingInteractionSource,
    RespondInteractionRequest,
    UserQuestion,
)
from clio_agent.tools.mcp_task_records import TaskRecord

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps

__all__ = [
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
    scope = {root_session_id}
    if include_children:
        scope.update(descendant_session_ids(app, root_session_id))
    return scope


def _question_correlation(app: FastAPI, question: UserQuestion) -> dict[str, str]:
    metadata = question.metadata if isinstance(question.metadata, Mapping) else {}
    elicitation = metadata.get("elicitation")
    elicitation = elicitation if isinstance(elicitation, Mapping) else {}
    child = app.state.user_questions.get(str(metadata.get("forwarded_from_question") or ""))
    child_meta = child.metadata if isinstance(getattr(child, "metadata", None), Mapping) else {}
    child_elicitation = child_meta.get("elicitation")
    child_elicitation = child_elicitation if isinstance(child_elicitation, Mapping) else {}
    return {
        "tool_name": str(elicitation.get("tool_name") or child_elicitation.get("tool_name") or ""),
        "invocation_id": str(
            metadata.get("invocation_id")
            or elicitation.get("invocation_id")
            or child_meta.get("invocation_id")
            or child_elicitation.get("invocation_id")
            or ""
        ),
        "task_id": str(
            metadata.get("task_id")
            or elicitation.get("task_id")
            or child_meta.get("task_id")
            or child_elicitation.get("task_id")
            or ""
        ),
        "input_key": str(elicitation.get("input_key") or child_elicitation.get("input_key") or ""),
    }


def _question_interaction(app: FastAPI, question: UserQuestion) -> PendingInteraction:
    correlation = _question_correlation(app, question)
    is_mcp = question.source == "mcp_elicitation" or bool(
        isinstance(question.metadata, Mapping) and question.metadata.get("elicitation")
    )
    task_id = correlation["task_id"]
    kind: Literal["question", "mcp_task_input"] = (
        "mcp_task_input" if is_mcp and task_id else "question"
    )
    return PendingInteraction(
        id=f"{kind}:{question.id}",
        kind=kind,
        owner_session_id=question.owner_session_id or question.session_id,
        attended_session_id=question.attended_session_id or question.session_id,
        task_id=task_id,
        status=question.status,
        title="MCP task input required" if kind == "mcp_task_input" else "Question from agent",
        prompt=question.prompt,
        source=PendingInteractionSource(
            protocol="mcp" if is_mcp else "native",
            tool_name=correlation["tool_name"],
            invocation_id=correlation["invocation_id"],
        ),
        created_at=question.created_at,
        payload={
            key: value
            for key, value in {
                "question_id": question.id,
                "question_kind": question.kind,
                "options": [option.model_dump() for option in question.options],
                "allow_freeform": question.allow_freeform,
                "expires_at": question.expires_at,
                "input_key": correlation["input_key"],
            }.items()
            if value not in ("", [], None)
        },
        actions=["answer", "cancel"] if question.status == "pending" else [],
    )


def _permission_interaction(app: FastAPI, row: Mapping[str, Any]) -> PendingInteraction:
    owner = str(row.get("session_id") or "")
    tool_call = row.get("tool_call")
    tool_call = tool_call if isinstance(tool_call, Mapping) else {}
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
        payload={"permission_id": str(row.get("id") or ""), "tool_call": dict(tool_call)},
        actions=(
            ["allow", "deny", "allow_session", "allow_workspace"] if status == "pending" else []
        ),
    )


def _surface_actions(surface: Mapping[str, Any]) -> list[str]:
    """Return registered server action names declared by a folded surface."""

    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            event = value.get("event")
            if isinstance(event, Mapping):
                name = str(event.get("name") or "")
                if name in SERVER_ACTIONS:
                    found.add(name)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(surface.get("messages") or [])
    return sorted(found)


def _surface_last_action(surface: Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``/lastAction`` the dispatcher wrote back, if the surface has one.

    ``dispatch_action`` acknowledges every accepted action by folding an
    ``updateDataModel`` at ``/lastAction`` into the surface. That fold IS the
    server's record that this surface was responded to, so the projection reads it
    rather than reporting every action-bearing surface ``pending`` forever.
    """

    latest: dict[str, Any] = {}
    for message in surface.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        update = message.get("updateDataModel")
        if not isinstance(update, Mapping) or str(update.get("path") or "") != "/lastAction":
            continue
        value = update.get("value")
        if isinstance(value, Mapping):
            latest = dict(value)
    return latest


def _a2ui_interactions(app: FastAPI, owner: str) -> list[PendingInteraction]:
    rows: list[PendingInteraction] = []
    for surface in app.state.a2ui_store.list_wire(owner):
        actions = _surface_actions(surface)
        if surface.get("state") == "deleted" or not actions:
            continue
        surface_id = str(surface.get("id") or "")
        last_action = _surface_last_action(surface)
        # A responded surface is SETTLED. It used to keep projecting ``pending``
        # forever, so an attention lane showed a surface the user had already
        # submitted, indefinitely and unboundedly. The action list is NOT cleared:
        # a surface can legitimately be acted on again (a chat-like agent.submit),
        # so the row states what happened rather than foreclosing what is offered.
        settled = bool(last_action)
        payload: dict[str, Any] = {"revision": surface.get("revision", 0)}
        if settled:
            payload["last_action"] = last_action
        rows.append(
            PendingInteraction(
                id=f"a2ui:{owner}:{surface_id}",
                kind="a2ui",
                owner_session_id=owner,
                attended_session_id=attended_session_id(app, owner),
                task_id=_task_id_for_owner(app, owner),
                status="answered" if settled else "pending",
                title="Interactive surface",
                source=PendingInteractionSource(
                    protocol="native",
                    tool_name="create_a2ui_surface",
                    invocation_id=str(surface.get("run_id") or surface.get("message_id") or ""),
                    surface_id=surface_id,
                ),
                created_at=str(surface.get("created_at") or ""),
                payload=payload,
                actions=actions,
            )
        )
    return rows


def _task_id_for_owner(app: FastAPI, owner: str) -> str:
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return ""
    tasks = [task for task in registry.snapshot() if task.child_session_id == owner]
    tasks.sort(key=lambda task: task.created_at, reverse=True)
    return tasks[0].task_id if tasks else ""


def _orphan_mcp_interaction(app: FastAPI, record: TaskRecord) -> PendingInteraction:
    owner = str(record.session_id or "")
    return PendingInteraction(
        id=f"mcp_task_input:{record.key.server_id}:{record.task_id}",
        kind="mcp_task_input",
        owner_session_id=owner,
        attended_session_id=attended_session_id(app, owner),
        task_id=record.task_id,
        title=f"{record.tool or 'MCP task'} requires input",
        source=PendingInteractionSource(protocol="mcp", tool_name=record.tool),
        created_at=record.created_at,
        payload={"server_id": record.key.server_id, "awaiting_question": True},
        actions=[],
    )


def interactions_projection_limit() -> int:
    """Maximum interaction rows one projection returns, newest first.

    The projection is a full scan of four ledgers with no bound of its own, and
    ``include_children=true`` on a deep spawn tree multiplies it. Config:
    ``gact.interactions.projection_limit`` / ``CLIO_INTERACTIONS_PROJECTION_LIMIT``.
    """

    return conf.resolve(
        "gact.interactions.projection_limit",
        env="CLIO_INTERACTIONS_PROJECTION_LIMIT",
        default=200,
        cast=conf.as_int,
    )


def project_pending_interactions(
    app: FastAPI, root_session_id: str, *, include_children: bool
) -> list[PendingInteraction]:
    """Project pending interactions from the existing authoritative ledgers."""

    scope = _session_scope(app, root_session_id, include_children)
    questions = [
        question
        for question in app.state.user_questions.values()
        if question.status == "pending"
        and (
            question.session_id in scope
            or question.owner_session_id in scope
            or question.attended_session_id == root_session_id
        )
    ]
    forwarded_children = {
        str(question.metadata.get("forwarded_from_question") or "")
        for question in questions
        if isinstance(question.metadata, Mapping)
    }
    question_rows = [
        _question_interaction(app, question)
        for question in questions
        if question.id not in forwarded_children
    ]
    correlated_tasks = {row.task_id for row in question_rows if row.task_id}
    rows: list[PendingInteraction] = list(question_rows)
    rows.extend(
        _permission_interaction(app, permission)
        for permission in app.state.permissions.values()
        if permission.get("status") == "pending"
        and str(permission.get("session_id") or "") in scope
    )
    for owner in scope:
        rows.extend(_a2ui_interactions(app, owner))
    rows.extend(
        _orphan_mcp_interaction(app, record)
        for record in app_task_store(app).list()
        if record.display_status == "input_required"
        and str(record.session_id or "") in scope
        and record.task_id not in correlated_tasks
    )
    return sorted(rows, key=lambda row: row.created_at, reverse=True)[
        : interactions_projection_limit()
    ]


def _interaction_wire(row: PendingInteraction) -> dict[str, Any]:
    """Serialize a projection while retaining required fields with defaults."""

    wire = row.model_dump(exclude_defaults=True)
    wire["status"] = row.status
    return wire


def _question_from_interaction_id(app: FastAPI, interaction_id: str) -> UserQuestion | None:
    prefixes = ("question:", "mcp_task_input:")
    question_id = interaction_id
    for prefix in prefixes:
        if interaction_id.startswith(prefix):
            question_id = interaction_id.removeprefix(prefix)
            break
    return app.state.user_questions.get(question_id)


def register_interaction_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register normalized list/respond routes without introducing a state store."""

    del deps
    restore_pending_ask_user_questions(app)

    @app.get("/v1/sessions/{root_session_id}/interactions")
    async def list_interactions(
        root_session_id: str, include_children: bool = False
    ) -> dict[str, Any]:
        if app.state.sessions.get(root_session_id) is None:
            raise _error(404, "not_found", f"session not found: {root_session_id}")
        rows = project_pending_interactions(app, root_session_id, include_children=include_children)
        return {
            "interactions": [_interaction_wire(row) for row in rows],
            "include_children": include_children,
        }

    @app.post(
        "/v1/sessions/{root_session_id}/interactions/{interaction_id}/response",
        include_in_schema=False,
    )
    @app.post("/v1/sessions/{root_session_id}/interactions/{interaction_id}/respond")
    async def respond_interaction(
        root_session_id: str,
        interaction_id: str,
        request: RespondInteractionRequest,
        http_request: Request,
    ) -> dict[str, Any]:
        if app.state.sessions.get(root_session_id) is None:
            raise _error(404, "not_found", f"session not found: {root_session_id}")
        scope = _session_scope(app, root_session_id, True)
        question = _question_from_interaction_id(app, interaction_id)
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
            action = request.action or "answer"
            if action == "cancel":
                updated = await app.state.cancel_user_question(question.session_id, question.id)
            elif action == "answer":
                interaction = _question_interaction(app, question)
                updated = await app.state.answer_user_question(
                    question.session_id,
                    question.id,
                    AnswerUserQuestionRequest(
                        answer=request.answer,
                        selected_options=request.selected_options,
                        metadata={
                            **request.metadata,
                            "interaction_id": interaction_id,
                            "owner_session_id": interaction.owner_session_id,
                            "attended_session_id": interaction.attended_session_id,
                            "task_id": interaction.task_id,
                            "invocation_id": interaction.source.invocation_id,
                        },
                    ),
                )
            else:
                raise _error(422, "validation_error", "question action must be answer or cancel")
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
            # P2.6 parity with ``POST /v1/permissions/{pid}``: an approved PreToolUse
            # defer may carry the modify/synthesize the approval decides. Dropping it
            # here made the normalized responder a strictly weaker door for the same
            # decision — an approve-with-modified-args arrived as a plain allow.
            updated = resolve_permission(
                app,
                permission_id,
                action,
                grantor=GRANTOR_USER,
                intercept=_permission_intercept(request.metadata),
            )
            if updated is None:
                raise _error(409, "interaction_resolved", "interaction is already resolved")
            return {"interaction": _interaction_wire(_permission_interaction(app, updated))}

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
