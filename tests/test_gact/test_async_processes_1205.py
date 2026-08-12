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

from clio_agent.errors import MCP_TASK_RECORD_HELD_LOCALLY, MCP_TASK_SESSION_DELETED
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
    """D1 (BLOCKING, 1st round): ``drop`` is the EXPLICIT dismiss action (#1205
    review, 2nd round: real drivers no longer drop a task at settle — see the
    retention tests below — a task is only ever dropped by an explicit later
    dismiss, ``run_registry.dismiss_run``). Before the 1st-round fix, ``drop``
    published NOTHING, so a live SSE subscriber never learned a dismissed task
    was gone; the row just vanished from the next GET with no explanation.
    This drives the same put-then-drop sequence a dismiss produces and asserts
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
    """D1 (BLOCKING, 1st round), the explicitly-named "including cancel" case:
    ``tasks/cancel`` is ack-only with NO later ``tasks/get`` to ever observe a
    terminal status — ``cancel_task`` stamps ``status="cancelled"`` itself so
    the published event carries the real transition, not a stale pre-cancel
    status.

    #1205 review D1 (2nd round): ``cancel_task`` no longer drops the record —
    RETAINED with its terminal status, so exactly ONE ``mcp_task.cancelled``
    fires here (the earlier round's second, drop-triggered copy is gone along
    with the drop itself; drop is now an explicit, separate dismiss action,
    covered above and in the route-level retention tests below).
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
        settled = store.get(key)
        assert settled is not None, "cancel retains the record"
        assert settled.status == "cancelled"

        events = app.state.bus.session_events_since(sid, cursor=1)
        mcp_events = [e for e in events if e.type.startswith("mcp_task.")]

    # working -> mcp_task.updated, cancel's own status stamp -> mcp_task.cancelled.
    # No drop, so no second copy.
    assert [e.type for e in mcp_events] == ["mcp_task.updated", "mcp_task.cancelled"]
    assert mcp_events[-1].payload["status"] == "cancelled"
    assert mcp_events[-1].payload["key"]["task_id"] == "jarvis-cancel-1"


# --------------------------------------------------------------------------- #
# #1205 review, 2nd round D1 (BLOCKING): retention -- the route must keep      #
# showing a settled mcp-task row until an explicit dismiss. Bus-only          #
# assertions don't prove this; every test below asserts on the ROUTE's own    #
# response.                                                                   #
# --------------------------------------------------------------------------- #


async def test_route_retains_a_cancelled_task_until_dismissed(tmp_path: Path) -> None:
    """Drives a task to settle through the REAL driver path (``cancel_task``,
    the same function the relay/jarvis cancel tool calls), then asserts on
    what ``GET /v1/sessions/{sid}/async-processes`` actually returns: the
    settled row present with its terminal status (the tray's "recently
    finished" section needs this to render at all), then gone once the
    existing dismiss control (``POST /v1/runs/{handle_id}/dismiss``,
    ``run_registry.dismiss_run``) removes it.
    """

    from clio_agent.tools.mcp_tasks import cancel_task  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-cancel-route")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        await cancel_task(_AckOnlyCancelSession(), key)

        response = client.get(f"/v1/sessions/{sid}/async-processes")
        processes = response.json()["processes"]
        row = next((r for r in processes if r["id"] == "jarvis-cancel-route"), None)
        assert row is not None, "a settled mcp-task must still be in the tray, not vanished"
        assert row["kind"] == "mcp-task"
        assert row["status"] == "cancelled"

        dismiss = client.post("/v1/runs/jarvis-cancel-route/dismiss")
        assert dismiss.status_code == 200

        after_dismiss = client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"]
        assert all(r["id"] != "jarvis-cancel-route" for r in after_dismiss)


