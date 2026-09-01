"""iowarp/clio-agent — the SPOTTER clearance barrier a protected parent blocks on.

Three contracts, none of which the barrier held while it lived inline in
``spotter_watcher``:

* fail CLOSED with a typed reason whenever the session is armed into
  ``spotter-ai`` but no live watcher exists (arm failure, or the standing task
  cancelled by the speculative ``POST /v1/sessions/{sid}/cancel``);
* a PER-EXCHANGE progress window that RESTARTS on every observable watcher
  signal — a long-but-progressing watcher must run to completion, and only a
  watcher silent for a whole window fails closed (with its own reason,
  distinct from a crashed watcher's);
* bounded retention of the per-session clearance-event map.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.loop_inbox import enqueue_user_steer
from clio_agent.gact.spotter_clearance import (
    CLEARANCE_GRANTED,
    CLEARANCE_PROGRESS_STALLED,
    CLEARANCE_WATCHER_FAILED,
    CLEARANCE_WATCHER_UNAVAILABLE,
    SPOTTER_CLEARANCE_REASONS,
    clearance_event,
    max_clearance_events,
    release_clearance_event,
    signal_clearance,
    wait_for_spotter_clearance,
)

_WATCHER_BLUEPRINT_ID = "spotter-ai"
_WATCHER_EXPERT_ID = "spotter_watcher"


# --------------------------------------------------------------------------- #
# Unit-level fake: the barrier reads exactly four app.state surfaces, so a
# hand-built app makes the wait's TIMING semantics deterministic (no real turn).
# --------------------------------------------------------------------------- #


class _FakeTask:
    def __init__(self, child_session_id: str, live_state: str = "waiting") -> None:
        self.child_session_id = child_session_id
        self.live_state = live_state
        self.is_terminal = False
        self.agent_ref = {"expert_id": _WATCHER_EXPERT_ID}


class _FakeRegistry:
    def __init__(self) -> None:
        self.tasks: dict[str, list[_FakeTask]] = {}

    def for_parent(self, parent_session_id: str) -> list[_FakeTask]:
        return list(self.tasks.get(parent_session_id, []))


class _FakeRunner:
    def __init__(self) -> None:
        self.busy_sessions: set[str] = set()

    def busy(self, session_id: str) -> bool:
        return session_id in self.busy_sessions


class _FakeSessions:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def get(self, session_id: str) -> Any:
        return self.sessions.get(session_id)


def _fake_app(*, approval_mode: str = "spotter-ai") -> tuple[Any, _FakeRegistry, _FakeRunner]:
    registry = _FakeRegistry()
    runner = _FakeRunner()
    sessions = _FakeSessions()
    sessions.sessions["sess_parent"] = SimpleNamespace(
        id="sess_parent", approval_mode=approval_mode
    )
    state = SimpleNamespace(
        sessions=sessions,
        agent_task_registry=registry,
        turn_runner=runner,
        loop_inboxes={},
    )
    return SimpleNamespace(state=state), registry, runner


def test_armed_session_with_no_live_watcher_fails_closed_typed() -> None:
    """Armed mode + no watcher must DENY: surveillance is off while advertised."""

    app, _registry, _runner = _fake_app()
    assert wait_for_spotter_clearance(app, "sess_parent") == CLEARANCE_WATCHER_UNAVAILABLE


def test_terminal_watcher_task_fails_closed_typed() -> None:
    """A cancelled standing task is indistinguishable from never-armed: DENY."""

    app, registry, _runner = _fake_app()
    task = _FakeTask("sess_watcher")
    task.is_terminal = True
    registry.tasks["sess_parent"] = [task]
    assert wait_for_spotter_clearance(app, "sess_parent") == CLEARANCE_WATCHER_UNAVAILABLE


def test_errored_watcher_reports_check_failure_not_a_deadline() -> None:
    """A crashed watcher is its own reason -- no waiting happened at all."""

    app, registry, _runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher", live_state="error")]
    started = time.monotonic()
    reason = wait_for_spotter_clearance(app, "sess_parent", progress_timeout_s=5.0)
    assert reason == CLEARANCE_WATCHER_FAILED
    assert time.monotonic() - started < 1.0  # returned immediately, never waited


def test_non_spotter_session_clears_without_a_watcher() -> None:
    app, _registry, _runner = _fake_app(approval_mode="ask")
    assert wait_for_spotter_clearance(app, "sess_parent") == CLEARANCE_GRANTED
    assert wait_for_spotter_clearance(app, "sess_missing") == CLEARANCE_GRANTED


def test_idle_healthy_watcher_clears_immediately() -> None:
    app, registry, _runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    assert wait_for_spotter_clearance(app, "sess_parent") == CLEARANCE_GRANTED


def test_silent_busy_watcher_fails_closed_with_its_own_stall_reason() -> None:
    """A watcher that publishes nothing for a whole window is a stall, and its
    reason is DISTINCT from a crashed watcher's."""

    app, registry, runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    runner.busy_sessions.add("sess_watcher")
    reason = wait_for_spotter_clearance(app, "sess_parent", progress_timeout_s=0.2)
    assert reason == CLEARANCE_PROGRESS_STALLED
    assert reason != CLEARANCE_WATCHER_FAILED


