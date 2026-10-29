"""Lifespan bindings for Core history, conversation ownership and native MCP roots."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from functools import partial
from typing import Any

from clio_agent.arc.task_types import AssignmentDetails, SubmissionContext, TaskSnapshot
from clio_agent.gact import boot_timing
from clio_agent.gact.builtin_boundary import BuiltinBoundary
from clio_agent.gact.conversations import ConversationOutcome, Conversations, ConversationSegment
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.native_storage import register_native_storage
from clio_agent.gact.native_tools import NativeTools
from clio_agent.gact.obligations import Obligations
from clio_agent.gact.question_events import QuestionEvents
from clio_agent.gact.question_surface import prepare_question_gate
from clio_agent.gact.questions import QuestionCommands
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.spawn_context import prepare_child_session
from clio_agent.gact.task_access import TaskAccess, TaskCaller
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_operations import TaskOperations
from clio_agent.gact.task_publication import subscription_caller

logger = logging.getLogger(__name__)


async def run_lifespan(
    app: Any, readiness: asyncio.Future[None], stopping: asyncio.Event
) -> tuple[BaseException, ...]:
    """Own SDK entry and exit in one task while health can serve during Core attachment."""
    try:
        await start(app)
    except BaseException as error:
        logger.exception("Task runtime unavailable reason=startup_failed; settling owned resources")
        try:
            cleanup_errors = await close(app)
        except BaseException as cleanup_error:
            logger.exception("Task runtime unsettled reason=startup_cleanup_failed")
            failure: BaseException = BaseExceptionGroup(
                "Task runtime startup settlement failed", [error, cleanup_error]
            )
            readiness.set_exception(failure)
            readiness.exception()
            raise failure from None  # Do not release Core past unknown owner settlement.
        failure = (
            BaseExceptionGroup("Task runtime startup failed", [error, *cleanup_errors])
            if cleanup_errors
            else error
        )
        readiness.set_exception(failure)
        readiness.exception()
        return (failure,)
    readiness.set_result(None)
    boot_timing.mark(app, "task_runtime_ready")
    await stopping.wait()
    return await close(app)


async def start(app: Any) -> None:
    """Attach one confirmed Core writer before exposing durable task acceptance."""
    from clio_agent.gact.server_boot import process_arc_off_loop

    session_store: SessionStore = app.state.sessions
    arc = await process_arc_off_loop(app)
    if arc is None:
        raise RuntimeError("Durable tasks require clio-core; History mode cannot accept them")
    history = getattr(app.state, "task_history", None)
    if history is None:
        history = TaskHistory(arc)
        app.state.task_history = history  # Startup failure still owns and retires this writer.
        await boot_timing.timed(app, "task_history_restore", history.io.run(history.restore))
    # Before any tree loads; normally `clio-agent migrate` copied it (one marker read).
    from clio_agent.gact.store_migrations import ensure_at_boot

    await history.io.run(ensure_at_boot, app, "task_event_lane")
    await history.io.run(ensure_at_boot, app, "task_lane_reads")  # after the lane copy
    recovery = CoreRecovery(history)
    app.state.task_history, app.state.task_recovery = history, recovery
    app.state.human_principal = "human:" + str(app.state.connected_storage.store.clio_id)
    # One-time (normally done by `clio-agent migrate`): historical formats migrate from
    # the build's full replay, then the derived task records are written. Boot reads
    # the marker; later boots replay nothing.
    await history.io.run(ensure_at_boot, app, "task_index_build")
    app.state.questions = QuestionCommands(history)
    app.state.question_events = QuestionEvents(app)
    app.state.questions.on_change = app.state.question_events.changed
    app.state.questions.before_post = lambda task, definition: prepare_question_gate(
        app, task, definition
    )
    app.state.external_tasks = ExternalTasks(app)

    def authorize(submission: SubmissionContext) -> None:
        from clio_agent.gact.permission_gate import _policy_action_for_tool
        from clio_agent.tools.task_call_context import permission_snapshot

        session = session_store.get(submission.owner.session_id)
        assignment = history.task_snapshot(submission.owner.assignment_id)
        if session is None or assignment is None or assignment.owner.session_id != session.id:
            raise PermissionError("Execution owner is unavailable")
        continuation = submission.provider_continuation
        cleanup_only = bool(continuation and continuation.cancellation_only)
        try:
            app.state.conversations.require_admission(
                history.task_snapshot(submission.handle) or assignment
            )
        except ValueError:
            if continuation is None or submission.tool != "connected_data_continue":
                raise
            cleanup_only = True
        workspace = app.state.workspaces.get(session.workspace_id)
        if workspace is None or workspace.root_path != submission.workspace:
            raise PermissionError("Execution workspace changed after acceptance")
        policy_context = submission
        if continuation is not None:
            policy_context = SubmissionContext.model_validate(
                history.read_content(continuation.original_submission)
            )
            if (
                submission.tool != "connected_data_continue"
                or policy_context.tool != "connected_data_download"
                or policy_context.owner != submission.owner
                or policy_context.workspace != submission.workspace
            ):
                raise PermissionError(
                    "Provider continuation differs from original download custody"
                )
        public_name = "shell_bash" if policy_context.tool == "bash" else policy_context.tool
        declared_read = (
            submission.backend.owner == "conversation"
            and submission.backend.server == "clio-builtins"
            and submission.attribution.get("read_only") is True
        )
        action = (
            "allow"
            if declared_read or cleanup_only
            else _policy_action_for_tool(
                app,
                session_id=session.id,
                session=session,
                tool_name=public_name,
                args=policy_context.arguments,
                mode="" if submission.attribution.get("human_action") is True else session.mode,
            )
        )
        if action == "deny":
            raise PermissionError("Execution permission was revoked")
        current_permissions = permission_snapshot(app, session.id)
        if current_permissions != submission.permissions and action != "allow":
            raise PermissionError("Execution permissions changed; obtain new approval")

    async def cancel_owner(task: TaskSnapshot, caller: TaskCaller) -> dict[str, Any]:
        current = TaskAccess(history, caller).require(task.handle, "control")
        if current.kind in {"Session", "Subagent"}:
            return await app.state.conversations.cancel(caller, current.handle)
        if current.kind == "Question":
            settled = await history.io.run(
                app.state.questions.cancel, current.handle, caller=caller
            )
            return {
                "handle": settled.handle,
                "cancellation_requested": settled.status == "cancelled",
                "status": settled.status,
                "settlement": settled.settlement,
            }
        if current.kind in {"Shell", "Download", "Indexing"} or (
            current.backend is not None and current.backend.owner == "native"
        ):
            return await app.state.native_tools.cancel(current.handle, caller=caller)
        return await app.state.external_tasks.cancel(current.handle, caller=caller)

    async def cancel_owned(task: TaskSnapshot) -> dict[str, Any]:
        return await cancel_owner(task, TaskCaller(task.owner.session_id, task.owner.human_id))

    async def run_segment(segment: ConversationSegment) -> ConversationOutcome:
        from clio_agent.gact.events import Event
        from clio_agent.gact.native_work import now
        from clio_agent.gact.turn import _run_turn_in_background
        from clio_agent.gact.types import Message, Part

        def prepare() -> tuple[Message, str, str]:
            original = history.read_content(segment.trigger.content)
            message_id = original.get("message_id") if isinstance(original, dict) else None
            existing = next(
                (
                    row
                    for row in app.state.messages.get(segment.session_id, [])
                    if row.id == message_id
                ),
                None,
            )
            agent = original.get("agent_id", "") if isinstance(original, dict) else ""
            if not isinstance(agent, str):
                raise ValueError("Retained agent identity must be a string")
            model_text = segment.text
            retained_metadata = original.get("metadata") if isinstance(original, dict) else None
            if (
                isinstance(original, dict)
                and isinstance(retained_metadata, dict)
                and retained_metadata.get("mcp_app")
            ):
                retained_text = original.get("model_text", segment.text)
                if not isinstance(retained_text, str):
                    raise ValueError("Retained MCP App context must be text")
                model_text = retained_text
            if (
                segment.trigger.kind in {"human", "composer"}
                and isinstance(original, dict)
                and original.get("message")
            ):
                recovered = Message.model_validate(original["message"])
                if recovered.session_id != segment.session_id or recovered.role != "user":
                    raise ValueError("Retained human input belongs to another session or role")
                from clio_agent.gact.messaging import queued_user_input
                from clio_agent.gact.session_store import project_accepted_mail_messages

                rendered = queued_user_input(app, segment.session_id, original)
                model_text = rendered.get("text", "")
                if not isinstance(model_text, str):
                    raise ValueError("Retained human model text must be a string")
                project_accepted_mail_messages(app, history, segment.session_id, (segment.trigger,))
                return recovered, agent, model_text
            if segment.trigger.kind in {"human", "composer"} and existing is not None:
                return existing, agent, model_text
            if segment.trigger.kind in {"human", "composer"}:
                if not isinstance(original, str):
                    raise ValueError("The retained typed human message is unavailable")
                stamp = now()
                return (
                    Message(
                        id=segment.trigger.id,
                        turn_id=segment.turn_id,
                        session_id=segment.session_id,
                        role="user",
                        created_at=stamp,
                        updated_at=stamp,
                        parts=[Part(type="text", text=original)],
                    ),
                    agent,
                    model_text,
                )
            stamp = now()
            assignment_details = (
                AssignmentDetails.model_validate(history.read_content(segment.assignment.details))
                if segment.assignment.details is not None
                else AssignmentDetails()
            )
            producer_metadata: dict[str, Any] = {}
            if isinstance(original, dict) and segment.trigger.kind in {
                "approval",
                "hook",
                "goal",
                "loop",
                "schedule",
            }:
                raw_metadata = original.get("metadata", {})
                if not isinstance(raw_metadata, dict):
                    raise ValueError("Trusted continuation metadata is malformed")
                producer_metadata = raw_metadata
            source = (
                original.get("source", segment.trigger.kind)
                if isinstance(original, dict)
                else segment.trigger.kind
            )
            if not isinstance(source, str):
                raise ValueError("Retained continuation source must be text")
            return (
                Message(
                    id=segment.turn_id,
                    turn_id=segment.turn_id,
                    session_id=segment.session_id,
                    role="system",
                    created_at=stamp,
                    updated_at=stamp,
                    parts=[
                        Part(
                            id=segment.turn_id + ":continuation",
                            type="injection",
                            source=source,
                            text=segment.text,
                        )
                    ],
                    metadata={
                        "system_injection": True,
                        "assignment_handle": segment.assignment.handle,
                        "parent_turn_id": assignment_details.parent_turn_id,
                        **producer_metadata,
                    },
                ),
                agent,
                model_text,
            )

        message, agent, model_text = await history.io.run(prepare)
        app.state.cancel_flags.discard(segment.session_id)
        updated = await history.io.run(session_store.update, segment.session_id, status="running")
        if updated is not None:
            app.state.bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=segment.session_id,
                    payload={
                        "session_id": segment.session_id,
                        "status": updated.status,
                        "updated_at": updated.updated_at,
                    },
                )
            )
        outcome = await _run_turn_in_background(app, segment.session_id, model_text, message, agent)
        return (
            outcome
            if isinstance(outcome, ConversationOutcome)
            else ConversationOutcome({}, disposition="yield")
        )

    def gate(sid: str) -> str | None:
        if app.state.agent is None:
            return "agent_not_ready"
        session = session_store.get(sid)
        return (
            "session_unavailable"
            if session is None
            else session.metadata.get("execution_blocked_reason")
        )

    conversations = Conversations(
        app, history, recovery, run_segment, cancel_owned, partial(prepare_child_session, app), gate
    )
    app.state.conversations = conversations

    def request_refresh(task: TaskSnapshot) -> None:
        if task.backend is None or task.backend.owner not in {"native", "external_mcp"}:
            return
        native_owner = task.backend.owner == "native"

        async def refresh() -> None:
            owner = app.state.native_tools if native_owner else app.state.external_tasks
            await owner.restore((task.handle,))

        recovery.defer("task-refresh:" + task.handle, refresh)

    app.state.task_operations = TaskOperations(
        history, cancel_owner, refresh_owner=request_refresh, app=app
    )

    def task_changed(task: TaskSnapshot) -> None:
        from clio_agent.gact.events import Event

        if task.kind in {"Session", "Subagent"} and (
            task.terminal_at is not None or task.activity == "yielded"
        ):
            session = session_store.get(task.owner.session_id)
            parent_id = (
                session.metadata.get("spotter_parent_session") if session is not None else None
            )
            if parent_id:
                from clio_agent.gact.spotter_clearance import signal_clearance

                conversations.runner.run_when_released(
                    task.owner.session_id, lambda: signal_clearance(app, str(parent_id))
                )
        recipients = {task.owner.session_id}
        parent = task.owner.parent_handle
        seen: set[str] = set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            ancestor = history.task_snapshot(parent)
            if ancestor is None:
                break
            recipients.add(ancestor.owner.session_id)
            parent = ancestor.owner.parent_handle
        for subscription in history.subscriptions(task.handle):
            caller = subscription_caller(history, subscription)
            if caller is None:
                continue
            access = TaskAccess(history, caller)
            if subscription.enabled and {"read", "observe"} <= access.rights(task):
                recipients.add(subscription.recipient_session)
        for sid in recipients:
            # A refresh hint contains no output or description. API reads authorize again.
            app.state.bus.publish(
                Event(
                    type="task.updated",
                    session_id=sid,
                    payload={
                        "handle": task.handle,
                        "kind": task.kind,
                        "revision": history.revision,
                        "owner_session_id": task.owner.session_id,
                        **(
                            {"invocation_id": task.invocation_id}
                            if task.backend is not None
                            and task.backend.owner in {"native", "external_mcp"}
                            else {}
                        ),
                    },
                )
            )

    history.observation_committed = task_changed
    stack = AsyncExitStack()
    app.state.task_resource_stack = stack
    native = NativeTools(history, authorize, recovery, admission=conversations.require_admission)
    register_native_storage(native.roots["storage"], native.work["storage"], app, native=native)
    app.state.native_tools = await stack.enter_async_context(native)
    app.state.builtin_boundary = await stack.enter_async_context(BuiltinBoundary(app, authorize))
    # Native and external work left open is reattached per conversation by the
    # obligations lane (conversation_recovery.restore_session), not at startup.
    conversations.start()
    if not history.requires_recovery:
        # Work left open at the last stop is an obligation: only the conversations that
        # hold some wait, and it resumes in the background once startup is done.
        obligations = Obligations(conversations)
        await boot_timing.timed(app, "obligations_held", obligations.hold())
        app.state.obligations = obligations
        app.state.obligations_task = asyncio.create_task(
            boot_timing.timed(app, "obligations", obligations.run()), name="clio-obligations"
        )


async def close(app: Any) -> tuple[BaseException, ...]:
    """Retire effects first; return independent cleanup errors for final lifespan reporting."""
    conversations = getattr(app.state, "conversations", None)
    if conversations is not None:
        conversations.close_admission()
    obligations_task = getattr(app.state, "obligations_task", None)
    if obligations_task is not None and not obligations_task.done():
        obligations_task.cancel()
        await asyncio.wait({obligations_task})
    native = getattr(app.state, "native_tools", None)
    if native is not None:
        await native.request_shutdown()
    if conversations is not None:
        await conversations.shutdown()
    errors: list[BaseException] = []
    if native is not None:
        errors.extend(await native.settle_shutdown())
    question_events = getattr(app.state, "question_events", None)
    stack = getattr(app.state, "task_resource_stack", None)
    external = getattr(app.state, "external_tasks", None)
    recovery = getattr(app.state, "task_recovery", None)
    for resource, method in (
        (question_events, "close"),
        (stack, "aclose"),
        (external, "close"),
        (recovery, "drain"),
    ):
        if resource is not None:
            try:
                await getattr(resource, method)()
            except BaseException as error:
                logger.exception("Task runtime resource close failed method=%s", method)
                errors.append(error)
    history = getattr(app.state, "task_history", None)
    if history is not None:
        history.observation_committed = None
        try:
            await history.io.close()
        except BaseException as error:
            logger.exception("Task runtime Core I/O retirement failed")
            errors.append(error)
    return tuple(errors)