async def test_route_retains_a_completed_task_until_dismissed_via_the_real_poll_driver(
    tmp_path: Path,
) -> None:
    """Same retention contract, driven through the REAL poll-to-terminal loop
    (``resume_task`` / ``_poll_until_terminal`` in tools/mcp_tasks.py) rather
    than a hand-rolled ``put`` — the actual code path a reconnect or a fresh
    ``wait`` uses to drive a task to completion.
    """

    from mcp.types import Result as McpResult

    from clio_agent.tools.mcp_tasks import resume_task  # noqa: PLC0415 - test-local

    class _TerminalPollSession:
        """Answers ONE ``tasks/get`` with a completed task, nothing else."""

        methods: list[str]

        def __init__(self) -> None:
            self.methods = []

        async def send_request(
            self,
            request: Any,
            result_type: Any,
            request_read_timeout_seconds: float | None = None,
        ) -> Any:
            self.methods.append(request.method)
            if request.method == "tasks/get":
                return result_type.model_validate(
                    {
                        "taskId": "jarvis-completed-route",
                        "status": "completed",
                        "createdAt": "2026-08-12T00:00:00+00:00",
                        "lastUpdatedAt": "2026-08-12T00:00:00+00:00",
                        "resultType": "complete",
                        "result": {"content": []},
                    }
                )
            return McpResult()

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-completed-route")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        final = await resume_task(_TerminalPollSession(), key)
        assert final.status == "completed"

        processes = client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"]
        row = next((r for r in processes if r["id"] == "jarvis-completed-route"), None)
        assert row is not None, "a settled mcp-task must still be in the tray, not vanished"
        assert row["status"] == "completed"

        dismiss = client.post("/v1/runs/jarvis-completed-route/dismiss")
        assert dismiss.status_code == 200

        after_dismiss = client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"]
        assert all(r["id"] != "jarvis-completed-route" for r in after_dismiss)


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


async def test_hold_preserves_a_more_specific_existing_reason(tmp_path: Path) -> None:
    """BLOCKING, new-in-rework: ``_hold`` was unconditionally overwriting an
    existing ``holding_reason`` with the generic ``MCP_TASK_RECORD_HELD_LOCALLY``
    — the reviewer-named case: a task already held with
    ``MCP_TASK_SESSION_DELETED`` (its session was deleted out from under it,
    via ``on_session_deleted``) that then gets ``cancel_task`` called on it.
    ``cancel_task``'s own ``put`` lands back on the SAME hold path (the session
    is still gone), and must not lose the more specific diagnosis to the
    generic one just because it passed through ``_hold`` a second time.
    """

    from clio_agent.tools.mcp_tasks import cancel_task  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "doomed"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-orphaned")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        # Delete the session out from under the live task: on_session_deleted
        # migrates it to the holding path stamped MCP_TASK_SESSION_DELETED.
        delete_response = client.delete(f"/v1/sessions/{sid}")
        assert delete_response.status_code == 204

        held = task_record_store().get(key)
        assert held is not None
        assert held.holding_reason == MCP_TASK_SESSION_DELETED

        # Cancel the now-orphaned task. Its key still names the deleted
        # session, so cancel_task's put() re-enters the SAME "session row
        # absent" hold path — the specific reason must survive that.
        await cancel_task(_AckOnlyCancelSession(), key)

    after_cancel = task_record_store().get(key)
    assert after_cancel is not None
    assert after_cancel.status == "cancelled"
    assert after_cancel.holding_reason == MCP_TASK_SESSION_DELETED, (
        "a more specific existing reason must survive a second hold, not be "
        "downgraded to the generic mcp_task_record_held_locally"
    )


# --------------------------------------------------------------------------- #
# #1205 review, 3rd round: dismiss must be safe (terminality-guarded,          #
# composite-key-precise) AND reachable, or retention is unbounded,             #
# unclearable accumulation in sessions.json.                                   #
# --------------------------------------------------------------------------- #


def test_dismiss_refuses_a_non_terminal_mcp_task(tmp_path: Path) -> None:
    """BLOCKING, item 1: ``POST /v1/runs/{task_id}/dismiss`` on a WORKING task
    must never drop it — that would delete the only durable local handle to a
    still-running remote task, violating ``mcp_task_store.py``'s own
    crash-recovery module contract. The old tool-name filter only incidentally
    protected this by accident, not by design; a dismiss request against a
    non-terminal task is refused (``False``, same shape as "no match"), and the
    record is left completely intact.
    """

    from clio_agent.gact.run_registry import dismiss_run  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-working")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        result = dismiss_run(app, "jarvis-working")

    assert result is False
    record = task_record_store().get(key)
    assert record is not None
    assert record.status == "working"