def test_progress_window_restarts_on_every_watcher_signal() -> None:
    """A watcher progressing for far longer than one window must NOT be denied.

    This is the ground-rule shape: the window bounds the gap BETWEEN observable
    watcher signals, never the total time a composite check turn may take. A
    single wall clock over the whole wait fails this test.
    """

    app, registry, runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    runner.busy_sessions.add("sess_watcher")
    window = 0.2
    total = 1.6  # eight windows of continuous, honest watcher progress
    stop = threading.Event()

    def _heartbeat() -> None:
        deadline = time.monotonic() + total
        while time.monotonic() < deadline and not stop.is_set():
            time.sleep(window / 4)
            signal_clearance(app, "sess_parent")
        runner.busy_sessions.discard("sess_watcher")
        signal_clearance(app, "sess_parent")

    beat = threading.Thread(target=_heartbeat)
    beat.start()
    started = time.monotonic()
    try:
        reason = wait_for_spotter_clearance(app, "sess_parent", progress_timeout_s=window)
    finally:
        stop.set()
        beat.join(timeout=10.0)
    assert reason == CLEARANCE_GRANTED
    assert time.monotonic() - started >= total  # it really did wait out the work


def test_a_buffered_coalesced_wake_also_holds_clearance() -> None:
    """An idle runner with a queued wake is still pending evidence."""

    app, registry, _runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    enqueue_user_steer(app, "sess_watcher", "wake", {"coalesce_key": "spotter_wake"})
    assert (
        wait_for_spotter_clearance(app, "sess_parent", progress_timeout_s=0.1)
        == CLEARANCE_PROGRESS_STALLED
    )


def test_every_denial_reason_carries_a_model_facing_message() -> None:
    for reason in (
        CLEARANCE_WATCHER_UNAVAILABLE,
        CLEARANCE_WATCHER_FAILED,
        CLEARANCE_PROGRESS_STALLED,
    ):
        assert SPOTTER_CLEARANCE_REASONS[reason]
    assert CLEARANCE_GRANTED not in SPOTTER_CLEARANCE_REASONS


# --------------------------------------------------------------------------- #
# Bounded retention of the per-session clearance-event map
# --------------------------------------------------------------------------- #


def test_release_clearance_event_wakes_and_drops_the_entry() -> None:
    app, registry, _runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    event = clearance_event(app, "sess_parent")
    assert "sess_parent" in app.state.spotter_clearance_events

    assert release_clearance_event(app, "sess_parent") is True
    assert "sess_parent" not in app.state.spotter_clearance_events
    # Any waiter still holding the released event is woken, never left blocked.
    assert event.is_set()
    assert release_clearance_event(app, "sess_parent") is False


def test_clearance_event_map_prunes_sessions_with_no_live_watcher() -> None:
    """The map is bounded: entries for sessions without a live watcher are
    released once it reaches its retention cap."""

    app, registry, _runner = _fake_app()
    registry.tasks["sess_parent"] = [_FakeTask("sess_watcher")]
    clearance_event(app, "sess_parent")
    for index in range(max_clearance_events()):
        clearance_event(app, f"sess_dead_{index}")
    events = app.state.spotter_clearance_events

    assert len(events) <= 2  # the armed session + the newest creation
    assert "sess_parent" in events  # a LIVE watcher's entry is never evicted


def test_clearance_events_are_released_when_the_watcher_disarms(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        assert wait_for_spotter_clearance(app, sid) == CLEARANCE_GRANTED
        assert sid in app.state.spotter_clearance_events

        assert client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "ask"}).status_code == 200
        assert sid not in app.state.spotter_clearance_events


