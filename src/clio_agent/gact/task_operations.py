"""Shared reads, mixed waits and controls over the single Core task representation."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from clio_agent.arc.task_types import CoordinationRight, TaskSnapshot, TaskSubscription
from clio_agent.gact.assignment_input import (
    AWAITING_INPUT,
    STOPPED,
    awaiting_input_outcome,
    waiting_for_input,
)
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor as _decode
from clio_agent.gact.task_cursors import encode_cursor as _cursor
from clio_agent.gact.task_forks import family_members
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_observations import pattern_text, read_observations
from clio_agent.gact.task_publication import publish_task_notice, task_owner_recipient
from clio_agent.gact.task_queries import query_tasks
from clio_agent.gact.task_result_content import committed_evidence, read_cursor, result_page
from clio_agent.gact.task_result_spill import save_task_result
from clio_agent.gact.tool_observation import CollectedTaskResult
from clio_agent.runtime.commitment_activity import track

CancelOwner = Callable[[TaskSnapshot, TaskCaller], Awaitable[dict[str, Any]]]


logger = logging.getLogger(__name__)


def _handles(tasks: str | list[str]) -> tuple[str, ...]:
    values = [tasks] if isinstance(tasks, str) else tasks
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str) or not item for item in values)
    ):
        raise ValueError("tasks must be a handle or nonempty handle list")
    return tuple(dict.fromkeys(values))


def _timeout(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or value < 0):
        raise ValueError("timeout_s must be finite and nonnegative")
    return value


def _wait_value(
    outcomes: list[dict[str, Any]], pending: list[str], *, timed_out: bool, interrupted: str = ""
) -> CollectedTaskResult:
    """Carry each disclosed wait outcome's permission to recipient-input commitment."""
    rows = [row for row in outcomes if row.get("handle") and not row.get("error")]
    reads = {row["handle"] for row in rows if row.get("pending") is False}
    observations = {row["handle"] for row in rows} | set(pending)
    value: dict[str, Any] = {"outcomes": outcomes, "pending": pending, "timed_out": timed_out}
    if interrupted:
        value["interrupted"] = interrupted
    return CollectedTaskResult(
        value,
        (),
        tuple(sorted(reads)),
        observe_handles=tuple(sorted(observations - reads)),
    )


