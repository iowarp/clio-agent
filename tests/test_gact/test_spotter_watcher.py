"""iowarp/clio-agent — spotter-ai standing watcher: arm/disarm + push-wake.

Owner design ruling (no timers anywhere): the watcher ARMS as a STANDING
record (``status=RUNNING``, ``live_state="waiting"``) with NO turn started; a
later PUSH -- real activity on the PARENT session (a tool call completing, or
its turn finishing) -- wakes it via the loop-inbox machinery. Covers:

* ARM creates exactly one standing AgentTask with NO turn on the child session
  (P2.4 #1122's ``session_scope_metadata`` binds the watcher's OWN Agent
  Blueprint regardless of the parent's).
* Idempotent arm (route-level transition-only + the lower-level idempotency
  check); disarm transitions the standing row to terminal (CANCELLED), typed.
* Arm-failure path: caught, logged typed, the route still succeeds.
* :class:`~clio_agent.gact.turn_spawn.TaskSpec` composition at the unit level.
* The push-wake: a parent tool-completion starts exactly one check turn
  carrying the wake text as its input; a coalescing burst collapses onto ONE
  buffered wake behind a running check turn; ``live_state`` flips
  waiting->running->waiting across a wake; a direct user ("Discuss") message
  to a waiting watcher works exactly like any session turn; the watcher's own
  activity never self-wakes (its own session is never itself in spotter-ai
  mode).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import STATUS_CANCELLED, STATUS_RUNNING
from clio_agent.gact.app import build_app
from clio_agent.gact.spotter_watcher import (
    WATCHER_RUN_LABEL,
    ensure_spotter_watcher,
    on_turn_finalized,
    wake_on_parent_activity,
)

_WATCHER_BLUEPRINT_ID = "spotter-ai"
_WATCHER_EXPERT_ID = "spotter_watcher"


# --------------------------------------------------------------------------- #
# 1. ARM: a standing row, no turn -- no blueprint/turn machinery needed at all
# --------------------------------------------------------------------------- #


def test_arm_creates_waiting_standing_row_with_no_turn(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]

        tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(tasks) == 1, tasks
        task = tasks[0]
        assert task.run_label == WATCHER_RUN_LABEL == "SPOTTER AI"
        assert task.agent_ref["expert_id"] == _WATCHER_EXPERT_ID
        # Standing: RUNNING (non-terminal, armed) but "waiting" (idle, no check
        # turn active) -- the whole point is that NO turn started at arm time.
        assert task.status == STATUS_RUNNING
        assert task.live_state == "waiting"
        assert not task.is_terminal

        child = app.state.sessions.get(task.child_session_id)
        assert child is not None
        assert child.parent_session_id == sid
        assert child.metadata.get("active_agent_blueprint_id") == _WATCHER_BLUEPRINT_ID
        # No turn ever ran: the child session never left "idle" and has no messages.
        assert child.status == "idle"
        assert app.state.messages.get(child.id, []) == []


def test_default_approval_mode_arms_nothing(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assert app.state.agent_task_registry.for_parent(sid) == []


def test_watcher_child_session_is_never_itself_in_spotter_mode(tmp_path: Path) -> None:
    """Pins the self-wake-impossible assumption (guard, item 3 of the brief):
    the watcher's OWN child session's approval_mode is never "spotter-ai" --
    it is never re-armed against itself, so its own activity can structurally
    never trigger wake_on_parent_activity for itself."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        child = app.state.sessions.get(task.child_session_id)
        assert child.approval_mode != "spotter-ai"


# --------------------------------------------------------------------------- #
# 2. idempotent arm
# --------------------------------------------------------------------------- #


