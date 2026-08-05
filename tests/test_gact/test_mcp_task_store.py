"""The durable home for SEP-2663 task ids: gact session metadata (#1115).

RULE 4 forbids a fifth store, so an in-flight task id is persisted onto the row it
belongs to — the gact session registry, whose every mutation write+fsyncs to a temp
file and atomically renames. These tests prove the guarantee on the REAL object: the
bytes on disk, re-read by a fresh registry, drive a real reconnect.

They also pin the two ways a session row can disappear underneath a live remote task
— a delete racing a write, and deleting a session that still owns tasks. Neither may
block the user's deletion, and neither may discard the record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.errors import MCP_TASK_RECORD_HELD_LOCALLY, MCP_TASK_SESSION_DELETED
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_task_store import (
    SESSION_TASKS_METADATA_KEY,
    SessionMetadataTaskStore,
    install_session_task_store,
)
from clio_agent.gact.sessions import SessionStore
from clio_agent.tools.mcp_task_records import (
    TaskInputAnswer,
    TaskKey,
    TaskRecord,
    set_task_canceller,
    set_task_record_store,
    task_record_store,
    task_record_store_is_durable,
)

SERVER_A = "server-a"
SERVER_B = "server-b"


@pytest.fixture(autouse=True)
def _isolate_process_hooks() -> Any:
    """Never leak this module's store/canceller installation into other tests."""

    yield
    set_task_record_store(None)
    set_task_canceller(None)


def _key(task_id: str, session_id: str | None, *, server: str = SERVER_A) -> TaskKey:
    """A composite task identity."""

    return TaskKey(server_id=server, session_id=session_id, task_id=task_id)


def _store_with_session(tmp_path: Path) -> tuple[SessionStore, SessionMetadataTaskStore, str]:
    """A persistent registry with one session, plus a task store over it."""

    sessions = SessionStore(path=tmp_path / "sessions.json")
    session = sessions.create(workspace_id="ws", title="tasks")
    return sessions, SessionMetadataTaskStore(sessions), session.id


def test_record_lands_in_session_metadata_on_disk(tmp_path: Path) -> None:
    """The id is written into the session row, not a new file."""

    sessions, store, sid = _store_with_session(tmp_path)
    key = _key("task-1", sid)
    store.put(TaskRecord(key=key, tool="slow", status="working"))

    on_disk = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
    rows = on_disk[sid]["metadata"][SESSION_TASKS_METADATA_KEY]

    assert rows[key.row_key]["key"]["task_id"] == "task-1"
    assert rows[key.row_key]["key"]["server_id"] == SERVER_A
    assert rows[key.row_key]["tool"] == "slow"
    assert rows[key.row_key]["status"] == "working"
    # No fifth store: the task id lives in the registry file; nothing task-shaped
    # was created next to it.
    written = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert "sessions.json" in written
    assert not any("task" in name for name in written)
    assert sessions.get(sid) is not None


def test_record_survives_losing_the_process(tmp_path: Path) -> None:
    """A fresh registry over the same path re-reads the id — crash recovery."""

    _sessions, store, sid = _store_with_session(tmp_path)
    key = _key("task-2", sid)
    store.put(
        TaskRecord(
            key=key,
            status="input_required",
            input_answers=(
                TaskInputAnswer(key="k1", payload={"action": "accept"}, delivered=False),
            ),
        )
    )

    # Simulate a restart: brand-new registry object, same file.
    reborn = SessionStore(path=tmp_path / "sessions.json")
    recovered = SessionMetadataTaskStore(reborn).get(key)

    assert recovered is not None
    assert recovered.key == key
    # The ANSWER PAYLOAD survives, so the resume retransmits identical bytes rather
    # than re-asking the human.
    assert recovered.input_answers[0].payload == {"action": "accept"}
    assert recovered.input_answers[0].delivered is False


def test_drop_removes_only_the_named_identity(tmp_path: Path) -> None:
    """A settled task leaves no row — and takes no other backend's row with it."""

    _sessions, store, sid = _store_with_session(tmp_path)
    mine = _key("shared-id", sid, server=SERVER_A)
    theirs = _key("shared-id", sid, server=SERVER_B)
    store.put(TaskRecord(key=mine))
    store.put(TaskRecord(key=theirs))

    store.drop(mine)

    assert store.get(mine) is None
    assert store.get(theirs) is not None


def test_same_task_id_across_two_servers_does_not_overwrite(tmp_path: Path) -> None:
    """Two backends may mint the same taskId; neither may clobber the other."""

    _sessions, store, sid = _store_with_session(tmp_path)
    mine = _key("shared-id", sid, server=SERVER_A)
    theirs = _key("shared-id", sid, server=SERVER_B)
    store.put(TaskRecord(key=mine, tool="a"))
    store.put(TaskRecord(key=theirs, tool="b"))

    assert (store.get(mine) or TaskRecord(key=mine)).tool == "a"
    assert (store.get(theirs) or TaskRecord(key=theirs)).tool == "b"
    assert len(store.list()) == 2


def test_same_task_id_across_two_sessions_does_not_overwrite(tmp_path: Path) -> None:
    """The same backend's taskId in two CLIO sessions stays two independent records."""

    sessions = SessionStore(path=tmp_path / "sessions.json")
    first = sessions.create(workspace_id="ws", title="one").id
    second = sessions.create(workspace_id="ws", title="two").id
    store = SessionMetadataTaskStore(sessions)
    one = _key("shared-id", first)
    two = _key("shared-id", second)
    store.put(TaskRecord(key=one, tool="one"))
    store.put(TaskRecord(key=two, tool="two"))

    store.drop(one)

    assert store.get(one) is None
    assert (store.get(two) or TaskRecord(key=two)).tool == "two"


