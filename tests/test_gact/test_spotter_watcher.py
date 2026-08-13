"""iowarp/clio-agent — spotter-ai approval-mode watcher arm/disarm (server-half).

Covers:

* ``POST /v1/sessions`` with ``approval_mode="spotter-ai"`` arms exactly ONE
  watcher :class:`~clio_agent.gact.agent_tasks.AgentTask`, run_label
  ``"SPOTTER AI"``, and the child session activates the watcher's OWN Agent
  Blueprint via ``active_agent_blueprint_id`` regardless of the parent's own
  blueprint (P2.4 #1122's ``session_scope_metadata`` seam).
* Idempotent arm: a second ``PATCH`` into ``spotter-ai`` does not double-spawn.
* Disarm: a ``PATCH`` away from ``spotter-ai`` cancels ONLY the watcher task
  (:func:`~clio_agent.gact.turn_spawn.cancel_agent_task`, never
  ``cancel_children_of``), reaching a terminal status.
* Arm-failure path: a spawn exception is caught, logged with a typed reason,
  and the route still succeeds (the session persists regardless).
* :class:`~clio_agent.gact.turn_spawn.TaskSpec` composition at the unit level
  (child_expert_id / run_label / session_scope_metadata / skip_declared_check),
  per the brief's lighter-weight alternative to a full spawn.

The first three use a REAL (non-monkeypatched) spawn against a minimal
workspace-scoped ``spotter-ai`` Agent Blueprint fixture (mirrors
``test_agent_resolution_unify.py``'s ``_write_simple_blueprint`` pattern) run
under a deliberately SLOW host fake, so the watcher's AgentTask stays
genuinely non-terminal for the test's lifetime — no race against the async
turn settling mid-assertion.
"""

from __future__ import annotations

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
)

# A spawned child's react expert resolves through the blueprint runtime; route
# it to the test's host fake instead of a real (LM-bound) DSPy compile (#948 S4b).
pytestmark = pytest.mark.usefixtures("host_agent_executor")

_WATCHER_BLUEPRINT_ID = "spotter-ai"
_WATCHER_EXPERT_ID = "spotter_watcher"


class _SlowAgent:
    """A host fake whose ``forward`` blocks — keeps a spawned child's AgentTask
    genuinely non-terminal for the lifetime of a test (deterministic; no race
    against the async turn settling mid-assertion, unlike an instant fake)."""

    def __init__(self, sleep_s: float = 5.0) -> None:
        self.sleep_s = sleep_s
        self.calls = 0

    def forward(self, question: str, session_id: str, **_kw: Any) -> Any:
        self.calls += 1
        time.sleep(self.sleep_s)
        return SimpleNamespace(answer="watching", selected_expert="", routing_rationale="")


def _write_watcher_blueprint(root: Path) -> None:
    """A minimal ONE-expert Agent Blueprint standing in for the real spotter-ai
    pack (a separate, not-yet-authored deliverable) — just enough for the
    watcher's expert id to RESOLVE so a real spawn actually runs end to end."""

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


def _build_app_with_watcher(tmp_path: Path, sleep_s: float = 5.0) -> tuple[Any, str, _SlowAgent]:
    """A ``build_app`` wired to a real workspace-scoped ``spotter-ai`` fixture
    blueprint, so :func:`ensure_spotter_watcher`'s spawn actually resolves and
    RUNS (never monkeypatched) — the run stays ``running`` for ``sleep_s``."""

    workspace = tmp_path / "workspace"
    _write_watcher_blueprint(workspace / ".clio" / "agent-blueprints" / _WATCHER_BLUEPRINT_ID)
    agent = _SlowAgent(sleep_s=sleep_s)
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    return app, str(workspace), agent


def _make_workspace(client: TestClient, root_path: str) -> str:
    return client.post(
        "/v1/workspaces",
        json={"name": "W", "root_path": root_path, "storage_root": f"{root_path}/.clio"},
    ).json()["id"]


# --------------------------------------------------------------------------- #
# 1. create with spotter-ai arms exactly one watcher task
# --------------------------------------------------------------------------- #


