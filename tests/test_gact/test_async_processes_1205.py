"""#1205: the session-scoped async-processes union (agents + durable MCP tasks).

Two layers, both against a REAL ``build_app`` (the durable ``SessionMetadataTaskStore``
+ event bus wiring only exists once a server boots, not in a bare unit-constructed
store):

* The route ``GET /v1/sessions/{sid}/async-processes`` unions ``AgentTask`` rows
  (``kind="agent"``) with durable ``TaskRecord`` rows (``kind="mcp-task"``), deduping
  a relay-backed AgentTask's own TaskRecord row the same way ``run_registry.py``
  does for the (separate) global runs projection.
* Every ``TaskRecordStore.put`` fires an ``mcp_task.*`` event onto the OWNING
  session's bus channel — the same channel ``GET /v1/sessions/{sid}/events``
  already streams — so the tray refreshes live instead of polling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.errors import MCP_TASK_RECORD_HELD_LOCALLY
from clio_agent.gact.agent_tasks import STATUS_RUNNING, seed_agent_task
from clio_agent.gact.app import build_app
from clio_agent.gact.routes.async_processes import project_session_async_processes
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, task_record_store


class _Agent:
    def forward(self, question: str, session_id: str, **_kwargs: Any) -> Any:
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


def _build(tmp_path: Path):
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=_Agent())
    return app


# --------------------------------------------------------------------------- #
# Route: kind-discriminated union                                             #
# --------------------------------------------------------------------------- #


def test_route_404s_for_unknown_session(tmp_path: Path) -> None:
    app = _build(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/sessions/sess_missing/async-processes")
    assert response.status_code == 404
    assert response.json()["error"]["error"] == "not_found"


def test_route_returns_empty_processes_for_a_session_with_none(tmp_path: Path) -> None:
    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        response = client.get(f"/v1/sessions/{sid}/async-processes")
    assert response.status_code == 200
    assert response.json() == {"processes": []}


def test_route_unions_agent_and_mcp_task_rows_with_kind_discriminator(tmp_path: Path) -> None:
    """A session with one spawned agent AND one durable jarvis-style task record
    gets BOTH back from the ONE new route, each correctly kind-tagged."""

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]

        agent_task = seed_agent_task(
            app,
            parent_session_id=sid,
            agent_ref={"expert_id": "data_expert"},
            status=STATUS_RUNNING,
        )

        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-task-1")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        response = client.get(f"/v1/sessions/{sid}/async-processes")

    assert response.status_code == 200
    processes = response.json()["processes"]
    kinds = {row["id"]: row["kind"] for row in processes}
    assert kinds == {agent_task.task_id: "agent", "jarvis-task-1": "mcp-task"}

    agent_row = next(row for row in processes if row["kind"] == "agent")
    assert agent_row["status"] == STATUS_RUNNING
    assert agent_row["title"]  # display_run_name always yields a non-empty label

    mcp_row = next(row for row in processes if row["kind"] == "mcp-task")
    assert mcp_row["title"] == "jarvis_run"
    assert mcp_row["status"] == "working"
    assert mcp_row["live_state"] == "running"
    assert mcp_row["updated_at"], "put() must stamp updated_at on every write"
    assert mcp_row["key"]["task_id"] == "jarvis-task-1"


def test_route_dedupes_a_relay_backed_agent_tasks_own_task_record(tmp_path: Path) -> None:
    """A ``relay_submit_remote_agent`` spawn has BOTH an AgentTask row and a durable
    TaskRecord row for the SAME task id. The union must show it once, as kind="agent",
    never twice — the identical dedupe idiom ``run_registry.project_runs`` uses."""

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]

        agent_task = seed_agent_task(
            app,
            parent_session_id=sid,
            agent_ref={"expert_id": "hpc_expert"},
            status=STATUS_RUNNING,
            placement="relay:ares",
        )
        mirrored_key = TaskKey(
            server_id="relay-ares", session_id=sid, task_id=agent_task.task_id
        )
        task_record_store().put(
            TaskRecord(key=mirrored_key, tool="relay_submit_remote_agent", status="working")
        )

        response = client.get(f"/v1/sessions/{sid}/async-processes")

    processes = response.json()["processes"]
    matching = [row for row in processes if row["id"] == agent_task.task_id]
    assert len(matching) == 1
    assert matching[0]["kind"] == "agent"


def test_project_session_async_processes_excludes_other_sessions(tmp_path: Path) -> None:
    """A task record scoped to a DIFFERENT session never leaks into this one's list."""

    app = _build(tmp_path)
    with TestClient(app):
        sid_a = app.state.sessions.create(workspace_id="ws", title="a").id
        sid_b = app.state.sessions.create(workspace_id="ws", title="b").id
        key = TaskKey(server_id="relay-ares", session_id=sid_b, task_id="task-b")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        assert project_session_async_processes(app, sid_a) == []
        assert len(project_session_async_processes(app, sid_b)) == 1


