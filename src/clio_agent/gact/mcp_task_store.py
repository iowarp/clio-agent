"""The durable home for in-flight SEP-2663 task ids (#1115).

WHY HERE, AND WHY THIS STORE. Reconnect-by-task-id needs the id to outlive the
client that created it — and, for crash recovery, the process. RULE 4 forbids a
fifth materialization, so the id goes into an EXISTING durable store rather than a
new one. The gact **session registry** (``gact/sessions.py``,
``<workspace>/.clio/agent/sessions.json``) is the honest home:

* It is already per-session, and a task IS per-session work — the record needs a
  session to be attributed to, resumed for, and cleaned up with.
* It is already durable with the write semantics this needs: every mutation
  write+fsyncs to a temp file and atomically renames, so a crash mid-write cannot
  leave a half-written task id behind.
* Its ``Session.metadata`` is the registry's declared extension point (pin /
  fork-lineage / autorename state already live there), reached through the existing
  ``SessionStore.update(metadata_patch=...)`` seam. No schema migration, no new file,
  no new lifecycle.

Rejected alternatives: ``run.extra.workflow_state`` is the blueprint-declared,
schema-validated vocabulary the MODEL authors — infra task ids are not model state
and would pollute a typed surface; ARC is the semantic event log, not a mutable
key-value registry of live handles; a dedicated ``tasks.json`` would be exactly the
fifth store RULE 4 rules out.

IDENTITY. Rows are keyed by the composite
:class:`~clio_agent.tools.mcp_task_records.TaskKey` — the session row scopes
``session_id``, and within it each row is keyed by ``server_id|task_id``. A
``task_id`` is minted by the SERVER, so keying on it alone would let one backend's
task overwrite another's record and let a ``drop`` delete an unrelated live task.

NOTHING IS EVER SILENTLY LOST. Two things can take a session row away underneath a
live task: a delete racing a write, and a delete of a session that still owns tasks.
Neither blocks the user, and neither discards the record:

* ``SessionStore.update`` returning ``None`` means the row is gone. That is a TYPED
  degraded write (``mcp_task_record_held_locally``), not an ignored return: the
  record moves to the process-local HOLDING PATH, where it stays resumable,
  cancellable and visible in diagnostics — just not across a restart.
* Deleting a session that owns live tasks (:meth:`SessionMetadataTaskStore.on_session_deleted`)
  never blocks the deletion. Each live task gets a bounded, best-effort
  ``tasks/cancel`` through the installed canceller hook, then migrates to the holding
  path stamped ``mcp_task_session_deleted`` with ``cancel_requested`` recorded. When
  no canceller is installed, that is reported — the store never pretends the remote
  task was stopped.

ATTRIBUTION. A record's session is resolved at claim time from the same receive-loop
correlation registry #1113 uses
(:func:`.elicitation_correlation.correlated_session_id`). A task that cannot be
attributed is held on the holding path with the same typed reason.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from clio_agent.errors import MCP_TASK_RECORD_HELD_LOCALLY, MCP_TASK_SESSION_DELETED
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
    set_task_record_store,
    set_task_session_resolver,
    task_canceller,
    task_change_listener,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.gact.sessions import SessionStore

logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_TASKS_METADATA_KEY",
    "SessionMetadataTaskStore",
    "install_session_task_store",
    "notify_session_deleted",
]

#: The ``Session.metadata`` key the task rows live under.
SESSION_TASKS_METADATA_KEY = "mcp_tasks"


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp, matching ``gact/events.py``'s own helper."""

    return datetime.now(timezone.utc).isoformat()


