"""Conversation-owner recovery algorithms; no independent state or worker."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from clio_agent.arc.task_types import (
    AssignmentDetails,
    ContextBoundary,
    MailItem,
    TaskProgress,
    TaskSnapshot,
)
from clio_agent.errors import ClioError
from clio_agent.gact import conversation_controls
from clio_agent.gact.native_work import now
from clio_agent.gact.session_input import input_delivery, withdraw_assignment_input
from clio_agent.gact.session_mail import StagedInput
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_history import RecordingUnconfirmed, TaskHistory
from clio_agent.gact.task_publication import publish_task_notice, task_owner_recipient

if TYPE_CHECKING:
    from clio_agent.gact.conversation_assignments import ConversationSegment
    from clio_agent.gact.conversations import (
        Conversations,
    )


def _input_committed(self: Conversations, staged: StagedInput) -> None:
    """Derive A2UI display state only after the Core input acknowledgement."""
    from clio_agent.gact.session_store import project_committed_feedback

    for sid in {
        item.recipient_session for item in staged.items if item.kind in {"human", "composer"}
    }:
        messages = self.app.state.messages
        resident = getattr(messages, "get_if_resident", messages.get)(sid)
        if resident is not None:
            project_committed_feedback(self.history, sid, resident)
    for item in staged.items:
        if item.kind not in {"a2ui", "question_answer", "human"} or (
            item.kind == "human" and not item.id.startswith("a2ui:")
        ):
            continue

        def update(item: MailItem = item) -> None:
            from clio_agent.gact.a2ui_actions.record import mark_a2ui_action_consumed

            content = self.history.read_content(item.content)
            metadata = content.get("metadata") if isinstance(content, dict) else None
            session_id = item.recipient_session
            if item.kind == "question_answer":
                task = self.history.task_snapshot(item.handle or "")
                if (
                    task is None
                    or task_owner_recipient(self.history, task) != item.recipient_session
                ):
                    return  # A subscriber cannot acknowledge the owner's receipt.
                action = content.get("source_action") if isinstance(content, dict) else None
                if not isinstance(action, dict):
                    return
                session_id = str(action["session_id"])
                metadata = {
                    "a2ui_action": action["action_id"],
                    "surface_id": action["surface_id"],
                }
            mark_a2ui_action_consumed(
                self.app,
                session_id,
                metadata if isinstance(metadata, dict) else None,
            )

        def settle(item: MailItem = item, update: Callable[[], None] = update) -> None:
            update()
            self.history.display_settled(item)

        async def project(settle: Callable[[], None] = settle) -> None:
            await self.history.io.run(settle)

        try:
            settle()
        except Exception:
            logger.exception("Confirmed input display projection remains pending id=%s", item.id)
            if self._loop is None:
                raise
            self._loop.call_soon_threadsafe(
                self.recovery.defer, "a2ui-consumed:" + item.id, project
            )


def _restore_input_projections(self: Conversations) -> None:
    """Repair display receipts from confirmed inputs, without redelivering mail."""
    _repair_workflow_references(self)
    items = self.history.acknowledged_mail(("a2ui", "question_answer", "human"))
    self._input_committed(StagedInput(items, ()))


_ASSIGNMENT_KINDS = frozenset({"Session", "Subagent"})


def open_work_sessions(history: TaskHistory) -> list[str]:
    """Conversations with open work, oldest first, read from the open records alone.

    Open tasks (``t~``), undelivered mail (``m~``) and owed display repairs (``d~``)
    each name the conversation they belong to; no conversation tree is loaded.
    """
    oldest: dict[str, str] = {}
    for name, row in history.index.open_records():
        session_id = str(row.get("session_id", ""))
        if not session_id or name.startswith("w~"):
            continue
        created = str(row.get("created_at", "") or "~")  # undated records sort last
        if created < oldest.get(session_id, "~~"):
            oldest[session_id] = created
    return sorted(oldest, key=lambda sid: (oldest[sid], sid))


def _open_tasks(history: TaskHistory, session_id: str) -> list[tuple[str, str]]:
    """``(handle, kind)`` of the open tasks one conversation owns, from its ``t~`` records."""
    return [
        (str(row.get("handle", "")), str(row.get("kind", "")))
        for _name, row in history.index.open_records("t~")
        if str(row.get("session_id", "")) == session_id
    ]


def _repair_workflow_references(self: Conversations) -> None:
    """Repair flow checkpoint owner pointers (``w~`` records), which no conversation owns."""
    from clio_agent.flows.journal import repair_run_reference
    from clio_agent.gact.task_index_writes import reconcile_open

    reconcile_open(self.history, prefix="w~")
    for root in self.history.pending_workflow_references():
        try:
            repair_run_reference(self.app, root)
        except ValueError as error:
            logger.exception("Flow checkpoint reference unavailable root=%s", root)
            self.history.workflow_reference_unavailable(root, str(error))
        except (OSError, ClioError, RecordingUnconfirmed) as error:
            logger.exception(
                "Flow checkpoint reference repair pending root=%s error_type=%s",
                root,
                type(error).__name__,
            )

            async def repair(root: str = root) -> None:
                await self.history.io.run(repair_run_reference, self.app, root)

            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self.recovery.defer, "workflow-reference:" + root, repair
                )


async def on_core_ready(self: Conversations) -> None:
    """After Core recovery: finish the restore it interrupted, then reopen admission."""
    if self._restoring:
        await self.restore()
    if self.history.requires_recovery:
        return
    await self.history.io.run(self.app.state.questions.restore)
    await self.app.state.question_events.restore()
    await self.history.io.run(self._restore_input_projections)
    self.signal_pending()


async def restore(self: Conversations) -> None:
    """Recover every conversation with open work, oldest first; then admission opens.

    The obligations lane (:mod:`clio_agent.gact.obligations`) runs the same
    per-conversation recovery in the background after startup; this whole pass serves
    Core recovery and the owner contracts. Dormant assignments do not wake.
    """
    try:
        await self.history.io.run(_repair_workflow_references, self)
        if self.history.requires_recovery:
            return
        for session_id in await self.history.io.run(open_work_sessions, self.history):
            await restore_session(self, session_id)
            if self.history.requires_recovery:
                return  # The existing readiness callback resumes this same restoration.
    except (OSError, ClioError, RecordingUnconfirmed):
        # Reads can fail before any confirmed writer has armed recovery. Keep
        # admission held and enlist the existing readiness callback for both.
        self.history.requires_recovery = True
        logger.exception("Assignment restoration awaits Core recovery")
        return
    self._restoring = False


async def restore_session(self: Conversations, session_id: str) -> None:
    """Recover one conversation's open work, reading only that conversation's records.

    In order: close its stale open records, repair owed displays from committed input,
    project accepted pending feedback, finish posting its Questions, reattach its native
    work and register its external MCP work, then recover its interrupted assignments.
    Admission for the conversation stays held until the caller (the obligations lane)
    releases it.
    """
    from clio_agent.gact.session_store import project_accepted_mail_messages
    from clio_agent.gact.task_index_writes import reconcile_open

    history = self.history

    def records_and_displays() -> None:
        reconcile_open(history, session_id=session_id)
        items = history.acknowledged_mail(("a2ui", "question_answer", "human"), session_id)
        if items:
            self._input_committed(StagedInput(items, ()))
        pending = history.pending_mail(session_id)
        if pending:
            project_accepted_mail_messages(self.app, history, session_id, pending)

    await history.io.run(records_and_displays)
    if history.requires_recovery:
        return
    await history.io.run(self.app.state.questions.restore, session_id)
    await self.app.state.question_events.restore(session_id)
    tasks = await history.io.run(_open_tasks, history, session_id)
    native = getattr(self.app.state, "native_tools", None)
    if native is not None and tasks:
        await native.restore(tuple(handle for handle, _kind in tasks))
    external = getattr(self.app.state, "external_tasks", None)
    if external is not None:
        await external.restore(tuple(handle for handle, kind in tasks if kind == "MCP"))
    for handle, kind in tasks:
        if kind in _ASSIGNMENT_KINDS:
            await _restore_assignment(self, handle)


async def _restore_assignment(self: Conversations, handle: str) -> None:
    """Replay one assignment owner's retained observations, never operations."""
    task = await self.history.io.run(self.history.task_snapshot, handle)
    if task is None or task.kind not in {"Session", "Subagent"}:
        return
    if not self.supports_execution(task):
        return  # Retained unavailable work is not converted into local execution.
    if task.terminal_at is not None:
        # Canonical outcomes can outlive a failed notice write. Historical
        # delivery remains governed by migration; never guess its receipt.
        if task.settlement in {"confirmed", "never_started"} and task.result is not None:
            await self.history.io.run(publish_task_notice, self.history, task)
        return
    if await self.history.io.run(self.history.latest_assignment, task.owner.session_id) != task:
        return
    session = self.sessions.get(task.owner.session_id)
    try:
        details = (
            AssignmentDetails.model_validate(
                await self.history.io.run(self.history.read_content, task.details)
            )
            if task.details is not None
            else AssignmentDetails()
        )
    except ValueError:
        logger.exception("Assignment history unavailable handle=%s", task.handle)
        unavailable_task: TaskSnapshot = task

        def unavailable(task: TaskSnapshot = unavailable_task) -> None:
            with self.history.serialized():
                current = self.history.task_snapshot(task.handle)
                if current is None or current.terminal_at is not None:
                    return
                message = "Retained assignment details are unavailable; inspect the original Core records before execution."
                if current.status == "task_state_uncertain" and current.progress.message == message:
                    return
                observed = current.model_copy(
                    update={
                        "status": "task_state_uncertain",
                        "activity": "unavailable",
                        "progress": TaskProgress(message=message),
                        "attention_episode": current.attention_episode
                        or "assignment-history:" + current.handle,
                        "supported_actions": ("observe", "wait", "get_result"),
                    }
                )
                self.history.record_snapshot(observed, observation_id=uuid4().hex)
                publish_task_notice(
                    self.history, observed, episode=observed.attention_episode or ""
                )

        await self.history.io.run(unavailable)
        return
    self.arm_deadline(handle, details.execution_deadline)
    if task.cancellation_requested_at is not None:
        self.retain_control(
            handle, asyncio.create_task(conversation_controls._cancel_subtree(self, task))
        )
        return
    if (
        task.activity == "paused"
        or session is not None
        and session.metadata.get("agent_paused")
        and session.metadata.get("agent_paused_assignment", task.handle) == task.handle
    ):
        self._paused[task.owner.session_id] = task.handle
        return
    if task.activity not in {"busy", "executing", "stopping", "interrupted", "recovering"}:
        return
    startup_error = await self.history.io.run(
        self.sessions.startup_retirement_error, task.owner.session_id
    )
    if startup_error:
        # Admission never runs this session again: settle with the typed outcome now.
        await self.history.io.run(_startup_unconfirmed, self, task.handle, startup_error)
        return
    if task.activity == "recovering":
        return

    def recover(task: TaskSnapshot | None = task) -> None:
        if task is None:
            raise ValueError("Recovery assignment is unavailable")
        with self.history.serialized():
            details = (
                AssignmentDetails.model_validate(self.history.read_content(task.details))
                if task.details is not None
                else AssignmentDetails()
            )
            from clio_agent.gact.context import scope_epoch

            committed = self.history.latest_input(task.handle, epoch=details.context_epoch)
            boundary = committed.boundary if committed is not None else details.context
            if boundary is not None and scope_epoch(boundary.scope) != details.context_epoch:
                boundary = None
            if boundary is not None:
                self._record_interrupted_calls(task, boundary)
            detail_ref = self.history.write_content(
                task.owner.session_id,
                details.model_copy(update={"context": boundary}).model_dump(mode="json"),
            )
            content = self.history.write_content(
                task.owner.session_id,
                {
                    "text": "The service restarted during this assignment. Continue from the retained Core context and confirmed input. Earlier operations may have affected the environment. Inspect query_tasks and their results/effects before deliberately retrying or revising work. No accepted operation was replayed.",
                    "assignment_handle": task.handle,
                    "assignment": task.description,
                    "confirmed_input": committed.id if committed is not None else None,
                    "context": boundary.model_dump(mode="json") if boundary else None,
                },
            )
            identity = (
                "recovery:"
                + task.handle
                + ":"
                + (committed.id if committed is not None else task.updated_at)
            )
            if self.history.mail_item(task.owner.session_id, identity) is None:
                self.history.record_mail(
                    MailItem(
                        id=identity,
                        recipient_session=task.owner.session_id,
                        kind="control",
                        content=content,
                        handle=task.handle,
                        created_at=now(),
                    )
                )
            self.history.record_snapshot(
                task.model_copy(
                    update={
                        "activity": "recovering",
                        "details": detail_ref,
                        "updated_at": now(),
                    }
                ),
                observation_id=uuid4().hex,
            )

    await self.history.io.run(recover)


