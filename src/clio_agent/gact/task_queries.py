"""Authorized bounded task queries over indexed execution lineage."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from heapq import merge
from itertools import islice
from typing import TYPE_CHECKING, Any, get_args

from clio_agent.arc.task_types import TaskKind, TaskSnapshot
from clio_agent.gact.task_access import TaskAccess, TaskAccessDenied, TaskCaller
from clio_agent.gact.task_cursors import decode_cursor as _decode
from clio_agent.gact.task_cursors import encode_cursor as _cursor
from clio_agent.gact.task_history import TaskHistory
from clio_agent.gact.tool_observation import CollectedTaskResult

if TYPE_CHECKING:
    from clio_agent.gact.task_operations import TaskOperations


def _ordered_after(
    ordering: Sequence[tuple[str, str]], after: tuple[str, str] | None
) -> Iterator[tuple[str, str]]:
    """Seek an immutable ordering without copying or walking its preceding history."""
    start = bisect_right(ordering, after) if after is not None else 0
    for index in range(start, len(ordering)):
        yield ordering[index]


def _direct_child(history: TaskHistory, task: TaskSnapshot, session_id: str) -> bool:
    """Match one assignment edge, including children owned by another session."""
    parent = history.task_snapshot(task.owner.parent_handle) if task.owner.parent_handle else None
    return bool(
        parent is not None
        and parent.kind in {"Session", "Subagent"}
        and parent.owner.session_id == session_id
    )


def query_tasks(
    self: TaskOperations,
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
    message_after: str | None = None,
    message_before: str | None = None,
    relation: str = "tree",
) -> dict[str, Any]:
    """Return authorized filtered snapshots; pages never disclose hidden counts."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if relation not in {"tree", "direct"}:
        raise ValueError("Task relation must be tree or direct")
    if relation == "direct" and (scope != "tree" or not caller.session_id):
        raise ValueError("Direct work requires one session's tree scope")
    access = TaskAccess(self.history, caller)
    query = query.strip() or None if query is not None else None
    if query is not None and scope == "authorized" and not caller.human_action:
        self._check_past_session_search(caller)
    start = end = None
    if message_after is not None or message_before is not None:
        from clio_agent.gact.task_transcripts import message_search_window

        start, end = message_search_window(message_after, message_before)
    content_filter = query is not None or start is not None or end is not None
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
    if relation == "direct":
        filters["relation"] = relation
    if start is not None or end is not None:
        filters.update(
            message_after=start.isoformat() if start is not None else None,
            message_before=end.isoformat() if end is not None else None,
        )
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
        orders = (
            (
                self.history.task_order(
                    "direct",
                    caller.session_id,
                    human_id=caller.human_id,
                    kind=canonical_kind,
                    active_only=active_only,
                ),
            )
            if relation == "direct"
            else access.query_orders(scope, kind=canonical_kind, active_only=active_only)
        )
        total: int | None = (
            len(orders[0])
            if len(orders) == 1
            and status is None
            and tool is None
            and agent is None
            and not content_filter
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
            (relation == "direct" and not _direct_child(self.history, current, caller.session_id))
            or (kind is not None and current.kind.casefold() != kind.casefold())
            or (status is not None and current.status != status)
            or (tool is not None and current.tool != tool)
            or (
                agent is not None
                and current.owner.session_id != agent
                and current.owner.assignment_id != agent
            )
            or (active_only and current.terminal_at is not None)
            or (
                content_filter
                and not self._matches_text(
                    caller, current, query, message_after=start, message_before=end
                )
            )
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
        if not ({"read"} if content_filter else {"read", "observe"}).intersection(
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
