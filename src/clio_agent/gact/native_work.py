"""Trusted admission and physical-cleanup observations for native owned workers.

Docket invokes these wrappers. They do not enqueue, poll, retry, or acknowledge
agent delivery. Durable state lives exclusively in the common Core history.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from clio_agent.arc.task_types import (
    BackendReference,
    NativeAttempt,
    SubmissionContext,
    TaskSnapshot,
    argument_digest,
)
from clio_agent.gact import context as gact_context
from clio_agent.gact.core_recovery import CoreRecovery
from clio_agent.gact.task_history import TaskHistory
from clio_agent.tools.execution import tool_workspace_context

SUBMISSION_ARGUMENT = "_clio_submission"
SubmissionAuthorizer = Callable[[SubmissionContext], None]
CleanupObserver = Callable[[str], Awaitable[None]]


logger = logging.getLogger(__name__)


def now() -> str:
    """Produce an observation timestamp without treating elapsed time as settlement."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkEvidence:
    """Attempt-local physical evidence supplied by the actual workload algorithm."""

    acquired: bool = False
    cleaned: bool = False
    local_retirement: dict[str, Any] | None = None

    def acquiring_effects(self) -> None:
        """Mark the boundary before effect acquisition, including uncertain spawn."""
        self.acquired = True

    def confirm_cleanup(self) -> None:
        """Record the workload's proof after process/provider and required cleanup settle."""
        self.cleaned = True

    def retire_provider_observer(self, details: dict[str, Any]) -> None:
        """Record joined local observation, without claiming remote cleanup."""
        self.local_retirement = {**details, "kind": "provider_observer", "local_work_joined": True}