def _startup_unconfirmed(self: Conversations, handle: str, message: str) -> None:
    """Settle an assignment whose SessionStart settlement is unconfirmed after a restart.

    The restart interrupted the session's startup after its ``started`` receipt: its
    hook processes and effects are unknown, so the session is never admitted again and
    hooks are never replayed (``SessionStore.startup_retirement_error``). The work is
    still an obligation of this start: it ends as ``task_state_uncertain`` with that
    reason, exactly as a turn that hit the same gate does (``_finish_segment``), and
    the notice reaches its owner, so a waiting parent receives a typed outcome.
    """
    with self.history.serialized():
        current = self.history.task_snapshot(handle)
        if current is None or current.terminal_at is not None:
            return
        if current.status == "task_state_uncertain" and current.progress.message == message:
            return
        observed = current.model_copy(
            update={
                "status": "task_state_uncertain",
                "activity": "yielded",
                "settlement": "unknown",
                "progress": current.progress.model_copy(update={"message": message}),
                "attention_episode": current.attention_episode or uuid4().hex,
                "updated_at": now(),
            }
        )
        self.history.record_snapshot(observed, observation_id=uuid4().hex)
        publish_task_notice(self.history, observed, episode=observed.attention_episode or "")


def _record_interrupted_calls(
    self: Conversations, task: TaskSnapshot, boundary: ContextBoundary
) -> None:
    """Record uncertainty for orphaned call observations without replay or history copying."""
    segments = self.history.arc.context_view(boundary.session_id, boundary.scope).segments
    from clio_agent.arc.context_restore import unanswered_calls

    for offset, (position, call) in enumerate(unanswered_calls(segments)):
        identity = call.content["id"]
        observation = {
            "call_id": identity,
            "text": json.dumps(
                {
                    "error": "operation_outcome_uncertain",
                    "message": "The service restarted before this tool observation was saved. Inspect task handles and effects before deciding whether to retry.",
                    "resubmitted": False,
                }
            ),
            "is_error": True,
        }
        from clio_agent.gact.agents.clio_react_record import _token_estimate

        self.history.confirm_write(
            partial(
                self.history.arc.insert_segment,
                task.owner.session_id,
                boundary.scope,
                position + offset,
                "observation",
                observation,
                token_count=_token_estimate(observation),
                turn_id="recovery:" + task.handle,
            )
        )


