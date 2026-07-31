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
  fork-lineage / autorename state already live there), reached through the
  existing ``SessionStore.update(metadata_patch=...)`` seam. No schema migration,
  no new file, no new lifecycle.

Rejected alternatives: ``run.extra.workflow_state`` is the blueprint-declared,
schema-validated vocabulary the MODEL authors — infra task ids are not model state
and would pollute a typed surface; ARC is the semantic event log, not a mutable
key-value registry of live handles; a dedicated ``tasks.json`` would be exactly the
fifth store RULE 4 rules out.

ATTRIBUTION. A record's session is resolved at claim time from the same
receive-loop correlation registry #1113 uses (:func:`.elicitation_correlation.correlated_session_id`).
A task that cannot be attributed is NOT dropped and NOT guessed onto some session:
it is held in a process-local overlay and reported with the typed reason
``mcp_task_record_store_absent`` — the write still works, the crash-recovery
guarantee is honestly reported as reduced.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from clio_agent.errors import MCP_TASK_RECORD_STORE_ABSENT
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskRecord,
    set_task_record_store,
    set_task_session_resolver,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.gact.sessions import SessionStore

logger = logging.getLogger(__name__)

__all__ = [
    "SESSION_TASKS_METADATA_KEY",
    "SessionMetadataTaskStore",
    "install_session_task_store",
]

#: The ``Session.metadata`` key the task rows live under.
SESSION_TASKS_METADATA_KEY = "mcp_tasks"


class SessionMetadataTaskStore:
    """A :class:`~clio_agent.tools.mcp_task_records.TaskRecordStore` over session metadata.

    Rows are stored as ``session.metadata["mcp_tasks"][task_id] = record.to_wire()``
    and written through ``SessionStore.update(metadata_patch=...)``, so each write
    inherits the registry's atomic write+fsync+rename. Reads scan the registry,
    which is an in-memory dict on the hot path (the registry loads the JSON once at
    construction), so a cold-start reconnect sweep needs no extra I/O.
    """

    def __init__(self, sessions: "SessionStore") -> None:
        self._sessions = sessions
        # Tasks whose CLIO session could not be resolved. Kept so the drive still
        # works; reported, never silently dropped.
        self._unattributed = InMemoryTaskRecordStore()
        self._lock = threading.Lock()

    def put(self, record: TaskRecord) -> None:
        """Persist (or update) one task record on its session row."""

        session_id = record.session_id
        if not session_id or self._sessions.get(session_id) is None:
            logger.warning(
                "mcp task %s not attributable to a session reason=%s "
                "(held process-locally; it will not survive a restart)",
                record.task_id,
                MCP_TASK_RECORD_STORE_ABSENT,
            )
            self._unattributed.put(record)
            return
        with self._lock:
            rows = self._rows(session_id)
            rows[record.task_id] = record.to_wire()
            self._sessions.update(session_id, metadata_patch={SESSION_TASKS_METADATA_KEY: rows})

    def get(self, task_id: str) -> TaskRecord | None:
        """Return the persisted record for ``task_id``, if any."""

        held = self._unattributed.get(task_id)
        if held is not None:
            return held
        for session in self._sessions.list():
            row = self._rows_of(session).get(task_id)
            if isinstance(row, dict):
                return TaskRecord.from_wire(row)
        return None

    def list(self) -> list[TaskRecord]:
        """Return every persisted record across every session."""

        records = list(self._unattributed.list())
        for session in self._sessions.list():
            records.extend(
                TaskRecord.from_wire(row)
                for row in self._rows_of(session).values()
                if isinstance(row, dict)
            )
        return records

    def drop(self, task_id: str) -> None:
        """Forget a settled task wherever it is held."""

        self._unattributed.drop(task_id)
        with self._lock:
            for session in self._sessions.list():
                rows = dict(self._rows_of(session))
                if rows.pop(task_id, None) is None:
                    continue
                self._sessions.update(session.id, metadata_patch={SESSION_TASKS_METADATA_KEY: rows})

    def _rows(self, session_id: str) -> dict[str, Any]:
        """A mutable copy of one session's task rows."""

        session = self._sessions.get(session_id)
        return dict(self._rows_of(session)) if session is not None else {}

    @staticmethod
    def _rows_of(session: Any) -> dict[str, Any]:
        """The task-row mapping on a session, or an empty mapping."""

        rows = (getattr(session, "metadata", None) or {}).get(SESSION_TASKS_METADATA_KEY)
        return rows if isinstance(rows, dict) else {}


def install_session_task_store(sessions: "SessionStore") -> None:
    """Publish ``sessions`` as the process's durable task-record home.

    Called from :class:`~clio_agent.gact.sessions.SessionStore` construction for a
    PERSISTENT registry only (an in-memory test registry is not a durable home and
    must not claim to be one). Also installs the receive-loop session resolver so a
    task minted mid-call is attributed to the CLIO session that made the call.
    """

    set_task_record_store(SessionMetadataTaskStore(sessions))
    from clio_agent.gact.elicitation_correlation import (  # noqa: PLC0415 - keep leaf
        correlated_session_id,
    )

    set_task_session_resolver(correlated_session_id)
