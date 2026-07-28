"""#1037 (epic #1031 Pillar 2, slice 3/3 — CLOSES Pillar 2): human-facing LIVE
execution handle.

Covers the seams the slice adds:

* :func:`project_live_handle` is a PURE read-only projection — it assembles
  task + timeline + handoff parts + bounded child head from existing stores and
  mutates NOTHING (task record, ``notify_pending``, registry, bus history all
  identical before/after).
* Ensemble handoff attribution: the SAME expert spawned N times in one parent turn
  is disambiguated by ``run_index`` — the projection picks the RIGHT parent-transcript
  ``expert_handoff`` Part.
* ``GET /v1/agent-tasks/{id}/live`` returns the handle for a running task and
  tolerates a gone child gracefully (empty head/timeline, record still returned).
* ``POST /v1/agent-tasks/{id}/steer`` enqueues into the CHILD inbox (202) only for a
  genuinely running child; a terminal / idle / gone child is refused with a typed
  409 ``child_not_running`` (never a silently stranded buffer).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import (
    STATUS_CANCELLED,
    persist_agent_task,
    seed_agent_task,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.live_handle import (
    _CHILD_HEAD_MAX,
    _CHILD_HEAD_PART_TEXT_MAX,
    STEER_REJECT_CHILD_GONE,
    STEER_REJECT_CHILD_IDLE,
    STEER_REJECT_TASK_TERMINAL,
    project_live_handle,
)
from clio_agent.gact.loop_inbox import USER_STEER_MARKER
from clio_agent.gact.types import Message, Part


class _Agent:
    def forward(self, question: str, session_id: str):
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _handoff_part(child_agent: str, run_index: int, stage: str, pid: str) -> Part:
    return Part(
        id=pid,
        type="expert_handoff",
        agent_id="main",
        parent_agent="main",
        child_agent=child_agent,
        stage=stage,
        status="running",
        text=f"main -> {child_agent}",
        metadata={"run_index": run_index, "question": f"run {run_index}"},
    )


def _assistant_msg(session_id: str, mid: str, parts: list[Part], turn_id: str = "") -> Message:
    return Message(
        id=mid,
        turn_id=turn_id,
        session_id=session_id,
        role="assistant",
        created_at=_now(),
        updated_at=_now(),
        parts=parts,
    )


def _mark_running(app, child_sid: str) -> None:
    """Simulate a genuinely-running child turn (shadow ``turn_runner.busy``)."""

    app.state.turn_runner.busy = lambda sid, _c=child_sid: sid == _c


# --------------------------------------------------------------------------- #
# project_live_handle: pure read-only assembly + attribution                  #
# --------------------------------------------------------------------------- #


def test_project_live_handle_assembles_with_zero_mutation(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id

        # Parent transcript carries the started handoff Part for this run.
        app.state.messages[parent] = [
            _assistant_msg(
                parent, "m1", [_handoff_part("data_expert", 0, "delegate.started", "p1")]
            )
        ]
        # Child transcript carries a model-authored message (the head).
        app.state.messages[child] = [
            _assistant_msg(child, "c1", [Part(id="t1", type="text", text="child working")])
        ]

        # Snapshot mutable state BEFORE the projection.
        before_task = app.state.agent_task_registry.get(task.task_id)
        before_notify = before_task.notify_pending
        before_bus_len = len(app.state.bus._history.get(child, []))

        handle = project_live_handle(app, task.task_id)
        assert handle is not None
        assert handle.task["task_id"] == task.task_id
        # timeline: the child channel's agent.task.queued lifecycle event.
        assert [e["type"] for e in handle.timeline] == ["agent.task.queued"]
        # handoff: exactly the started Part for this run.
        assert [p["id"] for p in handle.handoff_parts] == ["p1"]
        # child head: labeled + bounded, carries the child message.
        assert handle.child_head["label"]
        assert handle.child_head["truncated"] is False
        assert [m["id"] for m in handle.child_head["messages"]] == ["c1"]
        # actions: the 4-mode async menu.
        assert {a["mode"] for a in handle.actions} == {"observe", "wait", "cancel", "steer"}

        # ZERO mutation: record identity, notify_pending, and bus history unchanged.
        after_task = app.state.agent_task_registry.get(task.task_id)
        assert after_task == before_task
        assert after_task.notify_pending == before_notify
        assert len(app.state.bus._history.get(child, [])) == before_bus_len


def test_ensemble_handoff_attribution_by_run_index(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        # Two runs of the SAME expert (an ensemble) — distinct task records.
        t0 = seed_agent_task(app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"})
        t1 = seed_agent_task(app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"})
        # Give t1 run_index=1 (re-persist through the registry so the projection sees it).
        from dataclasses import replace  # noqa: PLC0415

        t1 = replace(t1, run_index=1)
        persist_agent_task(app, t1)

        # Parent transcript carries BOTH runs' started Parts (same child_agent).
        app.state.messages[parent] = [
            _assistant_msg(
                parent,
                "m1",
                [
                    _handoff_part("data_expert", 0, "delegate.started", "run0"),
                    _handoff_part("data_expert", 1, "delegate.started", "run1"),
                ],
            )
        ]

        h0 = project_live_handle(app, t0.task_id)
        h1 = project_live_handle(app, t1.task_id)
        assert [p["id"] for p in h0.handoff_parts] == ["run0"]
        assert [p["id"] for p in h1.handoff_parts] == ["run1"]


def test_cross_turn_handoff_not_misattributed(tmp_path: Path) -> None:
    """The attribution key is (child_agent, parent_turn_id, run_index) — NOT just
    (child_agent, run_index). run_index resets per parent turn, so a later turn that
    re-delegates to the SAME expert with the SAME run_index must NOT collect the earlier
    turn's Parts. Without the parent_turn_id scope, project(tB) returns turnA's Part too."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        # Same expert delegated in TWO different turns, each at run_index 0.
        tA = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "data_expert"},
            parent_turn_id="turnA",
        )
        tB = seed_agent_task(
            app,
            parent_session_id=parent,
            agent_ref={"expert_id": "data_expert"},
            parent_turn_id="turnB",
        )
        # Each turn's started Part lives in a Message stamped with that turn's id.
        app.state.messages[parent] = [
            _assistant_msg(
                parent, "mA", [_handoff_part("data_expert", 0, "delegate.started", "A0")], "turnA"
            ),
            _assistant_msg(
                parent, "mB", [_handoff_part("data_expert", 0, "delegate.started", "B0")], "turnB"
            ),
        ]
        hA = project_live_handle(app, tA.task_id)
        hB = project_live_handle(app, tB.task_id)
        assert [p["id"] for p in hA.handoff_parts] == ["A0"], "turnA task gets only turnA's Part"
        assert [p["id"] for p in hB.handoff_parts] == ["B0"], "turnB task must NOT collect turnA's"