logger = logging.getLogger(__name__)


def arm_deadline(self: Conversations, handle: str, deadline: str | None) -> None:
    """Own only an explicit assignment limit; its callback uses ordinary cancellation."""
    previous = self._deadlines.pop(handle, None)
    if previous is not None:
        previous.cancel()
    if deadline is None or self._closing or self._loop is None:
        return
    expires = datetime.fromisoformat(deadline)
    if expires.tzinfo is None:
        raise ValueError("An execution deadline requires a timezone")

    async def expire() -> None:
        def due() -> TaskSnapshot | None:
            with self.history.serialized():
                task = self.history.task_snapshot(handle)
                if task is None or task.terminal_at is not None or task.details is None:
                    return None
                details = AssignmentDetails.model_validate(self.history.read_content(task.details))
                if details.execution_deadline != deadline:
                    return None
                if datetime.now(timezone.utc) < expires:
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(self.arm_deadline, handle, deadline)
                    return None
                if task.cancellation_requested_at is not None:
                    return None
                return task

        task = await self.history.io.run(due)
        if task is not None:
            await self.cancel(TaskCaller(task.owner.session_id, task.owner.human_id), task.handle)

    def elapsed() -> None:
        self._deadlines.pop(handle, None)

        async def enforce() -> None:
            try:
                await expire()
            except Exception as error:
                logger.exception(
                    "Task boundary reason=execution_deadline_recording_unconfirmed error_type=%s",
                    type(error).__name__,
                )
                try:
                    task = await self.history.io.run(self.history.task_snapshot, handle)
                    if task is not None and task.terminal_at is None:
                        await self.cancel(
                            TaskCaller(task.owner.session_id, task.owner.human_id), handle
                        )
                finally:
                    self.recovery.defer("execution-deadline:" + handle, expire)

        self.retain_control("execution-deadline:" + handle, asyncio.create_task(enforce()))

    self._deadlines[handle] = self._loop.call_later(
        max(0.0, (expires - datetime.now(timezone.utc)).total_seconds()), elapsed
    )