def test_second_identical_patch_to_spotter_ai_is_a_route_level_no_op(
    tmp_path: Path, caplog
) -> None:
    """Arming fires ONLY on a genuine TRANSITION into spotter-ai
    (``sync_watcher_for_mode``), never on a repeat PATCH that leaves the mode
    unchanged -- the route must not even CALL ensure_spotter_watcher again."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        r1 = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})
        assert r1.status_code == 200
        first_tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(first_tasks) == 1

        # caplog accumulates for the WHOLE test, not just the `with` block below.
        caplog.clear()
        with caplog.at_level("INFO", logger="clio_agent.gact.spotter_watcher"):
            r2 = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})
        assert r2.status_code == 200

        tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(tasks) == 1, tasks
        assert tasks[0].task_id == first_tasks[0].task_id
        assert not any("spotter_watcher" in r.message for r in caplog.records), [
            r.message for r in caplog.records
        ]


def test_ensure_spotter_watcher_called_directly_twice_is_still_idempotent(
    tmp_path: Path, caplog
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        session = app.state.sessions.get(sid)
        first_tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(first_tasks) == 1

        with caplog.at_level("INFO", logger="clio_agent.gact.spotter_watcher"):
            result = ensure_spotter_watcher(app, session)

        assert result is not None and result.task_id == first_tasks[0].task_id
        assert len(app.state.agent_task_registry.for_parent(sid)) == 1
        assert any(
            "spotter_watcher_skip" in r.message and "already_running" in r.message
            for r in caplog.records
        )


# --------------------------------------------------------------------------- #
# 3. disarm: transitions the standing row to terminal
# --------------------------------------------------------------------------- #


def test_disarm_transitions_standing_row_to_terminal(tmp_path: Path, caplog) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        task_id = app.state.agent_task_registry.for_parent(sid)[0].task_id

        with caplog.at_level("INFO", logger="clio_agent.gact.spotter_watcher"):
            resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "ask"})
        assert resp.status_code == 200
        assert resp.json()["approval_mode"] == "ask"

        updated = app.state.agent_task_registry.get(task_id)
        assert updated is not None
        assert updated.status == STATUS_CANCELLED
        assert updated.is_terminal
        assert any(
            "spotter_watcher_disarmed" in r.message and "reason=mode_changed" in r.message
            for r in caplog.records
        )


def test_disarm_with_no_active_watcher_is_a_typed_no_op(tmp_path: Path) -> None:
    from clio_agent.gact.spotter_watcher import disarm_spotter_watcher

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        session = app.state.sessions.get(sid)
        assert disarm_spotter_watcher(app, session) == 0


# --------------------------------------------------------------------------- #
# 4. arm failure: caught, logged typed, route still succeeds
# --------------------------------------------------------------------------- #


def test_arm_failure_logs_typed_and_route_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def _boom(_app: Any, _spec: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _boom)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        with caplog.at_level("WARNING", logger="clio_agent.gact.spotter_watcher"):
            resp = client.post("/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"})
        assert resp.status_code == 200
        sid = resp.json()["id"]
        assert resp.json()["approval_mode"] == "spotter-ai"
        assert app.state.agent_task_registry.for_parent(sid) == []

    assert any(
        "spotter_watcher_arm_failed" in r.message and "reason=spawn_unexpected_error" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_arm_failure_on_patch_also_logs_typed_and_route_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    def _boom(_app: Any, _spec: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _boom)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        with caplog.at_level("WARNING", logger="clio_agent.gact.spotter_watcher"):
            resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})
        assert resp.status_code == 200
        assert app.state.agent_task_registry.for_parent(sid) == []
    assert any("spotter_watcher_arm_failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# 5. TaskSpec composition (unit level)
# --------------------------------------------------------------------------- #


def test_ensure_spotter_watcher_builds_expected_taskspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    captured: dict[str, Any] = {}

    def _fake_spawn(_app: Any, spec: Any) -> Any:
        captured["spec"] = spec
        return SimpleNamespace(task_id="task_fake")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _fake_spawn)

    session = app.state.sessions.create(workspace_id="ws_default", approval_mode="spotter-ai")
    result = ensure_spotter_watcher(app, session)

    assert result is not None and result.task_id == "task_fake"
    spec = captured["spec"]
    assert spec.child_expert_id == _WATCHER_EXPERT_ID
    assert spec.run_label == "SPOTTER AI"
    assert spec.requesting_expert_id == "main"
    assert spec.skip_declared_check is True
    assert spec.parent_session_id == session.id
    assert spec.session_scope_metadata == {"active_agent_blueprint_id": _WATCHER_BLUEPRINT_ID}
    # No timers, no turn at arm time: the standing shape.
    assert spec.start_turn is False
    assert spec.task_text == ""
    # workspace_id/session_mode intentionally unset so the child INHERITS them.
    assert spec.workspace_id is None
    assert spec.session_mode is None


def test_ensure_spotter_watcher_is_a_no_op_off_spotter_mode(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    session = app.state.sessions.create(workspace_id="ws_default", approval_mode="ask")
    assert ensure_spotter_watcher(app, session) is None
    assert app.state.agent_task_registry.for_parent(session.id) == []


# --------------------------------------------------------------------------- #
# 6. push-wake: needs a REAL check turn, so a resolvable watcher blueprint +
#    the host_agent_executor fixture (route react "main"-shaped builds to the
#    test's host fake, #948 S4b) -- applied per-test below.
# --------------------------------------------------------------------------- #


class _CapturingAgent:
    """A host fake whose ``forward`` records every input it received and can
    optionally BLOCK on a caller-supplied event before returning -- lets a
    test hold a check turn "in flight" to exercise the busy/coalesce path
    deterministically, with no timers."""

    def __init__(self, *, block_until: "threading.Event | None" = None) -> None:
        self.calls: list[str] = []
        self._block_until = block_until

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        self.calls.append(question)
        if self._block_until is not None:
            self._block_until.wait(timeout=10.0)
        return SimpleNamespace(answer="ack", selected_expert="", routing_rationale="")


def _write_watcher_blueprint(root: Path) -> None:
    """A minimal ONE-expert Agent Blueprint standing in for the real spotter-ai
    pack (a separate, not-yet-authored deliverable) — just enough for the
    watcher's expert id to RESOLVE so a real check turn actually runs."""

    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"""---
id: {_WATCHER_BLUEPRINT_ID}
version: 0.1.0
title: Spotter AI (test fixture)
root_expert: {_WATCHER_EXPERT_ID}
---
Spotter AI surveillance blueprint (test fixture).
""",
        encoding="utf-8",
    )
    root.joinpath("experts", f"{_WATCHER_EXPERT_ID}.md").write_text(
        f"""---
id: {_WATCHER_EXPERT_ID}
title: Spotter Watcher
tier: 1
module:
  kind: react
---
Watch the parent session's workload provenance.
""",
        encoding="utf-8",
    )


