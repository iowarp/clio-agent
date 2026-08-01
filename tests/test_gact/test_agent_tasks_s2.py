"""S2 (#948 / #950): AgentTask record + registry projection + agent.task.* events
+ task API.

Three layers:
* Unit — the record's status lifecycle (legal/illegal transitions, typed reason
  catalogs, wrong-input rejections), metadata round-trip, and the registry
  (index, wait-Event, consume, boot-fold).
* Integration — the task API over a real app, events on BOTH parent and child SSE
  channels, and the projection rebuilt from sessions.json across a restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    AgentTask,
    AgentTaskError,
    AgentTaskRegistry,
    persist_agent_task,
    seed_agent_task,
)
from clio_agent.gact.app import build_app
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskRecord,
    set_task_record_store,
)


class _Agent:
    def forward(self, question: str, session_id: str):
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


def _task(**kw) -> AgentTask:
    base = {
        "task_id": "task_1",
        "parent_session_id": "sess_parent",
        "child_session_id": "sess_child",
        "agent_ref": {"expert_id": "data_expert"},
        "created_at": "2026-07-17T00:00:00+00:00",
        "updated_at": "2026-07-17T00:00:00+00:00",
    }
    base.update(kw)
    return AgentTask(**base)


# --------------------------------------------------------------------------- #
# Unit: record + registry                                                      #
# --------------------------------------------------------------------------- #


def test_metadata_round_trip() -> None:
    task = _task(depth=2, agent_ref={"expert_id": "hpc", "blueprint_id": "bp"})
    meta = task.to_metadata()
    assert meta["session_type"] == "agent_task"

    class _Sess:
        metadata = meta

    assert AgentTask.from_session(_Sess()) == task

    # A non-agent-task session projects to None.
    class _Plain:
        metadata = {"session_type": "chat"}

    assert AgentTask.from_session(_Plain()) is None


def test_legal_transition_chain_sets_wait_event() -> None:
    reg = AgentTaskRegistry()
    reg.register(_task(status=STATUS_QUEUED))
    assert not reg.event("task_1").is_set()
    reg.transition("task_1", STATUS_RUNNING)
    assert reg.get("task_1").status == STATUS_RUNNING
    assert not reg.event("task_1").is_set()
    reg.transition("task_1", STATUS_COMPLETED, result={"answer_excerpt": "done"})
    assert reg.get("task_1").is_terminal
    assert reg.event("task_1").is_set()  # terminal -> wait primitive fires


@pytest.mark.parametrize(
    "start,target,reason",
    [
        # Terminal-start transitions are rejected by the immutability guard first.
        (STATUS_COMPLETED, STATUS_RUNNING, "already_terminal"),
        (STATUS_CANCELLED, STATUS_RUNNING, "already_terminal"),
        (STATUS_FAILED, STATUS_COMPLETED, "already_terminal"),
        # A non-terminal skip (queued straight to completed) is an illegal edge.
        (STATUS_QUEUED, STATUS_COMPLETED, "illegal_transition"),
    ],
)
def test_illegal_transitions_rejected(start: str, target: str, reason: str) -> None:
    reg = AgentTaskRegistry()
    reg.register(_task(status=start, error_reason="agent_error" if start == STATUS_FAILED else ""))
    with pytest.raises(AgentTaskError) as exc:
        reg.transition("task_1", target)
    assert exc.value.reason == reason


def test_typed_reason_and_input_rejections() -> None:
    reg = AgentTaskRegistry()
    reg.register(_task(status=STATUS_RUNNING))
    with pytest.raises(AgentTaskError) as e1:
        reg.transition("task_1", "bogus")
    assert e1.value.reason == "unknown_status"
    with pytest.raises(AgentTaskError) as e2:
        reg.transition("task_1", STATUS_FAILED, error_reason="not_a_real_reason")
    assert e2.value.reason == "unknown_error_reason"
    with pytest.raises(AgentTaskError) as e3:
        reg.transition("task_1", STATUS_FAILED)  # failed needs a typed reason
    assert e3.value.reason == "missing_error_reason"
    with pytest.raises(AgentTaskError) as e4:
        reg.transition("nope", STATUS_RUNNING)
    assert e4.value.reason == "unknown_task"


def test_terminal_records_are_immutable() -> None:
    """A same-status re-transition on a terminal record must NOT slip past the
    legality gate and clobber the settled result / re-fire the wait-Event."""

    reg = AgentTaskRegistry()
    reg.register(_task(status=STATUS_RUNNING))
    reg.transition("task_1", STATUS_COMPLETED, result={"answer_excerpt": "A"})
    with pytest.raises(AgentTaskError) as e1:
        reg.transition("task_1", STATUS_COMPLETED, result={"answer_excerpt": "B"})
    assert e1.value.reason == "already_terminal"
    assert reg.get("task_1").result == {"answer_excerpt": "A"}  # untouched
    with pytest.raises(AgentTaskError) as e2:
        reg.transition("task_1", STATUS_CANCELLED)  # terminal -> other terminal
    assert e2.value.reason == "already_terminal"


def test_boot_fold_skips_malformed_block_without_crashing() -> None:
    """One malformed agent_task block must not brick the boot fold (mirrors
    SessionStore._load's per-row resilience)."""

    reg = AgentTaskRegistry()

    class _Bad:
        id = "sess_bad"
        metadata = {
            "session_type": "agent_task",
            "agent_task": {"task_id": "x"},
        }  # missing required

    good = _task(task_id="task_ok")

    class _Good:
        id = "sess_good"
        metadata = good.to_metadata()

    n = reg.rebuild_from_sessions([_Bad(), _Good()])  # must NOT raise
    assert n == 1
    assert reg.get("task_ok") is not None
    assert reg.get("x") is None


def test_persist_raises_child_session_gone(tmp_path: Path) -> None:
    """persist_agent_task must not silently no-op when the authoritative store
    write finds no child session — it raises a typed reason instead of diverging."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        task = _task(task_id="task_gone", child_session_id="sess_missing")
        with pytest.raises(AgentTaskError) as exc:
            persist_agent_task(app, task)
        assert exc.value.reason == "child_session_gone"


def test_consume_requires_terminal() -> None:
    reg = AgentTaskRegistry()
    reg.register(_task(status=STATUS_RUNNING, notify_pending=True))
    with pytest.raises(AgentTaskError) as exc:
        reg.mark_consumed("task_1", "2026-07-17T01:00:00+00:00")
    assert exc.value.reason == "not_terminal"
    reg.transition("task_1", STATUS_COMPLETED, notify_pending=True)
    consumed = reg.mark_consumed("task_1", "2026-07-17T01:00:00+00:00")
    assert consumed.notify_pending is False
    assert consumed.consumed_at == "2026-07-17T01:00:00+00:00"


def test_rebuild_and_parent_index() -> None:
    reg = AgentTaskRegistry()

    class _Sess:
        def __init__(self, t):
            self.metadata = t.to_metadata()

    a = _task(task_id="task_a", parent_session_id="P", created_at="2026-07-17T00:00:01+00:00")
    b = _task(task_id="task_b", parent_session_id="P", created_at="2026-07-17T00:00:02+00:00")
    c = _task(task_id="task_c", parent_session_id="Q", created_at="2026-07-17T00:00:03+00:00")
    n = reg.rebuild_from_sessions([_Sess(a), _Sess(b), _Sess(c), type("X", (), {"metadata": {}})()])
    assert n == 3
    p_ids = [t.task_id for t in reg.for_parent("P")]
    assert p_ids == ["task_b", "task_a"]  # newest-created first
    assert [t.task_id for t in reg.for_parent("Q")] == ["task_c"]


# --------------------------------------------------------------------------- #
# Integration: API + events + restart-projection                              #
# --------------------------------------------------------------------------- #


def _bus(app, sid, event_type):
    return [e for e in app.state.bus._history.get(sid, []) if e.type == event_type]


def test_task_api_events_both_channels_and_cancel(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id

        # GET one + list-by-parent.
        got = client.get(f"/v1/agent-tasks/{task.task_id}")
        assert got.status_code == 200 and got.json()["task_id"] == task.task_id
        listed = client.get(f"/v1/sessions/{parent}/agent-tasks").json()["tasks"]
        assert [t["task_id"] for t in listed] == [task.task_id]

        # agent.task.queued observed on BOTH parent and child SSE channels.
        assert _bus(app, parent, "agent.task.queued"), "no queued event on parent channel"
        assert _bus(app, child, "agent.task.queued"), "no queued event on child channel"

        # Cancel -> cancelled (+ event on both channels), then idempotent.
        c1 = client.post(f"/v1/agent-tasks/{task.task_id}/cancel")
        assert c1.status_code == 200 and c1.json()["status"] == STATUS_CANCELLED
        assert _bus(app, parent, "agent.task.cancelled")
        assert _bus(app, child, "agent.task.cancelled")
        c2 = client.post(f"/v1/agent-tasks/{task.task_id}/cancel")  # idempotent
        assert c2.status_code == 200 and c2.json()["status"] == STATUS_CANCELLED

        # Unknown ids -> 404.
        assert client.get("/v1/agent-tasks/task_nope").status_code == 404
        assert client.get("/v1/sessions/sess_nope/agent-tasks").status_code == 404


def test_projection_rebuilt_from_sessions_json_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    app1 = build_app(sessions_path=path, agent=_Agent())
    with TestClient(app1) as c1:
        parent = c1.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(app1, parent_session_id=parent, agent_ref={"expert_id": "hpc"})
        task_id = task.task_id

    # A fresh app over the SAME sessions.json re-derives the projection at boot
    # (no fifth store) — the persisted child-session metadata IS the source.
    app2 = build_app(sessions_path=path, agent=_Agent())
    with TestClient(app2) as c2:
        got = c2.get(f"/v1/agent-tasks/{task_id}")
        assert got.status_code == 200, "task projection not rebuilt from sessions.json"
        assert got.json()["agent_ref"] == {"expert_id": "hpc"}
        assert [
            t["task_id"] for t in c2.get(f"/v1/sessions/{parent}/agent-tasks").json()["tasks"]
        ] == [task_id]


def test_runs_api_projects_local_and_relay_handles_with_live_state(tmp_path: Path) -> None:
    """#1127: the runs registry is a union view over the two existing stores."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    relay_store = InMemoryTaskRecordStore()
    set_task_record_store(relay_store, durable=False)
    try:
        with TestClient(app) as client:
            parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
            local = seed_agent_task(
                app,
                parent_session_id=parent,
                agent_ref={"expert_id": "data_expert"},
                status=STATUS_RUNNING,
                placement="local",
            )
            relay_store.put(
                TaskRecord(
                    key=TaskKey(
                        server_id="relay-ares",
                        session_id=parent,
                        task_id="task_relay_only",
                    ),
                    tool="relay_submit_remote_agent",
                    backend={"cluster": "ares", "transport": "relay"},
                    status="working",
                    created_at="2026-08-01T00:00:00+00:00",
                )
            )

            response = client.get("/v1/runs")
            assert response.status_code == 200
            rows = {row["handle_id"]: row for row in response.json()["runs"]}
            assert set(rows) == {local.task_id, "task_relay_only"}
            assert rows[local.task_id]["placement"] == "local"
            assert rows[local.task_id]["live_state"] == "running"
            assert rows["task_relay_only"]["placement"] == "relay:ares"
            assert rows["task_relay_only"]["live_state"] == "running"

            detached = client.post(f"/v1/runs/{local.task_id}/detach")
            assert detached.status_code == 200
            assert detached.json()["detached"] is True
            assert app.state.agent_task_registry.get(local.task_id).status == STATUS_RUNNING

            dismissed = client.post("/v1/runs/task_relay_only/dismiss")
            assert dismissed.status_code == 200
            assert dismissed.json() == {"dismissed": True, "handle_id": "task_relay_only"}
            remaining = {row["handle_id"] for row in client.get("/v1/runs").json()["runs"]}
            assert remaining == {local.task_id}
    finally:
        set_task_record_store(None)
