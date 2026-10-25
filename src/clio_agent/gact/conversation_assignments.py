"""Conversation-owner assignments algorithms; no independent state or worker."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from clio_agent.arc.task_types import (
    AssignmentDetails,
    AssignmentEvidence,
    BackendReference,
    ContextBoundary,
    CoordinationGrant,
    DeferredCompletion,
    MailItem,
    TaskOwner,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact.fleet.child_model import warm_model_catalog
from clio_agent.gact.fleet.guard import guard_spending
from clio_agent.gact.native_work import now
from clio_agent.gact.session_input import input_delivery
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_publication import publish_task_notice

if TYPE_CHECKING:
    from clio_agent.gact.conversations import (
        Conversations,
    )


@dataclass(frozen=True)
class ConversationSegment:
    """One execution segment of an existing finite assignment."""

    session_id: str
    assignment: TaskSnapshot
    turn_id: str
    trigger: MailItem
    text: str
    context_epoch: str = ""
    context_epoch_after: str | None = None


@dataclass(frozen=True)
class ConversationOutcome:
    """The conversation program's result, separate from assignment disposition."""

    outputs: dict[str, Any]
    disposition: str | None = None
    context: ContextBoundary | None = None
    error: dict[str, Any] | None = None
    input_hold: str | None = None


def note_startup_interruption(self: Conversations, sid: str) -> None:
    """Retain settled-but-uncertain startup effects in the ordinary Core input lane."""
    session = self.sessions.get(sid)
    if session is None or session.creation_hook_state != "interrupted":
        return
    with self.history.serialized(writing=True):
        identity = "startup-interrupted:" + sid
        current = self.history.latest_assignment(sid)
        if current is None:
            raise ValueError("Interrupted startup has no owning assignment")
        original = self.history.mail_item(sid, identity)
        if (
            original is not None
            and original.handle != current.handle
            and self.history.mail_withdrawn(sid, identity)
        ):
            identity += ":" + current.handle
        if self.history.mail_item(sid, identity) is not None:
            return
        message = (
            "SessionStart observation hooks were interrupted. Their owned process work has settled, "
            "but external effects may have occurred. Hooks were not replayed. Inspect the retained "
            "startup evidence and environment before deliberately retrying or revising that work."
        )
        content = (
            original.content
            if original is not None
            else self.history.write_content(
                sid,
                {
                    "message": message,
                    "session_id": sid,
                    "hook_state": "interrupted",
                    "effects": "unconfirmed",
                    "cleanup": "confirmed",
                },
            )
        )
        if current.terminal_at is None:
            observed = current
            if current.progress.message != message:
                observed = current.model_copy(
                    update={
                        "progress": current.progress.model_copy(update={"message": message}),
                        "updated_at": now(),
                    }
                )
                self.history.record_snapshot(observed, observation_id=uuid4().hex)
            publish_task_notice(self.history, observed, episode=identity, attention_content=content)
        # Last confirmed write: a retry must not mistake a partially published
        # owner notice for a fully prepared input. The content is address-stable.
        self.history.record_mail(
            MailItem(
                id=identity,
                recipient_session=sid,
                kind="hook",
                content=content,
                created_at=now(),
                handle=current.handle,
                episode=identity,
            )
        )


def clear_model_context(self: Conversations, sid: str, question: str, boundary: str) -> None:
    """Confirm one exact Question epoch without erasing history or replaying accepted work."""
    with self.history.serialized():
        current = self.history.latest_assignment(sid)
        if current is None:
            raise ValueError("Plan context owner is unavailable")
        details = (
            AssignmentDetails.model_validate(self.history.read_content(current.details))
            if current.details
            else AssignmentDetails()
        )
        if details.context_epoch == question:
            return
        self.runner.request_stop(sid)  # close the old segment before any uncertain epoch write
        reference = self.history.write_content(
            sid,
            details.model_copy(
                update={
                    "context_epoch": question,
                    "context_epoch_after": boundary,
                    "context": None,
                    "last_output": None,
                }
            ).model_dump(mode="json"),
        )
        self.history.record_snapshot(
            current.model_copy(update={"details": reference}), observation_id=uuid4().hex
        )