def test_dismiss_touches_only_the_matched_composite_key(tmp_path: Path) -> None:
    """BLOCKING, item 2: ``row_key`` is ``f"{server_id}|{task_id}"`` precisely
    because two backends can legitimately mint the same task id
    (``mcp_task_records.py``'s own module contract; ``cancel_task``'s docstring
    states the identical invariant, guarded by
    ``test_cancel_stamps_only_the_named_identity``). Dismissing one server's
    settled task must never sweep an unrelated backend's same-task_id record as
    collateral damage — exactly ONE composite key is dropped, and the survivor
    is untouched (not merely "still terminal": byte-identical to its pre-dismiss
    snapshot).
    """

    from clio_agent.gact.run_registry import dismiss_run  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key_a = TaskKey(server_id="relay-ares", session_id=sid, task_id="shared-id")
        key_b = TaskKey(server_id="relay-metis", session_id=sid, task_id="shared-id")
        task_record_store().put(TaskRecord(key=key_a, tool="jarvis_run", status="completed"))
        task_record_store().put(TaskRecord(key=key_b, tool="jarvis_run", status="completed"))

        before_a = task_record_store().get(key_a)
        before_b = task_record_store().get(key_b)
        assert before_a is not None
        assert before_b is not None

        result = dismiss_run(app, "shared-id")

        after_a = task_record_store().get(key_a)
        after_b = task_record_store().get(key_b)

    assert result is True
    assert (after_a is None) != (after_b is None), (
        "exactly one of the two colliding composite keys must be gone, never both"
    )
    if after_a is not None:
        assert after_a == before_a, "the surviving record must be byte-identical, never re-written"
    else:
        assert after_b == before_b, "the surviving record must be byte-identical, never re-written"


async def test_dismiss_removes_a_settled_mcp_task_from_both_async_processes_and_runs(
    tmp_path: Path,
) -> None:
    """BLOCKING, item 3: retention (#1205 2nd round) without a REACHABLE dismiss
    is unbounded, unclearable accumulation in ``sessions.json`` (``SessionStore.update``
    rewrites the whole row on every mutation). Proves the full path end to end: a
    task settles through the real driver (``cancel_task``), the settled row is
    visible on BOTH ``GET /v1/sessions/{sid}/async-processes`` (the tray) AND
    ``GET /v1/runs`` (``project_runs``, widened the same way ``dismiss_run`` was
    — the surface that OWNS the dismiss control must actually list what it can
    dismiss), the tray's own dismiss path (``POST /v1/runs/{id}/dismiss``) removes
    it, and it is then gone from BOTH listings.
    """

    from clio_agent.tools.mcp_tasks import cancel_task  # noqa: PLC0415 - test-local

    app = _build(tmp_path)
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
        key = TaskKey(server_id="relay-ares", session_id=sid, task_id="jarvis-reachable")
        task_record_store().put(TaskRecord(key=key, tool="jarvis_run", status="working"))

        await cancel_task(_AckOnlyCancelSession(), key)

        processes = client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"]
        assert any(r["id"] == "jarvis-reachable" for r in processes), (
            "a settled mcp-task must be visible in the session-scoped tray"
        )
        runs = client.get("/v1/runs").json()["runs"]
        assert any(r["handle_id"] == "jarvis-reachable" for r in runs), (
            "project_runs must be widened the same way dismiss_run was — the "
            "listing that owns the dismiss control must actually serve this row"
        )

        dismiss = client.post("/v1/runs/jarvis-reachable/dismiss")
        assert dismiss.status_code == 200

        processes_after = client.get(f"/v1/sessions/{sid}/async-processes").json()["processes"]
        assert all(r["id"] != "jarvis-reachable" for r in processes_after)
        runs_after = client.get("/v1/runs").json()["runs"]
        assert all(r["handle_id"] != "jarvis-reachable" for r in runs_after)