def _build_app_with_watcher_blueprint(tmp_path: Path, agent: Any) -> tuple[Any, str]:
    workspace = tmp_path / "workspace"
    _write_watcher_blueprint(workspace / ".clio" / "agent-blueprints" / _WATCHER_BLUEPRINT_ID)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    return app, str(workspace)


def _make_workspace(client: TestClient, root_path: str) -> str:
    return client.post(
        "/v1/workspaces",
        json={"name": "W", "root_path": root_path, "storage_root": f"{root_path}/.clio"},
    ).json()["id"]


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.mark.usefixtures("host_agent_executor")
def test_parent_tool_completion_wakes_exactly_one_check_turn(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    app, root_path = _build_app_with_watcher_blueprint(tmp_path, agent)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        assert task.live_state == "waiting"

        # Simulate the tool_observer.py seam: real parent tool-call activity.
        wake_on_parent_activity(app, sid)

        assert _wait_for(lambda: len(agent.calls) == 1)
        assert "Session activity" in agent.calls[0]
        assert "provenance" in agent.calls[0]

        # Exactly one check turn: no duplicate/second forward call.
        time.sleep(0.2)
        assert len(agent.calls) == 1


@pytest.mark.usefixtures("host_agent_executor")
def test_coalescing_under_burst_of_tool_completions(tmp_path: Path) -> None:
    """A burst of tool completions while a check turn is ALREADY running must
    collapse onto AT MOST ONE buffered wake -- not one per completion."""

    block = threading.Event()
    agent = _CapturingAgent(block_until=block)
    app, root_path = _build_app_with_watcher_blueprint(tmp_path, agent)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        child_sid = task.child_session_id

        # First activity: starts the (blocked-in-flight) check turn.
        wake_on_parent_activity(app, sid)
        assert _wait_for(lambda: len(agent.calls) == 1)
        assert _wait_for(lambda: app.state.turn_runner.busy(child_sid))

        from clio_agent.gact.loop_inbox import inbox_for

        inbox = inbox_for(app, child_sid)
        assert not inbox.peek_nonempty()  # nothing buffered yet

        # A burst of MORE activity while the check turn is in flight.
        for _ in range(20):
            wake_on_parent_activity(app, sid)

        # AT MOST ONE buffered wake, never 20.
        assert inbox.peek_nonempty()
        assert len(inbox.drain()) == 1

        block.set()  # release the in-flight check turn
        assert _wait_for(lambda: not app.state.turn_runner.busy(child_sid))
        # Still exactly one forward call -- the drained coalesced wake was
        # consumed by this assertion (drain() above), not re-driven into a
        # second turn (nothing left buffered for the idle hook to promote).
        assert len(agent.calls) == 1


@pytest.mark.usefixtures("host_agent_executor")
def test_live_state_flips_waiting_running_waiting_across_a_wake(tmp_path: Path) -> None:
    agent = _CapturingAgent()
    app, root_path = _build_app_with_watcher_blueprint(tmp_path, agent)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        assert task.live_state == "waiting"

        wake_on_parent_activity(app, sid)

        # Immediately after starting: running.
        assert _wait_for(
            lambda: app.state.agent_task_registry.get(task.task_id).live_state == "running"
        )
        # After the (fast) check turn settles: back to waiting.
        assert _wait_for(
            lambda: app.state.agent_task_registry.get(task.task_id).live_state == "waiting"
        )
        # Never terminal across the wake -- only disarm goes terminal.
        assert not app.state.agent_task_registry.get(task.task_id).is_terminal


@pytest.mark.usefixtures("host_agent_executor")
def test_user_message_to_waiting_watcher_works_and_returns_to_waiting(tmp_path: Path) -> None:
    """A direct "Discuss" message to the watcher's own session must work like
    any ordinary session turn while it is "waiting", and must not break the
    standing-task transition -- live_state ends back at "waiting"."""

    agent = _CapturingAgent()
    app, root_path = _build_app_with_watcher_blueprint(tmp_path, agent)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        child_sid = task.child_session_id
        assert task.live_state == "waiting"

        resp = client.post(f"/v1/sessions/{child_sid}/messages", json={"text": "what's up?"})
        assert resp.status_code == 200

        assert _wait_for(lambda: len(agent.calls) == 1)
        assert agent.calls[0] == "what's up?"
        assert _wait_for(
            lambda: app.state.agent_task_registry.get(task.task_id).live_state == "waiting"
        )
        assert not app.state.agent_task_registry.get(task.task_id).is_terminal


@pytest.mark.usefixtures("host_agent_executor")
def test_watcher_activity_never_self_wakes(tmp_path: Path) -> None:
    """The watcher's OWN session's activity must never trigger a wake for
    itself -- both the direct call (mirrors the real hook seam) and the
    turn-finalize hook are no-ops against the watcher's own child session,
    since its approval_mode is never itself "spotter-ai"."""

    agent = _CapturingAgent()
    app, root_path = _build_app_with_watcher_blueprint(tmp_path, agent)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]
        child_sid = task.child_session_id

        # Simulate a tool call INSIDE the watcher's own session.
        wake_on_parent_activity(app, child_sid)
        time.sleep(0.2)
        assert agent.calls == []  # never started a self-directed check turn

        # Simulate the watcher's own turn finalizing -- must flip live_state
        # back to "waiting" (the OTHER on_turn_finalized branch), never
        # interpret itself as a spotter-ai parent needing a wake.
        on_turn_finalized(app, child_sid)
        time.sleep(0.2)
        assert agent.calls == []
        assert app.state.agent_task_registry.get(task.task_id).live_state == "waiting"
