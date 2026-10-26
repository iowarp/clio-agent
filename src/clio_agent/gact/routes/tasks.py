"""Human task and Question routes over the same Core records and owner commands."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from clio_agent.arc.task_types import (
    CoordinationGrant,
    QuestionDefinition,
    QuestionOption,
    QuestionReply,
    TaskOwner,
    TaskSnapshot,
)
from clio_agent.errors import ClioError
from clio_agent.gact.auth import configured_bearer_token, has_valid_bearer
from clio_agent.gact.fleet.child_model import recorded_choice
from clio_agent.gact.human_controls import intervene
from clio_agent.gact.native_work import now
from clio_agent.gact.off_loop import run_off_loop
from clio_agent.gact.question_surface import question_view
from clio_agent.gact.routes.session_question_helpers import (
    answer_question_response,
    normalize_question_options,
)
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_flow_summary import task_flow_summary
from clio_agent.gact.task_history import RecordingUnconfirmed
from clio_agent.gact.types import CreateUserQuestionRequest, UserQuestion


class SelectedTasks(BaseModel):
    """Shared per-handle selection; a failed member never affects its neighbors."""

    tasks: str | list[str]


class ObserveRequest(SelectedTasks):
    """The shared observation contract; omission preserves subscription policy."""

    cursor: str | None = None
    pattern: str | None = None
    timeout_s: float | None = Field(default=None, ge=0)
    notify: bool | None = None
    view: Literal["progress", "transcript", "summary", "artifacts"] = "progress"
    history: Literal["assignment", "session"] = "assignment"
    limit: int = Field(default=50, ge=1, le=200)
    artifact_id: str | None = None


class WaitRequest(SelectedTasks):
    """An explicit mixed-task wait, with no product deadline by default."""

    return_when: Literal["any", "all"] = "all"
    timeout_s: float | None = Field(default=None, ge=0)


class GrantRequest(BaseModel):
    """Authenticated human/configuration rights, never model intent."""

    model_config = ConfigDict(extra="forbid")
    recipient_session: str
    target_session: str
    target_scope: Literal["session", "tree"] = "session"
    rights: tuple[Literal["read", "observe", "message", "control"], ...]


class AnswerRequest(BaseModel):
    """Validated answer payload; responder identity is assigned by this boundary."""

    answer: str = ""
    value: str | None = None  # Released single-choice/freeform wire alias.
    selected_options: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    parts: tuple[dict[str, Any], ...] = ()


def process_view(app: Any, task: dict[str, Any]) -> dict[str, Any]:
    """A shipped wire alias projected from one snapshot, without joining old stores."""
    owner, backend = task["owner"], task.get("backend") or {}
    parent = app.state.task_history.task_snapshot(owner.get("parent_handle", ""))
    activity = task.get("activity", "")
    status = task["status"]
    displayed = (
        status
        if task.get("terminal_at") or status == "task_state_uncertain"
        else {
            "busy": "running",
            "executing": "running",
            "idle": "yielded",
            "cancelling": "cancellation_requested",
            "waiting_for_input": "waiting_user",
        }.get(activity, activity or status)
    )
    agent_task = task["kind"] in {"Session", "Subagent"}
    child = app.state.sessions.get(owner["session_id"]) if agent_task else None
    flow = ((getattr(child, "metadata", None) or {}).get("agent_flow") or {}) if child else {}
    return {
        **task,
        "id": task["handle"],
        "handle_id": task["handle"],
        "task_kind": task["kind"],
        # A child conversation that runs a flow: the UI opens its run view, not a chat.
        "flow": task_flow_summary(app.state.task_history, task, str(flow["id"]))
        if flow.get("id")
        else None,
        # The model the child runs on and why, recorded when it was created.
        "model_choice": recorded_choice(app, owner["session_id"]) if agent_task else None,
        "kind": "agent"
        if task["kind"] in {"Session", "Subagent"}
        else "question"
        if task["kind"] == "Question"
        else "mcp-task",
        "title": task["label"],
        "live_state": displayed,
        "effective_status": displayed,
        "owner_session_id": owner["session_id"],
        "child_session_id": owner["session_id"] if task["kind"] in {"Session", "Subagent"} else "",
        "parent_session_id": parent.owner.session_id if parent is not None else owner["session_id"],
        "parent_turn_id": task["invocation_id"],
        "cancel_requested": task["cancellation_requested_at"] is not None,
        "placement": backend.get("placement", "local"),
        "metadata": {},
        "result_reference": task.get("result"),
        "result": None,
        "error_reason": task.get("progress", {}).get("message", "")
        if status in {"failed", "task_state_uncertain"}
        else "",
    }


def agent_task_view(app: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Project shipped agent aliases through the same snapshot and display mapping."""
    process = process_view(app, task)
    return {
        "task_id": task["handle"],
        "child_session_id": process["child_session_id"],
        "parent_session_id": process["parent_session_id"],
        "parent_turn_id": task["invocation_id"],
        "status": task["status"],
        "live_state": process["live_state"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "run_label": task["label"],
        "error_reason": process["error_reason"],
        "model_choice": process["model_choice"],
    }


def register_task_routes(app: FastAPI) -> None:
    """Register canonical controls plus pure compatibility projections."""

    def caller(sid: str) -> TaskCaller:
        if not sid:
            return TaskCaller("", app.state.human_principal, human_action=True)
        session = app.state.sessions.get(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="Session unavailable")
        assignment = app.state.task_history.latest_assignment(sid)
        owner = (
            assignment.owner.human_id
            if assignment is not None
            else session.metadata.get("owner_human_id", app.state.human_principal)
        )
        if owner != app.state.human_principal:
            raise HTTPException(status_code=404, detail="Session unavailable")
        return TaskCaller(sid, app.state.human_principal, human_action=True)

    @app.get("/v1/sessions/{sid}/tasks/query")
    async def tasks(
        sid: str,
        kind: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        scope: str = "tree",
        handle: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
        active_only: bool = False,
        query: str | None = None,
        message_after: str | None = None,
        message_before: str | None = None,
        relation: str = "tree",
    ) -> dict[str, Any]:
        try:
            return await run_off_loop(
                app.state.task_operations.query,
                await run_off_loop(caller, sid),
                kind=kind,
                status=status,
                cursor=cursor,
                limit=limit,
                scope=scope,
                handle=handle,
                tool=tool,
                agent=agent,
                active_only=active_only,
                query=query,
                message_after=message_after,
                message_before=message_before,
                relation=relation,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/v1/tasks/query")
    async def all_tasks(
        kind: str | None = None,
        active_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
        query: str | None = None,
        message_after: str | None = None,
        message_before: str | None = None,
    ) -> dict[str, Any]:
        """Page the authenticated human's indexed work across sessions without joins."""
        return await tasks(
            "",
            kind=kind,
            active_only=active_only,
            cursor=cursor,
            limit=limit,
            scope="authorized",
            query=query,
            message_after=message_after,
            message_before=message_before,
        )

    @app.post("/v1/sessions/{sid}/tasks/observe")
    async def observe(sid: str, body: ObserveRequest) -> dict[str, Any]:
        try:
            return await app.state.task_operations.observe(
                await app.state.task_history.io.run(caller, sid), **body.model_dump()
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/v1/sessions/{sid}/tasks/wait")
    async def wait(sid: str, body: WaitRequest) -> dict[str, Any]:
        try:
            return await app.state.task_operations.wait(
                await app.state.task_history.io.run(caller, sid), **body.model_dump()
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/v1/sessions/{sid}/tasks/{handle}/result")
    async def result(
        sid: str,
        handle: str,
        cursor: str | None = None,
        select: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            return await app.state.task_history.io.run(
                app.state.task_operations.result,
                await app.state.task_history.io.run(caller, sid),
                handle,
                cursor,
                select,
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/v1/sessions/{sid}/tasks/{handle}/flow")
    async def flow(sid: str, handle: str) -> dict[str, Any]:
        """The flow run checkpointed under this task: the live plan, calls, events, outcome."""
        from clio_agent.flows.run_view import flow_run
        from clio_agent.gact.fleet.run_usage import call_models, run_spend

        def read() -> tuple[str, dict[str, Any] | None]:
            task = TaskAccess(app.state.task_history, caller(sid)).require(handle, "read")
            run = flow_run(app.state.task_history, task.handle, reconcile_children=True)
            if run is not None:
                run["usage"] = {"calls": call_models(app, run["calls"])}
                # The diagram needs timestamps, not one full agent projection per node.
                # Read these from the same authorized task view and history lane.
                handles = {task.handle, *(row["handle"] for row in run["calls"] if row["handle"])}
                run["timings"] = {}
                for child_handle in handles:
                    snapshot = app.state.task_history.task_snapshot(child_handle)
                    if snapshot is not None and snapshot.owner.human_id == task.owner.human_id:
                        run["timings"][child_handle] = {
                            "created_at": snapshot.created_at,
                            "updated_at": snapshot.updated_at,
                            "terminal_at": snapshot.terminal_at,
                        }
            return task.owner.session_id, run

        try:
            run_session, run = await app.state.task_history.io.run(read)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        if run is None:
            raise HTTPException(404, f"No flow run is recorded under {handle}")
        # Spend reads transcripts: off the task history's lane.
        run["usage"].update(await asyncio.to_thread(run_spend, app, run_session))
        return run

    @app.get("/v1/sessions/{sid}/tasks/mailbox")
    async def mailbox(sid: str) -> dict[str, Any]:
        selected = await app.state.task_history.io.run(caller, sid)

        def project() -> dict[str, Any]:
            owner = app.state.conversations
            access = TaskAccess(app.state.task_history, selected)
            return {
                "pending": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "handle": item.handle,
                        "created_at": item.created_at,
                        "delivery": item.delivery,
                        "held_reason": item.held_reason,
                        "eligible": owner.eligible(sid, item)
                        and owner.mail._authorized(item, access),
                    }
                    for item in app.state.task_history.pending_mail(sid)
                ]
            }

        return await app.state.task_history.io.run(project)

    @app.get("/v1/sessions/{sid}/async-processes")
    async def processes(
        sid: str,
        cursor: str | None = None,
        limit: int = 50,
        active_only: bool = False,
        include_descendants: bool = True,
    ) -> dict[str, Any]:
        """Project retained work; the Work pane explicitly requests direct children."""
        page = await tasks(
            sid,
            cursor=cursor,
            limit=limit,
            active_only=active_only,
            relation="tree" if include_descendants else "direct",
        )

        def project() -> dict[str, Any]:
            return {
                "processes": [process_view(app, row) for row in page["tasks"]],
                **{key: value for key, value in page.items() if key != "tasks"},
            }

        return await app.state.task_history.io.run(project)

    @app.get("/v1/sessions/{sid}/agent-tasks")
    async def agent_tasks(
        sid: str, cursor: str | None = None, limit: int = 50, active_only: bool = False
    ) -> dict[str, Any]:
        page = await tasks(
            sid, kind="Subagent", cursor=cursor, limit=limit, active_only=active_only
        )

        def project() -> dict[str, Any]:
            return {**page, "tasks": [agent_task_view(app, row) for row in page["tasks"]]}

        return await app.state.task_history.io.run(project)

    @app.get("/v1/agent-tasks/{handle}")
    async def agent_task(handle: str) -> dict[str, Any]:
        def project() -> dict[str, Any]:
            access = TaskAccess(
                app.state.task_history,
                TaskCaller("", app.state.human_principal, human_action=True),
            )
            task = access.require(handle, "read")
            if task.kind not in {"Session", "Subagent"}:
                raise ValueError("Task is unavailable for this operation")
            return agent_task_view(app, access.view(task))

        try:
            return await app.state.task_history.io.run(project)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/v1/sessions/{sid}/tasks/cancel")
    @app.post("/v1/sessions/{sid}/async-tasks/cancel")
    async def cancel(sid: str, body: SelectedTasks) -> dict[str, Any]:
        caller(sid)
        selected = [body.tasks] if isinstance(body.tasks, str) else body.tasks
        results, errors = [], []
        for handle in selected:
            try:
                effect = await intervene(app, handle, "cancel")
                if "error" in effect:
                    errors.append({"handle": handle, "error": effect["error"]})
                else:
                    results.append(
                        {
                            **effect,
                            "cancellation_requested": effect.get("cancellation_requested", False),
                            "status": effect.get("status", "cancellation_requested"),
                        }
                    )
            except ValueError as error:
                errors.append({"handle": handle, "error": str(error)})
        return {"results": results, "errors": errors}

    @app.post("/v1/sessions/{sid}/agent-controls/{handle}/{action}")
    async def control(
        sid: str, handle: str, action: Literal["pause", "resume", "restart"]
    ) -> dict[str, Any]:
        identity = caller(sid)
        try:
            task = TaskAccess(app.state.task_history, identity).require(handle, "control")
        except ValueError as error:
            raise HTTPException(404, "Task is unavailable") from error
        try:
            effect = await intervene(app, task.handle, action)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return {
            **effect,
            "session_id": task.owner.session_id,
            "status": effect.get("status", action + "_requested"),
        }

    @app.post("/v1/sessions/{sid}/questions", response_model=UserQuestion, status_code=201)
    async def create_question(sid: str, body: CreateUserQuestionRequest) -> dict[str, Any]:
        """Retain the public creation contract through the shared Question owner."""
        identity = "task_" + uuid4().hex
        invocation = "http-question:" + uuid4().hex

        def create() -> dict[str, Any]:
            actor = caller(sid)
            definition = QuestionDefinition(
                prompt=body.prompt.strip(),
                kind=body.kind,
                options=tuple(
                    QuestionOption.model_validate(option.model_dump(mode="json"))
                    for option in normalize_question_options(body)
                ),
                allow_freeform=body.allow_freeform or body.kind == "freeform",
                source=body.source or "orchestrator",
                expires_at=body.expires_at or None,
                metadata={
                    **body.metadata,
                    **({"source_user_message_id": body.turn_id} if body.turn_id else {}),
                    **({"attempt_id": body.attempt_id} if body.attempt_id else {}),
                },
            )
            app.state.questions.validate_definition(definition)
            app.state.questions._deadline(definition)
            with app.state.task_history.serialized():
                parent = app.state.task_history.latest_assignment(sid)
                if parent is not None:
                    try:
                        app.state.conversations.require_admission(parent)
                    except ValueError as error:
                        raise HTTPException(409, str(error)) from error
                if parent is None or parent.terminal_at is not None:
                    parent = app.state.conversations._new_assignment(
                        sid,
                        definition.prompt,
                        actor.human_id,
                        predecessor=parent,
                        invocation_id=invocation,
                    )
                task = app.state.questions.create(
                    TaskOwner(
                        human_id=actor.human_id,
                        session_id=sid,
                        assignment_id=parent.handle,
                        parent_handle=parent.handle,
                    ),
                    definition,
                    invocation_id=invocation,
                    handle=identity,
                )
                return question_view(app, task)

        try:
            return await app.state.task_history.io.run(create)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except (ClioError, OSError, TimeoutError, RecordingUnconfirmed) as error:
            raise HTTPException(
                503,
                {
                    "error": {
                        "error": "question_recording_unconfirmed",
                        "message": "Question recording could not be confirmed. Inspect its handle before retrying.",
                        "details": {"handle": identity},
                        "recoverable": True,
                    }
                },
            ) from error

    @app.get("/v1/sessions/{sid}/questions")
    async def questions(
        sid: str, status: str | None = None, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        # The public Question state belongs to QuestionCommands. Select its
        # effective state before paging; pending reads use the active index.
        effective = {
            "pending": "input_required",
            "answered": "completed",
            "cancelled": "cancelled",
            "expired": "failed",
        }.get(status or "", status)
        page = await tasks(
            sid,
            kind="Question",
            status=effective,
            active_only=status == "pending",
            cursor=cursor,
            limit=limit,
            scope="tree" if sid else "authorized",
        )

        def project() -> dict[str, Any]:
            history = app.state.task_history
            access = TaskAccess(history, caller(sid))
            scope = "tree" if sid else "authorized"
            # This compatibility endpoint promises an exact filtered count.
            # Count only its authorized Question index, never unrelated history.
            handles = {
                handle
                for ordering in access.query_orders(
                    scope, kind="Question", active_only=status == "pending"
                )
                for _created, handle in ordering
            }
            total = sum(
                1
                for handle in handles
                if (task := history.task_snapshot(handle)) is not None
                and access.in_scope(task, scope)
                and "read" in access.rights(task)
                and (effective is None or task.status == effective)
            )
            values = [
                question_view(app, access.require(row["handle"], "read")) for row in page["tasks"]
            ]
            return {
                "questions": [row for row in values if status is None or row["status"] == status],
                "metadata": {"status": status, "total": total},
                **{key: value for key, value in page.items() if key != "tasks"},
            }

        return await app.state.task_history.io.run(project)

    @app.get("/v1/questions")
    async def all_questions(
        session_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await questions(session_id or "", status=status, cursor=cursor, limit=limit)

    def session_question(sid: str, handle: str) -> TaskSnapshot:
        """Bind a compatibility Question address to its selected session tree."""
        access = TaskAccess(app.state.task_history, caller(sid))
        try:
            current = access.require(handle, "control")
            if current.kind != "Question" or not access.in_scope(current, "tree"):
                raise ValueError("Question unavailable")
        except ValueError as error:
            raise HTTPException(404, "Question is unavailable") from error
        return current

    @app.get("/v1/sessions/{sid}/questions/{handle}", response_model=UserQuestion)
    async def get_question(sid: str, handle: str) -> dict[str, Any]:
        """Read the authorized Question and its existing recipient-input projection."""

        def read() -> dict[str, Any]:
            return question_view(app, session_question(sid, handle))

        return await app.state.task_history.io.run(read)

    @app.post("/v1/sessions/{sid}/questions/{handle}/answer")
    async def answer(sid: str, handle: str, body: AnswerRequest) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            authorized = session_question(sid, handle)
            payload = body.model_dump(exclude={"value"})
            if body.value is not None:
                if body.answer or body.selected_options:
                    raise HTTPException(422, "Use value or answer/selected_options, not both")
                if authorized.details is None:
                    raise HTTPException(409, "The retained Question definition is unavailable")
                definition = QuestionDefinition.model_validate(
                    app.state.task_history.read_content(authorized.details)
                )
                if body.value in {option.value for option in definition.options}:
                    payload["selected_options"] = (body.value,)
                else:
                    payload["answer"] = body.value
            reply = QuestionReply(**payload, responder_human=app.state.human_principal)
            current = answer_question_response(app, caller(sid), authorized.handle, reply)
            return question_view(app, current)

        return await app.state.task_history.io.run(apply)

    @app.post("/v1/sessions/{sid}/questions/{handle}/cancel")
    async def cancel_question(sid: str, handle: str) -> dict[str, Any]:
        current = await app.state.task_history.io.run(session_question, sid, handle)
        try:
            await intervene(app, current.handle, "cancel")
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return await app.state.task_history.io.run(
            lambda: question_view(app, app.state.task_history.task_snapshot(current.handle))
        )

    def authenticated_human(request: Request) -> None:
        if not has_valid_bearer(request.scope, configured_bearer_token()):
            raise HTTPException(
                status_code=401,
                detail="Session grants require the authenticated human control channel",
            )

    @app.get("/v1/sessions/{sid}/coordination-grants")
    async def grants(sid: str, request: Request) -> dict[str, Any]:
        authenticated_human(request)
        await app.state.task_history.io.run(caller, sid)
        return await app.state.task_history.io.run(
            lambda: {
                "grants": [
                    row.model_dump(mode="json")
                    for row in app.state.task_history.coordination_grants(sid)
                ]
            }
        )

    @app.post("/v1/sessions/{sid}/coordination-grants")
    async def grant(sid: str, body: GrantRequest, request: Request) -> dict[str, Any]:
        authenticated_human(request)
        await app.state.task_history.io.run(caller, sid)
        if body.recipient_session != sid:
            raise HTTPException(status_code=400, detail="Grant recipient differs from this session")

        def apply() -> dict[str, Any]:
            target = app.state.sessions.get(body.target_session)
            assignment = app.state.task_history.latest_assignment(body.target_session)
            human = (
                assignment.owner.human_id
                if assignment is not None
                else target.metadata.get("owner_human_id", app.state.human_principal)
                if target is not None
                else None
            )
            if target is None or human != app.state.human_principal:
                raise HTTPException(status_code=404, detail="Grant target is unavailable")
            row = CoordinationGrant(
                id="grant_" + uuid4().hex,
                granting_human=app.state.human_principal,
                created_at=now(),
                **body.model_dump(),
            )
            app.state.task_history.record_grant(row, observation_id=uuid4().hex)
            return row.model_dump(mode="json")

        return await app.state.task_history.io.run(apply)

    @app.delete("/v1/sessions/{sid}/coordination-grants/{identity}")
    async def revoke(sid: str, identity: str, request: Request) -> dict[str, Any]:
        authenticated_human(request)
        await app.state.task_history.io.run(caller, sid)

        def apply() -> dict[str, Any]:
            row = next(
                (
                    row
                    for row in app.state.task_history.coordination_grants(sid)
                    if row.id == identity
                ),
                None,
            )
            if row is None or row.granting_human != app.state.human_principal:
                raise HTTPException(404, "Grant is unavailable")
            revoked = (
                row if row.revoked_at is not None else row.model_copy(update={"revoked_at": now()})
            )
            app.state.task_history.record_grant(revoked, observation_id=uuid4().hex)
            return revoked.model_dump(mode="json")

        return await app.state.task_history.io.run(apply)
