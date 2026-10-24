"""Authorization over Core hierarchy and human-created session relationships."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property

from clio_agent.arc.task_types import CoordinationGrant, CoordinationRight, TaskSnapshot
from clio_agent.gact.task_history import TaskHistory

ALL_RIGHTS: frozenset[CoordinationRight] = frozenset({"read", "observe", "message", "control"})


class AdmissionClosed(ValueError):
    """A new descendant was refused because its lineage is stopping or out of time."""

    def __init__(self, message: str, *, stopping: bool) -> None:
        super().__init__(message)
        self.stopping = stopping


class TaskAccessDenied(ValueError):
    """An unavailable task cannot be disclosed or controlled by this caller."""


@dataclass(frozen=True)
class TaskCaller:
    """Identity supplied by the authenticated request/agent boundary, never tool arguments."""

    session_id: str
    human_id: str
    human_action: bool = False


class TaskAccess:
    """Resolve rights before lookup disclosure, filtering, pagination or injection."""

    def __init__(self, history: TaskHistory, caller: TaskCaller) -> None:
        self.history, self.caller = history, caller

    @cached_property
    def _grants(self) -> tuple[CoordinationGrant, ...]:
        return self.history.coordination_grants(self.caller.session_id)

    def _in_session_tree(self, task: TaskSnapshot, session_id: str) -> bool:
        """Check immutable lineage from the target, without loading retained siblings."""
        seen: set[str] = set()
        current: TaskSnapshot | None = task
        while current is not None and current.handle not in seen:
            if current.owner.session_id == session_id:
                return True
            seen.add(current.handle)
            parent = current.owner.parent_handle
            current = self.history.task_snapshot(parent) if parent is not None else None
        return False

    def _matches(self, grant: CoordinationGrant, task: TaskSnapshot) -> bool:
        if grant.revoked_at is not None:
            return False
        if grant.target_handle is not None:
            return task.handle == grant.target_handle
        if grant.target_scope == "tree":
            return self._in_session_tree(task, grant.target_session)
        return task.owner.session_id == grant.target_session

    def in_scope(self, task: TaskSnapshot, scope: str) -> bool:
        """Scope a narrow indexed candidate without turning it into a whole-tree query."""
        if scope not in {"tree", "authorized"}:
            raise ValueError("Task scope must be tree or authorized")
        if self.caller.human_action and not self.caller.session_id and scope == "authorized":
            return task.owner.human_id == self.caller.human_id
        if self._in_session_tree(task, self.caller.session_id):
            return True
        return (
            scope == "authorized"
            and not self.history.requires_recovery
            and any(
                {"read", "observe"}.intersection(grant.rights) and self._matches(grant, task)
                for grant in self._grants
            )
        )

    def rights(self, task: TaskSnapshot) -> frozenset[CoordinationRight]:
        """Keep control, messaging, observation and content rights independent."""
        with self.history.read_view():
            if self.caller.human_action and task.owner.human_id == self.caller.human_id:
                return ALL_RIGHTS
            if task.owner.human_id == self.caller.human_id and self._in_session_tree(
                task, self.caller.session_id
            ):
                return ALL_RIGHTS
            if self.history.requires_recovery:
                return frozenset()
            rights: set[CoordinationRight] = set()
            for grant in self._grants:
                if self._matches(grant, task):
                    rights.update(grant.rights)
            return frozenset(rights)

    def require(self, handle: str, right: CoordinationRight) -> TaskSnapshot:
        """An absent, ambiguous or unauthorized handle yields the same public error."""
        resolved = self.history.resolve_handle(handle)
        current = self.history.task_snapshot(resolved) if resolved is not None else None
        if current is None or right not in self.rights(current):
            raise TaskAccessDenied("Task is unavailable for this operation")
        return current

    def stable_message_right(self, task: TaskSnapshot) -> bool:
        """Exact historical grants do not silently grant access to a successor assignment."""
        if task.owner.human_id == self.caller.human_id and self._in_session_tree(
            task, self.caller.session_id
        ):
            return True
        if self.caller.human_action and self.caller.human_id == task.owner.human_id:
            return True
        if self.history.requires_recovery:
            return False
        for grant in self._grants:
            if (
                grant.revoked_at is not None
                or grant.target_handle is not None
                or "message" not in grant.rights
            ):
                continue
            if grant.target_session == task.owner.session_id:
                return True
            if grant.target_scope == "tree" and self._in_session_tree(task, grant.target_session):
                return True
        return False

    def session_message_right(self, session_id: str) -> bool:
        """Authorize a stable address even before it has its first finite assignment."""
        return session_id in self.session_candidates("message")

    def session_candidates(self, right: CoordinationRight) -> set[str]:
        """Read stable-session rights without broadening exact assignment grants."""
        sessions = {self.caller.session_id} if self.caller.session_id else set()
        sessions.update(
            self.history.session_tree_ids(self.caller.session_id, human_id=self.caller.human_id)
        )
        if self.history.requires_recovery:
            return sessions
        for grant in self._grants:
            if (
                grant.revoked_at is not None
                or grant.target_handle is not None
                or right not in grant.rights
            ):
                continue
            sessions.add(grant.target_session)
            if grant.target_scope == "tree":
                sessions.update(self.history.session_tree_ids(grant.target_session))
        return sessions

    def query_orders(
        self, scope: str, *, kind: str = "", active_only: bool = False
    ) -> tuple[Sequence[tuple[str, str]], ...]:
        """Select authorized scope streams before examining retained task candidates."""
        if scope not in {"tree", "authorized"}:
            raise ValueError("Task scope must be tree or authorized")
        if self.caller.human_action and not self.caller.session_id and scope == "authorized":
            return (
                self.history.task_order(
                    "human", self.caller.human_id, kind=kind, active_only=active_only
                ),
            )
        orders = [
            self.history.task_order(
                "tree",
                self.caller.session_id,
                human_id=self.caller.human_id,
                kind=kind,
                active_only=active_only,
            )
        ]
        if scope == "authorized" and not self.history.requires_recovery:
            for grant in self._grants:
                if grant.revoked_at is not None or not {"read", "observe"}.intersection(
                    grant.rights
                ):
                    continue
                if grant.target_handle is not None:
                    task = self.history.task_snapshot(grant.target_handle)
                    if (
                        task is not None
                        and (not kind or task.kind == kind)
                        and (not active_only or task.terminal_at is None)
                    ):
                        orders.append(((task.created_at, task.handle),))
                else:
                    orders.append(
                        self.history.task_order(
                            grant.target_scope,
                            grant.target_session,
                            kind=kind,
                            active_only=active_only,
                        )
                    )
        return tuple(orders)

    def view(self, task: TaskSnapshot) -> dict[str, object]:
        """Observe-only grants expose safe counts/status, never descriptions or output."""
        rights = self.rights(task)
        if self.history.requires_recovery:
            task = task.model_copy(update={"recording": "unconfirmed"})
        if "read" in rights:
            result = task.model_dump(mode="json")
            required = {
                "observe": "observe",
                "wait": "observe",
                "get_result": "read",
                "cancel": "control",
                "pause": "control",
                "resume": "control",
                "restart": "control",
                "message": "message",
                "answer": "control",
            }
            result["supported_actions"] = [
                action
                for action in task.supported_actions
                if required.get(action, "read") in rights
            ]
            return result
        if "observe" not in rights:
            raise ValueError("Task is unavailable for this operation")
        return {
            "handle": task.handle,
            "kind": task.kind,
            "status": task.status,
            "raw_status": task.raw_status,
            "activity": task.activity,
            "progress": {
                "completed": task.progress.completed,
                "total": task.progress.total,
                "unit": task.progress.unit,
            },
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "terminal_at": task.terminal_at,
            "settlement": task.settlement,
            "recording": task.recording,
            "connection_freshness": task.connection_freshness,
            "supported_actions": ["observe", "wait"],
        }