def test_unattributed_record_is_reported_not_dropped(tmp_path: Path, caplog: Any) -> None:
    """A task with no resolvable session still works, and says so."""

    import logging

    _sessions, store, _sid = _store_with_session(tmp_path)
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_task_store"):
        store.put(TaskRecord(key=_key("task-4", None)))

    assert MCP_TASK_RECORD_HELD_LOCALLY in caplog.text
    held = store.get(_key("task-4", None))
    assert held is not None
    assert held.holding_reason == MCP_TASK_RECORD_HELD_LOCALLY


def test_delete_racing_a_put_holds_the_record_instead_of_losing_it(
    tmp_path: Path, caplog: Any
) -> None:
    """The session vanishing BETWEEN the existence check and the write is not a loss.

    ``SessionStore.update`` returning ``None`` is the only signal the registry gives.
    Ignoring it would silently drop the only local handle on a still-running remote
    task, so it becomes a typed degraded write onto the holding path.
    """

    import logging

    sessions, store, sid = _store_with_session(tmp_path)
    key = _key("task-race", sid)
    real_update = sessions.update

    def deleting_update(target: str, **kwargs: Any) -> Any:
        """Delete the session the instant the write is attempted."""

        sessions.delete(target)
        return real_update(target, **kwargs)

    sessions.update = deleting_update  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_task_store"):
        store.put(TaskRecord(key=key, tool="slow", status="working"))

    assert "session deleted mid-write" in caplog.text
    assert MCP_TASK_RECORD_HELD_LOCALLY in caplog.text
    held = store.get(key)
    assert held is not None, "the record must survive the race, on the holding path"
    assert held.holding_reason == MCP_TASK_RECORD_HELD_LOCALLY
    assert held.tool == "slow"


def test_deleting_a_session_migrates_its_live_tasks(tmp_path: Path, caplog: Any) -> None:
    """Deletion is never blocked; live tasks are cancel-requested and held locally."""

    import logging

    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="tasks").id
    install_session_task_store(sessions)
    key = _key("task-live", sid)
    task_record_store().put(TaskRecord(key=key, tool="slow", status="working"))

    cancelled: list[str] = []

    def canceller(record: TaskRecord) -> bool:
        """A stand-in for the bounded best-effort remote cancel."""

        cancelled.append(record.key.task_id)
        return True

    set_task_canceller(canceller)
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_task_store"):
        deleted = sessions.delete(sid)

    assert deleted is True, "the user's deletion must never be blocked by a live task"
    assert sessions.get(sid) is None
    assert cancelled == ["task-live"]
    assert MCP_TASK_SESSION_DELETED in caplog.text

    migrated = task_record_store().get(key)
    assert migrated is not None, "a live remote task must not lose its local handle"
    assert migrated.holding_reason == MCP_TASK_SESSION_DELETED
    assert migrated.cancel_requested is True
    # It is still visible in diagnostics.
    assert [r.key.task_id for r in task_record_store().list()] == ["task-live"]


def test_deleting_a_session_without_a_canceller_reports_that_nothing_was_cancelled(
    tmp_path: Path, caplog: Any
) -> None:
    """With no canceller installed the store never pretends the task was stopped."""

    import logging

    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="tasks").id
    install_session_task_store(sessions)
    key = _key("task-uncancelled", sid)
    task_record_store().put(TaskRecord(key=key, status="working"))
    set_task_canceller(None)

    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.mcp_task_store"):
        sessions.delete(sid)

    assert "was NOT cancelled on its server" in caplog.text
    migrated = task_record_store().get(key)
    assert migrated is not None
    assert migrated.cancel_requested is False


def test_a_failing_canceller_still_migrates_the_record(tmp_path: Path) -> None:
    """A cancel that raises is bounded and logged; the record is still preserved."""

    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="tasks").id
    install_session_task_store(sessions)
    key = _key("task-cancel-fails", sid)
    task_record_store().put(TaskRecord(key=key, status="working"))

    def boom(record: TaskRecord) -> bool:
        """A canceller whose remote call fails."""

        raise RuntimeError("backend unreachable")

    set_task_canceller(boom)
    assert sessions.delete(sid) is True

    migrated = task_record_store().get(key)
    assert migrated is not None
    assert migrated.cancel_requested is False
    assert migrated.holding_reason == MCP_TASK_SESSION_DELETED


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


def test_capabilities_reports_non_durable_task_record_store(tmp_path: Path) -> None:
    """Finding 9: the in-memory fallback is queryable with its typed reason."""

    class Agent:
        def forward(self, question: str, session_id: str, **_kwargs: Any) -> Any:
            return type("Prediction", (), {"answer": question, "selected_expert": ""})()

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=Agent())
    set_task_record_store(None)

    with TestClient(app) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json()["capabilities"]["x_clio_task_record_store"] == {
        "durable": False,
        "reason": "mcp_task_record_store_absent",
    }


async def test_reconnect_through_the_durable_home_after_a_restart(tmp_path: Path) -> None:
    """End to end: persist via the gact home, restart, resume to completion."""

    from clio_agent.tools.mcp_tasks import resume_task

    sessions, _store, sid = _store_with_session(tmp_path)
    install_session_task_store(sessions)
    key = _key("task-5", sid)
    task_record_store().put(TaskRecord(key=key, tool="slow", status="working"))

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
    final = await resume_task(session, key)

    assert final.status == "completed"
    assert session.methods == ["tasks/get"]
    assert task_record_store().get(key) is None