# --------------------------------------------------------------------------- #
# SSE: every TaskRecord mutation publishes onto the owning session's channel  #
# --------------------------------------------------------------------------- #


def test_put_publishes_an_mcp_task_event_on_the_owning_session_bus(tmp_path: Path) -> None:
    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-task-2")

        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        events = app.state.bus.session_events_since(sid, cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    assert len(mcp_events) == 1
    assert mcp_events[0].type == "mcp_task.updated"  # "working" has no terminal mapping
    assert mcp_events[0].payload["key"]["task_id"] == "jarvis-task-2"
    assert mcp_events[0].payload["status"] == "working"


@pytest.mark.parametrize(
    ("status", "expected_event_type"),
    [
        ("completed", "mcp_task.completed"),
        ("failed", "mcp_task.failed"),
        ("cancelled", "mcp_task.cancelled"),
    ],
)
def test_put_publishes_the_typed_terminal_event_per_status(
    tmp_path: Path, status: str, expected_event_type: str
) -> None:
    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id=f"jarvis-{status}")

        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status=status))

        events = app.state.bus.session_events_since(sid, cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    assert [e.type for e in mcp_events] == [expected_event_type]


def test_put_for_an_unattributed_record_publishes_nothing(tmp_path: Path) -> None:
    """A held (session-less) record has no channel to publish to — a quiet no-op,
    not a second report of the store's own already-typed holding-path degrade."""

    app = _build(tmp_path)
    with TestClient(app):
        key = TaskKey(server_id="relay-ares", session_id=None, task_id="orphan-task")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        # No session id means no session channel: nothing to assert per-session,
        # but the global ("") channel must not have picked it up either.
        assert app.state.bus.session_events_since("", cursor=1) == []


# --------------------------------------------------------------------------- #
# #1205 adversarial review D1/D2 rework (BLOCKING) -- failing-first           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected_event_type"),
    [
        ("completed", "mcp_task.completed"),
        ("failed", "mcp_task.failed"),
    ],
)
def test_drop_publishes_the_terminal_event_before_the_record_leaves_the_store(
    tmp_path: Path, status: str, expected_event_type: str
) -> None:
    """D1 (BLOCKING): a settled task is almost always REMOVED via ``drop`` (the
    real drivers persist the terminal status via ``put`` then drop the now-
    settled row, mirroring ``_poll_until_terminal`` in tools/mcp_tasks.py) —
    before the fix, ``drop`` published NOTHING, so a live SSE subscriber never
    learned the task settled; the row just vanished from the next GET with no
    explanation. This drives the SAME sequence a real driver does and asserts
    drop's own publish arrives, carrying the record's full final state, before
    the row is gone from the store.
    """

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id=f"jarvis-{status}")
        store = task_record_store()

        store.put(TaskRecord(key=key, tool="jarvis_run", status="working"))
        store.put(TaskRecord(key=key, tool="jarvis_run", status=status))
        store.drop(key)

        assert store.get(key) is None, "the row must actually be gone after drop"

        events = app.state.bus.session_events_since(sid, cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    # working -> mcp_task.updated (put), status -> the typed terminal event
    # (put), then drop's OWN republish of that same final state -- the
    # required guarantee is that drop's publish exists at all; a caller that
    # already persisted the terminal status gets one harmless extra idempotent
    # copy of the identical event (this codebase's SSE consumers apply the
    # full record verbatim, never a delta).
    assert [e.type for e in mcp_events] == [
        "mcp_task.updated",
        expected_event_type,
        expected_event_type,
    ]
    final_event = mcp_events[-1]
    assert final_event.payload["key"]["task_id"] == f"jarvis-{status}"
    assert final_event.payload["status"] == status


def test_drop_of_an_unknown_key_publishes_nothing(tmp_path: Path) -> None:
    """A key that was never stored has no final state to describe -- a quiet
    no-op, not a fabricated event."""

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="never-existed")

        task_record_store().drop(key)

        events = app.state.bus.session_events_since(sid, cursor=1)
        assert [e for e in events if e.type.startswith("mcp_task.")] == []


