"""Shared reads, mixed waits and controls over the single Core task representation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from bisect import bisect_right
from collections.abc import Awaitable, Callable, Iterator, Sequence
from heapq import merge
from itertools import islice
from typing import Any, get_args

from clio_agent.arc.task_types import CoordinationRight, TaskKind, TaskSnapshot, TaskSubscription
from clio_agent.gact.native_work import now
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor as _decode
from clio_agent.gact.task_cursors import encode_cursor as _cursor
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.task_observations import pattern_text, read_observations
from clio_agent.gact.task_publication import publish_task_notice, task_owner_recipient
from clio_agent.gact.task_result_content import committed_evidence, read_cursor, result_page
from clio_agent.gact.tool_observation import CollectedTaskResult
from clio_agent.runtime.commitment_activity import track

CancelOwner = Callable[[TaskSnapshot, TaskCaller], Awaitable[dict[str, Any]]]


logger = logging.getLogger(__name__)


def _ordered_after(
    ordering: Sequence[tuple[str, str]], after: tuple[str, str] | None
) -> Iterator[tuple[str, str]]:
    """Seek an immutable ordering without copying or walking its preceding history."""
    start = bisect_right(ordering, after) if after is not None else 0
    for index in range(start, len(ordering)):
        yield ordering[index]


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

    def query(
        self,
        caller: TaskCaller,
        kind: str | None = None,
        status: str | None = None,
        handle: str | None = None,
        tool: str | None = None,
        agent: str | None = None,
        active_only: bool = False,
        cursor: str | None = None,
        limit: int = 50,
        scope: str = "tree",
        query: str | None = None,
    ) -> dict[str, Any]:
        """Return authorized filtered snapshots; pages never disclose hidden counts."""
        if limit < 1:
            raise ValueError("limit must be positive")
        access = TaskAccess(self.history, caller)
        query = query.strip() or None if query is not None else None
        filters = {
            "kind": kind,
            "status": status,
            "handle": handle,
            "tool": tool,
            "agent": agent,
            "active_only": active_only,
            "scope": scope,
            "query": query,
            "recipient": caller.session_id,
            "human": caller.human_id,
            "human_action": caller.human_action,
        }
        signature = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
        position = _decode(cursor)
        if position and position.get("filter") != signature:
            raise ValueError("Task cursor belongs to a different query")
        after = position.get("after", [])
        if (
            not isinstance(after, list)
            or len(after) not in {0, 2}
            or any(not isinstance(value, str) for value in after)
        ):
            raise ValueError("Task query cursor ordering is malformed")
        canonical_kind = next(
            (
                value
                for value in get_args(TaskKind)
                if kind is not None and value.casefold() == kind.casefold()
            ),
            kind or "",
        )
        with self.history.read_view():
            orders = access.query_orders(scope, kind=canonical_kind, active_only=active_only)
            total: int | None = (
                len(orders[0])
                if len(orders) == 1
                and status is None
                and tool is None
                and agent is None
                and query is None
                else None
            )
            if handle is not None:
                resolved = self.history.resolve_handle(handle)
                task = self.history.task_snapshot(resolved) if resolved is not None else None
                orders = (((task.created_at, task.handle),),) if task is not None else ((),)
                total = None
            anchor = (after[0], after[1]) if after else None
            streams = (_ordered_after(ordering, anchor) for ordering in orders)
            scan_limit = max(200, limit * 4)
            # Copy only this bounded candidate page while the orderings are
            # protected. Transcript hydration must not hold the writer's lock.
            candidates = list(islice(merge(*streams), scan_limit + 1))
        page, previous, last = [], None, None
        last_readable, scanned = False, 0
        for key in candidates[:scan_limit]:
            scanned += 1
            access = TaskAccess(self.history, caller)
            if key == previous:
                continue
            previous = key
            current = self.history.task_snapshot(key[1])
            if (
                current is None
                or not access.in_scope(current, scope)
                or not {"read", "observe"}.intersection(access.rights(current))
            ):
                if handle is not None:
                    continue
                raise ValueError("Task query visibility changed; restart the query")
            last = key
            last_readable = "read" in access.rights(current)
            if (
                (kind is not None and current.kind.casefold() != kind.casefold())
                or (status is not None and current.status != status)
                or (tool is not None and current.tool != tool)
                or (
                    agent is not None
                    and current.owner.session_id != agent
                    and current.owner.assignment_id != agent
                )
                or (active_only and current.terminal_at is not None)
                or (query is not None and not self._matches_text(caller, current, query))
            ):
                matched = False
            else:
                matched = True
                page.append(current)
            if handle is not None:
                total = int(matched)
            if len(page) >= limit:
                break
        exhausted = scanned == len(candidates)
        next_cursor = (
            _cursor({"filter": signature, "after": list(last)})
            if not exhausted and last is not None
            else None
        )
        if handle is not None and total is None:
            total = 0
        access = TaskAccess(self.history, caller)
        for task in page:
            if not ({"read"} if query is not None else {"read", "observe"}).intersection(
                access.rights(task)
            ):
                raise TaskAccessDenied("Task query visibility changed; restart the query")
        value = {
            "tasks": [access.view(task) for task in page],
            "total": total,
            "cursor": next_cursor,
            "revision": self.history.revision,
        }
        return CollectedTaskResult(
            value,
            (),
            (last[1],) if last is not None and last_readable else (),
            observe_handles=(last[1],) if last is not None and not last_readable else (),
            collect=False,
        )

    def _matches_text(self, caller: TaskCaller, task: TaskSnapshot, query: str) -> bool:
        """Search only readable task content through the existing conversation source."""
        if self.app is None or "read" not in TaskAccess(self.history, caller).rights(task):
            return False
        from clio_agent.gact.task_transcripts import matches_task_text

        return matches_task_text(self.app, caller, task, query)

    def result(self, caller: TaskCaller, handle: str, cursor: str | None = None) -> dict[str, Any]:
        """Read immutable result content outside the writer lock, then recheck disclosure."""
        value = self._read_result(caller, handle, cursor)
        if value.get("result_kind") == "shared_evidence":
            pin = committed_evidence(self.history, caller, value["handle"])
            if value.get("result_reference") != pin.result.model_dump(mode="json"):
                raise TaskAccessDenied("The exact delegated result is unavailable")
        else:
            TaskAccess(self.history, caller).require(value["handle"], "read")
        return value

    def _read_result(self, caller: TaskCaller, handle: str, cursor: str | None) -> dict[str, Any]:
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
                    self.history, caller, pin.handle, pin.result, "shared_evidence", position
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
                self.history, caller, current.handle, current.result, "task_result", position
            )
        )
        return result

    async def wait(
        self,
        caller: TaskCaller,
        tasks: str | list[str],
        return_when: str = "all",
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Await mixed owner observations; timeout/cancellation never cancels a member."""
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
                    outcomes, pending = await self.history.io.run(self._outcomes, caller, handles)
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
                    try:
                        await asyncio.wait_for(changed.wait(), remaining)
                    except TimeoutError:
                        outcomes, pending = await self.history.io.run(
                            self._outcomes, caller, handles
                        )
                        return _wait_value(outcomes, pending, timed_out=True)
            finally:
                remove()

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
        self, caller: TaskCaller, handles: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        outcomes: list[dict[str, Any]] = []
        pending: list[str] = []
        checks: dict[int, tuple[str, CoordinationRight]] = {}
        for handle in handles:
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
                elif current.terminal_at is None:
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