async def _confirm_segment_write(history: TaskHistory, action: Callable[[], None]) -> None:
    """Retain a finite Core write through Stop before reconciling segment retirement."""
    recording = asyncio.create_task(history.io.run(action))
    interrupted = False
    while not recording.done():
        try:
            await asyncio.shield(recording)
        except asyncio.CancelledError:
            interrupted = True
    recording.result()
    if interrupted:
        raise asyncio.CancelledError


async def _retired_segment(
    self: Conversations, segment: ConversationSegment, *, stopped: bool
) -> None:
    """Reconcile Stop once, after physical retirement and before slot release."""
    from clio_agent.gact.conversation_assignments import observe_child_lifecycle

    task = self.history.task_snapshot(segment.assignment.handle)
    if task is not None and task.kind == "Subagent" and task.terminal_at is not None:
        await self.history.io.run(observe_child_lifecycle, self, task, started=False)
    if not stopped:
        return
    sid, handle = segment.session_id, segment.assignment.handle
    identity = "stop:" + segment.turn_id
    self._model_recording.setdefault(sid, set()).add(identity)

    def record() -> None:
        with self.history.serialized():
            self.note_startup_interruption(sid)
            current = self.history.task_snapshot(handle)
            latest = self.history.latest_assignment(sid)
            if current is None or latest is None or latest.handle != handle:
                return
            if current.cancellation_requested_at is not None:
                withdraw_assignment_input(self.history, sid, handle)
            elif not self._closing and not self.model_paused(sid):
                pending = self.history.mail_item(sid, segment.trigger.id)
                if (
                    pending == segment.trigger
                    and pending.delivery == "eligible"
                    and pending.id not in self.history.acknowledged_notifications(sid)
                    and not self.history.mail_withdrawn(sid, pending.id)
                ):
                    self.history.update_mail(
                        input_delivery(pending, held=True, reason="stopped_before_input_commit"),
                        expected_revision=pending.revision,
                    )
            if current.terminal_at is None:
                self.history.record_snapshot(
                    current.model_copy(
                        update={
                            "activity": "cancelling"
                            if current.cancellation_requested_at
                            else "interrupted"
                            if self._closing
                            else "paused"
                            if self.model_paused(sid)
                            else "stopped",
                            "updated_at": now(),
                        }
                    ),
                    observation_id=uuid4().hex,
                )

    async def confirm() -> None:
        await _confirm_segment_write(self.history, record)
        self._recording_confirmed(sid, identity)

    try:
        await confirm()
    except Exception:
        logger.exception("Stopped input recording remains unconfirmed session=%s", sid)
        self.recovery.defer("conversation-stopped:" + handle + ":" + segment.turn_id, confirm)
