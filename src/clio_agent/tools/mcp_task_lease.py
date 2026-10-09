"""Exclusive task-driver leases shared by the existing durable record store."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from clio_agent.errors import MCP_TASK_LEASE_HELD, ToolError

if TYPE_CHECKING:
    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore

#: A dead driver's claim becomes reclaimable after this unchanged TTL.
DEFAULT_LEASE_SECONDS = 300.0

_LEASE_LOCKS_GUARD = threading.Lock()
_LEASE_LOCKS: dict[tuple[str, str | None, str], threading.Lock] = {}


def _lease_lock(key: TaskKey) -> threading.Lock:
    """The process-local lock serializing compare-and-set on one task's lease."""

    index = (key.server_id, key.session_id, key.task_id)
    with _LEASE_LOCKS_GUARD:
        lock = _LEASE_LOCKS.get(index)
        if lock is None:
            lock = threading.Lock()
            _LEASE_LOCKS[index] = lock
        return lock


def _new_owner_id() -> str:
    """A per-driver owner token."""

    return f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:8]}"


class TaskLease:
    """An exclusive, expiring claim on driving one task.

    Acquiring is a compare-and-set on the persisted record, serialized by a
    process-local lock so two drivers in this process cannot both read "free" before
    either writes. A second driver of the SAME task is refused with the typed reason
    ``mcp_task_lease_held`` instead of silently double-polling and double-answering.

    A lease whose ``lease_expires_at`` has passed is reclaimable: the owning process
    died mid-drive, and a task must not be wedged forever by a crash. Cross-process
    exclusivity beyond that TTL is the relay's job (P2), not this client's.

    Use as a context manager; the lease is always released, including on error.
    """

    def __init__(
        self,
        store: TaskRecordStore,
        key: TaskKey,
        *,
        ttl_seconds: float = DEFAULT_LEASE_SECONDS,
        owner: str | None = None,
    ) -> None:
        self._store = store
        self._key = key
        self._ttl = ttl_seconds
        self._owner = owner or _new_owner_id()
        self._held = False

    @property
    def owner(self) -> str:
        """This driver's owner token."""

        return self._owner

    def acquire(self) -> None:
        """Take the lease, or raise the typed refusal if another driver holds it.

        Raises:
            ToolError: Another live driver holds an unexpired lease on this task.
        """

        with _lease_lock(self._key):
            record = self._store.get(self._key)
            if record is None:
                # Nothing persisted yet (the create-and-record window, or a drive
                # against a store with no row). The process-local lock still
                # serializes this task's drivers; there is simply no row to CAS on.
                self._held = True
                return
            now = time.time()
            held_by_other = (
                record.lease_owner is not None
                and record.lease_owner != self._owner
                and (record.lease_expires_at or 0.0) > now
            )
            if held_by_other:
                raise ToolError(
                    f"task {self._key.task_id} is already being driven by another driver",
                    details={
                        "reason": MCP_TASK_LEASE_HELD,
                        "task_id": self._key.task_id,
                        "server_id": self._key.server_id,
                        "session_id": self._key.session_id,
                        "lease_owner": record.lease_owner,
                        "lease_expires_at": record.lease_expires_at,
                    },
                )
            self._store.put(
                replace(record, lease_owner=self._owner, lease_expires_at=now + self._ttl)
            )
            self._held = True

    def release(self) -> None:
        """Clear the lease if this driver still owns it (idempotent)."""

        if not self._held:
            return
        self._held = False
        with _lease_lock(self._key):
            record = self._store.get(self._key)
            if record is None or record.lease_owner != self._owner:
                return
            self._store.put(replace(record, lease_owner=None, lease_expires_at=None))

    @property
    def renewal_interval(self) -> float:
        """Renew before expiry, including while an RPC or human input is pending."""
        return max(0.001, self._ttl / 3)

    def renew(self) -> None:
        """Extend only this driver's existing claim; never steal another claim."""
        with _lease_lock(self._key):
            record = self._store.get(self._key)
            if not self._held or (record is not None and record.lease_owner != self._owner):
                raise ToolError(
                    "Task driver lost its exclusive lease",
                    details={"reason": "mcp_task_lease_lost", **self._key.to_wire()},
                )
            if record is not None:
                self._store.put(replace(record, lease_expires_at=time.time() + self._ttl))

    def __enter__(self) -> "TaskLease":
        """Acquire on entry."""

        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Always release."""

        self.release()