def observe_child_lifecycle(self: Conversations, task: TaskSnapshot, *, started: bool) -> None:
    """Publish retained observation hooks from the conversation owner's transitions."""
    if task.kind != "Subagent":
        return
    from clio_agent.gact.hooks.dispatcher import (
        dispatch_subagent_start,
        dispatch_subagent_stop,
        get_global_dispatcher,
    )

    if get_global_dispatcher() is None:
        return

    session = self.sessions.get(task.owner.session_id)
    workspace = self.app.state.workspaces.get(session.workspace_id) if session is not None else None
    dispatch = dispatch_subagent_start if started else dispatch_subagent_stop
    dispatch(
        session_id=task.owner.session_id,
        cwd=workspace.root_path if workspace is not None else "",
        payload={"task_id": task.handle, "status": task.status},
    )


def _new_assignment(
    self: Conversations,
    sid: str,
    description: str | None,
    human_id: str,
    *,
    predecessor: TaskSnapshot | None = None,
    parent: TaskSnapshot | None = None,
    invocation_id: str,
    handle: str | None = None,
    initial_details: AssignmentDetails | None = None,
) -> TaskSnapshot:
    startup_error = self.sessions.startup_retirement_error(sid)
    if startup_error:
        raise ValueError(startup_error)
    if predecessor is not None and predecessor.terminal_at is None:
        raise ValueError("An active assignment must settle before a successor is created")
    if predecessor is not None and parent is None and predecessor.owner.parent_handle:
        parent = self.history.task_snapshot(predecessor.owner.parent_handle)
        if parent is not None:
            successor_parent = self.history.latest_assignment(parent.owner.session_id)
            if successor_parent is not None:
                parent = successor_parent
    if parent is not None:
        self.require_admission(parent)
    handle, stamp = handle or "task_" + uuid4().hex, now()
    previous_details = (
        AssignmentDetails.model_validate(self.history.read_content(predecessor.details))
        if predecessor is not None and predecessor.details is not None
        else None
    )
    parent_input = self.history.latest_input(parent.handle) if parent is not None else None
    details = self.history.write_content(
        sid,
        (initial_details or AssignmentDetails())
        .model_copy(
            update={
                "predecessor": predecessor.handle if predecessor is not None else None,
                "context": previous_details.context if previous_details is not None else None,
                "context_epoch": previous_details.context_epoch if previous_details else "",
                "context_epoch_after": previous_details.context_epoch_after
                if previous_details
                else None,
                "input_evidence": previous_details.input_evidence
                if previous_details is not None
                else (initial_details.input_evidence if initial_details is not None else ()),
                "evidence_assignment": previous_details.evidence_assignment
                if previous_details is not None
                else initial_details.evidence_assignment
                if initial_details is not None
                else None,
                "parent_turn_id": parent_input.turn_id
                if parent_input is not None
                else previous_details.parent_turn_id
                if previous_details is not None
                else None,
            }
        )
        .model_dump(mode="json"),
    )
    session = self.sessions.get(sid)
    if session is None:
        raise ValueError("Assignment session is unavailable")
    task = TaskSnapshot(
        handle=handle,
        kind="Subagent" if parent is not None else "Session",
        owner=TaskOwner(
            human_id=human_id,
            session_id=sid,
            assignment_id=handle,
            parent_handle=parent.handle if parent is not None else None,
        ),
        label=session.title,
        description=description,
        description_missing=description is None,
        invocation_id=invocation_id,
        tool="conversation",
        created_at=stamp,
        updated_at=stamp,
        backend=BackendReference(owner="conversation", server="clio", session=sid, task_id=handle),
        raw_status="active",
        status="active",
        activity="queued",
        details=details,
        resource_id=predecessor.resource_id if predecessor is not None else None,
        supported_actions=(
            "observe",
            "wait",
            "message",
            "pause",
            "resume",
            "cancel",
            "restart",
        ),
    )
    self.history.record_snapshot(task, observation_id=uuid4().hex)
    if self._loop is not None:
        self._loop.call_soon_threadsafe(
            self.arm_deadline,
            task.handle,
            initial_details.execution_deadline if initial_details else None,
        )
    return task


