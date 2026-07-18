"""S4 (#948 / #952): spawn-runtime tools for react mains.

The routing surface that replaces the inline delegate_to_<child> / fanout tools +
the next_expert settle loop. A react main with declared children gets
spawn_agent_task / wait_agent_tasks / check_agent_tasks / spawn_agents_parallel;
a leaf (no children) gets none.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.turn_spawn import SpawnError


@contextmanager
def _active_turn(app: Any, session_id: str = "sess_x") -> Iterator[None]:
    """Bind BOTH the app and the turn session id the spawn tools read.

    ``_tool_session_context`` binds ``turn.tool_session_id``, but the spawn-runtime
    tools resolve their session via ``context.active_session_id()`` (=``turn.session_id``),
    so a tool CALL (not just a build) needs ``set_session_id`` too.
    """

    with _gact_app_context(app):
        token = ctx.set_session_id(session_id)
        try:
            yield
        finally:
            ctx.reset(token)


class _Agent:
    def forward(self, question: str, session_id: str):
        return type("P", (), {"answer": "ok", "selected_expert": "", "routing_rationale": ""})()


class _Def:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.metadata = {"agent_blueprint_id": "bp"}


def _tool_names(app, agent_id: str, declared: set[str], monkeypatch) -> list[str]:
    from clio_agent.gact.agents import spawn_runtime

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": set(declared),
    )
    with _gact_app_context(app), _tool_session_context("sess_x"):
        tools = spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def(agent_id))
    return [getattr(t, "name", "") for t in tools]


def test_react_main_with_children_gets_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        names = _tool_names(app, "main", {"data_expert", "hpc_expert"}, monkeypatch)
        assert set(names) == {
            "spawn_agent_task",
            "wait_agent_tasks",
            "check_agent_tasks",
            "spawn_agents_parallel",
        }


def test_leaf_expert_without_children_gets_no_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        assert _tool_names(app, "leaf_expert", set(), monkeypatch) == []


# ---------------------------------------------------------------------------
# Wire-parity + error-path tests.
#
# These MIGRATE the assertions the deleted inline delegate/fan-out tool tests in
# test_agent_blueprints.py locked (payload shape, semantic event emission, the
# blueprint block, undeclared-child rejection, fan-out bounds) onto the new
# spawn-runtime surface. No real server: a bare SimpleNamespace app carries the
# AgentTaskRegistry, ``spawn_child_turn_threadsafe`` and ``_emit_semantic_event``
# are monkeypatched so the tools are exercised against captured real structures.
# ---------------------------------------------------------------------------


def _fake_app(registry: AgentTaskRegistry | None = None) -> SimpleNamespace:
    """A minimal app carrying only the AgentTaskRegistry the tools reach for."""

    return SimpleNamespace(
        state=SimpleNamespace(agent_task_registry=registry or AgentTaskRegistry())
    )


def _capture_emits(monkeypatch) -> list[dict[str, Any]]:
    """Patch the spawn-runtime semantic-event emitter and return the capture list.

    ``_emit_semantic_event`` is bound into ``spawn_runtime`` at import, so we patch
    the name on that module. Each call is recorded as a flat dict (event_type + all
    keyword fields) so tests assert on the REAL emitted payload, not just a count.
    """

    emitted: list[dict[str, Any]] = []

    def _capture(app: Any, sid: str, event_type: str, **kwargs: Any) -> dict[str, Any]:
        emitted.append({"event_type": event_type, "session_id": sid, **kwargs})
        return {}

    monkeypatch.setattr("clio_agent.gact.agents.spawn_runtime._emit_semantic_event", _capture)
    return emitted


def _tools_by_name(app: Any, agent_id: str, declared: set[str], monkeypatch) -> dict[str, Any]:
    from clio_agent.gact.agents import spawn_runtime

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": set(declared),
    )
    tools = spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def(agent_id))
    return {getattr(t, "name", ""): t for t in tools}


def _completed_task(task_id: str = "task_done") -> AgentTask:
    return AgentTask(
        task_id=task_id,
        parent_session_id="sess_x",
        child_session_id="child_1",
        agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
        status="completed",
        result={
            "answer_excerpt": "child produced the staged CSV",
            "workflow_state": {"profile": {"status": "ready", "rows": 1024}},
            "message_ref": "msg_1",
        },
    )


def test_spawn_agent_task_success_emits_delegation_started_and_returns_task(monkeypatch) -> None:
    app = _fake_app()
    emitted = _capture_emits(monkeypatch)
    monkeypatch.setattr(
        "clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe",
        lambda a, spec: SimpleNamespace(task_id="task_abc", status="running"),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="analyze"))

    # Returns the task handle for a later wait.
    assert result == {"task_id": "task_abc", "status": "running"}
    # Exactly one delegation.started event, with the full wire block (migrated from
    # the deleted test_generated_child_expert_tool_emits_semantic_delegation_events).
    assert [e["event_type"] for e in emitted] == ["blueprint.delegation.started"]
    started = emitted[0]
    assert started["status"] == "running"
    assert started["actor"] == {"agent_id": "main", "role": "parent_expert"}
    assert started["subject"] == {"agent_id": "data_expert", "role": "child_expert"}
    assert started["blueprint"] == {
        "agent_blueprint_id": "bp",
        "parent_expert": "main",
        "child_expert": "data_expert",
    }


def test_spawn_agent_task_spawn_error_returns_reason_and_emits_nothing(monkeypatch) -> None:
    app = _fake_app()
    emitted = _capture_emits(monkeypatch)

    def _raise(a: Any, spec: Any) -> Any:
        raise SpawnError("child not declared", reason="undeclared_child")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _raise)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="ghost_expert", task="x"))

    # Typed reason surfaced structurally; NO delegation.started for a refused spawn.
    assert result == {"error": "undeclared_child", "message": "child not declared"}
    assert emitted == []


def test_wait_agent_tasks_completed_returns_wire_payload_and_emits_completed(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(registry)
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0))

    (payload,) = result["results"]
    assert payload["task_id"] == "task_done"
    assert payload["status"] == "completed"
    assert payload["stage"] == "delegate.completed"
    assert payload["output"] == "child produced the staged CSV"
    assert payload["workflow_state"] == {"profile": {"status": "ready", "rows": 1024}}
    assert payload["agent_id"] == "data_expert"
    assert payload["parent_id"] == "main"

    # One completed event, flowing the completion payload + the return-direction block.
    assert [e["event_type"] for e in emitted] == ["blueprint.delegation.completed"]
    completed = emitted[0]
    assert completed["status"] == "completed"
    assert completed["actor"] == {"agent_id": "data_expert", "role": "child_expert"}
    assert completed["subject"] == {"agent_id": "main", "role": "parent_expert"}
    assert completed["blueprint"] == {
        "agent_blueprint_id": "bp",
        "parent_expert": "main",
        "child_expert": "data_expert",
    }
    assert completed["payload"]["stage"] == "delegate.completed"


def test_wait_agent_tasks_failed_emits_delegation_failed_with_status(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    # register bypasses transition validation, so a terminal failed record with a
    # typed error_reason can be seeded directly.
    registry.register(
        AgentTask(
            task_id="task_bad",
            parent_session_id="sess_x",
            child_session_id="child_2",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="failed",
            error_reason="agent_error",
            result={"answer_excerpt": "", "workflow_state": {}, "message_ref": ""},
        )
    )
    app = _fake_app(registry)
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_bad"], timeout_s=1.0))

    (payload,) = result["results"]
    assert payload["status"] == "failed"
    assert payload["stage"] == "delegate.failed"
    assert payload["error_reason"] == "agent_error"
    assert [e["event_type"] for e in emitted] == ["blueprint.delegation.failed"]
    assert emitted[0]["status"] == "failed"


def test_wait_agent_tasks_unknown_task_returns_error_and_emits_nothing(monkeypatch) -> None:
    app = _fake_app()
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        # timeout_s=0 so the never-set completion Event of an unknown id returns at once.
        result = json.loads(
            tools["wait_agent_tasks"].func(task_ids=["task_missing"], timeout_s=0.0)
        )

    assert result["results"] == [{"task_id": "task_missing", "error": "unknown_task"}]
    assert emitted == []


def test_spawn_agents_parallel_emits_fanout_started_and_spawns_each(monkeypatch) -> None:
    app = _fake_app()
    emitted = _capture_emits(monkeypatch)
    spawn_calls: list[str] = []

    def _fake_spawn(a: Any, spec: Any) -> Any:
        spawn_calls.append(spec.child_expert_id)
        return SimpleNamespace(task_id=f"task_{spec.child_expert_id}", status="running")

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _fake_spawn)

    spawns = [
        {"agent": "data_expert", "task": "profile the CSV"},
        {"agent": "hpc_expert", "task": "run the job"},
    ]
    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert", "hpc_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agents_parallel"].func(spawns=spawns))

    # Each declared child spawned, in order, with its own task id returned.
    assert spawn_calls == ["data_expert", "hpc_expert"]
    assert result == {
        "spawned": [
            {"task_id": "task_data_expert", "status": "running"},
            {"task_id": "task_hpc_expert", "status": "running"},
        ]
    }
    # fanout.started fires ONCE up front (with the spawn count in its summary), then
    # one delegation.started per spawned child.
    assert [e["event_type"] for e in emitted] == [
        "blueprint.fanout.started",
        "blueprint.delegation.started",
        "blueprint.delegation.started",
    ]
    fanout = emitted[0]
    assert fanout["status"] == "running"
    assert "2 children" in fanout["summary"]
    assert fanout["actor"] == {"agent_id": "main", "role": "parent_expert"}
    assert fanout["blueprint"] == {
        "agent_blueprint_id": "bp",
        "parent_expert": "main",
        "child_expert": "",
    }