class NativeWork:
    """One native root's validated worker boundary, with no execution registry."""

    def __init__(
        self,
        history: TaskHistory,
        backend: BackendReference,
        authorize: SubmissionAuthorizer,
        cleanup_observer: CleanupObserver,
        recovery: CoreRecovery,
        admission: Callable[[TaskSnapshot], None],
    ) -> None:
        self.history, self.backend = history, backend
        self.authorize, self.cleanup_observer = authorize, cleanup_observer
        self.recovery, self.admission = recovery, admission
        # Workload metadata only; the native owner alone establishes runtime loss.
        self.runtime_loss_observer: Callable[[TaskSnapshot], None] | None = None

    async def run(
        self,
        reference: str,
        tool: str,
        arguments: Mapping[str, object],
        body: Callable[[SubmissionContext, WorkEvidence], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Validate the queued invocation, then retain its cleanup across cancellation."""
        from clio_agent.tools.task_client import native_execution_identity

        task = native_execution_identity()
        backend = self.backend.model_copy(
            update={"task_id": task.task_id, "auth_scope": task.auth_scope}
        )
        attempt = NativeAttempt(
            handle="",  # resolved from the confirmed opaque reference, never a model argument
            incarnation=uuid4().hex,
            backend=backend,
            leg=task.leg,
            execution_key=task.execution_key,
            started_at=now(),
        )
        # Admission itself must finish before cancellation can release the wrapper.
        admission = asyncio.create_task(
            self.history.io.run(self._admit, reference, tool, arguments, attempt)
        )
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                submission, admitted = await asyncio.shield(admission)
                break
            except asyncio.CancelledError as error:
                cancelled = error
                if admission.cancelled():
                    raise

        evidence = WorkEvidence()

        async def execute() -> dict[str, Any]:
            from clio_agent.tools.write_scope import tool_write_root_context  # noqa: PLC0415

            token = gact_context.set_session_id(submission.owner.session_id)
            # An isolated workflow node's shell starts in, and writes only, its folder.
            node = str(submission.confinement.get("write_root") or "")
            try:
                with tool_workspace_context(submission.workspace), tool_write_root_context(node):
                    return await body(submission, evidence)
            finally:
                gact_context.reset(token)

        operation = asyncio.create_task(execute()) if cancelled is None else None
        outcome = "never_started" if operation is None else "completed"
        try:
            if operation is None:
                assert cancelled is not None
                raise cancelled
            while True:
                try:
                    result = await asyncio.shield(operation)
                    if cancelled is not None:
                        raise cancelled
                    return result
                except asyncio.CancelledError as error:
                    cancelled = error
                    outcome = "cancelled"
                    if operation.done():
                        raise
                    # Cancel the body once. Repeated native cancellation must not
                    # interrupt its process-tree/readers cleanup a second time.
                    if operation.cancelling() == 0:
                        operation.cancel()
        except Exception:
            outcome = "failed"
            raise
        finally:

            async def finish() -> None:
                async def record() -> None:
                    await self.history.io.run(self._finished, admitted, outcome, evidence)
                    await self.cleanup_observer(admitted.handle)

                try:
                    await record()
                except Exception as error:
                    logger.exception(
                        "Task boundary reason=native_cleanup_recording_unconfirmed error_type=%s",
                        type(error).__name__,
                    )
                    self.recovery.defer("native-cleanup:" + admitted.incarnation, record)
                    return

            finishing = asyncio.create_task(finish())
            while True:
                try:
                    await asyncio.shield(finishing)
                    break
                except asyncio.CancelledError:
                    if finishing.cancelled():
                        raise

    def _admit(
        self,
        reference: str,
        tool: str,
        arguments: Mapping[str, object],
        attempt: NativeAttempt,
    ) -> tuple[SubmissionContext, NativeAttempt]:
        with self.history.serialized():
            submission = self.history.submission_context(reference)
            if (
                submission.tool != tool
                or submission.argument_digest != argument_digest(arguments)
                or submission.backend != self.backend
                or attempt.backend.auth_scope != self.backend.auth_scope
            ):
                raise ValueError("Native invocation differs from confirmed custody")
            current = self.history.task_snapshot(submission.handle)
            if current is None or current.submission is None:
                raise ValueError("Task acceptance was not confirmed")
            if SubmissionContext.model_validate(self.history.read_content(current.submission)) != (
                submission
            ):
                raise ValueError("Task context does not match the worker reference")
            if current.backend_observation is not None and current.backend == attempt.backend:
                observed = self.history.read_content(current.backend_observation)
                if isinstance(observed, dict) and observed.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    raise ValueError("A terminal native execution leg cannot enter again")
            continuation = submission.provider_continuation
            cleanup_leg = continuation is not None and submission.tool == "connected_data_continue"
            if cleanup_leg:
                if current.terminal_at is not None:
                    raise ValueError("Historical provider work cannot execute again")
                # This host-only body must still enter when cancellation closes
                # normal admission: its only permitted effects then are control
                # and cleanup of the verified pre-existing provider job.
            else:
                self._check_admission(current)
            self.authorize(submission)
            bound = current.model_copy(
                update={
                    "backend": attempt.backend,
                    "status": "running",
                    "activity": "executing",
                    "settlement": "cleanup_pending",
                    "updated_at": now(),
                }
            )
            self.history.record_snapshot(bound, observation_id=uuid4().hex)
            admitted = attempt.model_copy(update={"handle": current.handle})
            self.history.record_attempt(admitted)
            return submission, admitted

    def _check_admission(self, current: TaskSnapshot) -> None:
        if current.terminal_at is not None:
            raise ValueError("Historical task cannot execute again")
        self.admission(current)

    def _finished(self, attempt: NativeAttempt, outcome: str, evidence: WorkEvidence) -> None:
        with self.history.serialized():
            current = self.history.task_snapshot(attempt.handle)
            if current is None:
                raise ValueError("Native task disappeared before cleanup recording")
            previous = next(
                (
                    row
                    for row in self.history.native_attempts(attempt.handle)
                    if row.incarnation == attempt.incarnation
                ),
                None,
            )
            if previous is not None and previous.cleanup_state in {"confirmed", "never_started"}:
                return
            state = (
                "never_started"
                if not evidence.acquired
                else "confirmed"
                if evidence.cleaned
                else "unknown"
            )
            receipt = self.history.write_content(
                current.owner.session_id,
                {
                    "incarnation": attempt.incarnation,
                    "execution_key": attempt.execution_key,
                    "outcome": outcome,
                    "physical_cleanup": state,
                    "local_retirement": evidence.local_retirement,
                },
            )
            self.history.record_attempt(
                attempt.model_copy(
                    update={
                        "cleanup_receipt": receipt,
                        "finished_at": now(),
                        "cleanup_state": state,
                    }
                )
            )
