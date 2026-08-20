"""Durable records for in-flight SEP-2663 tasks (#1115) — the leaf half.

Split from :mod:`clio_agent.tools.mcp_tasks` on purpose. That module imports
``fastmcp_tasks``, which pulls ``docket``/``redis`` (~1.7s cold) and registers an
internal client-extension factory with fastmcp core process-wide; it must stay
lazily imported from the one execution-path client factory. THIS module carries no
protocol dependency at all, so the durable home — :mod:`clio_agent.gact.mcp_task_store`,
published when a gact session registry is constructed — can be wired at server boot
without paying either cost.

Three invariants live here, each the answer to a way crash recovery breaks:

**Identity is composite.** A ``taskId`` is minted by the SERVER, so two independent
backends can legitimately (or maliciously) mint the same one. A record is therefore
keyed by :class:`TaskKey` — ``(server_id, session_id, task_id)`` — and carries the
reconnectable ``backend`` locator. Every ``get`` / ``drop`` / resume takes the full
key, so one backend's task can neither overwrite nor delete another's.

**An answer is durable before it is transmitted.** :class:`TaskInputAnswer` holds the
human's answer PAYLOAD, not merely the fact that a key was seen, and is persisted
before ``tasks/update`` goes out. A lost acknowledgement, a rejected update, or a
server that keeps re-reporting an answered key is then retried with the IDENTICAL
payload — the human is never asked twice, and a divergence never starves the task.

**One driver per task.** :class:`TaskLease` is a compare-and-set over the persisted
record guarded by a process-local lock, so two concurrent resumes of the same task
cannot both poll and answer it. Cross-PROCESS leasing is deliberately out of scope
(the relay owns durable multi-host task ownership in P2); a lease taken by a process
that died mid-drive expires by TTL rather than wedging the task forever.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from clio_agent.errors import (
    MCP_TASK_LEASE_HELD,
    MCP_TASK_RECORD_STORE_ABSENT,
    ToolError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "TERMINAL_TASK_STATES",
    "InMemoryTaskRecordStore",
    "TaskInputAnswer",
    "TaskInputLedger",
    "TaskKey",
    "TaskLease",
    "TaskRecord",
    "TaskRecordStore",
    "iter_task_records",
    "open_task_records",
    "persist_ledger",
    "resolve_store",
    "resolve_task_session_id",
    "set_task_canceller",
    "set_task_change_listener",
    "set_task_console_listener",
    "set_task_record_store",
    "set_task_session_resolver",
    "task_canceller",
    "task_change_listener",
    "task_console_listener",
    "task_record_store",
    "task_record_store_is_durable",
]

#: Terminal SEP-2663 task states.
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})

#: How long one driver's lease on a task stays valid without renewal. A lease is
#: released on every exit path; the TTL exists only so a lease taken by a process
#: that died mid-drive cannot wedge the task forever.
DEFAULT_LEASE_SECONDS = 300.0


@dataclass(frozen=True)
class TaskKey:
    """The composite durable identity of one task.

    ``task_id`` alone is NOT an identity: it is minted by the server, so two
    backends can mint the same string. ``server_id`` is CLIO's stable digest of the
    backend the task lives on, and ``session_id`` is the CLIO session it was started
    for (``None`` when the call could not be attributed).
    """

    server_id: str
    session_id: str | None
    task_id: str

    @property
    def row_key(self) -> str:
        """The stable string the durable row is stored under, within its session."""

        return f"{self.server_id}|{self.task_id}"

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable projection."""

        return {
            "server_id": self.server_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "TaskKey":
        """Rebuild a key from its persisted projection."""

        return cls(
            server_id=str(payload.get("server_id") or ""),
            session_id=payload.get("session_id") or None,
            task_id=str(payload.get("task_id") or ""),
        )


@dataclass(frozen=True)
class TaskInputAnswer:
    """One in-task input key's answer, and whether the server has accepted it.

    ``payload`` is the exact ``tasks/update`` value the elicitation produced. It is
    persisted BEFORE transmission so any retry re-sends the identical bytes instead
    of re-asking the human. ``delivered`` flips only once ``tasks/update`` returned.
    """

    key: str
    payload: dict[str, Any]
    delivered: bool = False

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable projection."""

        return {"key": self.key, "payload": self.payload, "delivered": self.delivered}

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "TaskInputAnswer":
        """Rebuild an answer from its persisted projection."""

        raw = payload.get("payload")
        return cls(
            key=str(payload.get("key") or ""),
            payload=dict(raw) if isinstance(raw, Mapping) else {},
            delivered=bool(payload.get("delivered")),
        )


@dataclass(frozen=True)
class TaskRecord:
    """One durable, reconnectable SEP-2663 task."""

    key: TaskKey
    tool: str = ""
    backend: dict[str, Any] = field(default_factory=dict)
    status: str = "working"
    # #1236 (clio-relay#265's client half, owner ruling 2026-08-20): ``status`` above
    # is the RAW SEP-2663 wire status verbatim -- kept for back-compat, never
    # destroyed. SEP-2663 semantics count DELIVERING a result as "completed" even
    # when the delivered ``CallToolResult`` itself carries ``isError: true`` (the
    # application failed; the dispatch merely succeeded in reporting that). A run
    # card reading raw ``status`` alone launders a real failure into a bare
    # "completed" -- the owner's "completed is a terrible status indicator" ruling.
    # ``effective_status`` is the PROTOCOL-TRUTH-DERIVED status a display should
    # treat as primary (see :func:`clio_agent.tools.mcp_tasks.derive_effective_status`):
    # identical to ``status`` for every state except a completed-with-isError
    # delivery, which downgrades to ``"failed"`` with ``effective_status_reason``
    # carrying the extracted error text. Empty string means "not yet derived"
    # (a record from before this field existed, or one no poll has touched yet) --
    # every reader falls back to ``status`` in that case, never crashes on it.
    effective_status: str = ""
    effective_status_reason: str | None = None
    created_at: str = ""
    # Stamped by :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore`
    # on every ``put`` (the single write path), mirroring ``AgentTask.updated_at`` —
    # the "created/updated" pair the session-scoped async-processes projection (#1205)
    # needs. Empty until the first durable write; never invented client-side.
    updated_at: str = ""
    input_answers: tuple[TaskInputAnswer, ...] = ()
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    cancel_requested: bool = False
    holding_reason: str | None = None

    @property
    def task_id(self) -> str:
        """The server-minted task id."""

        return self.key.task_id

    @property
    def session_id(self) -> str | None:
        """The CLIO session this task was started for."""

        return self.key.session_id

    @property
    def display_status(self) -> str:
        """The honest, protocol-truth-derived status a display should read (#1236).

        ``status`` stays the raw SEP-2663 wire value (never destroyed); this is
        ``effective_status`` when one has been derived, falling back to ``status``
        for a record no poll has touched yet (or one persisted before this field
        existed). Every consumer that decides what a run card / SSE event TYPE
        shows should read this, not ``status`` directly.
        """

        return self.effective_status or self.status

    def to_wire(self) -> dict[str, Any]:
        """JSON-serialisable projection (the shape persisted in session metadata)."""

        return {
            "key": self.key.to_wire(),
            "tool": self.tool,
            "backend": dict(self.backend),
            "status": self.status,
            "effective_status": self.effective_status,
            "effective_status_reason": self.effective_status_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "input_answers": [answer.to_wire() for answer in self.input_answers],
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "cancel_requested": self.cancel_requested,
            "holding_reason": self.holding_reason,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> "TaskRecord":
        """Rebuild a record from its persisted projection."""

        raw_key = payload.get("key")
        answers = payload.get("input_answers") or ()
        expires = payload.get("lease_expires_at")
        return cls(
            key=TaskKey.from_wire(raw_key if isinstance(raw_key, Mapping) else {}),
            tool=str(payload.get("tool") or ""),
            backend=dict(payload.get("backend") or {}),
            status=str(payload.get("status") or "working"),
            effective_status=str(payload.get("effective_status") or ""),
            effective_status_reason=payload.get("effective_status_reason") or None,
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            input_answers=tuple(
                TaskInputAnswer.from_wire(row) for row in answers if isinstance(row, Mapping)
            ),
            lease_owner=payload.get("lease_owner") or None,
            lease_expires_at=float(expires) if isinstance(expires, (int, float)) else None,
            cancel_requested=bool(payload.get("cancel_requested")),
            holding_reason=payload.get("holding_reason") or None,
        )


class TaskRecordStore(Protocol):
    """Durable home for in-flight task ids (RULE 4: an EXISTING store, never a new one)."""

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record."""
        ...

    def get(self, key: TaskKey) -> TaskRecord | None:
        """Return the persisted record for the FULL composite key, if any."""
        ...

    def list(self) -> list[TaskRecord]:
        """Return every persisted record."""
        ...

    def drop(self, key: TaskKey) -> None:
        """Forget the settled task named by the FULL composite key."""
        ...


class InMemoryTaskRecordStore:
    """Process-local store, keyed by the full composite identity.

    Used both as the fallback when no durable home is published and as the HOLDING
    PATH inside the gact-backed store, for records whose session row is gone (see
    :mod:`clio_agent.gact.mcp_task_store`). Reconnect then survives losing the CLIENT
    but not the PROCESS, which :func:`task_record_store` says out loud.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str | None, str], TaskRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _index(key: TaskKey) -> tuple[str, str | None, str]:
        """The dict index for one composite key."""

        return (key.server_id, key.session_id, key.task_id)

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record."""

        with self._lock:
            self._records[self._index(record.key)] = record

    def get(self, key: TaskKey) -> TaskRecord | None:
        """Return the persisted record for the full composite key, if any."""

        with self._lock:
            return self._records.get(self._index(key))

    def list(self) -> list[TaskRecord]:
        """Return every persisted record."""

        with self._lock:
            return list(self._records.values())

    def drop(self, key: TaskKey) -> None:
        """Forget exactly one task."""

        with self._lock:
            self._records.pop(self._index(key), None)


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
# Input answers: persisted BEFORE transmission, retried IDENTICALLY            #
# --------------------------------------------------------------------------- #


class TaskInputLedger:
    """The per-task input-answer state machine, written through to the store.

    Each server-minted input key moves ``absent -> captured -> delivered``:

    * **absent** — never elicited. The next round asks the human, exactly once.
    * **captured** — the human answered and the payload is PERSISTED, but the server
      has not acknowledged it. A lost ``tasks/update`` response, a rejected update,
      or a server still reporting the key re-sends the IDENTICAL payload; the human
      is never asked again.
    * **delivered** — ``tasks/update`` returned. If the server nonetheless keeps
      reporting the key (ledger/server divergence), the stored payload is
      RETRANSMITTED rather than suppressed, so the task cannot starve; the drive's
      no-progress bound still stops an endless loop.

    The ledger is a per-drive in-memory view over the record's persisted
    ``input_answers``; every mutation writes through before anything is transmitted.
    """

    def __init__(self, answers: Sequence[TaskInputAnswer] = ()) -> None:
        self._answers: dict[str, TaskInputAnswer] = {a.key: a for a in answers}
        self._lock = threading.Lock()

    @classmethod
    def from_record(cls, record: TaskRecord | None) -> "TaskInputLedger":
        """Seed a ledger from a persisted record (the resume path)."""

        return cls(record.input_answers if record is not None else ())

    def answer(self, key: str) -> TaskInputAnswer | None:
        """The stored answer for one key, or ``None`` if it was never elicited."""

        with self._lock:
            return self._answers.get(key)

    def unelicited(self, keys: Sequence[str]) -> list[str]:
        """The subset of ``keys`` with no stored answer — the ONLY keys to ask about."""

        with self._lock:
            return [key for key in keys if key not in self._answers]

    def capture(self, key: str, payload: Mapping[str, Any]) -> None:
        """Record a human's answer payload (state ``captured``)."""

        with self._lock:
            self._answers[key] = TaskInputAnswer(key=key, payload=dict(payload))

    def mark_delivered(self, keys: Sequence[str]) -> None:
        """Flip ``captured -> delivered`` once ``tasks/update`` returned."""

        with self._lock:
            for key in keys:
                existing = self._answers.get(key)
                if existing is not None:
                    self._answers[key] = replace(existing, delivered=True)

    def payloads_for(self, keys: Sequence[str]) -> dict[str, Any]:
        """The stored payloads for ``keys`` — what a retry re-sends verbatim."""

        with self._lock:
            return {key: dict(self._answers[key].payload) for key in keys if key in self._answers}

    def snapshot(self) -> tuple[TaskInputAnswer, ...]:
        """The persistable answer tuple."""

        with self._lock:
            return tuple(self._answers[key] for key in sorted(self._answers))

    def delivered_keys(self) -> frozenset[str]:
        """Keys the server has acknowledged."""

        with self._lock:
            return frozenset(key for key, a in self._answers.items() if a.delivered)


def persist_ledger(store: TaskRecordStore, key: TaskKey, ledger: TaskInputLedger) -> None:
    """Write a ledger's current answer state onto the durable record.

    Called BEFORE every ``tasks/update`` so a crash between eliciting and
    transmitting still leaves the answer recoverable and retriable verbatim.
    """

    record = store.get(key)
    if record is None:
        return
    store.put(replace(record, input_answers=ledger.snapshot()))


# --------------------------------------------------------------------------- #
# One driver per task                                                         #
# --------------------------------------------------------------------------- #

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

    def __enter__(self) -> "TaskLease":
        """Acquire on entry."""

        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Always release."""

        self.release()


# --------------------------------------------------------------------------- #
# Wiring hooks the gact layer installs                                        #
# --------------------------------------------------------------------------- #

_HOOK_LOCK = threading.Lock()
_SESSION_RESOLVER: Any = None
_TASK_CANCELLER: Any = None


def set_task_session_resolver(resolver: Any) -> None:
    """Install the ``ClientSession -> CLIO session id`` resolver (gact wiring)."""

    global _SESSION_RESOLVER
    with _HOOK_LOCK:
        _SESSION_RESOLVER = resolver


def resolve_task_session_id(session: Any) -> str | None:
    """Resolve the CLIO session owning a task, when a resolver is installed."""

    with _HOOK_LOCK:
        resolver = _SESSION_RESOLVER
    if resolver is None:
        return None
    return resolver(session)


def set_task_canceller(canceller: Any) -> None:
    """Install the best-effort ``TaskRecord -> bool`` remote canceller.

    Used when a session is deleted out from under live tasks: the store asks this
    hook to send ``tasks/cancel`` before migrating the record to the holding path.
    Absent (the default), the store reports that no cancel was attempted — it never
    pretends the remote task was stopped.
    """

    global _TASK_CANCELLER
    with _HOOK_LOCK:
        _TASK_CANCELLER = canceller


def task_canceller() -> Any:
    """The installed remote canceller, or ``None``."""

    with _HOOK_LOCK:
        return _TASK_CANCELLER


_TASK_CHANGE_LISTENER: Any = None


def set_task_change_listener(listener: Any) -> None:
    """Install the ``TaskRecord -> None`` change hook (gact SSE wiring, #1205).

    Called by :class:`~clio_agent.gact.mcp_task_store.SessionMetadataTaskStore`
    after every successful ``put`` (its single write path), so the live-view layer
    (``gact/mcp_task_events.py``) learns of a task mutation by callback instead of
    polling for one. Absent (the default, and in every process with no durable gact
    session registry), a mutation is simply not published — there is no separate
    silent-fallback reason here because there is no live SSE surface to have failed;
    the store's own durability degrade (``mcp_task_record_held_locally`` etc.) is
    reported independently.
    """

    global _TASK_CHANGE_LISTENER
    with _HOOK_LOCK:
        _TASK_CHANGE_LISTENER = listener


def task_change_listener() -> Any:
    """The installed change listener, or ``None``."""

    with _HOOK_LOCK:
        return _TASK_CHANGE_LISTENER


_TASK_CONSOLE_LISTENER: Any = None


def set_task_console_listener(listener: Any) -> None:
    """Install the ``(TaskKey, channel, delta, offset, truncated) -> None`` hook
    fired when a backend's ``on_poll`` observer folds NEW console bytes into a
    record (#1236, gact SSE wiring).

    Deliberately SEPARATE from :func:`set_task_change_listener`: that hook only
    ever receives the resulting :class:`TaskRecord`, with no way to tell "the
    whole rolling tail grew a bit" apart from any other mutation (a status
    transition, a lease change) — publishing the FULL accumulated tail on every
    one of those would bloat the live stream and risk crowding other events out
    of the bounded per-session history/queue. This hook instead carries just the
    NEW bytes (a delta), so the live-view layer (``gact/mcp_task_events.py``) can
    publish a lean, dedicated event; the record itself remains the source of
    truth for the full tail (unbounded reads always see it via a reload).

    ``channel`` names which stream the delta came from (``"console"`` today;
    a future ``"stderr"`` once clio-relay tails it too — never hardcoded to one
    value here). Absent (the default) is a quiet no-op: there is no live SSE
    surface to have silently failed when no durable gact session registry is
    booted (e.g. a bare unit test), mirroring :func:`set_task_change_listener`.
    """

    global _TASK_CONSOLE_LISTENER
    with _HOOK_LOCK:
        _TASK_CONSOLE_LISTENER = listener


def task_console_listener() -> Any:
    """The installed console-delta listener, or ``None``."""

    with _HOOK_LOCK:
        return _TASK_CONSOLE_LISTENER