class TaskOperations:
    """Common controls delegate execution; no task is driven or scheduled here."""

    def __init__(
        self,
        history: TaskHistory,
        cancel_owner: CancelOwner,
        *,
        refresh_owner: Callable[[TaskSnapshot], None] | None = None,
        app: Any = None,
    ) -> None:
        self.history, self.cancel_owner = history, cancel_owner
        self.refresh_owner = refresh_owner
        self.app = app

    query = query_tasks

    def _check_past_session_search(self, caller: TaskCaller) -> None:
        """Text search across other conversations follows the past-session switches."""
        if self.app is None or not caller.session_id:
            return
        from clio_agent.gact.memory.policy import (  # noqa: PLC0415
            PastSessionSearchWithheld,
            past_session_search_reason,
        )

        if reason := past_session_search_reason(self.app, caller.session_id):
            raise PastSessionSearchWithheld(reason)

    def _matches_text(
        self,
        caller: TaskCaller,
        task: TaskSnapshot,
        query: str | None,
        *,
        message_after: datetime | None = None,
        message_before: datetime | None = None,
    ) -> bool:
        """Search only readable task content through the existing conversation source."""
        if self.app is None or "read" not in TaskAccess(self.history, caller).rights(task):
            return False
        from clio_agent.gact.task_transcripts import matches_task_text

        return matches_task_text(
            self.app,
            caller,
            task,
            query,
            message_after=message_after,
            message_before=message_before,
        )

    def result(
        self,
        caller: TaskCaller,
        handle: str,
        cursor: str | None = None,
        select: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read immutable result content outside the writer lock, then recheck disclosure."""
        value = self._read_result(caller, handle, cursor, select)
        if value.get("result_kind") == "shared_evidence":
            pin = committed_evidence(self.history, caller, value["handle"])
            if value.get("result_reference") != pin.result.model_dump(mode="json"):
                raise TaskAccessDenied("The exact delegated result is unavailable")
        else:
            TaskAccess(self.history, caller).require(value["handle"], "read")
        return value

    def _read_result(
        self, caller: TaskCaller, handle: str, cursor: str | None, select: list[str] | None
    ) -> dict[str, Any]:
        """Capture a retained reference and read its bounded page without serializing writers."""
        canonical = self.history.resolve_handle(handle) or handle
        position = read_cursor(cursor, caller, canonical) if cursor is not None else {}
        current = None
        if position.get("mode") != "shared_evidence":
            try:
                current = TaskAccess(self.history, caller).require(handle, "read")
            except TaskAccessDenied:
                if position:
                    raise  # A task-result continuation cannot silently change read mode.
        if current is None:
            pin = committed_evidence(self.history, caller, canonical)
            return {
                "result_kind": "shared_evidence",
                "handle": pin.handle,
                "status": pin.status,
                "pending": False,
                "source_owner": pin.owner.model_dump(mode="json"),
                "terminal_at": pin.terminal_at,
                "description": pin.description,
                "description_missing": pin.description_missing,
                "partial": pin.partial,
                **result_page(
                    self.history,
                    caller,
                    pin.handle,
                    pin.result,
                    "shared_evidence",
                    position,
                    select,
                    self._saver(caller, pin.handle),
                ),
            }
        result: dict[str, Any] = {
            "handle": current.handle,
            "status": current.status,
            "pending": current.terminal_at is None,
            "settlement": current.settlement,
        }
        if current.status in {"failed", "task_state_uncertain"}:
            result["reason"] = current.progress.message
        if current.terminal_at is None:
            if current.backend_observation is not None and current.raw_status in {
                "completed",
                "failed",
                "cancelled",
                "inline",
            }:
                result.update(
                    result_page(
                        self.history,
                        caller,
                        current.handle,
                        current.backend_observation,
                        "task_result",
                        position,
                        select,
                        self._saver(caller, current.handle),
                    )
                )
                result.update(
                    result_kind="backend_evidence",
                    raw_status=current.raw_status,
                    evidence_is_final=False,
                )
            return result
        if current.result is None:
            result["result"] = None
            return result
        result.update(
            result_page(
                self.history,
                caller,
                current.handle,
                current.result,
                "task_result",
                position,
                select,
                self._saver(caller, current.handle),
            )
        )
        return result

    def _saver(
        self, caller: TaskCaller, handle: str
    ) -> Callable[[Any, str], dict[str, Any]] | None:
        """Save a result too big to return whole into the caller's session (#1844)."""
        if self.app is None:
            return None
        app = self.app
        return lambda value, encoded: save_task_result(
            app, caller.session_id, handle, value, encoded
        )

    async def wait(
        self,
        caller: TaskCaller,
        tasks: str | list[str],
        return_when: str = "all",
        timeout_s: float | None = None,
        *,
        settle_awaiting_input: bool = False,
        interrupt_on_mail: bool = False,
    ) -> dict[str, Any]:
        """Await mixed owner observations; timeout/cancellation never cancels a member.

        ``settle_awaiting_input`` (a flow's wait) settles a child that yielded, or whose
        turn stopped, with nothing left to wake it as ``awaiting_input`` or ``stopped``
        instead of waiting on it forever (:mod:`clio_agent.gact.assignment_input`).
        ``interrupt_on_mail`` returns a truthful pending snapshot when authorized
        coordination/input needs the caller. It never settles or consumes a task.
        """
        with track(timeout_s != 0, session_id=caller.session_id):
            if return_when not in {"all", "any"}:
                raise ValueError("return_when must be all or any")
            handles, timeout = (
                await self.history.io.run(self._canonical_observed, caller, _handles(tasks)),
                _timeout(timeout_s),
            )
            deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
            changed, loop = asyncio.Event(), asyncio.get_running_loop()
            # An agent's wait ends when its own conversation is stopped (the waited
            # tasks keep running): Stop retires the turn only once this call returns.
            # A cancellation is different: it cascades to the waited members, and the
            # caller (a flow above all) keeps waiting to observe them settle.
            own = (
                None
                if caller.human_action
                else await self.history.io.run(self.history.latest_assignment, caller.session_id)
            )

            def wake(_revision: int) -> None:
                loop.call_soon_threadsafe(changed.set)

            watched = handles + ((own.handle,) if own is not None else ())
            remove = self.history.listen(wake, handles=watched, recipient_session=caller.session_id)
            try:
                while True:
                    changed.clear()
                    outcomes, pending = await self.history.io.run(
                        self._outcomes, caller, handles, settle_awaiting_input, wake
                    )
                    for handle in pending:
                        # A cancellation can end unconfirmed with no further history change.
                        if (job := self._cancellation_job(handle)) is not None:
                            job.get_loop().call_soon_threadsafe(
                                job.add_done_callback, lambda _job: wake(0)
                            )
                    if own is not None and await self.history.io.run(self._stopping, own.handle):
                        return _wait_value(
                            outcomes, pending, timed_out=False, interrupted="stopped"
                        )
                    uncertain = any(
                        item.get("status") == "task_state_uncertain" for item in outcomes
                    )
                    if not pending or uncertain or (return_when == "any" and outcomes):
                        return _wait_value(outcomes, pending, timed_out=False)
                    remaining = None if deadline is None else max(0.0, deadline - loop.time())
                    if remaining == 0:
                        return _wait_value(outcomes, pending, timed_out=True)
                    if interrupt_on_mail and await self.history.io.run(
                        self._has_interrupting_input, caller
                    ):
                        return _wait_value(
                            outcomes, pending, timed_out=False, interrupted="mail_received"
                        )
                    try:
                        await asyncio.wait_for(changed.wait(), remaining)
                    except TimeoutError:
                        outcomes, pending = await self.history.io.run(
                            self._outcomes, caller, handles, settle_awaiting_input
                        )
                        return _wait_value(outcomes, pending, timed_out=True)
            finally:
                remove()

    def _has_interrupting_input(self, caller: TaskCaller) -> bool:
        """Use the sole mailbox's authorization and delivery policy for addressed input."""
        owner = getattr(getattr(self.app, "state", None), "conversations", None)
        return bool(owner is not None and owner.mail.interrupting_input(caller))

    def _stopping(self, handle: str) -> bool:
        """Whether the calling assignment was stopped (not cancelled) or has ended.

        A cancellation unwinds through ``stopping`` too, before its request is recorded.
        """
        current = self.history.task_snapshot(handle)
        if current is None:
            return False
        if current.terminal_at is not None:
            return True
        conversations = getattr(getattr(self.app, "state", None), "conversations", None)
        cancelling = (
            conversations.cancelling(current)
            if conversations is not None
            else current.cancellation_requested_at is not None
        )
        return current.activity == "stopping" and not cancelling

    def _cancellation_settling(self, current: TaskSnapshot) -> bool:
        """A cancellation still being confirmed is progress, not an uncertain state.

        Its job marks a member uncertain while descendants settle; the wait goes on
        until the job confirms the cancellation or records it as unconfirmed.
        """
        return self._cancellation_job(current.handle) is not None

    def _cancellation_job(self, handle: str) -> asyncio.Task[None] | None:
        conversations = getattr(getattr(self.app, "state", None), "conversations", None)
        return conversations.cancellation_job(handle) if conversations is not None else None

    def _canonical_observed(self, caller: TaskCaller, handles: tuple[str, ...]) -> tuple[str, ...]:
        """Authorize aliases before registering for canonical owner observations."""
        with self.history.serialized():
            access = TaskAccess(self.history, caller)
            resolved: list[str] = []
            for handle in handles:
                try:
                    resolved.append(access.require(handle, "observe").handle)
                except ValueError:
                    resolved.append(handle)  # Preserve an independent, non-disclosing outcome.
            return tuple(dict.fromkeys(resolved))

    def _outcomes(
        self,
        caller: TaskCaller,
        handles: tuple[str, ...],
        settle_awaiting_input: bool = False,
        wake: Callable[[int], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        outcomes: list[dict[str, Any]] = []
        pending: list[str] = []
        checks: dict[int, tuple[str, CoordinationRight]] = {}
        # Waiting on a handle waits on its forks too (task_forks): read again on every
        # pass, since a fork may be created while the wait is open.
        for handle in family_members(self.history, handles):
            access = TaskAccess(self.history, caller)
            try:
                current = access.require(handle, "observe")
                if (
                    not caller.human_action
                    and current.kind in {"Session", "Subagent"}
                    and current.owner.session_id == caller.session_id
                    and current.terminal_at is None
                ):
                    outcomes.append(
                        {"handle": current.handle, "error": "cannot_wait_for_own_active_assignment"}
                    )
                elif current.terminal_at is None and (
                    waiting := waiting_for_input(self.app, current) if settle_awaiting_input else ""
                ) in {AWAITING_INPUT, STOPPED}:
                    outcomes.append(awaiting_input_outcome(self.app, current, waiting))
                elif current.terminal_at is None:
                    if waiting == "retiring" and wake is not None:
                        # No history change follows the release: look again once it lands.
                        sid = current.owner.session_id
                        self.app.state.conversations.runner.run_when_released(sid, lambda: wake(0))
                    pending.append(handle)
                    if (
                        current.status == "task_state_uncertain"
                        or current.attention_episode is not None
                    ) and not self._cancellation_settling(current):
                        checks[len(outcomes)] = (handle, "observe")
                        outcomes.append({"handle": handle, "status": "task_state_uncertain"})
                elif "read" in access.rights(current):
                    value = self.result(caller, handle)
                    checks[len(outcomes)] = (handle, "read")
                    outcomes.append(value)
                else:
                    checks[len(outcomes)] = (handle, "observe")
                    outcomes.append(access.view(current))
            except TaskAccessDenied:
                outcomes.append({"handle": handle, "error": "task_unavailable"})
        # Another member's bounded Core read may have overlapped revocation.
        with self.history.serialized():
            access = TaskAccess(self.history, caller)
            for index, (handle, right) in checks.items():
                try:
                    access.require(handle, right)
                except TaskAccessDenied:
                    outcomes[index] = {"handle": handle, "error": "task_unavailable"}
                    pending = [item for item in pending if item != handle]
            for handle in tuple(pending):
                try:
                    access.require(handle, "observe")
                except TaskAccessDenied:
                    pending.remove(handle)
                    if not any(
                        row.get("handle") == handle and row.get("error") for row in outcomes
                    ):
                        outcomes.append({"handle": handle, "error": "task_unavailable"})
            for row in outcomes:
                member = self.history.task_snapshot(str(row.get("handle") or ""))
                if member is not None and member.fork_of and member.handle not in handles:
                    row["fork_of"] = member.fork_of
        return outcomes, pending

    async def observe(
        self,
        caller: TaskCaller,
        tasks: str | list[str],
        cursor: str | None = None,
        pattern: str | None = None,
        timeout_s: float | None = None,
        notify: bool | None = None,
        *,
        row_limit: int | None = None,
        include_state: bool = False,
        view: str = "progress",
        history: str = "assignment",
        limit: int = 50,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Read available changes immediately, or explicitly await output/settlement."""
        if view != "progress":
            if self.app is None or pattern is not None or notify is not None:
                raise ValueError(
                    "Context views are immediate reads; use progress for patterns/subscriptions"
                )
            from clio_agent.gact.off_loop import join_off_loop
            from clio_agent.gact.task_transcripts import read_task_context

            return await join_off_loop(
                read_task_context,
                self.app,
                caller,
                _handles(tasks),
                view,
                history,
                cursor,
                limit,
                artifact_id,
            )
        with track(pattern is not None and timeout_s != 0, session_id=caller.session_id):
            handles = await self.history.io.run(self._canonical_observed, caller, _handles(tasks))
            timeout, previous = _timeout(timeout_s), _decode(cursor)
            search_start = _cursor(previous) if previous else None
            scanned_pages = 0
            try:
                expression = re.compile(pattern) if pattern is not None else None
            except re.error as error:
                raise ValueError("Observation pattern is invalid") from error
            deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
            changed, loop = asyncio.Event(), asyncio.get_running_loop()

            def wake(revision: int) -> None:
                loop.call_soon_threadsafe(changed.set)

            remove = self.history.listen(wake, handles=handles, recipient_session=caller.session_id)
            try:
                initial: dict[str, Any] | None = await self.history.io.run(
                    self._observations,
                    caller,
                    handles,
                    previous,
                    row_limit,
                    include_state,
                    expression,
                )
                if self.refresh_owner is not None:
                    for handle in handles:
                        try:
                            task = await self.history.io.run(
                                TaskAccess(self.history, caller).require, handle, "observe"
                            )
                        except ValueError:
                            continue
                        if task.terminal_at is None and task.status == "task_state_uncertain":
                            self.refresh_owner(task)
                subscription_errors = []
                if notify is not None:
                    subscription_errors = await self.history.io.run(
                        self._subscribe, caller, handles, notify
                    )
                while True:
                    if initial is not None:
                        result, initial = initial, None
                    else:
                        changed.clear()
                        result = await self.history.io.run(
                            self._observations,
                            caller,
                            handles,
                            previous,
                            row_limit,
                            include_state,
                            expression,
                        )
                    if expression is not None:
                        result.update(search_start_cursor=search_start, scanned_pages=scanned_pages)
                    if subscription_errors:
                        result["subscription_errors"] = subscription_errors
                    if (
                        expression is None
                        or result["any_settled"]
                        or any(
                            "pattern_match" in chunk
                            for row in result["tasks"]
                            for chunk in row.get("output", [])
                        )
                        or any(expression.search(pattern_text(row)) for row in result["tasks"])
                    ):
                        return result
                    remaining = None if deadline is None else max(0.0, deadline - loop.time())
                    if remaining == 0:
                        return {
                            **result,
                            "search_start_cursor": search_start,
                            "scanned_pages": scanned_pages,
                            "timed_out": True,
                        }
                    previous = _decode(result["cursor"])
                    if result["has_more"]:
                        scanned_pages += 1
                        await asyncio.sleep(0)  # Cancellation remains responsive while paging.
                        continue
                    try:
                        await asyncio.wait_for(changed.wait(), remaining)
                    except TimeoutError:
                        return {
                            **await self.history.io.run(
                                self._observations,
                                caller,
                                handles,
                                previous,
                                row_limit,
                                include_state,
                                expression,
                            ),
                            **(
                                {"subscription_errors": subscription_errors}
                                if subscription_errors
                                else {}
                            ),
                            "search_start_cursor": search_start,
                            "scanned_pages": scanned_pages,
                            "timed_out": True,
                        }
            finally:
                remove()

    def _observations(
        self,
        caller: TaskCaller,
        handles: tuple[str, ...],
        previous: dict[str, Any],
        row_limit: int | None = None,
        include_state: bool = False,
        pattern: re.Pattern[str] | None = None,
    ) -> dict[str, Any]:
        return read_observations(
            self.history, caller, handles, previous, row_limit, include_state, pattern
        )

    def _subscribe(
        self, caller: TaskCaller, handles: tuple[str, ...], enabled: bool
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        access = TaskAccess(self.history, caller)
        with self.history.serialized():
            for handle in handles:
                try:
                    current = access.require(handle, "observe")
                    access.require(handle, "read")
                except ValueError:
                    errors.append({"handle": handle, "error": "task_unavailable"})
                    continue
                previous = next(
                    (
                        subscription
                        for subscription in self.history.subscriptions(current.handle)
                        if subscription.recipient_session == caller.session_id
                    ),
                    None,
                )
                if previous is not None and previous.recipient_human not in {None, caller.human_id}:
                    errors.append({"handle": handle, "error": "task_unavailable"})
                    continue
                if (
                    previous is not None
                    and previous.enabled == enabled
                    and previous.recipient_human == caller.human_id
                ):
                    continue
                generation = previous.generation if previous is not None else 1
                if enabled and previous is not None and not previous.enabled:
                    generation += 1
                self.history.record_subscription(
                    TaskSubscription(
                        handle=current.handle,
                        recipient_session=caller.session_id,
                        recipient_human=caller.human_id,
                        enabled=enabled,
                        updated_at=now(),
                        generation=generation,
                    )
                )
                if enabled:
                    publish_task_notice(self.history, current)
                elif task_owner_recipient(self.history, current) != caller.session_id:
                    for item in self.history.pending_mail(caller.session_id):
                        if item.handle == current.handle and item.kind in {
                            "task_result",
                            "attention",
                            "question_answer",
                        }:
                            self.history.withdraw_mail(caller.session_id, item.id)
        return errors

    async def cancel(self, caller: TaskCaller, tasks: str | list[str]) -> dict[str, Any]:
        """Authorize and independently request each task's owner to cancel it."""
        outcomes = []
        for handle in _handles(tasks):
            try:
                current = TaskAccess(self.history, caller).require(handle, "control")
                outcomes.append(await self.cancel_owner(current, caller))
            except Exception as error:
                logger.exception(
                    "Task boundary reason=task_member_control_failed error_type=%s",
                    type(error).__name__,
                )
                outcomes.append({"handle": handle, "error": str(error)})
        return {"outcomes": outcomes}
