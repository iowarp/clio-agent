"""Durable records for in-flight SEP-2663 tasks (#1115) — the leaf half.

Split from :mod:`clio_agent.tools.mcp_tasks` on purpose. That module imports
``fastmcp_tasks``, which pulls ``docket``/``redis`` (~1.7s cold) and registers an
internal client-extension factory with fastmcp core process-wide; it must stay
lazily imported from the one execution-path client factory. THIS module carries no
protocol dependency at all, so the durable home — :mod:`clio_agent.gact.mcp_task_store`,
published when a gact session registry is constructed — can be wired at server boot
without paying either cost.

What lives here: the record shape, the store Protocol plus its in-memory fallback,
the process-level store registry, the input-key dedup ledger, and the
``ClientSession -> CLIO session id`` resolver registry. What does not: anything that
speaks the wire.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from clio_agent.errors import MCP_TASK_RECORD_STORE_ABSENT

logger = logging.getLogger(__name__)

__all__ = [
    "TERMINAL_TASK_STATES",
    "InMemoryTaskRecordStore",
    "TaskInputLedger",
    "TaskRecord",
    "TaskRecordStore",
    "iter_task_records",
    "open_task_records",
    "resolve_task_session_id",
    "set_task_record_store",
    "set_task_session_resolver",
    "task_record_store",
    "task_record_store_is_durable",
]

#: Terminal SEP-2663 task states.
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class TaskRecord:
    """One durable, reconnectable SEP-2663 task.

    ``session_id`` names the CLIO session the task was started for — both the
    attribution and, for the gact-backed store, the row the record is written into.
    ``answered_input_keys`` travels with the record so a resume after a crash does
    not re-ask a question the human already answered.
    """

    task_id: str
    tool: str = ""
    namespace: str | None = None
    session_id: str | None = None
    status: str = "working"
    created_at: str = ""
    answered_input_keys: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable projection (the shape persisted in session metadata)."""

        return {
            "task_id": self.task_id,
            "tool": self.tool,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "answered_input_keys": list(self.answered_input_keys),
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "TaskRecord":
        """Rebuild a record from its persisted projection."""

        keys = payload.get("answered_input_keys") or ()
        return cls(
            task_id=str(payload.get("task_id") or ""),
            tool=str(payload.get("tool") or ""),
            namespace=payload.get("namespace") or None,
            session_id=payload.get("session_id") or None,
            status=str(payload.get("status") or "working"),
            created_at=str(payload.get("created_at") or ""),
            answered_input_keys=tuple(str(key) for key in keys),
        )


class TaskRecordStore(Protocol):
    """Durable home for in-flight task ids (RULE 4: an EXISTING store, never a new one)."""

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record."""
        ...

    def get(self, task_id: str) -> TaskRecord | None:
        """Return the persisted record for ``task_id``, if any."""
        ...

    def list(self) -> list[TaskRecord]:
        """Return every persisted record."""
        ...

    def drop(self, task_id: str) -> None:
        """Forget a settled task."""
        ...


class InMemoryTaskRecordStore:
    """Process-local fallback store.

    In effect only when no durable home has been published — the tools layer running
    without a gact session registry (unit tests, the CLI smoke path). Reconnect then
    survives losing the CLIENT but not the PROCESS, which :func:`task_record_store`
    says out loud rather than pretending otherwise.
    """

    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record."""

        with self._lock:
            self._records[record.task_id] = record

    def get(self, task_id: str) -> TaskRecord | None:
        """Return the persisted record for ``task_id``, if any."""

        with self._lock:
            return self._records.get(task_id)

    def list(self) -> list[TaskRecord]:
        """Return every persisted record."""

        with self._lock:
            return list(self._records.values())

    def drop(self, task_id: str) -> None:
        """Forget a settled task."""

        with self._lock:
            self._records.pop(task_id, None)


_STORE_LOCK = threading.RLock()
_STORE: TaskRecordStore | None = None
_STORE_IS_DURABLE = False


def set_task_record_store(store: TaskRecordStore | None, *, durable: bool = True) -> None:
    """Install the process's task-record home (``None`` restores the in-memory one).

    Called by :mod:`clio_agent.gact.mcp_task_store` when a durable gact session
    registry exists. ``durable=False`` marks a non-surviving store so
    :func:`task_record_store_is_durable` stays honest.
    """

    global _STORE, _STORE_IS_DURABLE
    with _STORE_LOCK:
        _STORE = store
        _STORE_IS_DURABLE = bool(store is not None and durable)


def task_record_store() -> TaskRecordStore:
    """Return the installed task-record home, or the in-memory fallback.

    The fallback is a real degradation of the crash-recovery guarantee (a task id
    dies with the process), so it is reported with the typed reason
    ``mcp_task_record_store_absent`` rather than taken silently.
    """

    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        _STORE = InMemoryTaskRecordStore()
        logger.warning(
            "mcp task record store degraded reason=%s (task ids survive losing the "
            "client but not this process; no durable gact session registry published)",
            MCP_TASK_RECORD_STORE_ABSENT,
        )
        return _STORE


def task_record_store_is_durable() -> bool:
    """Whether the installed store survives the process."""

    with _STORE_LOCK:
        return _STORE_IS_DURABLE


def resolve_store(store: TaskRecordStore | None) -> TaskRecordStore:
    """The explicit store, else the installed one."""

    return store if store is not None else task_record_store()


def open_task_records(store: TaskRecordStore | None = None) -> list[TaskRecord]:
    """Every persisted, still-unsettled task record — the reconnect work list."""

    return [r for r in resolve_store(store).list() if r.status not in TERMINAL_TASK_STATES]


def iter_task_records(store: TaskRecordStore | None = None) -> Iterator[TaskRecord]:
    """Iterate persisted task records (diagnostics / reconnect sweeps)."""

    yield from resolve_store(store).list()


# --------------------------------------------------------------------------- #
# Input-key dedup                                                             #
# --------------------------------------------------------------------------- #


class TaskInputLedger:
    """Per-task record of which server-minted input keys were already answered.

    SEP-2663 servers report the FULL outstanding ``inputRequests`` map on every
    ``tasks/get`` while a task is ``input_required``: a key stays in the map until
    the server has processed the ``tasks/update`` carrying its answer. A poll racing
    that update therefore re-reports a key the client already answered, and
    answering it again would re-prompt the human on the ONE HITL surface for a
    question they have already answered. This ledger makes the answer exactly-once
    per key.
    """

    def __init__(self) -> None:
        self._answered: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def unanswered(self, task_id: str, keys: Sequence[str]) -> list[str]:
        """Return the subset of ``keys`` this task has not answered yet."""

        with self._lock:
            answered = self._answered.get(task_id, set())
        return [key for key in keys if key not in answered]

    def mark_answered(self, task_id: str, keys: Sequence[str]) -> None:
        """Record ``keys`` as answered for ``task_id``."""

        with self._lock:
            self._answered.setdefault(task_id, set()).update(keys)

    def answered(self, task_id: str) -> frozenset[str]:
        """The keys already answered for ``task_id``."""

        with self._lock:
            return frozenset(self._answered.get(task_id, ()))

    def forget(self, task_id: str) -> None:
        """Drop a settled task's ledger entry."""

        with self._lock:
            self._answered.pop(task_id, None)


# --------------------------------------------------------------------------- #
# ClientSession -> CLIO session id                                            #
# --------------------------------------------------------------------------- #

_RESOLVER_LOCK = threading.Lock()
_SESSION_RESOLVER: Any = None


def set_task_session_resolver(resolver: Any) -> None:
    """Install the ``ClientSession -> CLIO session id`` resolver (gact wiring)."""

    global _SESSION_RESOLVER
    with _RESOLVER_LOCK:
        _SESSION_RESOLVER = resolver


def resolve_task_session_id(session: Any) -> str | None:
    """Resolve the CLIO session owning a task, when a resolver is installed."""

    with _RESOLVER_LOCK:
        resolver = _SESSION_RESOLVER
    if resolver is None:
        return None
    return resolver(session)