def assignment_outputs(history: Any, details: AssignmentDetails, outputs: Any) -> Any:
    """A turn's outputs, retaining earlier tool calls and public response parts.

    An assignment can span turns: a child that yields on a question resumes once it is
    answered. An editor's proposals and authored evidence from before that yield
    still reach the caller. The final value remains the completing turn's answer.
    """

    if not details.last_output or not isinstance(outputs, dict):
        return outputs
    earlier = history.read_content(details.last_output)
    if not isinstance(earlier, dict):
        return outputs
    merged = dict(outputs)
    if calls := earlier.get("tools_called"):
        merged["tools_called"] = [*calls, *(outputs.get("tools_called") or [])]
    if parts := earlier.get("response_parts"):
        from clio_agent.gact.assignment_response import merge_response_parts

        merged["response_parts"] = merge_response_parts(parts, outputs.get("response_parts") or [])
    return merged if merged != outputs else outputs


def _finish_segment(
    self: Conversations, segment: ConversationSegment, outcome: ConversationOutcome
) -> None:
    with self.history.serialized():
        current = self.history.task_snapshot(segment.assignment.handle)
        if current is not None and current.terminal_at is not None:
            publish_task_notice(self.history, current)
            return
        if current is None or current.cancellation_requested_at is not None:
            return
        pending = [
            self.history.task_snapshot(handle)
            for handle in self.history.tree_handles(segment.session_id, active_only=True)
            if handle != current.handle
        ]
        details = (
            AssignmentDetails.model_validate(self.history.read_content(current.details))
            if current.details
            else AssignmentDetails()
        )
        if segment.context_epoch != details.context_epoch:
            # Model execution has returned. A late Core retry must not change
            # a newer reservation or restore output into the cleared epoch.
            reservation = self.runner.handle(segment.session_id)
            if reservation is None or reservation.turn_id == segment.turn_id:
                self.history.record_snapshot(
                    current.model_copy(update={"activity": "yielded", "updated_at": now()}),
                    observation_id=uuid4().hex,
                )
            return
        if details.execution_deadline is not None and datetime.now(
            timezone.utc
        ) >= datetime.fromisoformat(details.execution_deadline):
            if self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self.arm_deadline, current.handle, details.execution_deadline
                )
            return
        # A veto/setup failure has not consumed its trigger. Keep that exact
        # revision pending, without letting ordinary wake signals retry it.
        # Early approval/retry may already have changed or withdrawn the input.
        reason = outcome.input_hold or (
            "execution_failed_before_input_commit" if outcome.error else ""
        )
        attempted = self.history.mail_item(segment.session_id, segment.trigger.id)
        if (
            reason
            and attempted == segment.trigger
            and attempted.delivery == "eligible"
            and attempted.id not in self.history.acknowledged_notifications(segment.session_id)
            and not self.history.mail_withdrawn(segment.session_id, attempted.id)
        ):
            self.history.update_mail(
                input_delivery(attempted, held=True, reason=reason),
                expected_revision=attempted.revision,
            )
        dependencies = [self.history.task_snapshot(handle) for handle in details.dependencies]
        session = self.sessions.get(segment.session_id)
        if session is None:
            raise ValueError("Assignment session is no longer available")
        has_work = (
            any(task is not None and task.terminal_at is None for task in pending)
            or any(task is None or task.terminal_at is None for task in dependencies)
            or bool(session.metadata.get("execution_gate"))
            or any(
                item.delivery == "eligible" and self.eligible(segment.session_id, item)
                for item in self.history.pending_mail(segment.session_id)
            )
        )
        gated = bool(session.metadata.get("execution_gate")) or any(
            task is not None
            and task.kind == "Question"
            and task.tool in {"hook_defer", "plan_exit"}
            and task.owner.assignment_id == current.handle
            and task.terminal_at is None
            for task in pending
        )
        startup_error = self.sessions.startup_retirement_error(segment.session_id)
        disposition = (
            "yield"
            if gated or startup_error
            else outcome.disposition or ("yield" if has_work else "complete")
        )
        if disposition not in {"yield", "complete"}:
            raise ValueError("Invalid assignment disposition")
        # Empty presentation on a wake is not an execution failure while the
        # assignment still owns pending work. Retain the diagnostic in last_output;
        # genuine provider failures and explicit completion retain their outcome.
        presentation_only = (
            has_work
            and outcome.disposition != "complete"
            and outcome.error is not None
            and outcome.error.get("error") == "empty_response"
            and outcome.error.get("recoverable") is True
        )
        execution_failed = outcome.error is not None and not presentation_only
        output = self.history.write_content(
            segment.session_id,
            outcome.error or assignment_outputs(self.history, details, outcome.outputs),
        )
        detail_ref = self.history.write_content(
            segment.session_id,
            details.model_copy(
                update={
                    "context": outcome.context or details.context,
                    "disposition": disposition,
                    "last_output": output,
                }
            ).model_dump(mode="json"),
        )
        stamp = now()
        updates: dict[str, Any] = {
            "activity": "yielded",
            "updated_at": stamp,
            "details": detail_ref,
        }
        if startup_error:
            updates.update(
                status="task_state_uncertain",
                raw_status="failed" if execution_failed else current.raw_status,
                settlement="unknown",
                progress=current.progress.model_copy(update={"message": startup_error}),
                attention_episode=current.attention_episode or uuid4().hex,
            )
        elif not gated and (execution_failed or disposition == "complete"):
            updates.update(
                status="failed" if execution_failed else "completed",
                raw_status="failed" if execution_failed else "completed",
                activity="",
                settlement="confirmed",
                terminal_at=stamp,
                result=output,
                supported_actions=("get_result", "message", "restart"),
            )
        observed = current.model_copy(update=updates)
        self.history.record_snapshot(observed, observation_id=uuid4().hex)
        if observed.terminal_at is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self.arm_deadline, observed.handle, None)
        publish_task_notice(
            self.history,
            observed,
            episode=observed.attention_episode or "" if startup_error else "",
        )


