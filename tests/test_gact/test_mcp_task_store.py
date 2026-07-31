"""The durable home for SEP-2663 task ids: gact session metadata (#1115).

RULE 4 forbids a fifth store, so an in-flight task id is persisted onto the row it
belongs to — the gact session registry, whose every mutation write+fsyncs to a temp
file and atomically renames. These tests prove the guarantee on the REAL object: the
bytes on disk, re-read by a fresh registry, drive a real reconnect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.mcp_task_store import (
    SESSION_TASKS_METADATA_KEY,
    SessionMetadataTaskStore,
    install_session_task_store,
)
from clio_agent.gact.sessions import SessionStore
from clio_agent.tools.mcp_task_records import (
    TaskRecord,
    set_task_record_store,
    task_record_store,
    task_record_store_is_durable,
)


@pytest.fixture(autouse=True)
def _isolate_process_store() -> Any:
    """Never leak this module's store installation into other tests."""

    yield
    set_task_record_store(None)


def _store_with_session(tmp_path: Path) -> tuple[SessionStore, SessionMetadataTaskStore, str]:
    """A persistent registry with one session, plus a task store over it."""

    sessions = SessionStore(path=tmp_path / "sessions.json")
    session = sessions.create(workspace_id="ws", title="tasks")
    return sessions, SessionMetadataTaskStore(sessions), session.id


def test_record_lands_in_session_metadata_on_disk(tmp_path: Path) -> None:
    """The id is written into the session row, not a new file."""

    sessions, store, sid = _store_with_session(tmp_path)
    store.put(TaskRecord(task_id="task-1", tool="slow", session_id=sid, status="working"))

    on_disk = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    rows = on_disk[sid]["metadata"][SESSION_TASKS_METADATA_KEY]

    assert rows["task-1"]["task_id"] == "task-1"
    assert rows["task-1"]["tool"] == "slow"
    assert rows["task-1"]["status"] == "working"
    # No fifth store: the task id lives in the registry file; nothing task-shaped
    # was created next to it.
    written = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert "sessions.json" in written
    assert not any("task" in name for name in written)
    assert sessions.get(sid) is not None


def test_record_survives_losing_the_process(tmp_path: Path) -> None:
    """A fresh registry over the same path re-reads the id — crash recovery."""

    _sessions, store, sid = _store_with_session(tmp_path)
    store.put(
        TaskRecord(
            task_id="task-2",
            session_id=sid,
            status="input_required",
            answered_input_keys=("k1",),
        )
    )

    # Simulate a restart: brand-new registry object, same file.
    reborn = SessionStore(path=tmp_path / "sessions.json")
    recovered = SessionMetadataTaskStore(reborn).get("task-2")

    assert recovered is not None
    assert recovered.task_id == "task-2"
    assert recovered.session_id == sid
    assert recovered.answered_input_keys == ("k1",)


def test_drop_removes_the_row(tmp_path: Path) -> None:
    """A settled task leaves no row behind for a later sweep to resume."""

    _sessions, store, sid = _store_with_session(tmp_path)
    store.put(TaskRecord(task_id="task-3", session_id=sid))
    store.drop("task-3")

    assert store.get("task-3") is None
    assert store.list() == []


def test_unattributed_record_is_reported_not_dropped(tmp_path: Path, caplog: Any) -> None:
    """A task with no resolvable session still works, and says so."""

    import logging

    _sessions, store, _sid = _store_with_session(tmp_path)
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_task_store"):
        store.put(TaskRecord(task_id="task-4", session_id=None))

    assert "mcp_task_record_store_absent" in caplog.text
    held = store.get("task-4")
    assert held is not None and held.task_id == "task-4"


def test_persistent_session_store_publishes_itself_as_the_durable_home(
    tmp_path: Path,
) -> None:
    """Constructing a PERSISTENT registry installs it as the process task home."""

    set_task_record_store(None)
    sessions = SessionStore(path=tmp_path / "sessions.json")

    installed = task_record_store()

    assert isinstance(installed, SessionMetadataTaskStore)
    assert task_record_store_is_durable() is True
    assert sessions.count() == 0


def test_in_memory_session_store_does_not_claim_durability() -> None:
    """A path-less registry must not pretend to be a crash-recovery home."""

    set_task_record_store(None)
    SessionStore(path=None)

    assert task_record_store_is_durable() is False


async def test_reconnect_through_the_durable_home_after_a_restart(tmp_path: Path) -> None:
    """End to end: persist via the gact home, restart, resume to completion."""

    from clio_agent.tools.mcp_tasks import resume_task

    sessions, store, sid = _store_with_session(tmp_path)
    install_session_task_store(sessions)
    task_record_store().put(
        TaskRecord(task_id="task-5", tool="slow", session_id=sid, status="working")
    )

    # Restart: new registry, new task store, nothing carried in memory.
    set_task_record_store(None)
    reborn = SessionStore(path=tmp_path / "sessions.json")
    install_session_task_store(reborn)

    class Session:
        """A minimal scripted ``ClientSession`` for the resumed poll loop."""

        def __init__(self) -> None:
            self.methods: list[str] = []

        async def send_request(
            self,
            request: Any,
            result_type: Any,
            request_read_timeout_seconds: float | None = None,
        ) -> Any:
            """Answer ``tasks/get`` with a completed task."""

            self.methods.append(request.method)
            return result_type.model_validate(
                {
                    "taskId": "task-5",
                    "status": "completed",
                    "createdAt": "2026-07-31T00:00:00+00:00",
                    "lastUpdatedAt": "2026-07-31T00:00:00+00:00",
                    "resultType": "complete",
                    "result": {"content": []},
                }
            )

    session = Session()
    final = await resume_task(session, "task-5")

    assert final.status == "completed"
    assert session.methods == ["tasks/get"]
    assert task_record_store().get("task-5") is None