# --------------------------------------------------------------------------- #
# Route level: the reachability path the barrier failed OPEN on
# --------------------------------------------------------------------------- #


def test_cancelling_a_spotter_session_denies_later_mutations_typed(tmp_path: Path) -> None:
    """``POST /cancel`` cascades to the standing watcher; the session still
    advertises spotter-ai, so every later mutating call must DENY, not clear."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]

        assert client.post(f"/v1/sessions/{sid}/cancel").status_code == 204
        assert app.state.agent_task_registry.get(task.task_id).is_terminal
        assert app.state.sessions.get(sid).approval_mode == "spotter-ai"

        assert wait_for_spotter_clearance(app, sid) == CLEARANCE_WATCHER_UNAVAILABLE


def test_arm_failure_denies_the_gate_instead_of_clearing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session whose watcher never armed must not run mutations unobserved."""

    def _boom(_app: Any, _spec: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _boom)

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post(
            "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
        ).json()["id"]
        assert app.state.agent_task_registry.for_parent(sid) == []

        from clio_agent.gact.permission_gate import _make_permission_gate
        from clio_agent.gact.runtime.globals import _tool_session_context

        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            decision = gate("shell_bash", {"command": "rm -rf /"})

        assert getattr(decision, "reason", "") or str(decision)
        row = next(iter(app.state.permissions.values()))
        assert row["reason"] == CLEARANCE_WATCHER_UNAVAILABLE
        assert row["action"] == "deny"
        assert SPOTTER_CLEARANCE_REASONS[CLEARANCE_WATCHER_UNAVAILABLE] in row["summary"]


# --------------------------------------------------------------------------- #
# Route level: a crashed watcher is never reported as a clearance timeout
# --------------------------------------------------------------------------- #


class _FailingAgent:
    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        return SimpleNamespace(
            answer="",
            selected_expert="",
            routing_rationale="",
            error_info={
                "error": "not_implemented",
                "message": "The requested custom tools are unavailable.",
                "details": {"reason": "custom_agent_tools_unavailable"},
                "recoverable": False,
            },
        )


def _write_watcher_blueprint(root: Path) -> None:
    (root / "experts").mkdir(parents=True)
    root.joinpath("AGENT.md").write_text(
        f"---\nid: {_WATCHER_BLUEPRINT_ID}\nversion: 0.1.0\ntitle: Spotter AI (test fixture)\n"
        f"root_expert: {_WATCHER_EXPERT_ID}\n---\nfixture\n",
        encoding="utf-8",
    )
    root.joinpath("experts", f"{_WATCHER_EXPERT_ID}.md").write_text(
        f"---\nid: {_WATCHER_EXPERT_ID}\ntitle: Spotter Watcher\ntier: 1\nmodule:\n  kind: react\n"
        "---\nwatch\n",
        encoding="utf-8",
    )


def _wait_for(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.usefixtures("host_agent_executor")
def test_failed_watcher_denial_is_recorded_as_a_check_failure(tmp_path: Path) -> None:
    """The audit row AND the model text must say the review FAILED -- never that
    it ran out of time, which is an affirmative false fact (no waiting occurred)."""

    workspace = tmp_path / "workspace"
    _write_watcher_blueprint(workspace / ".clio" / "agent-blueprints" / _WATCHER_BLUEPRINT_ID)
    app = build_app(sessions_path=tmp_path / "s.json", agent=_FailingAgent())
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "W",
                "root_path": str(workspace),
                "storage_root": f"{workspace}/.clio",
            },
        ).json()["id"]
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]
        task = app.state.agent_task_registry.for_parent(sid)[0]

        from clio_agent.gact.spotter_watcher import wake_on_parent_activity

        wake_on_parent_activity(app, sid, tool_name="phenotype_measure_cohort")
        assert _wait_for(
            lambda: app.state.agent_task_registry.get(task.task_id).live_state == "error"
        )

        from clio_agent.gact.permission_gate import _make_permission_gate
        from clio_agent.gact.runtime.globals import _tool_session_context

        gate = _make_permission_gate(app)
        with _tool_session_context(sid):
            gate("phenotype_measure_cohort", {"runs": 5})

        row = next(iter(app.state.permissions.values()))
        assert row["reason"] == CLEARANCE_WATCHER_FAILED
        assert "deadline" not in row["summary"]
        assert SPOTTER_CLEARANCE_REASONS[CLEARANCE_WATCHER_FAILED] in row["summary"]