def test_create_session_arms_exactly_one_watcher_task(tmp_path: Path) -> None:
    app, root_path, _agent = _build_app_with_watcher(tmp_path)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]

        tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(tasks) == 1, tasks
        task = tasks[0]
        assert task.run_label == WATCHER_RUN_LABEL == "SPOTTER AI"
        assert task.agent_ref["expert_id"] == _WATCHER_EXPERT_ID
        assert task.status == STATUS_RUNNING

        child = app.state.sessions.get(task.child_session_id)
        assert child is not None
        assert child.parent_session_id == sid
        assert child.metadata.get("active_agent_blueprint_id") == _WATCHER_BLUEPRINT_ID


def test_default_approval_mode_arms_nothing(tmp_path: Path) -> None:
    app, root_path, _agent = _build_app_with_watcher(tmp_path)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
        assert app.state.agent_task_registry.for_parent(sid) == []


# --------------------------------------------------------------------------- #
# 2. idempotent arm: a second PATCH into spotter-ai does not double-spawn
# --------------------------------------------------------------------------- #


def test_second_patch_to_spotter_ai_does_not_double_spawn(tmp_path: Path, caplog) -> None:
    app, root_path, _agent = _build_app_with_watcher(tmp_path)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]

        r1 = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})
        assert r1.status_code == 200
        first_tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(first_tasks) == 1

        with caplog.at_level("INFO", logger="clio_agent.gact.spotter_watcher"):
            r2 = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "spotter-ai"})
        assert r2.status_code == 200

        tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(tasks) == 1, tasks
        assert tasks[0].task_id == first_tasks[0].task_id
        # Still running (the slow agent hasn't settled) at both checkpoints — the
        # idempotency check genuinely found a NON-TERMINAL task, not a lucky race.
        assert tasks[0].status == STATUS_RUNNING
        assert any(
            "spotter_watcher_skip" in r.message and "already_running" in r.message
            for r in caplog.records
        )


# --------------------------------------------------------------------------- #
# 3. disarm: PATCH away from spotter-ai cancels ONLY the watcher task
# --------------------------------------------------------------------------- #


def test_patch_away_from_spotter_ai_cancels_watcher(tmp_path: Path, caplog) -> None:
    app, root_path, _agent = _build_app_with_watcher(tmp_path)
    with TestClient(app) as client:
        wid = _make_workspace(client, root_path)
        sid = client.post(
            "/v1/sessions",
            json={"title": "t", "workspace_id": wid, "approval_mode": "spotter-ai"},
        ).json()["id"]

        tasks = app.state.agent_task_registry.for_parent(sid)
        assert len(tasks) == 1
        task_id = tasks[0].task_id
        assert tasks[0].status == STATUS_RUNNING

        with caplog.at_level("INFO", logger="clio_agent.gact.spotter_watcher"):
            resp = client.patch(f"/v1/sessions/{sid}", json={"approval_mode": "ask"})
        assert resp.status_code == 200
        assert resp.json()["approval_mode"] == "ask"

        updated = app.state.agent_task_registry.get(task_id)
        assert updated is not None
        assert updated.status == STATUS_CANCELLED
        assert any(
            "spotter_watcher_disarmed" in r.message and "reason=mode_changed" in r.message
            for r in caplog.records
        )


def test_disarm_with_no_active_watcher_is_a_typed_no_op(tmp_path: Path) -> None:
    """Not just idempotent but explicit: disarming a session that never armed a
    watcher must not raise and must count zero (never silently guessed)."""

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
            resp = client.post(
                "/v1/sessions", json={"title": "t", "approval_mode": "spotter-ai"}
            )
        # The session create itself must NEVER fail because arming failed.
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
# 5. TaskSpec composition (unit level — the brief's lighter alternative)
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
    # workspace_id/session_mode intentionally unset so the child INHERITS them.
    assert spec.workspace_id is None
    assert spec.session_mode is None


def test_ensure_spotter_watcher_is_a_no_op_off_spotter_mode(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    session = app.state.sessions.create(workspace_id="ws_default", approval_mode="ask")
    assert ensure_spotter_watcher(app, session) is None
    assert app.state.agent_task_registry.for_parent(session.id) == []