def test_project_live_handle_unknown_task_is_none(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        assert project_live_handle(app, "task_nope") is None


def test_child_head_bounded_and_truncated(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id
        n = _CHILD_HEAD_MAX + 5
        app.state.messages[child] = [
            _assistant_msg(child, f"c{i}", [Part(id=f"t{i}", type="text", text=str(i))])
            for i in range(n)
        ]
        handle = project_live_handle(app, task.task_id)
        assert handle.child_head["total"] == n
        assert handle.child_head["returned"] == _CHILD_HEAD_MAX
        assert handle.child_head["truncated"] is True
        # The LAST N messages (the head), in order.
        assert [m["id"] for m in handle.child_head["messages"]] == [
            f"c{i}" for i in range(n - _CHILD_HEAD_MAX, n)
        ]


def test_child_head_bounds_oversized_part_text(tmp_path: Path) -> None:
    """Bounding the message COUNT is not enough — a single oversized child text part
    is truncated to _CHILD_HEAD_PART_TEXT_MAX with an explicit text_truncated flag."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p").id
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id
        huge = "x" * (_CHILD_HEAD_PART_TEXT_MAX + 500)
        app.state.messages[child] = [
            _assistant_msg(child, "c1", [Part(id="t1", type="text", text=huge)])
        ]
        handle = project_live_handle(app, task.task_id)
        part = handle.child_head["messages"][0]["parts"][0]
        assert part["text_truncated"] is True
        assert len(part["text"]) <= _CHILD_HEAD_PART_TEXT_MAX + len("…[truncated]")


# --------------------------------------------------------------------------- #
# GET /live route                                                             #
# --------------------------------------------------------------------------- #


def test_get_live_route_and_gone_child_tolerated(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(app, parent_session_id=parent, agent_ref={"expert_id": "hpc"})

        got = client.get(f"/v1/agent-tasks/{task.task_id}/live")
        assert got.status_code == 200
        body = got.json()
        assert body["task"]["task_id"] == task.task_id
        assert [e["type"] for e in body["timeline"]] == ["agent.task.queued"]

        # Gone child: the projection tolerates it (empty head, record still kept).
        # (The bus replay history is independent of session deletion, so the
        # lifecycle timeline may persist — the point is the record + head are safe.)
        app.state.sessions.delete(task.child_session_id)
        gone = client.get(f"/v1/agent-tasks/{task.task_id}/live")
        assert gone.status_code == 200
        gbody = gone.json()
        assert gbody["task"]["task_id"] == task.task_id
        assert gbody["child_head"]["messages"] == []

        # Unknown task -> typed 404.
        assert client.get("/v1/agent-tasks/task_nope/live").status_code == 404


# --------------------------------------------------------------------------- #
# POST /steer route                                                           #
# --------------------------------------------------------------------------- #


def test_steer_running_child_enqueues_into_child_inbox(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id
        _mark_running(app, child)

        resp = client.post(
            f"/v1/agent-tasks/{task.task_id}/steer",
            json={"text": "focus on LA stations", "metadata": {"k": "v"}},
        )
        assert resp.status_code == 202
        assert resp.json()["child_session_id"] == child

        # The steer landed on the CHILD inbox (not the parent), as a user_message.
        inbox = app.state.loop_inboxes.get(child)
        assert inbox is not None
        events = inbox.drain()
        assert len(events) == 1
        assert events[0].kind == "user_message"
        assert events[0].text == "focus on LA stations"
        assert events[0].metadata == {"k": "v"}
        # Nothing was enqueued onto the parent inbox.
        assert app.state.loop_inboxes.get(parent) is None


def test_steer_rejects_reserved_metadata_key(tmp_path: Path) -> None:
    """#1057 B2 (BLOCKER): the agent-task steer is a THIRD client-writable ingest onto a
    turn's ``user_msg.metadata`` (POST /messages + /retry are the other two). A client
    smuggling a reserved turn-control key (``hook_defer_resume``) via ``metadata`` would
    ride ``enqueue_user_steer`` onto the CHILD inbox and — if the child turn ends before
    the drain — into the promoted turn's ``user_msg.metadata``, making the
    UserPromptSubmit once-gate skip hook dispatch. The steer is rejected 400 (typed
    ``reserved_metadata_key``), NOT stripped, and NOTHING is enqueued."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id
        _mark_running(app, child)

        resp = client.post(
            f"/v1/agent-tasks/{task.task_id}/steer",
            json={"text": "focus on LA", "metadata": {"hook_defer_resume": True}},
        )
        assert resp.status_code == 400
        inner = resp.json()["error"]
        assert inner["error"] == "reserved_metadata_key"
        assert inner["details"]["reserved_keys"] == ["hook_defer_resume"]
        assert inner["details"]["session_id"] == child
        # The reserved key was rejected, never smuggled — nothing buffered on the child.
        assert app.state.loop_inboxes.get(child) is None


def test_steer_benign_metadata_still_accepted(tmp_path: Path) -> None:
    """A steer with benign (non-reserved) metadata is unaffected by the B2 guard: it is
    accepted 202 and enqueued onto the child inbox verbatim."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        child = task.child_session_id
        _mark_running(app, child)

        resp = client.post(
            f"/v1/agent-tasks/{task.task_id}/steer",
            json={"text": "focus on LA", "metadata": {"note": "human steer"}},
        )
        assert resp.status_code == 202
        inbox = app.state.loop_inboxes.get(child)
        assert inbox is not None
        events = inbox.drain()
        assert len(events) == 1
        assert events[0].metadata == {"note": "human steer"}


def test_steer_empty_text_rejected_422(tmp_path: Path) -> None:
    """An empty/whitespace steer would enqueue an empty ### steer block — reject 422,
    and enqueue NOTHING (even for a genuinely running child)."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        _mark_running(app, task.child_session_id)
        resp = client.post(f"/v1/agent-tasks/{task.task_id}/steer", json={"text": "   "})
        assert resp.status_code == 422
        assert resp.json()["error"]["error"] == "invalid_request"
        assert app.state.loop_inboxes.get(task.child_session_id) is None


def test_steer_terminal_child_rejected_typed_409(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        # Settle the task terminal (queued -> cancelled).
        updated = app.state.agent_task_registry.transition(
            task.task_id, STATUS_CANCELLED, updated_at=_now()
        )
        persist_agent_task(app, updated)

        resp = client.post(f"/v1/agent-tasks/{task.task_id}/steer", json={"text": "hi"})
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["error"] == "child_not_running"
        assert err["details"]["reason"] == STEER_REJECT_TASK_TERMINAL
        # Nothing buffered anywhere.
        assert app.state.loop_inboxes.get(task.child_session_id) is None


def test_steer_idle_child_rejected_typed_409(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        # No running turn on the child (busy defaults to False).
        resp = client.post(f"/v1/agent-tasks/{task.task_id}/steer", json={"text": "hi"})
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["error"] == "child_not_running"
        assert err["details"]["reason"] == STEER_REJECT_CHILD_IDLE
        assert app.state.loop_inboxes.get(task.child_session_id) is None


def test_steer_gone_child_rejected_typed_409(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = seed_agent_task(
            app, parent_session_id=parent, agent_ref={"expert_id": "data_expert"}
        )
        # Mark running THEN delete the child session — gone wins over the busy check.
        _mark_running(app, task.child_session_id)
        app.state.sessions.delete(task.child_session_id)

        resp = client.post(f"/v1/agent-tasks/{task.task_id}/steer", json={"text": "hi"})
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["error"] == "child_not_running"
        assert err["details"]["reason"] == STEER_REJECT_CHILD_GONE


def test_steer_unknown_task_404(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        resp = client.post("/v1/agent-tasks/task_nope/steer", json={"text": "hi"})
        assert resp.status_code == 404


def test_steer_marker_constant_matches_producer() -> None:
    # The steer surfaces via #1036's producer, so its drained block uses the same
    # USER_STEER_MARKER — a light guard that we reuse the existing carrier.
    assert USER_STEER_MARKER.startswith("## Mid-turn user steer")