async def complete_deferred(
    self: Conversations,
    sid: str,
    assignment_handle: str,
    *,
    context_epoch: str,
    completion: DeferredCompletion,
) -> None:
    """Accept a trusted Stop-hook approval after the model worker actually retires."""
    released, loop = asyncio.Event(), asyncio.get_running_loop()
    self.runner.run_when_released(sid, lambda: loop.call_soon_threadsafe(released.set))
    await released.wait()

    def complete() -> None:
        with self.history.serialized():
            current = self.history.task_snapshot(assignment_handle)
            if current is None or current.owner.session_id != sid:
                raise ValueError("Deferred completion no longer addresses this assignment")
            if current.terminal_at is not None:
                publish_task_notice(self.history, current)
                return
            if self.history.latest_assignment(sid) != current:
                raise ValueError("Deferred completion no longer addresses this assignment")
            if current.cancellation_requested_at is not None:
                return
            details = (
                AssignmentDetails.model_validate(self.history.read_content(current.details))
                if current.details
                else AssignmentDetails()
            )
            if details.context_epoch != context_epoch:
                raise ValueError("Deferred completion context changed before worker retirement")
            if (
                completion.result.session_id != sid
                or completion.context is not None
                and completion.context.session_id != sid
            ):
                raise ValueError("Deferred completion belongs to another session")
            self.history.read_content(completion.result)
            if details.execution_deadline and datetime.now(timezone.utc) >= datetime.fromisoformat(
                details.execution_deadline
            ):
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(
                        self.arm_deadline, current.handle, details.execution_deadline
                    )
                return
            reference = self.history.write_content(
                sid,
                details.model_copy(
                    update={
                        "disposition": "complete",
                        "last_output": completion.result,
                        "context": completion.context or details.context,
                    }
                ).model_dump(mode="json"),
            )
            stamp = now()
            settled = current.model_copy(
                update={
                    "status": "failed" if completion.failed else "completed",
                    "raw_status": "failed" if completion.failed else "completed",
                    "activity": "",
                    "terminal_at": stamp,
                    "updated_at": stamp,
                    "settlement": "confirmed",
                    "result": completion.result,
                    "details": reference,
                    "supported_actions": ("get_result", "message", "restart"),
                }
            )
            self.history.record_snapshot(settled, observation_id=uuid4().hex)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self.arm_deadline, settled.handle, None)
            publish_task_notice(self.history, settled)

    await self.history.io.run(complete)