class SessionMetadataTaskStore:
    """A :class:`~clio_agent.tools.mcp_task_records.TaskRecordStore` over session metadata.

    Rows are stored as ``session.metadata["mcp_tasks"][server_id|task_id] =
    record.to_wire()`` and written through ``SessionStore.update(metadata_patch=...)``,
    so each write inherits the registry's atomic write+fsync+rename. Reads scan the
    registry, which is an in-memory dict on the hot path (the registry loads the JSON
    once at construction), so a cold-start reconnect sweep needs no extra I/O.
    """

    def __init__(self, sessions: "SessionStore") -> None:
        self._sessions = sessions
        # The HOLDING PATH: records whose session row is gone or unresolved. They stay
        # usable here and are reported; they are never dropped on the floor.
        self._held = InMemoryTaskRecordStore()
        self._lock = threading.Lock()

    # ---- TaskRecordStore ---------------------------------------------------

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record on its session row.

        A missing session, or an ``update`` that reports the row is gone (the delete
        vs. put race), degrades to the holding path with a typed reason instead of
        losing the record. Every call stamps ``updated_at`` (this is the ONE write
        funnel, so it is the one place that can honestly claim "just written") and
        notifies the installed change listener (#1205's SSE bridge), regardless of
        which of the three outcomes below the write took.

        #1205 review D2: the three hold-path branches notify with ``_hold``'s
        RETURN VALUE (the held record, ``holding_reason`` set), never the pre-hold
        ``stamped`` record — publishing ``stamped`` verbatim would silently strip
        the one field that makes a holding-path degrade non-silent to a live SSE
        subscriber.
        """

        stamped = replace(record, updated_at=_utcnow_iso())
        session_id = stamped.key.session_id
        if not session_id:
            self._notify(self._hold(stamped, "no CLIO session resolved"))
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                self._notify(self._hold(stamped, "session row absent"))
                return
            rows = dict(self._rows_of(session))
            rows[stamped.key.row_key] = stamped.to_wire()
            updated = self._sessions.update(
                session_id, metadata_patch={SESSION_TASKS_METADATA_KEY: rows}
            )
            if updated is None:
                # The session was deleted between the existence check and the write.
                # The Optional return is the ONLY signal the registry gives; ignoring
                # it would silently drop a live remote task's only local handle.
                self._notify(self._hold(stamped, "session deleted mid-write"))
                return
            self._held.drop(stamped.key)
        self._notify(stamped)

    def get(self, key: TaskKey) -> TaskRecord | None:
        """Return the record for the FULL composite key, if any."""

        held = self._held.get(key)
        if held is not None:
            return held
        if not key.session_id:
            return None
        row = self._rows_of(self._sessions.get(key.session_id)).get(key.row_key)
        return TaskRecord.from_wire(row) if isinstance(row, dict) else None

    def list(self) -> list[TaskRecord]:
        """Return every persisted record across every session, plus held ones."""

        records = list(self._held.list())
        for session in self._sessions.list():
            records.extend(
                TaskRecord.from_wire(row)
                for row in self._rows_of(session).values()
                if isinstance(row, dict)
            )
        return records

    def drop(self, key: TaskKey) -> None:
        """Forget exactly one task — never every row sharing its ``task_id``.

        #1205 review D1 (BLOCKING): a terminal task is almost always REMOVED
        via ``drop`` (the driver persists the terminal status, then drops the
        now-settled row) rather than left sitting in the store — so ``drop``
        is the ONE place a terminal ``mcp_task.completed`` / ``.failed`` /
        ``.cancelled`` event is guaranteed to fire. Reads the record's current
        state and publishes it BEFORE the row is removed from either the
        holding path or the session row, so a live SSE subscriber never sees
        the row simply vanish with no explanation. A caller that already
        persisted the same terminal status via ``put`` immediately before
        calling this gets one extra, harmless, idempotent copy of the SAME
        event (this codebase's SSE consumers apply the full record verbatim,
        see ``McpTaskPeekView`` — never a delta), which is the accepted
        trade-off against a caller that drops WITHOUT ever persisting the
        terminal status first (e.g. an ack-only cancel) getting no event at
        all under the old code.
        """

        record = self.get(key)
        if record is not None:
            self._notify(record)
        self._held.drop(key)
        if not key.session_id:
            return
        with self._lock:
            session = self._sessions.get(key.session_id)
            if session is None:
                return
            rows = dict(self._rows_of(session))
            if rows.pop(key.row_key, None) is None:
                return
            self._sessions.update(key.session_id, metadata_patch={SESSION_TASKS_METADATA_KEY: rows})

    # ---- session lifecycle -------------------------------------------------

    def on_session_deleted(self, session_id: str, rows: dict[str, Any]) -> tuple[TaskRecord, ...]:
        """Migrate a deleted session's live tasks to the holding path.

        The user's deletion is NEVER blocked and the records are NEVER discarded.
        Each live task is offered to the installed canceller (bounded, best-effort),
        then held locally stamped ``mcp_task_session_deleted`` with whether a cancel
        was actually attempted — so a remote task that is still running keeps a local
        handle that can resume or cancel it, and shows up in diagnostics.
        """

        migrated: list[TaskRecord] = []
        canceller = task_canceller()
        for row in rows.values():
            if not isinstance(row, dict):
                continue
            record = TaskRecord.from_wire(row)
            attempted = self._attempt_cancel(canceller, record)
            held = replace(
                record, cancel_requested=attempted, holding_reason=MCP_TASK_SESSION_DELETED
            )
            self._held.put(held)
            migrated.append(held)
            logger.warning(
                "mcp task %s migrated to the holding path reason=%s session=%s server=%s "
                "cancel_attempted=%s (still resumable in this process, not across a restart)",
                record.key.task_id,
                MCP_TASK_SESSION_DELETED,
                session_id,
                record.key.server_id,
                attempted,
            )
        return tuple(migrated)

    @staticmethod
    def _attempt_cancel(canceller: Any, record: TaskRecord) -> bool:
        """Best-effort remote cancel; ``False`` when none was (or could be) attempted."""

        if canceller is None:
            logger.warning(
                "no remote canceller installed: task %s was NOT cancelled on its server",
                record.key.task_id,
            )
            return False
        try:
            return bool(canceller(record))
        except Exception as exc:  # noqa: BLE001 - best-effort cancel; reason logged, record kept
            logger.warning(
                "best-effort tasks/cancel for task %s failed during session delete: %s",
                record.key.task_id,
                exc,
            )
            return False

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _notify(record: TaskRecord) -> None:
        """Call the installed change listener (#1205 SSE wiring), if any.

        Runs after every ``put`` outcome — durable write, or a degrade to the
        holding path — so a subscriber learns of a status change, a lease change,
        or an input-answer capture the same way it learns of a durable write, with
        no separate polling path. No listener installed (no durable gact session
        registry booted, e.g. a bare unit test) is a quiet no-op: there is no live
        SSE surface to have silently failed.
        """

        listener = task_change_listener()
        if listener is not None:
            listener(record)

    def _hold(self, record: TaskRecord, detail: str) -> TaskRecord:
        """Move one record to the holding path with a typed reason.

        Returns the HELD record (``holding_reason`` set) — #1205 review D2: a
        caller must publish THIS returned record, never the pre-hold one it
        passed in, or the published payload silently drops the one field that
        makes the degrade non-silent to a live SSE subscriber.
        """

        held = replace(record, holding_reason=MCP_TASK_RECORD_HELD_LOCALLY)
        self._held.put(held)
        logger.warning(
            "mcp task %s held process-locally reason=%s (%s); it stays resumable here "
            "but will not survive a restart",
            record.key.task_id,
            MCP_TASK_RECORD_HELD_LOCALLY,
            detail,
        )
        return held

    @staticmethod
    def _rows_of(session: Any) -> dict[str, Any]:
        """The task-row mapping on a session, or an empty mapping."""

        rows = (getattr(session, "metadata", None) or {}).get(SESSION_TASKS_METADATA_KEY)
        return rows if isinstance(rows, dict) else {}


def install_session_task_store(sessions: "SessionStore") -> SessionMetadataTaskStore:
    """Publish ``sessions`` as the process's durable task-record home.

    Called from :class:`~clio_agent.gact.sessions.SessionStore` construction for a
    PERSISTENT registry only (an in-memory test registry is not a durable home and
    must not claim to be one). Also installs the receive-loop session resolver so a
    task minted mid-call is attributed to the CLIO session that made the call.
    """

    store = SessionMetadataTaskStore(sessions)
    set_task_record_store(store)
    from clio_agent.gact.elicitation_correlation import (  # noqa: PLC0415 - keep leaf
        correlated_session_id,
    )

    set_task_session_resolver(correlated_session_id)
    return store


def notify_session_deleted(session_id: str, rows: dict[str, Any]) -> None:
    """Tell the installed task store that a session holding task rows was deleted.

    Called from ``SessionStore.delete`` AFTER the row is gone, so deletion is never
    delayed, blocked, or made to fail by task cleanup. A store that is not the
    gact-backed one (a unit-test in-memory store) has nothing to migrate, which is
    reported rather than assumed.
    """

    if not rows:
        return
    from clio_agent.tools.mcp_task_records import task_record_store  # noqa: PLC0415 - keep leaf

    migrate = getattr(task_record_store(), "on_session_deleted", None)
    if migrate is None:
        logger.warning(
            "session %s was deleted with %d live task record(s) but the installed task "
            "store cannot migrate them reason=%s",
            session_id,
            len(rows),
            MCP_TASK_SESSION_DELETED,
        )
        return
    migrate(session_id, rows)