class _AckOnlyCancelSession:
    """A minimal fake ``ClientSession`` answering ``tasks/cancel`` with a bare ack."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def send_request(
        self,
        request: Any,
        result_type: Any,
        request_read_timeout_seconds: float | None = None,
    ) -> Any:
        self.methods.append(request.method)
        return result_type()


async def test_cancel_task_publishes_mcp_task_cancelled(tmp_path: Path) -> None:
    """D1 (BLOCKING), the explicitly-named "including cancel" case: ``tasks/cancel``
    is ack-only with NO later ``tasks/get`` to ever observe a terminal status —
    before the fix, ``cancel_task`` dropped the record WITHOUT ever stamping
    ``status="cancelled"`` first, so even a drop() that publishes something would
    have republished the STALE pre-cancel status instead of the real transition.
    """

    from clio_agent.tools.mcp_tasks import cancel_task  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-cancel-1")
        store = task_record_store()
        store.put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        session = _AckOnlyCancelSession()
        await cancel_task(session, key)

        assert session.methods == ["tasks/cancel"]
        assert store.get(key) is None, "cancel still drops the record"

        events = app.state.bus.session_events_since(sid, cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    # working -> mcp_task.updated, cancel's own status stamp -> mcp_task.cancelled
    # (put), then drop's republish of the same cancelled state.
    assert [e.type for e in mcp_events] == [
        "mcp_task.updated",
        "mcp_task.cancelled",
        "mcp_task.cancelled",
    ]
    assert mcp_events[-1].payload["status"] == "cancelled"
    assert mcp_events[-1].payload["key"]["task_id"] == "jarvis-cancel-1"


def test_hold_path_publish_carries_holding_reason(tmp_path: Path) -> None:
    """D2 (BLOCKING): the pre-fix code notified with the PRE-hold record —
    ``holding_reason`` (the field that makes a holding-path degrade non-silent
    to an SSE subscriber) was silently stripped from the published payload. This
    drives a ``put`` whose ``session_id`` IS set but whose session row does not
    exist (a genuine holding-path degrade, distinct from the session_id=None
    case covered above) and asserts the published payload's ``holding_reason``
    equals the value on the record actually left in the store.
    """

    app = _build(tmp_path)
    with TestClient(app):
        key = TaskKey(
            server_id="relay-ares", session_id="sess_never_created", task_id="jarvis-orphan"
        )
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        stored = task_record_store().get(key)
        assert stored is not None
        assert stored.holding_reason == MCP_TASK_RECORD_HELD_LOCALLY

        events = app.state.bus.session_events_since("sess_never_created", cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    assert len(mcp_events) == 1
    assert mcp_events[0].payload["holding_reason"] == MCP_TASK_RECORD_HELD_LOCALLY
    assert mcp_events[0].payload["holding_reason"] == stored.holding_reason