async def spawn(
    self: Conversations,
    caller: TaskCaller,
    assignment: str,
    agent_id: str,
    invocation_id: str,
    *,
    workflow_id: str | None = None,
    workflow_step: str | None = None,
    execution_deadline: str | None = None,
    inference_concurrency: int | None = None,
    launch_context: dict[str, Any] | None = None,
    target_scope: dict[str, Any] | None = None,
    strategy: dict[str, Any] | None = None,
    parent_handle: str | None = None,
    session_metadata: dict[str, Any] | None = None,
    approval_profile: str | None = None,
    resource_id: str | None = None,
    start: bool = True,
    model: str = "",
) -> dict[str, Any]:
    """Persist the original assignment before enrichment or child inference starts.

    ``model`` names the model the child runs on (``account/model`` or a bare id);
    empty lets the child's preferences, then the caller's model, decide.
    """
    if not assignment.strip():
        raise ValueError("Subagent assignment cannot be empty")
    from clio_agent.gact.assignment_evidence import validate_assignment_evidence

    raw_evidence = (launch_context or {}).get("input_task_refs", [])
    if not isinstance(raw_evidence, list):
        raise ValueError("Delegation evidence must be an immutable reference list")
    pins = tuple(AssignmentEvidence.model_validate(value) for value in raw_evidence)
    await self.history.io.run(validate_assignment_evidence, self.history, caller, pins)
    normalized_strategy = None
    if strategy is not None:
        from clio_agent.gact.agents.module_variants import strategy_module

        try:
            normalized_strategy = strategy_module(strategy, agent_id=agent_id)
        except ValueError as error:
            return {"accepted": False, "error": str(error), "reason": "invalid_strategy"}
    handle = (
        "task_" + hashlib.sha256((caller.session_id + "\0" + invocation_id).encode()).hexdigest()
    )

    def preflight() -> str | None:
        with self.history.serialized():
            if self.history.task_snapshot(handle) is not None:
                return None  # Accepted identities never require target rediscovery.
            parent = (
                self.history.task_snapshot(parent_handle)
                if parent_handle
                else self.history.latest_assignment(caller.session_id)
            )
            if (
                parent is None
                or parent.terminal_at is not None
                or parent.owner.session_id != caller.session_id
                or parent.owner.human_id != caller.human_id
            ):
                raise ValueError("Spawning requires an active owning assignment")
            self.require_admission(parent)
            validate_assignment_evidence(self.history, caller, pins)
            return parent.handle

    prepared_parent = await self.history.io.run(preflight)
    from clio_agent.gact.off_loop import join_off_loop

    if prepared_parent is not None:
        await warm_model_catalog(self.app)

    prepared_child = (
        await join_off_loop(self.prepare_child, caller.session_id, agent_id, target_scope, model)
        if prepared_parent is not None
        else None
    )
    if prepared_child is not None and (
        refused := await guard_spending(
            self.app, caller.session_id, dict(getattr(prepared_child, "model_choice", {}) or {})
        )
    ):
        return refused

    def create() -> TaskSnapshot:
        with self.history.serialized(writing=True):
            validate_assignment_evidence(self.history, caller, pins)
            existing = self.history.task_snapshot(handle)
            if existing is not None:
                session = self.sessions.get(existing.owner.session_id)
                if (
                    existing.description != assignment
                    or existing.owner.human_id != caller.human_id
                    or session is None
                    or session.metadata.get("spawn_target", session.agent.get("id")) != agent_id
                    or session.metadata.get("subagent_name", "")
                    != str((session_metadata or {}).get("subagent_name") or "").strip()
                ):
                    raise ValueError("Spawn invocation was reused with different work or name")
                if session.metadata.get("variant_strategy") != normalized_strategy:
                    raise ValueError("Spawn invocation was reused with a different strategy")
                if existing.details is None:
                    raise ValueError("Accepted assignment details are unavailable")
                previous = AssignmentDetails.model_validate(
                    self.history.read_content(existing.details)
                )
                if previous.input_evidence != pins:
                    raise ValueError("Spawn invocation was reused with different input evidence")
                if existing.terminal_at is not None:
                    return existing
            parent = (
                self.history.task_snapshot(parent_handle)
                if parent_handle is not None
                else self.history.latest_assignment(caller.session_id)
            )
            if (
                parent is None
                or parent.terminal_at is not None
                or parent.owner.session_id != caller.session_id
                or parent.owner.human_id != caller.human_id
            ):
                raise ValueError("Spawning requires an active owning assignment")
            terminal_parents = {
                closed_parent
                for closed_parent, _ in self._closed_spawn_invocations
                if (closed_task := self.history.task_snapshot(closed_parent)) is None
                or closed_task.terminal_at is not None
            }
            if terminal_parents:
                self._closed_spawn_invocations.difference_update(
                    {
                        (closed_parent, closed_invocation)
                        for closed_parent, closed_invocation in self._closed_spawn_invocations
                        if closed_parent in terminal_parents
                    }
                )
            if (
                existing is None
                and (parent.handle, invocation_id) in self._closed_spawn_invocations
            ):
                raise ValueError("Spawn invocation was closed before acceptance")
            self.require_admission(parent)
            if prepared_parent is not None and parent.handle != prepared_parent:
                raise ValueError("Owning assignment changed during child preparation")
            creation_digest = argument_digest(
                {
                    "parent": parent.handle,
                    "human": caller.human_id,
                    "assignment": assignment,
                    "agent": agent_id,
                    "scope": target_scope,
                    "strategy": normalized_strategy,
                    "launch_context": launch_context,
                    "workflow": workflow_id,
                    "step": workflow_step,
                    "deadline": execution_deadline,
                    "concurrency": inference_concurrency,
                    "metadata": session_metadata,
                    "approval_profile": approval_profile,
                    "resource": resource_id,
                    **({"model": model} if model else {}),
                }
            )
            initial_metadata = dict(session_metadata or {})
            if normalized_strategy is not None:
                initial_metadata["variant_strategy"] = normalized_strategy
            if workflow_id is not None:
                initial_metadata.update(workflow_id=workflow_id, workflow_step=workflow_step)
            child = (
                session
                if existing is not None
                else prepared_child(
                    handle,
                    creation_digest,
                    initial_metadata,
                    approval_profile,
                )
                if prepared_child is not None
                else None
            )
            if child is None:
                raise ValueError("Accepted child session is no longer available")
            if child.creation_digest and child.creation_digest != creation_digest:
                raise ValueError("Session creation identity was reused with different work")
            if approval_profile is not None and child.approval_profile != approval_profile:
                raise ValueError("Accepted child approval profile changed")
            if any(child.metadata.get(key) != value for key, value in initial_metadata.items()):
                raise ValueError("Accepted child execution configuration changed")
            task = existing or self._new_assignment(
                child.id,
                assignment,
                parent.owner.human_id,
                parent=parent,
                invocation_id=invocation_id,
                handle=handle,
                initial_details=AssignmentDetails(
                    workflow_id=workflow_id,
                    workflow_step=workflow_step,
                    execution_deadline=execution_deadline,
                    inference_concurrency=inference_concurrency,
                    input_evidence=pins,
                    evidence_assignment=handle if pins else None,
                ),
            )
            if resource_id is not None:
                if task.resource_id is not None and task.resource_id != resource_id:
                    raise ValueError("Accepted child resource identity changed")
                if task.resource_id is None:
                    task = task.model_copy(update={"resource_id": resource_id})
                    self.history.record_snapshot(task, observation_id=uuid4().hex)
            # Trusted delegation configuration grants only the return-message
            # channel. It conveys no ancestor read/observe/control rights.
            grant_id = "delegation-reply:" + task.handle
            if not any(
                grant.id == grant_id for grant in self.history.coordination_grants(child.id)
            ):
                self.history.record_grant(
                    CoordinationGrant(
                        id=grant_id,
                        granting_human=parent.owner.human_id,
                        recipient_session=child.id,
                        target_session=caller.session_id,
                        rights=("message",),
                        created_at=task.created_at,
                    ),
                    observation_id=uuid4().hex,
                )
            if not start:
                if task.activity == "queued":
                    task = task.model_copy(update={"activity": "idle", "updated_at": now()})
                    self.history.record_snapshot(task, observation_id=uuid4().hex)
                return task
            content = self.history.write_content(
                child.id,
                {
                    "assignment": assignment,
                    "parent_handle": parent.handle,
                    "parent_session": caller.session_id,
                    **(
                        {"strategy": normalized_strategy} if normalized_strategy is not None else {}
                    ),
                    **({"launch_context": launch_context} if launch_context is not None else {}),
                },
            )
            mail_id = "assignment:" + task.handle
            if self.history.mail_item(child.id, mail_id) is None:
                self.history.record_mail(
                    MailItem(
                        id=mail_id,
                        recipient_session=child.id,
                        sender_session=caller.session_id,
                        kind="delegation",
                        content=content,
                        created_at=task.created_at,
                        handle=task.handle,
                    )
                )
            elif self.history.require_mail(child.id, mail_id).content != content:
                raise ValueError("Accepted child launch context changed")
            return task

    task = await self.history.io.run(create)
    return {
        "handle": task.handle,
        "kind": "Subagent",
        "accepted": True,
        "session_id": task.owner.session_id,
        "assignment": assignment,
    }
