"""S4 (#948 / #952): spawn-runtime tools for react mains.

The routing surface that replaces the inline delegate_to_<child> / fanout tools +
the next_expert settle loop. A react main with declared children gets
spawn_agent_task / wait_agent_tasks / check_agent_tasks / spawn_agents_parallel;
a leaf (no children) gets none.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
from clio_agent.gact.agents.invoker import InProcessExpertInvoker, TaskHandle, TaskResult
from clio_agent.gact.app import build_app
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.turn_spawn import MAX_SPAWN_DEPTH, SpawnError
from clio_agent.gact.types import Message, Part


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


class _InvokeSpy:
    """Minimal invoker spy for the P2.6 spawn-routing acceptance lock."""

    def __init__(self) -> None:
        self.specs: list[Any] = []

    def invoke(self, spec: Any) -> TaskHandle:
        self.specs.append(spec)
        return TaskHandle(
            task_id="task_via_invoker",
            parent_session_id=spec.parent_session_id,
            child_session_id="child_via_invoker",
            status="running",
            run_index=0,
            depth=spec.depth,
        )


class _ProtocolSpy(_InvokeSpy):
    """Invoker stub recording the model-facing spawn/wait/check operation set."""

    def __init__(self, registry: AgentTaskRegistry) -> None:
        super().__init__()
        self.registry = registry
        self.wait_calls: list[tuple[TaskHandle, float]] = []
        self.check_calls: list[list[TaskHandle]] = []

    def wait(self, handle: TaskHandle, timeout_s: float) -> TaskResult:
        self.wait_calls.append((handle, timeout_s))
        task = self.registry.get(handle.task_id)
        assert task is not None
        return TaskResult.from_task(task)

    def check(self, handles: list[TaskHandle]) -> list[TaskResult]:
        self.check_calls.append(list(handles))
        tasks = [self.registry.get(handle.task_id) for handle in handles]
        assert all(task is not None for task in tasks)
        return [TaskResult.from_task(task) for task in tasks if task is not None]

    def cancel(self, handle: TaskHandle) -> bool:
        del handle
        raise AssertionError("spawn-runtime tools do not own workflow cancellation")


def _tool_names(
    app,
    agent_id: str,
    declared: set[str],
    monkeypatch,
    *,
    enable_skill_task_collection: bool = False,
) -> list[str]:
    from clio_agent.gact.agents import spawn_runtime

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": set(declared),
    )
    with _gact_app_context(app), _tool_session_context("sess_x"):
        tools = spawn_runtime.build_spawn_runtime_tools(
            _Agent(),
            _Def(agent_id),
            enable_skill_task_collection=enable_skill_task_collection,
        )
    return [getattr(t, "name", "") for t in tools]


def test_react_main_with_children_gets_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        names = _tool_names(app, "main", {"data_expert", "hpc_expert"}, monkeypatch)
        assert set(names) == {
            "spawn_agent_task",
            "wait_agent_tasks",
            "check_agent_tasks",
            "message_agent",
            "observe_agent_tasks",
            "get_agent_task_output",
            "spawn_agents_parallel",
        }


def test_leaf_expert_without_children_gets_no_spawn_tools(tmp_path: Path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        assert _tool_names(app, "leaf_expert", set(), monkeypatch) == []


def test_spawn_effect_leaf_gets_collectors_not_declared_child_spawners(
    tmp_path: Path, monkeypatch
) -> None:
    """A skill-created child is collectable without inventing static children."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        names = _tool_names(
            app,
            "dynamic_parent",
            set(),
            monkeypatch,
            enable_skill_task_collection=True,
        )
        assert set(names) == {
            "wait_agent_tasks",
            "check_agent_tasks",
            "message_agent",
            "observe_agent_tasks",
            "get_agent_task_output",
        }
        assert "spawn_agent_task" not in names
        assert "spawn_agents_parallel" not in names


# ---------------------------------------------------------------------------
# Wire-parity + error-path tests.
#
# These MIGRATE the assertions the deleted inline delegate/fan-out tool tests in
# test_agent_blueprints.py locked (payload shape, semantic event emission, the
# blueprint block, undeclared-child rejection, fan-out bounds) onto the new
# spawn-runtime surface. No real server: a bare SimpleNamespace app carries the
# AgentTaskRegistry, the configured invoker, and ``_emit_semantic_event`` are
# controlled so the tools are exercised against captured real structures.
# ---------------------------------------------------------------------------


class _StubSessions:
    """Minimal session store for the bare-app spawn-runtime tests.

    Records metadata patches (so ``persist_agent_task`` succeeds — it refuses when
    ``update`` returns ``None``) and resolves sessions seeded via :meth:`seed` (so
    ``_current_session_depth`` can read a child session's agent-task depth)."""

    def __init__(self) -> None:
        self._sessions: dict[str, SimpleNamespace] = {}
        self.updates: list[tuple[str, dict]] = []

    def seed(self, sid: str, metadata: dict | None = None) -> SimpleNamespace:
        sess = SimpleNamespace(id=sid, metadata=dict(metadata or {}))
        self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Any:
        return self._sessions.get(sid)

    def update(self, sid: str, *, metadata_patch: dict | None = None, **_kw: Any) -> Any:
        self.updates.append((sid, dict(metadata_patch or {})))
        sess = self._sessions.get(sid) or self.seed(sid)
        sess.metadata.update(metadata_patch or {})
        return sess  # non-None → persist_agent_task succeeds


def _fake_app(
    registry: AgentTaskRegistry | None = None,
    messages: dict[str, list[Message]] | None = None,
) -> SimpleNamespace:
    """A minimal app carrying the AgentTaskRegistry + a session/message store stub
    the spawn tools reach for (depth computation, verbatim-output resolution,
    report-flag persistence)."""

    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_task_registry=registry or AgentTaskRegistry(),
            sessions=_StubSessions(),
            messages=dict(messages or {}),
        )
    )
    app.state.expert_invoker = InProcessExpertInvoker(app)
    return app


def _assistant_message(msg_id: str, session_id: str, text: str) -> Message:
    """A real persisted assistant message whose full text is ``text`` (the #880
    verbatim-output source resolved at wait-time via message_ref)."""

    return Message(
        id=msg_id,
        session_id=session_id,
        role="assistant",
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
        parts=[Part(type="text", text=text)],
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
    # The spawn/wait tools also append expert_handoff Parts to the PARENT transcript
    # (#948 S4 finding [7]); the bare test app has no transcript/bus, so stub the append
    # to a no-op here. Tests that ASSERT on the Parts call _capture_parts to override
    # this with a capturing stub (last monkeypatch.setattr wins).
    monkeypatch.setattr(
        "clio_agent.gact.agents.spawn_runtime._append_live_assistant_part",
        lambda app, sid, part: None,
    )
    return emitted


def _capture_parts(monkeypatch) -> list[tuple[str, Part]]:
    """Capture the ``expert_handoff`` Parts the spawn/wait tools append to the PARENT
    transcript (#948 S4 finding [7]). Overrides the _capture_emits no-op stub; each
    append is recorded as ``(session_id, Part)`` so tests assert on the real Part."""

    parts: list[tuple[str, Part]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.spawn_runtime._append_live_assistant_part",
        lambda app, sid, part: parts.append((sid, part)),
    )
    return parts


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
        app.state.expert_invoker,
        "invoke",
        lambda spec: SimpleNamespace(
            task_id="task_abc", status="running", run_index=0, queued_reason=""
        ),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="analyze"))

    # Returns the task handle for a later wait (run_index is the ensemble run id, #948 S5);
    # queued_reason is the typed at-cap reason (#948 S6 fire-and-forget handle).
    assert result == {
        "task_id": "task_abc",
        "status": "running",
        "run_index": 0,
        "queued_reason": "",
        "handle_id": "task_abc",
        "run_label": "data_expert #1",
        "live_state": "running",
        "host": "local",
        "placement": "local",
    }
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


def test_spawn_agent_task_routes_invoke_through_app_expert_invoker(monkeypatch) -> None:
    """P2.6: the model-facing spawn crosses the configured invoker boundary."""

    app = _fake_app()
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    def _direct_spawn_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("spawn_agent_task bypassed app.state.expert_invoker")

    monkeypatch.setattr(
        "clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _direct_spawn_forbidden
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="analyze"))

    assert len(spy.specs) == 1
    assert spy.specs[0].child_expert_id == "data_expert"
    assert result["task_id"] == "task_via_invoker"


def test_spawn_runtime_has_no_direct_spawn_substrate_reference() -> None:
    """P2.6 deletion lock: the routing owner cannot reaccrete the direct call."""

    from clio_agent.gact.agents import spawn_runtime

    source = Path(spawn_runtime.__file__).read_text(encoding="utf-8")
    assert "spawn_child_turn_threadsafe" not in source


def test_spawn_wait_and_check_all_route_through_expert_invoker(monkeypatch) -> None:
    """P2.6: the complete model-facing operation set crosses one invoker stub."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(registry)
    spy = _ProtocolSpy(registry)
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="data_expert", task="analyze")
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)
        tools["check_agent_tasks"].func(task_ids=["task_done"])

    assert len(spy.specs) == 1
    assert [handle.task_id for handle, _timeout in spy.wait_calls] == ["task_done"]
    assert [[handle.task_id for handle in batch] for batch in spy.check_calls] == [["task_done"]]


def test_spawn_agent_task_spawn_error_returns_reason_and_emits_nothing(monkeypatch) -> None:
    app = _fake_app()
    emitted = _capture_emits(monkeypatch)

    def _raise(spec: Any) -> Any:
        raise SpawnError("child not declared", reason="undeclared_child")

    monkeypatch.setattr(app.state.expert_invoker, "invoke", _raise)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="ghost_expert", task="x"))

    # Typed reason surfaced structurally; NO delegation.started for a refused spawn.
    assert result == {"error": "undeclared_child", "message": "child not declared"}
    assert emitted == []


def test_wait_agent_tasks_completed_returns_wire_payload_and_emits_completed(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    # The full child answer lives in the child session's message store, keyed by the
    # result's message_ref — the tool re-reads it verbatim (the excerpt is only the
    # registry's bounded copy).
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0))

    (payload,) = result["results"]
    assert payload["task_id"] == "task_done"
    assert payload["status"] == "completed"
    assert payload["stage"] == "delegate.completed"
    assert payload["output"] == "child produced the staged CSV"
    # Resolved from the real message (not the fallback path) → no degradation marker.
    assert "output_source" not in payload
    assert payload["workflow_state"] == {"profile": {"status": "ready", "rows": 1024}}
    assert payload["agent_id"] == "data_expert"
    assert payload["parent_id"] == "main"

    # One completed event (flowing the completion payload + return-direction block),
    # then the parent_resumed event that re-pins the active-agent indicator (finding [6]).
    assert [e["event_type"] for e in emitted] == [
        "blueprint.delegation.completed",
        "blueprint.delegation.parent_resumed",
    ]
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


def test_wait_agent_tasks_return_key_order_matches_declared_tail(monkeypatch) -> None:
    """Harmless-but-matching courtesy (owner amendment): the model-facing return's
    TOP-LEVEL key order is ``results``, then ``workflow_state_conflicts``, then
    ``merged_workflow_state`` LAST — matching the declared structured_content
    shape's tail so the raw-JSON view reads the same way. This is NOT the
    presentation mechanism (see the declared-structured_content test below); it
    is a courtesy that must still hold."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        raw = tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)

    # Content is unchanged (parses to the same rows the wire-payload test above
    # asserts) — only ORDER is under test here, on the raw JSON text itself
    # (json.loads discards key order, so the earlier content test cannot see it).
    positions = {
        key: raw.index(f'"{key}"')
        for key in ("results", "workflow_state_conflicts", "merged_workflow_state")
    }
    assert sorted(positions, key=positions.get) == [
        "results",
        "workflow_state_conflicts",
        "merged_workflow_state",
    ]


def test_wait_agent_tasks_declares_typed_structured_content_shape(monkeypatch) -> None:
    """Owner ruling (P5 wire semantics): wait_agent_tasks DECLARES a typed
    structured payload for the wire's structured_content channel — summary line
    FIRST, per-task ``results`` rows, ``workflow_state_conflicts``, then
    ``merged_workflow_state`` LAST — instead of the UI inferring presentation
    from JSON key order. The declaration rides ``declare_structured_content``
    (tool_instrumentation's one-shot side channel), NOT the tool's own return
    value, so the model-facing return (asserted separately above) is untouched."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    _capture_emits(monkeypatch)
    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)

    assert len(declared) == 1
    shape = declared[0]
    assert list(shape.keys()) == [
        "summary",
        "results",
        "workflow_state_conflicts",
        "merged_workflow_state",
    ]
    assert shape["summary"].startswith("waited ")
    assert "1 completed" in shape["summary"]
    (row,) = shape["results"]
    # The compact UI-ladder row: display name (the SAME rule waited_tasks uses),
    # typed status, duration, and the ALREADY-BOUNDED excerpt (never the full
    # verbatim output — that stays on the model-facing row, the #880 contract).
    assert row == {
        "name": "data_expert #1",
        "status": "completed",
        "duration_ms": 0.0,
        "answer_excerpt": "child produced the staged CSV",
    }
    assert shape["workflow_state_conflicts"] == []
    assert shape["merged_workflow_state"] == {"profile": {"status": "ready", "rows": 1024}}


# --------------------------------------------------------------------------- #
# check_agent_tasks — the SAME declared structured_content grammar (P5).       #
# --------------------------------------------------------------------------- #


def test_check_agent_tasks_declares_typed_structured_content_shape(monkeypatch) -> None:
    """check_agent_tasks gets wait_agent_tasks's OWN treatment: a tally ``message``
    FIRST, then the SAME per-task rows the model-facing return already carries."""

    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_done"))
    registry.register(
        AgentTask(
            task_id="task_running",
            parent_session_id="sess_x",
            child_session_id="child_2",
            agent_ref={"expert_id": "hpc_expert", "requesting_expert_id": "main"},
            status="running",
        )
    )
    app = _fake_app(registry)
    _capture_emits(monkeypatch)
    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert", "hpc_expert"}, monkeypatch)
        tools["check_agent_tasks"].func()

    assert len(declared) == 1
    shape = declared[0]
    assert list(shape.keys()) == ["message", "tasks"]
    assert shape["message"] == "2 tasks: 1 running, 1 completed"
    assert {row["task_id"] for row in shape["tasks"]} == {"task_done", "task_running"}


def test_check_agent_tasks_structured_content_empty_case(monkeypatch) -> None:
    """No spawned tasks at all -> the honest "no tasks" message, never "0 tasks: "."""

    app = _fake_app()
    _capture_emits(monkeypatch)
    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["check_agent_tasks"].func()

    assert declared == [{"message": "no tasks", "tasks": []}]


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
    # A FAILED child still re-pins the parent (finding [6]): failed + parent_resumed.
    assert [e["event_type"] for e in emitted] == [
        "blueprint.delegation.failed",
        "blueprint.delegation.parent_resumed",
    ]
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

    def _fake_spawn(spec: Any) -> Any:
        spawn_calls.append(spec.child_expert_id)
        return SimpleNamespace(
            task_id=f"task_{spec.child_expert_id}", status="running", run_index=0, queued_reason=""
        )

    monkeypatch.setattr(app.state.expert_invoker, "invoke", _fake_spawn)

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
            {
                "task_id": "task_data_expert",
                "status": "running",
                "run_index": 0,
                "queued_reason": "",
                "handle_id": "task_data_expert",
                "run_label": "data_expert #1",
                "live_state": "running",
                "host": "local",
                "placement": "local",
            },
            {
                "task_id": "task_hpc_expert",
                "status": "running",
                "run_index": 0,
                "queued_reason": "",
                "handle_id": "task_hpc_expert",
                "run_label": "hpc_expert #1",
                "live_state": "running",
                "host": "local",
                "placement": "local",
            },
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


class _GroupSpy(_InvokeSpy):
    """Invoker spy that projects a TaskSpec's spawn_group_id/group_size onto its
    returned handle exactly like the real substrate does (spawn_child_turn's
    minted AgentTask -> TaskHandle.from_task) — proves the id
    spawn_agents_parallel mints reaches the started Part without driving the
    full real spawn machinery (that real, end-to-end path is covered
    separately in test_spawn_ensemble_s5.py)."""

    def invoke(self, spec: Any) -> TaskHandle:
        self.specs.append(spec)
        return TaskHandle(
            task_id=f"task_{spec.child_expert_id}",
            parent_session_id=spec.parent_session_id,
            child_session_id=f"child_{spec.child_expert_id}",
            status="running",
            run_index=0,
            depth=spec.depth,
            spawn_group_id=spec.spawn_group_id,
            group_size=spec.group_size,
        )


def test_spawn_agents_parallel_mints_one_group_id_shared_by_every_spawn(monkeypatch) -> None:
    """spawn_agents_parallel mints ONE spawn_group_id for the whole call and
    stamps it (+ group_size == len(spawns)) on EVERY sibling's TaskSpec —
    reaching the started expert_handoff Part's metadata via the returned
    TaskHandle. The blueprint.fanout.started event carries the SAME id for
    cross-referencing."""

    app = _fake_app()
    spy = _GroupSpy()
    app.state.expert_invoker = spy
    emitted = _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    spawns = [
        {"agent": "geo_a", "task": "t1"},
        {"agent": "geo_b", "task": "t2"},
        {"agent": "geo_c", "task": "t3"},
    ]
    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"geo_a", "geo_b", "geo_c"}, monkeypatch)
        json.loads(tools["spawn_agents_parallel"].func(spawns=spawns))

    group_ids = {spec.spawn_group_id for spec in spy.specs}
    assert len(group_ids) == 1, "every sibling spawn must share ONE minted group id"
    (group_id,) = group_ids
    assert group_id.startswith("fanout_")
    assert {spec.group_size for spec in spy.specs} == {3}

    fanout_started = next(e for e in emitted if e["event_type"] == "blueprint.fanout.started")
    assert fanout_started["payload"]["spawn_group_id"] == group_id

    started_handoffs = [p for _sid, p in parts if p.type == "expert_handoff"]
    assert len(started_handoffs) == 3
    assert {p.metadata["spawn_group_id"] for p in started_handoffs} == {group_id}
    assert {p.metadata["group_size"] for p in started_handoffs} == {3}


def test_spawn_agent_task_single_spawn_carries_neither_group_field(monkeypatch) -> None:
    """A bare spawn_agent_task call — never through spawn_agents_parallel — mints
    NEITHER spawn_group_id NOR group_size: the TaskSpec carries the empty/0
    default, and the started Part's metadata has NEITHER key (absent, not
    null/empty — the UI never sees a grouping signal for a solo spawn)."""

    app = _fake_app()
    spy = _GroupSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="analyze"))

    assert len(spy.specs) == 1
    assert spy.specs[0].spawn_group_id == ""
    assert spy.specs[0].group_size == 0
    (started,) = [p for _sid, p in parts if p.type == "expert_handoff"]
    assert "spawn_group_id" not in started.metadata
    assert "group_size" not in started.metadata


class _PartialFailureSpy(_InvokeSpy):
    """Invoker spy that refuses ONE specific child id with a typed SpawnError
    and otherwise succeeds like ``_GroupSpy`` (stamping spawn_group_id/group_size
    onto the returned handle) -- proves a parallel batch's declared group_size
    reconciles even when one sibling never gets a child session (finding [E])."""

    def __init__(self, fail_agent: str) -> None:
        super().__init__()
        self._fail_agent = fail_agent

    def invoke(self, spec: Any) -> TaskHandle:
        if spec.child_expert_id == self._fail_agent:
            raise SpawnError("child not declared", reason="undeclared_child")
        self.specs.append(spec)
        return TaskHandle(
            task_id=f"task_{spec.child_expert_id}",
            parent_session_id=spec.parent_session_id,
            child_session_id=f"child_{spec.child_expert_id}",
            status="running",
            run_index=0,
            depth=spec.depth,
            spawn_group_id=spec.spawn_group_id,
            group_size=spec.group_size,
        )


def test_spawn_agents_parallel_failed_sibling_still_reconciles_group_size(monkeypatch) -> None:
    """Failing-first for finding [E]: spawn_runtime.py:776 set group_size to the
    declared batch size, but ``_do_spawn`` returned early on SpawnError BEFORE
    appending any Part — a refused sibling silently vanished, leaving
    group_size=3 with only 2 parts forever and no visible reason. A batch of 3
    with ONE undeclared child must still produce 3 expert_handoff Parts sharing
    the SAME spawn_group_id/group_size; the refused slot concludes DIRECTLY on
    the terminal lane (status "failed", stage "delegate.completed") carrying the
    typed SpawnError reason — never a dangling started with no terminal."""

    app = _fake_app()
    spy = _PartialFailureSpy("geo_b")
    app.state.expert_invoker = spy
    emitted = _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    spawns = [
        {"agent": "geo_a", "task": "t1"},
        {"agent": "geo_b", "task": "t2"},  # undeclared -> refused mid-batch
        {"agent": "geo_c", "task": "t3"},
    ]
    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"geo_a", "geo_b", "geo_c"}, monkeypatch)
        result = json.loads(tools["spawn_agents_parallel"].func(spawns=spawns))

    # The refused slot's own model-facing return still carries the typed reason.
    assert result["spawned"][1] == {"error": "undeclared_child", "message": "child not declared"}

    handoffs = [p for _sid, p in parts if p.type == "expert_handoff"]
    assert len(handoffs) == 3, "one Part per declared slot, including the refused one"
    group_ids = {p.metadata["spawn_group_id"] for p in handoffs}
    assert len(group_ids) == 1, "every sibling — success or failure — shares ONE group id"
    assert {p.metadata["group_size"] for p in handoffs} == {3}

    by_child = {p.child_agent: p for p in handoffs}
    failed = by_child["geo_b"]
    assert failed.status == "failed"
    assert failed.stage == "delegate.completed"
    assert failed.metadata["error"] == "undeclared_child"
    # The two real siblings still spawned normally, unaffected by geo_b's refusal.
    assert by_child["geo_a"].status == "running"
    assert by_child["geo_c"].status == "running"

    # blueprint.fanout.started declares the SAME group id + the declared total
    # (finding [13]: it carried spawn_group_id but not group_size before).
    fanout_started = next(e for e in emitted if e["event_type"] == "blueprint.fanout.started")
    assert fanout_started["payload"]["spawn_group_id"] == next(iter(group_ids))
    assert fanout_started["payload"]["group_size"] == 3


def test_spawn_agent_task_bare_spawn_error_still_emits_nothing(monkeypatch) -> None:
    """A BARE (non-batch) spawn_agent_task failure has no group to reconcile --
    the finding [E] fix is scoped to spawn_agents_parallel batches, so a solo
    refusal keeps its pre-existing behavior: no Part, no event, just the typed
    error returned to the model (regression guard for
    test_spawn_agent_task_spawn_error_returns_reason_and_emits_nothing)."""

    app = _fake_app()
    emitted = _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    def _raise(spec: Any) -> Any:
        raise SpawnError("child not declared", reason="undeclared_child")

    monkeypatch.setattr(app.state.expert_invoker, "invoke", _raise)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["spawn_agent_task"].func(agent="ghost_expert", task="x"))

    assert result == {"error": "undeclared_child", "message": "child not declared"}
    assert emitted == []
    assert parts == []


# ---------------------------------------------------------------------------
# Fix 1 (#880 verbatim output): the delegation ``output`` is the child's FULL
# answer byte-for-byte, re-read at wait-time — NOT the registry's bounded excerpt.
# ---------------------------------------------------------------------------


def test_wait_returns_child_answer_verbatim_past_the_excerpt_bound(monkeypatch) -> None:
    # No leading/trailing whitespace: _message_text strips (as it does when minting
    # the excerpt), so the verbatim contract is byte-for-byte on the stripped body.
    # Past the 2000-char excerpt bound but well under the #1306 digest cap (8000) --
    # the oversize/digest case has its OWN test below with a fixture sized past
    # that cap (test_wait_digests_oversize_child_answer_with_durable_reference).
    big = " | ".join(f"line-{i:04d} the child's deliverable" for i in range(120))
    assert 2000 < len(big) < 8_000, "fixture must sit strictly between the two bounds"
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_big",
            parent_session_id="sess_x",
            child_session_id="child_big",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": big[:2000], "workflow_state": {}, "message_ref": "msg_big"},
        )
    )
    app = _fake_app(
        registry, messages={"child_big": [_assistant_message("msg_big", "child_big", big)]}
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_big"], timeout_s=1.0))

    (payload,) = result["results"]
    # Byte-identical, FULL length — the truncated excerpt would be 2000 chars.
    assert payload["output"] == big
    assert len(payload["output"]) == len(big) > 2000
    assert "output_source" not in payload


# ---------------------------------------------------------------------------
# #1306: an oversize completed child's output is DIGESTED on the model-facing
# wait_agent_tasks row (a durable reference replaces the inline full text),
# while the UI/semantic-event lane (the #880 return Part + delegation event)
# keeps carrying the FULL verbatim output untouched — a different lane, same
# split mcp_result_projection.py already draws for one MCP call's result.
# ---------------------------------------------------------------------------


def test_wait_digests_oversize_child_answer_with_durable_reference(monkeypatch) -> None:
    # Sized to match the #1306 live evidence: one completed research child's own
    # output measured roughly 14-15K chars (the proven-bad two-child observation
    # totalled 30,546 chars). Comfortably over the 8000-char digest cap.
    big = " | ".join(f"line-{i:04d} the child's deliverable" for i in range(400))
    assert len(big) > 8_000, "fixture must exceed the #1306 digest cap"
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_big",
            parent_session_id="sess_x",
            child_session_id="child_big",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": big[:2000], "workflow_state": {}, "message_ref": "msg_big"},
        )
    )
    app = _fake_app(
        registry, messages={"child_big": [_assistant_message("msg_big", "child_big", big)]}
    )
    emitted = _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_big"], timeout_s=1.0))

    (payload,) = result["results"]
    # The MODEL-facing row never carries the raw full text a second time. The
    # envelope is a nested OBJECT (finding 3 of the #1306 review round) -- the
    # WHOLE wait_agent_tasks return was already one json.loads above, so
    # payload["output"] is already a parsed dict here, never a second
    # JSON-encoded string to re-parse (that double-encoding was the bug).
    envelope = payload["output"]
    assert isinstance(envelope, dict)
    assert "output" not in envelope
    assert big not in json.dumps(envelope)
    assert envelope["_clio"]["status"] == "digested"
    assert envelope["_clio"]["reason"] == "agent_task_output_oversize"
    assert envelope["_clio"]["original_chars"] == len(big)
    assert envelope["answer_excerpt"] == big[:2000]
    assert envelope["task_id"] == "task_big"
    assert envelope["child_session_id"] == "child_big"
    assert envelope["message_ref"] == "msg_big"
    assert envelope["fetch_full_output"] == {
        "tool": "get_agent_task_output",
        "args": {"task_id": "task_big"},
    }

    # The UI return Part + the delegation.completed semantic event are UNTOUCHED —
    # the #880 verbatim contract still holds on that separate lane.
    assert len(parts) == 1
    _sid, part = parts[0]
    assert part.metadata["output"] == big
    completed = next(e for e in emitted if e["event_type"] == "blueprint.delegation.completed")
    assert completed["payload"]["output"] == big


def test_wait_verbatim_at_default_cap_boundary_through_real_path(monkeypatch) -> None:
    """#1306 review round, finding 9a: a verbatim regression pin at the DEFAULT
    cap boundary (no set_config override) through the REAL wait path -- proves
    the boundary itself (len(output) <= cap stays inline), not a reconfigured
    one, so drift in the resolved default silently breaks this."""

    from clio_agent.gact.agents.agent_task_output_digest import (
        agent_task_output_digest_chars,
    )

    default_cap = agent_task_output_digest_chars()
    assert default_cap == 8_000, "this pin assumes the documented default; update it if it moves"
    exact = "x" * default_cap
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_exact",
            parent_session_id="sess_x",
            child_session_id="child_exact",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": exact[:2000], "workflow_state": {}, "message_ref": "msg_e"},
        )
    )
    app = _fake_app(
        registry, messages={"child_exact": [_assistant_message("msg_e", "child_exact", exact)]}
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_exact"], timeout_s=1.0))

    (payload,) = result["results"]
    assert payload["output"] == exact
    assert len(payload["output"]) == default_cap


def test_check_agent_tasks_never_inlines_full_output_regardless_of_size(monkeypatch) -> None:
    """Regression pin: check_agent_tasks already returns only the bounded
    ``answer_excerpt`` (never the raw ``output``) — true both below and above
    the #1306 digest cap, so this tool needed no change to satisfy #1306."""

    big = " | ".join(f"line-{i:04d} the child's deliverable" for i in range(400))
    assert len(big) > 8_000
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_big",
            parent_session_id="sess_x",
            child_session_id="child_big",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": big[:2000], "workflow_state": {}, "message_ref": "msg_big"},
        )
    )
    app = _fake_app(
        registry, messages={"child_big": [_assistant_message("msg_big", "child_big", big)]}
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["check_agent_tasks"].func(task_ids=["task_big"]))

    (row,) = result["tasks"]
    assert "output" not in row["result"]
    assert row["result"]["answer_excerpt"] == big[:2000]
    assert row["result"]["message_ref"] == "msg_big"
    assert row["result"]["child_session_id"] == "child_big"


def test_get_agent_task_output_tool_fetches_full_output_for_completed_task(monkeypatch) -> None:
    """The #1306 recoverability half: the digest envelope's fetch_full_output
    names this tool, and it resolves the FULL stored output verbatim."""

    big = " | ".join(f"line-{i:04d} the child's deliverable" for i in range(400))
    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_big"))
    app = _fake_app(
        registry,
        messages={"child_1": [_assistant_message("msg_1", "child_1", big)]},
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["get_agent_task_output"].func(task_id="task_big"))

    assert result["task_id"] == "task_big"
    assert result["status"] == "completed"
    assert result["output"] == big


def test_get_agent_task_output_tool_exempt_from_model_tool_result_chars(monkeypatch) -> None:
    """#1306 review round, finding 7: the crux regression pin. get_agent_task_output
    is a NATIVE tool, not an MCP call -- it must never be silently swept into the
    generic MCP-result bound a future cleanup could apply uniformly. Fetch
    something longer than model_tool_result_chars() through the REAL tool and
    assert byte-length equality: the truncation exemption is an emergent
    property today (only mcp_executor.py calls bounded_model_tool_result), not
    an enforced contract -- this pin is what would catch it silently regressing."""

    from clio_agent.tools.mcp_result_projection import model_tool_result_chars

    bound = model_tool_result_chars()
    big = "x" * (bound + 5_000)
    assert len(big) > bound
    registry = AgentTaskRegistry()
    registry.register(_completed_task("task_huge"))
    app = _fake_app(
        registry,
        messages={"child_1": [_assistant_message("msg_1", "child_1", big)]},
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["get_agent_task_output"].func(task_id="task_huge"))

    assert len(result["output"]) == len(big)
    assert result["output"] == big


def test_get_agent_task_output_tool_returns_material_for_failed_task(monkeypatch) -> None:
    """#1306 review round, finding 9b: the chosen contract for get_agent_task_output
    on a FAILED (terminal, not completed) task is the FULL stored material --
    whatever answer text existed plus the typed error_reason -- never a refusal.
    Only an unknown id or a still-in-flight one refuses."""

    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_bad",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="failed",
            error_reason="agent_error",
            result={
                "answer_excerpt": "partial draft before it failed",
                "workflow_state": {},
                "message_ref": "msg_1",
            },
        )
    )
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "partial draft before it failed")]
        },
    )
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["get_agent_task_output"].func(task_id="task_bad"))

    assert "error" not in result
    assert result["status"] == "failed"
    assert result["output"] == "partial draft before it failed"
    assert result["error_reason"] == "agent_error"


# ---------------------------------------------------------------------------
# #1306 review round, finding 1 (the crux): input_task_ids on spawn_agent_task /
# spawn_agents_parallel -- the OTHER recoverability direction. The parent hands
# a CHILD (never itself) the full stored output of its own already-finished
# tasks as labeled evidence, so a critic never forces the coordinator to
# fetch-and-reinline researcher material through itself.
# ---------------------------------------------------------------------------


def _register_completed_researcher(
    registry: AgentTaskRegistry, task_id: str, child_session_id: str
) -> None:
    registry.register(
        AgentTask(
            task_id=task_id,
            parent_session_id="sess_x",
            child_session_id=child_session_id,
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="completed",
            result={"answer_excerpt": "excerpt", "workflow_state": {}, "message_ref": "msg_1"},
        )
    )


def test_spawn_agent_task_input_task_ids_forwards_full_evidence_to_child(monkeypatch) -> None:
    """The crux flow: a parent spawns a critic with two already-finished
    researchers' full output as evidence. The CHILD's own briefing carries
    both full answers, clearly labeled; the parent's spawn call itself never
    touches that text (it only passed ids)."""

    registry = AgentTaskRegistry()
    _register_completed_researcher(registry, "task_r1", "child_r1")
    _register_completed_researcher(registry, "task_r2", "child_r2")
    app = _fake_app(
        registry,
        messages={
            "child_r1": [_assistant_message("msg_1", "child_r1", "researcher ONE's full report")],
            "child_r2": [_assistant_message("msg_1", "child_r2", "researcher TWO's full report")],
        },
    )
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"researcher", "critic"}, monkeypatch)
        result = json.loads(
            tools["spawn_agent_task"].func(
                agent="critic",
                task="synthesize a verdict",
                input_task_ids=["task_r1", "task_r2"],
            )
        )

    assert result["task_id"] == "task_via_invoker"
    assert len(spy.specs) == 1
    briefing = spy.specs[0].task_text
    assert briefing.startswith("synthesize a verdict")
    assert "researcher ONE's full report" in briefing
    assert "researcher TWO's full report" in briefing
    assert "task_r1" in briefing
    assert "task_r2" in briefing
    assert "researcher" in briefing


def test_spawn_input_task_ids_started_part_stays_bare_but_names_the_ids(monkeypatch) -> None:
    """#1306 final review round, findings N3 + N4: the PARENT's own STARTED
    handoff Part must stay lean (bare task text, never the evidence text the
    child received) while still being transcript-honest that an input
    existed -- the bounded id LIST, never the material itself."""

    registry = AgentTaskRegistry()
    _register_completed_researcher(registry, "task_r1", "child_r1")
    app = _fake_app(
        registry,
        messages={
            "child_r1": [_assistant_message("msg_1", "child_r1", "researcher's full report")]
        },
    )
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"researcher", "critic"}, monkeypatch)
        tools["spawn_agent_task"].func(
            agent="critic", task="synthesize a verdict", input_task_ids=["task_r1"]
        )

    (_sid, part) = parts[0]
    assert part.metadata["question"] == "synthesize a verdict"  # N3: the BARE task
    assert "researcher's full report" not in part.metadata["question"]  # N3: never the evidence
    assert part.metadata["input_task_ids"] == ["task_r1"]  # N4: ids only


def test_spawn_without_input_task_ids_started_part_carries_no_field(monkeypatch) -> None:
    """The absent-not-empty convention this Part already follows for
    spawn_group_id/group_size: a bare spawn never invents the field."""

    app = _fake_app()
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="data_expert", task="profile the CSV")

    (_sid, part) = parts[0]
    assert "input_task_ids" not in part.metadata


def test_spawn_agents_parallel_per_entry_input_task_ids(monkeypatch) -> None:
    """The batch path threads input_task_ids per spawn entry -- one child gets
    evidence, its sibling in the SAME batch call is unaffected."""

    registry = AgentTaskRegistry()
    _register_completed_researcher(registry, "task_r1", "child_r1")
    app = _fake_app(
        registry,
        messages={
            "child_r1": [_assistant_message("msg_1", "child_r1", "researcher's full report")]
        },
    )
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"researcher", "critic"}, monkeypatch)
        result = json.loads(
            tools["spawn_agents_parallel"].func(
                spawns=[
                    {"agent": "critic", "task": "review", "input_task_ids": ["task_r1"]},
                    {"agent": "researcher", "task": "dig deeper"},
                ]
            )
        )

    assert len(result["spawned"]) == 2
    assert len(spy.specs) == 2
    assert "researcher's full report" in spy.specs[0].task_text
    assert spy.specs[1].task_text == "dig deeper"


def test_spawn_agent_task_input_task_ids_unknown_refuses_no_child_created(monkeypatch) -> None:
    """Failing-first: a foreign/unknown/incomplete id refuses the WHOLE spawn --
    no child is ever created (the invoker is never even called)."""

    app = _fake_app()
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"critic"}, monkeypatch)
        result = json.loads(
            tools["spawn_agent_task"].func(
                agent="critic", task="review", input_task_ids=["task_ghost"]
            )
        )

    assert result["error"] == "task_ref_unknown"
    assert spy.specs == []


def test_spawn_agent_task_input_task_ids_foreign_refuses_no_child_created(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_other",
            parent_session_id="sess_someone_else",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "someone_else"},
            status="completed",
            result={"answer_excerpt": "", "workflow_state": {}, "message_ref": ""},
        )
    )
    app = _fake_app(registry)
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"critic"}, monkeypatch)
        result = json.loads(
            tools["spawn_agent_task"].func(
                agent="critic", task="review", input_task_ids=["task_other"]
            )
        )

    assert result["error"] == "task_ref_not_yours"
    assert spy.specs == []


def test_spawn_agent_task_input_task_ids_incomplete_refuses_no_child_created(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_running",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "researcher", "requesting_expert_id": "main"},
            status="running",
        )
    )
    app = _fake_app(registry)
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"researcher", "critic"}, monkeypatch)
        result = json.loads(
            tools["spawn_agent_task"].func(
                agent="critic", task="review", input_task_ids=["task_running"]
            )
        )

    assert result["error"] == "task_ref_not_terminal"
    assert spy.specs == []


def test_spawn_agent_task_without_input_task_ids_is_unaffected(monkeypatch) -> None:
    """Regression pin: a bare spawn_agent_task call (no input_task_ids) keeps
    the child's task_text byte-identical to today's passing flows."""

    app = _fake_app()
    spy = _InvokeSpy()
    app.state.expert_invoker = spy
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="data_expert", task="profile the CSV")

    assert spy.specs[0].task_text == "profile the CSV"


def test_get_agent_task_output_tool_typed_error_for_unknown_task(monkeypatch) -> None:
    app = _fake_app()
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["get_agent_task_output"].func(task_id="task_ghost"))

    assert result == {"error": "unknown_task", "task_id": "task_ghost"}


def test_get_agent_task_output_tool_typed_error_for_non_terminal_task(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_running",
            parent_session_id="sess_x",
            child_session_id="child_1",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="running",
        )
    )
    app = _fake_app(registry)
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["get_agent_task_output"].func(task_id="task_running"))

    assert result == {"error": "task_not_terminal", "task_id": "task_running", "status": "running"}


def test_wait_falls_back_to_excerpt_with_typed_marker_when_message_gone(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(
        AgentTask(
            task_id="task_gone",
            parent_session_id="sess_x",
            child_session_id="child_gone",
            agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
            status="completed",
            result={
                "answer_excerpt": "bounded excerpt only",
                "workflow_state": {},
                "message_ref": "msg_absent",
            },
        )
    )
    # message_ref points at a message that is not in the store (child pruned).
    app = _fake_app(registry, messages={})
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_gone"], timeout_s=1.0))

    (payload,) = result["results"]
    # Never silent: the excerpt is served WITH a typed degradation marker.
    assert payload["output"] == "bounded excerpt only"
    assert payload["output_source"] == "excerpt_fallback"
    assert payload["output_fallback_reason"] == "child_message_gone"


# ---------------------------------------------------------------------------
# Fix 2 (unknown task id): an unknown/typo id returns immediately — it must NOT
# block on a phantom never-set Event for the full timeout budget.
# ---------------------------------------------------------------------------


def test_unknown_task_id_returns_immediately_not_after_timeout(monkeypatch) -> None:
    app = _fake_app()
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        start = time.monotonic()
        # A LARGE timeout: the old code (event().wait BEFORE get) would block the
        # full 30s on an unknown id's freshly-minted, never-set Event.
        result = json.loads(tools["wait_agent_tasks"].func(task_ids=["ghost"], timeout_s=30.0))
        elapsed = time.monotonic() - start

    assert result["results"] == [{"task_id": "ghost", "error": "unknown_task"}]
    assert elapsed < 1.0, f"unknown id blocked {elapsed:.1f}s (should be instant)"
    # And it never leaked a phantom Event into the registry.
    assert "ghost" not in app.state.agent_task_registry._events


# ---------------------------------------------------------------------------
# Fix 3 (once-per-task terminal event): the RESULT ROW is returned on every wait,
# but the terminal wire EVENT fires exactly once (server owns the de-duped stream).
# ---------------------------------------------------------------------------


def test_double_wait_emits_terminal_event_once_but_returns_row_each_time(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        first = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0))
        second = json.loads(tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0))

    # The row is RETURNED both times (the model may legitimately re-collect).
    assert first["results"][0]["output"] == "child produced the staged CSV"
    assert second["results"][0]["output"] == "child produced the staged CSV"
    # The terminal EVENTS (completed + parent_resumed) are emitted exactly ONCE across
    # the two waits — the re-wait claims nothing from the once-per-task gate.
    assert [e["event_type"] for e in emitted] == [
        "blueprint.delegation.completed",
        "blueprint.delegation.parent_resumed",
    ]


def test_same_terminal_id_twice_in_one_batch_emits_event_once(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        result = json.loads(
            tools["wait_agent_tasks"].func(task_ids=["task_done", "task_done"], timeout_s=1.0)
        )

    # Two rows returned (once per requested id), but exactly one terminal + one
    # parent_resumed wire event.
    assert len(result["results"]) == 2
    assert [e["event_type"] for e in emitted] == [
        "blueprint.delegation.completed",
        "blueprint.delegation.parent_resumed",
    ]


# ---------------------------------------------------------------------------
# Fix 4 (computed depth): the spawn tools compute depth from the CURRENT session,
# so nesting increments through the real tool path and the backstop is reachable.
# ---------------------------------------------------------------------------


def _seed_child_depth(app: Any, sid: str, depth: int) -> None:
    task = AgentTask(
        task_id=f"seed_{depth}",
        parent_session_id="root",
        child_session_id=sid,
        agent_ref={"expert_id": "main", "requesting_expert_id": "main"},
        depth=depth,
        status="running",
    )
    app.state.sessions.seed(sid, task.to_metadata())


def test_spawn_depth_computed_and_increments_through_tool_path(monkeypatch) -> None:
    captured: list[Any] = []

    def _capture_spawn(spec: Any) -> Any:
        captured.append(spec)
        return SimpleNamespace(
            task_id=f"task_d{spec.depth}", status="running", run_index=0, queued_reason=""
        )

    # Stub the started-Part append (the bare app has no transcript/bus); this test
    # asserts on computed depth, not on the Part.
    _capture_parts(monkeypatch)

    app = _fake_app()
    monkeypatch.setattr(app.state.expert_invoker, "invoke", _capture_spawn)
    _seed_child_depth(app, "child_d1", 1)
    _seed_child_depth(app, "child_d2", 2)

    # A ROOT session (no agent-task projection) spawns at depth 1.
    with _active_turn(app, session_id="root_sess"):
        tools = _tools_by_name(app, "main", {"main"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="main", task="go")
    assert captured[-1].depth == 1

    # A depth-1 child spawns at depth 2.
    with _active_turn(app, session_id="child_d1"):
        tools = _tools_by_name(app, "main", {"main"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="main", task="go")
    assert captured[-1].depth == 2

    # A depth-2 child spawns at depth 3.
    with _active_turn(app, session_id="child_d2"):
        tools = _tools_by_name(app, "main", {"main"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="main", task="go")
    assert captured[-1].depth == 3


def test_spawn_at_backstop_depth_rejected_through_tool_path(tmp_path: Path, monkeypatch) -> None:
    """The runaway backstop is reachable through the REAL tool path: a child already
    at MAX_SPAWN_DEPTH computes depth+1 and its spawn is refused typed (the old code
    always passed depth=1, so the guard was dead)."""

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda a, pid, session_id="": {"main"},
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        parent = app.state.sessions.create(workspace_id="ws_default", title="p")
        child = app.state.sessions.create(
            workspace_id="ws_default", title="c", parent_session_id=parent.id
        )
        seed = AgentTask(
            task_id="task_deep",
            parent_session_id=parent.id,
            child_session_id=child.id,
            agent_ref={"expert_id": "main", "requesting_expert_id": "main"},
            depth=MAX_SPAWN_DEPTH,
            status="running",
            created_at="2026-07-18T00:00:00+00:00",
            updated_at="2026-07-18T00:00:00+00:00",
        )
        app.state.sessions.update(child.id, metadata_patch=seed.to_metadata())

        with _active_turn(app, session_id=child.id):
            from clio_agent.gact.agents import spawn_runtime

            tools = {
                t.name: t for t in spawn_runtime.build_spawn_runtime_tools(_Agent(), _Def("main"))
            }
            result = json.loads(tools["spawn_agent_task"].func(agent="main", task="go"))

    assert result["error"] == "spawn_depth_exceeded"


# ---------------------------------------------------------------------------
# Finding [7] (transcript render parity): spawned-child delegations render in the
# PARENT transcript via expert_handoff Parts (the canonical transcriptDelegationModel.ts
# keys the header / nesting / return row off Parts, NOT the semantic events). Without
# these Parts a spawned child renders NOTHING (a failed child is invisible outside raw
# tool JSON). Sabotage: reverting the append seam makes every assertion here go red.
# ---------------------------------------------------------------------------


def test_spawn_appends_started_expert_handoff_part(monkeypatch) -> None:
    app = _fake_app()
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)
    monkeypatch.setattr(
        app.state.expert_invoker,
        "invoke",
        lambda spec: SimpleNamespace(
            task_id="task_abc", status="running", run_index=0, queued_reason=""
        ),
    )

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["spawn_agent_task"].func(agent="data_expert", task="profile the CSV")

    # Exactly one started Part, in the parent session, with the shape the pinned TUI
    # consumes (child/parent links for the depth graph, stage, task on metadata.question).
    assert len(parts) == 1
    sid, part = parts[0]
    assert sid == "sess_x"
    assert part.type == "expert_handoff"
    assert part.stage == "delegate.started"
    assert part.status == "running"
    assert part.parent_agent == "main"
    assert part.child_agent == "data_expert"
    assert part.agent_id == "main"
    assert part.metadata["question"] == "profile the CSV"
    assert part.metadata["stream_source"] == "live"


def test_wait_completed_appends_return_part_with_verbatim_output(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)

    assert len(parts) == 1
    _sid, part = parts[0]
    assert part.type == "expert_handoff"
    # #882: success concludes on the terminal lane (stage delegate.completed).
    assert part.stage == "delegate.completed"
    assert part.status == "completed"
    assert part.parent_agent == "main"
    assert part.child_agent == "data_expert"
    # #880: metadata.output is the child's FULL answer byte-for-byte (behind show more).
    assert part.metadata["output"] == "child produced the staged CSV"
    assert part.metadata["workflow_state"] == {"profile": {"status": "ready", "rows": 1024}}


def test_wait_failed_appends_return_part_on_terminal_lane_visible(monkeypatch) -> None:
    registry = AgentTaskRegistry()
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
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["wait_agent_tasks"].func(task_ids=["task_bad"], timeout_s=1.0)

    # A FAILED child is NOT invisible: it still gets a return Part, on the SAME terminal
    # lane (stage delegate.completed, #882) with status=failed and the typed reason.
    assert len(parts) == 1
    _sid, part = parts[0]
    assert part.stage == "delegate.completed"
    assert part.status == "failed"
    assert part.child_agent == "data_expert"
    assert part.metadata["output"] == ""
    assert part.metadata["error"] == "agent_error"


def test_return_part_appended_once_on_double_wait(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)

    # The return Part shares the once-per-task gate with the terminal event: exactly one
    # across both waits (no duplicate return row on a re-collect).
    assert len(parts) == 1
    assert parts[0][1].stage == "delegate.completed"


# ---------------------------------------------------------------------------
# Finding [6] (active-agent indicator): after a spawned child reaches a terminal
# state, blueprint.delegation.parent_resumed re-pins the executing agent to the parent.
# The TUI resets ONLY on parent_resumed (completed/failed fall through to the child).
# ---------------------------------------------------------------------------


def test_parent_resumed_event_re_pins_parent_after_terminal(monkeypatch) -> None:
    registry = AgentTaskRegistry()
    registry.register(_completed_task())
    app = _fake_app(
        registry,
        messages={
            "child_1": [_assistant_message("msg_1", "child_1", "child produced the staged CSV")]
        },
    )
    emitted = _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        tools["wait_agent_tasks"].func(task_ids=["task_done"], timeout_s=1.0)

    resumed = [e for e in emitted if e["event_type"] == "blueprint.delegation.parent_resumed"]
    assert len(resumed) == 1
    ev = resumed[0]
    # Re-pins to the PARENT: the parent is the actor, the child the subject.
    assert ev["actor"] == {"agent_id": "main", "role": "parent_expert"}
    assert ev["subject"] == {"agent_id": "data_expert", "role": "child_expert"}
    assert ev["payload"]["stage"] == "parent.resumed"
    assert ev["payload"]["resumed_from"] == "data_expert"


def test_orchestrator_max_iters_is_unlimited_by_default() -> None:
    """#1226 D1b (supersedes the #948 S4 scaling formula): 0/unlimited is the
    default react iteration budget, not a deterministic turn count of any
    shape. #948 S4's "scale with declared children" formula was itself a
    deterministic cap wearing a smarter-looking curve -- it starved a
    long-running orchestrator the exact way the old flat 5 did (live: the
    L3 run died at a turn budget mid-task instead of finishing or giving up
    by its own judgment). ``declared_children`` no longer changes the
    default at all -- see ``feedback_no_deterministic_turn_caps``. A cap
    survives ONLY as an explicit, blueprint-declared opt-in runaway
    backstop, honored verbatim in both directions."""

    from types import SimpleNamespace

    from clio_agent.gact.agents.builders import _tool_user_agent_max_iters

    leaf = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(leaf) == 0
    assert _tool_user_agent_max_iters(leaf, declared_children=0) == 0
    four_children = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(four_children, declared_children=4) == 0
    many = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(many, declared_children=10) == 0
    # An explicit blueprint param always wins, both directions -- the ONLY
    # sanctioned way a cap exists at all.
    pinned = SimpleNamespace(parameters={"max_iters": 7})
    assert _tool_user_agent_max_iters(pinned, declared_children=4) == 7
    pinned_unlimited = SimpleNamespace(parameters={"max_iters": 0})
    assert _tool_user_agent_max_iters(pinned_unlimited, declared_children=4) == 0


def test_orchestrator_max_iters_rejects_negative() -> None:
    """A negative max_iters is a genuine misconfiguration, not "unlimited" --
    only 0 carries that meaning."""

    from types import SimpleNamespace

    from clio_agent.gact.agents.builders import _tool_user_agent_max_iters

    with pytest.raises(ValueError, match="zero .unlimited. or positive"):
        _tool_user_agent_max_iters(SimpleNamespace(parameters={"max_iters": -1}))


# ---------------------------------------------------------------------------
# P2.10 (#1127): ONE spawn surface selects placement; grammar stays uniform.
# ---------------------------------------------------------------------------


class _PlacementSpy(_InvokeSpy):
    """Invoker spy returning the additive P2.10 run-handle vocabulary."""

    def __init__(self, placement: str, host: str) -> None:
        super().__init__()
        self.placement = placement
        self.host = host

    def invoke(self, spec: Any) -> TaskHandle:
        self.specs.append(spec)
        return TaskHandle(
            task_id=f"task_{self.host}",
            parent_session_id=spec.parent_session_id,
            child_session_id=f"child_{self.host}",
            status="running",
            run_index=0,
            depth=spec.depth,
            handle_id=f"task_{self.host}",
            run_label="data_expert #1",
            live_state="running",
            host=self.host,
            placement=self.placement,
        )


def test_spawn_surface_real_invokers_share_handle_and_part_grammar(
    tmp_path: Path, monkeypatch
) -> None:
    """The same tool drives real local/relay invokers with shape-identical output."""

    from tests.test_gact.test_invoker_s7 import (
        _declare,
        _FakeRelayBackend,
        _relay_invoker,
    )

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "spawn parity"}).json()["id"]
        app.state.relay_expert_invokers = {"ares": _relay_invoker(app, backend)}
        _capture_emits(monkeypatch)
        parts = _capture_parts(monkeypatch)
        with _active_turn(app, session_id=parent):
            tools = _tools_by_name(app, "main", {"main"}, monkeypatch)
            local = json.loads(
                tools["spawn_agent_task"].func(agent="main", task="local", placement="local")
            )
            relay = json.loads(
                tools["spawn_agent_task"].func(agent="main", task="relay", placement="relay:ares")
            )

        assert set(local) == set(relay)
        assert local["placement"] == "local"
        assert relay["placement"] == "relay:ares"
        assert len(parts) == 2
        assert set(parts[0][1].to_wire()) == set(parts[1][1].to_wire())
        local_task = app.state.agent_task_registry.get(local["task_id"])
        relay_task = app.state.agent_task_registry.get(relay["task_id"])
        assert local_task is not None and local_task.placement == "local"
        assert relay_task is not None and relay_task.placement == "relay:ares"


def test_one_placement_parameter_drives_local_and_relay_with_part_shape_parity(
    monkeypatch,
) -> None:
    """#1127: one spawn tool selects either invoker and preserves run-handle grammar."""

    app = _fake_app()
    local = _PlacementSpy("local", "local")
    relay = _PlacementSpy("relay:ares", "ares")
    app.state.expert_invoker = local
    app.state.relay_expert_invokers = {"ares": relay}
    _capture_emits(monkeypatch)
    parts = _capture_parts(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        local_wire = json.loads(
            tools["spawn_agent_task"].func(
                agent="data_expert", task="profile locally", placement="local"
            )
        )
        relay_wire = json.loads(
            tools["spawn_agent_task"].func(
                agent="data_expert", task="profile remotely", placement="relay:ares"
            )
        )

    assert len(local.specs) == len(relay.specs) == 1
    assert local.specs[0].placement == "local"
    assert relay.specs[0].placement == "relay:ares"
    assert (
        set(local_wire)
        == set(relay_wire)
        == {
            "handle_id",
            "host",
            "live_state",
            "placement",
            "queued_reason",
            "run_index",
            "run_label",
            "status",
            "task_id",
        }
    )
    assert len(parts) == 2
    assert set(parts[0][1].to_wire()) == set(parts[1][1].to_wire())
    for (_sid, part), wire in zip(parts, (local_wire, relay_wire), strict=True):
        assert part.handle_id == wire["handle_id"]
        assert part.run_label == wire["run_label"]
        assert part.live_state == wire["live_state"]
        assert part.host == wire["host"]
        assert part.placement == wire["placement"]


def test_spawn_placement_precedence_explicit_then_session_policy_then_local(
    monkeypatch,
) -> None:
    """An explicit placement wins; otherwise session policy wins; local is the default."""

    app = _fake_app()
    local = _PlacementSpy("local", "local")
    relay = _PlacementSpy("relay:ares", "ares")
    app.state.expert_invoker = local
    app.state.relay_expert_invokers = {"ares": relay}
    app.state.sessions.seed("sess_x", {"spawn_placement": "relay:ares"})
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tools = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)
        policy = json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="policy"))
        explicit = json.loads(
            tools["spawn_agent_task"].func(agent="data_expert", task="override", placement="local")
        )
        app.state.sessions.get("sess_x").metadata.pop("spawn_placement")
        default = json.loads(tools["spawn_agent_task"].func(agent="data_expert", task="default"))

    assert policy["placement"] == "relay:ares"
    assert explicit["placement"] == "local"
    assert default["placement"] == "local"


def test_parallel_spawn_uses_one_batch_placement_parameter(monkeypatch) -> None:
    """The existing fan-out tool shares the same placement parameter and resolver."""

    app = _fake_app()
    relay = _PlacementSpy("relay:ares", "ares")
    app.state.relay_expert_invokers = {"ares": relay}
    _capture_emits(monkeypatch)

    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agents_parallel"]
        wire = json.loads(
            tool.func(
                spawns=[
                    {"agent": "data_expert", "task": "one"},
                    {"agent": "data_expert", "task": "two"},
                ],
                placement="relay:ares",
            )
        )

    assert len(relay.specs) == 2
    assert {spec.placement for spec in relay.specs} == {"relay:ares"}
    assert {row["placement"] for row in wire["spawned"]} == {"relay:ares"}


def test_parallel_spawn_pins_session_placement_once_for_batch(monkeypatch) -> None:
    """A batch adopts one session policy before launching any of its members."""

    from clio_agent.gact.agents import spawn_placement

    app = _fake_app()
    relay = _PlacementSpy("relay:ares", "ares")
    app.state.relay_expert_invokers = {"ares": relay}
    app.state.sessions.seed("sess_x", {"spawn_placement": "relay:ares"})
    session_reads = 0
    original = spawn_placement._session_placement

    def _tracked_session_placement(app_arg: Any, session_id: str) -> str | None:
        nonlocal session_reads
        session_reads += 1
        return original(app_arg, session_id)

    monkeypatch.setattr(spawn_placement, "_session_placement", _tracked_session_placement)
    _capture_emits(monkeypatch)
    with _active_turn(app):
        tool = _tools_by_name(app, "main", {"data_expert"}, monkeypatch)["spawn_agents_parallel"]
        wire = json.loads(
            tool.func(
                spawns=[
                    {"agent": "data_expert", "task": "one"},
                    {"agent": "data_expert", "task": "two"},
                ]
            )
        )

    assert session_reads == 1
    assert len(relay.specs) == 2
    assert {row["placement"] for row in wire["spawned"]} == {"relay:ares"}


def test_started_handoff_part_stamps_group_fields_when_present() -> None:
    """spawn_agents_parallel's minted spawn_group_id/group_size ride the run
    handle (TaskHandle) all the way onto the STARTED expert_handoff Part."""

    from clio_agent.gact.agents.spawn_runtime import _started_handoff_part

    spawned = TaskHandle(
        task_id="task_g1",
        parent_session_id="sess_x",
        child_session_id="child_g1",
        status="running",
        run_index=0,
        depth=1,
        spawn_group_id="fanout_abc123",
        group_size=3,
    )
    part = _started_handoff_part(_Def("main"), "geospatial", "scan LA", 1, spawned)
    assert part.metadata["spawn_group_id"] == "fanout_abc123"
    assert part.metadata["group_size"] == 3


def test_started_handoff_part_omits_group_fields_when_absent() -> None:
    """A single spawn_agent_task run (spawn_group_id empty on the handle) never
    stamps spawn_group_id/group_size on the Part — absent, not null/empty."""

    from clio_agent.gact.agents.spawn_runtime import _started_handoff_part

    spawned = TaskHandle(
        task_id="task_solo",
        parent_session_id="sess_x",
        child_session_id="child_solo",
        status="running",
        run_index=0,
        depth=1,
    )
    part = _started_handoff_part(_Def("main"), "geospatial", "scan LA", 1, spawned)
    assert "spawn_group_id" not in part.metadata
    assert "group_size" not in part.metadata


def test_return_handoff_part_stamps_group_fields_when_present() -> None:
    """The COMPLETED expert_handoff Part is minted at wait-time from the
    AgentTask/TaskResult record — the group identity must ride THAT record
    (durable on AgentTask, projected onto TaskResult) to reach here, not be
    reconstructed heuristically."""

    from clio_agent.gact.agents.spawn_runtime import _return_handoff_part

    task = AgentTask(
        task_id="task_g1",
        parent_session_id="sess_x",
        child_session_id="child_g1",
        agent_ref={"expert_id": "geospatial", "requesting_expert_id": "main"},
        status="completed",
        spawn_group_id="fanout_abc123",
        group_size=3,
    )
    part = _return_handoff_part(_Def("main"), task, {"output": "done"})
    assert part.metadata["spawn_group_id"] == "fanout_abc123"
    assert part.metadata["group_size"] == 3


def test_return_handoff_part_omits_group_fields_when_absent() -> None:
    """A single-spawn task (no spawn_group_id on the record) never stamps the
    group fields on its completed Part."""

    from clio_agent.gact.agents.spawn_runtime import _return_handoff_part

    task = AgentTask(
        task_id="task_solo",
        parent_session_id="sess_x",
        child_session_id="child_solo",
        agent_ref={"expert_id": "geospatial", "requesting_expert_id": "main"},
        status="completed",
    )
    part = _return_handoff_part(_Def("main"), task, {"output": "done"})
    assert "spawn_group_id" not in part.metadata
    assert "group_size" not in part.metadata


def test_return_handoff_part_stamps_child_duration_ms() -> None:
    """The terminal handoff Part carries the child's real wall-clock duration
    (task.updated_at - task.created_at). The wire showed duration_ms: 0 on every
    delegate.completed part (Part.to_wire drops the 0.0 default), leaving the UI
    with no duration for the Call box."""

    from clio_agent.gact.agents.spawn_runtime import _return_handoff_part

    task = AgentTask(
        task_id="task_dur",
        parent_session_id="sess_x",
        child_session_id="child_1",
        agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
        status="completed",
        created_at="2026-08-05T10:00:00+00:00",
        updated_at="2026-08-05T10:01:12+00:00",
    )
    part = _return_handoff_part(_Def("main"), task, {"output": "done"})
    assert part.duration_ms == 72_000.0


def test_return_handoff_part_survives_unparseable_timestamps() -> None:
    """A malformed/missing timestamp never breaks the return Part — duration
    stays unstamped (0.0) rather than raising mid-delegation."""

    from clio_agent.gact.agents.spawn_runtime import _return_handoff_part

    task = AgentTask(
        task_id="task_nodur",
        parent_session_id="sess_x",
        child_session_id="child_1",
        agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
        status="completed",
        created_at="not-a-timestamp",
        updated_at="",
    )
    part = _return_handoff_part(_Def("main"), task, {"output": "done"})
    assert part.duration_ms == 0.0


def test_terminal_handoff_updates_started_part_in_place() -> None:
    """ONE delegation = ONE expert_handoff part (clean-wire rule): the terminal
    return UPDATES the started part (same id/sequence, merged metadata carrying
    the brief AND the output) and publishes message.part.updated — never a
    second part for the same handle."""

    from clio_agent.gact.transcript import TurnTranscript

    events: list[tuple[str, dict]] = []

    class _Pub:
        def publish(self, event_type, payload):
            events.append((event_type, payload))

    transcript = TurnTranscript.__new__(TurnTranscript)
    transcript.__init__(  # type: ignore[misc]
        session_id="sess_x", turn_id="turn_1", publisher=_Pub()
    )
    started = Part(
        id="p_started",
        type="expert_handoff",
        agent_id="main",
        child_agent="geospatial",
        stage="delegate.started",
        handle_id="task_1",
        status="running",
        metadata={"question": "Resolve LA."},
    )
    terminal = Part(
        id="p_terminal",
        type="expert_handoff",
        agent_id="main",
        child_agent="geospatial",
        stage="delegate.completed",
        handle_id="task_1",
        status="completed",
        duration_ms=1234.0,
        metadata={"output": "Resolved."},
    )
    transcript.upsert_delegation_part(started)
    transcript.upsert_delegation_part(terminal)

    parts = [p for p in transcript._parts if p.type == "expert_handoff"]
    assert len(parts) == 1
    merged = parts[0]
    assert merged.id == "p_started"  # identity survives the update
    assert merged.stage == "delegate.completed"
    assert merged.metadata["question"] == "Resolve LA."
    assert merged.metadata["output"] == "Resolved."
    assert merged.duration_ms == 1234.0
    kinds = [e for e, _ in events]
    assert kinds.count("message.part.added") == 1
    assert kinds.count("message.part.updated") == 1


def _collector_transcript_app(sid: str = "sess_x") -> tuple[Any, Any, list[tuple[str, dict]]]:
    """A minimal app whose open TurnTranscript ledger backs the live observer's
    ``_append_live_assistant_part`` — the REAL producer path for tool parts."""

    from clio_agent.gact.transcript import TurnTranscriptRegistry

    events: list[tuple[str, dict]] = []

    class _Pub:
        def publish(self, event_type: str, payload: dict) -> None:
            events.append((event_type, dict(payload)))

    app = SimpleNamespace(state=SimpleNamespace(turn_transcripts=TurnTranscriptRegistry()))
    transcript = app.state.turn_transcripts.open_turn(sid, "turn_1", _Pub())
    return app, transcript, events


def _collector_call(
    call_id: str,
    tool_name: str = "wait_agent_tasks",
    *,
    waited_tasks: list[dict[str, Any]] | None = None,
    **args: Any,
) -> Part:
    """A live tool_call Part shaped exactly like the observer's started append.

    ``waited_tasks`` mirrors the observer's ``wait_agent_tasks``-only resolved
    display rows (metadata) when a test needs to exercise the collector-collapse
    union merge.
    """

    metadata: dict[str, Any] = {"stream_source": "live", "telemetry_source": "live_observer"}
    if waited_tasks is not None:
        metadata["waited_tasks"] = waited_tasks
    return Part(
        id=f"live_{call_id}_call",
        type="tool_call",
        agent_id="main",
        call_id=call_id,
        tool_name=tool_name,
        input=dict(args),
        metadata=metadata,
    )


def _collector_result(
    call_id: str,
    text: str,
    duration_ms: float,
    tool_name: str = "wait_agent_tasks",
    is_error: bool = False,
    structured_content: object | None = None,
) -> Part:
    """A live tool_result Part shaped exactly like the observer's completed append."""

    return Part(
        id=f"live_{call_id}_result",
        type="tool_result",
        agent_id="main",
        call_id=call_id,
        tool_name=tool_name,
        is_error=is_error,
        duration_ms=duration_ms,
        # #1190: the structured copy rides the part TOP LEVEL (never metadata).
        structured_content=structured_content,
        content=[Part(id=f"live_{call_id}_result_text", type="text", agent_id="main", text=text)],
        metadata={
            "stream_source": "live",
            "telemetry_source": "live_observer",
            **({} if is_error else {"result": text}),
        },
    )


def test_repeated_same_args_waits_collapse_to_one_tool_pair() -> None:
    """One logical activity (waiting on task_X) = ONE tool_call+tool_result pair
    (clean-wire rule): a re-polled wait with identical args REPLACES the prior
    pair in place — same part ids, cumulative attempts/total_wait_ms, the NEWEST
    result text verbatim — publishing message.part.updated, never new rows."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    for call_id, text in [
        ("call_a", "running"),
        ("call_b", "still running"),
        ("call_c", "completed"),
    ]:
        _append_live_assistant_part(
            app, "sess_x", _collector_call(call_id, task_ids=["task_1"], timeout_s=30.0)
        )
        _append_live_assistant_part(app, "sess_x", _collector_result(call_id, text, 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result"]
    call, result = parts
    assert call.id == "live_call_a_call"  # identity survives the collapse
    assert call.call_id == "call_c"  # ...but the newest attempt owns the call
    assert call.metadata["attempts"] == 3
    assert result.id == "live_call_a_result"
    assert result.metadata["attempts"] == 3
    assert result.metadata["total_wait_ms"] == 90000.0
    assert result.content[0].text == "completed"  # newest result VERBATIM
    kinds = [e for e, _ in events]
    assert kinds.count("message.part.added") == 2  # one pair, ever
    assert kinds.count("message.part.updated") == 4  # 2 re-polls x (call + result)


def test_different_timeout_budget_same_task_ids_still_collapses() -> None:
    """Round-6 real-turn evidence: the model re-polls the SAME task set with a
    DIFFERENT ``timeout_s`` each time (observed 60 then 90 on one task set —
    the owner's original wait-wall varied budgets 60/90/120s too). Canonicalizing
    the FULL args dict (timeout_s included) never collapses this shape — the
    EXACT case the feature exists for. The collapse identity is the SEMANTIC
    activity (tool name + task set) only, so this still collapses to one pair,
    and the per-attempt budgets are recorded honestly rather than silently
    dropped."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=60.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 60000.0))
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=90.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 90000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result"]
    call, result = parts
    assert call.id == "live_call_a_call"  # identity survives the collapse
    assert call.call_id == "call_b"  # ...but the newest attempt owns the call
    assert call.metadata["attempts"] == 2
    assert call.metadata["budgets"] == [60.0, 90.0]  # honest per-attempt budgets
    assert result.id == "live_call_a_result"
    assert result.metadata["attempts"] == 2
    assert result.metadata["total_wait_ms"] == 150000.0
    assert result.content[0].text == "completed"  # newest result VERBATIM
    kinds = [e for e, _ in events]
    assert kinds.count("message.part.added") == 2  # one pair, ever
    assert kinds.count("message.part.updated") == 2  # 1 re-poll x (call + result)


def test_task_ids_reordered_between_polls_still_collapses() -> None:
    """The collapse identity sorts ``task_ids`` (order-insensitive): a re-poll
    that lists the same task set in a different order is still ONE activity."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, _events = _collector_transcript_app()
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_call("call_a", task_ids=["task_1", "task_2"], timeout_s=30.0),
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_call("call_b", task_ids=["task_2", "task_1"], timeout_s=45.0),
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 45000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result"]
    assert parts[0].metadata["attempts"] == 2
    assert parts[0].metadata["budgets"] == [30.0, 45.0]


def test_check_error_repoll_collapses_and_shows_newest_error_verbatim() -> None:
    """check_agent_tasks collapses the same way, and a failed re-poll's VISIBLE
    result is the newest error verbatim — never a merge that keeps the prior
    attempt's stale result evidence under the failure."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, _events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", tool_name="check_agent_tasks", task_ids=None)
    )
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_result("call_a", '{"results": []}', 5.0, tool_name="check_agent_tasks"),
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", tool_name="check_agent_tasks", task_ids=None)
    )
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_result(
            "call_b", "registry gone", 3.0, tool_name="check_agent_tasks", is_error=True
        ),
    )

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result"]
    result = parts[1]
    assert result.is_error is True
    assert result.content[0].text == "registry gone"
    assert result.metadata["attempts"] == 2
    assert result.metadata["total_wait_ms"] == 8.0
    assert "result" not in result.metadata  # no stale prior-attempt evidence


def test_repoll_structured_content_follows_the_newest_attempt() -> None:
    """#1190: the TOP-LEVEL ``structured_content`` field stays consistent across
    collector re-poll upserts — the newest attempt's value (or absence) owns the
    merged part, exactly like the visible result text. A prior attempt's
    structured payload must never survive under a newer attempt that lacks it,
    and never leak back in via the metadata merge (metadata carries no copy)."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, _events = _collector_transcript_app()
    # Attempt 1 carries a structured payload; the re-poll (attempt 2) does not.
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_result("call_a", "running", 30000.0, structured_content={"status": "running"}),
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 5.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result"]
    result = parts[1]
    assert result.metadata["attempts"] == 2
    assert result.structured_content is None  # newest attempt owns the facts
    assert "structured_content" not in result.to_wire()  # absent-when-None
    assert "structured_content" not in result.metadata  # ONE home: never metadata

    # And the reverse: a re-poll that GAINS a structured payload serves it.
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_c", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_result("call_c", "completed", 3.0, structured_content={"status": "completed"}),
    )
    result = transcript.snapshot()[1]
    assert result.metadata["attempts"] == 3
    assert result.structured_content == {"status": "completed"}
    assert result.to_wire()["structured_content"] == {"status": "completed"}
    assert "structured_content" not in result.metadata


def test_repoll_waited_tasks_union_by_task_id() -> None:
    """A collapsed wait covering two re-poll attempts on the SAME task set must
    carry the UNION of resolved ``waited_tasks`` rows, never a narrower result
    than either attempt saw (the collector collapse's generic ``{**existing,
    **new}`` metadata merge would otherwise let the newest attempt silently
    drop a row an earlier attempt resolved)."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, _events = _collector_transcript_app()
    row_a = {
        "task_id": "task_1",
        "agent_id": "geospatial",
        "run_index": 0,
        "run_label": "",
        "child_session_id": "child_1",
        "name": "geospatial #1",
    }
    row_b = {
        "task_id": "task_2",
        "agent_id": "ndp",
        "run_index": 0,
        "run_label": "",
        "child_session_id": "child_2",
        "name": "ndp #1",
    }
    # Attempt 1 resolves only task_1 (task_2's registry row wasn't there yet, or
    # attempt 1 simply requested a subset); attempt 2 resolves BOTH, with an
    # updated row_a (its run_label got set in between).
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_call(
            "call_a", task_ids=["task_1", "task_2"], timeout_s=30.0, waited_tasks=[row_a]
        ),
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    row_a_updated = {**row_a, "run_label": "LA scan", "name": "LA scan"}
    _append_live_assistant_part(
        app,
        "sess_x",
        _collector_call(
            "call_b",
            task_ids=["task_1", "task_2"],
            timeout_s=30.0,
            waited_tasks=[row_a_updated, row_b],
        ),
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 5.0))

    call_part = next(p for p in transcript.snapshot() if p.type == "tool_call")
    assert call_part.metadata["attempts"] == 2
    # Union by task_id: BOTH rows present, and task_1's NEWEST (attempt 2) facts win.
    assert call_part.metadata["waited_tasks"] == [row_a_updated, row_b]


def test_different_args_waits_stay_separate_rows() -> None:
    """A wait on DIFFERENT task ids is a different activity — separate row pairs,
    never an in-place update."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_2"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "running", 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert "attempts" not in parts[2].metadata
    assert [e for e, _ in events].count("message.part.updated") == 0


def test_narration_between_waits_collapses_and_keeps_narration() -> None:
    """Real turns interleave narration TEXT between every re-poll (round-4 live
    evidence, msg_asst_bf61e558ce51: 5 separate wait rows under the strict
    adjacency rule). Narration never breaks the chain: the same-args re-poll
    still collapses onto the prior pair at its ORIGINAL position, while the
    narration parts stay exactly where they are, in order, as separate text
    parts — never absorbed, never reordered."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    transcript.append_text_delta("main", "next_thought", "Still waiting on task_1...")
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_result("call_b", "still running", 30000.0)
    )
    transcript.append_text_delta("main", "next_thought", "Task_1 is close, polling again...")
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_c", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_c", "completed", 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result", "text", "text"]
    call, result, narration1, narration2 = parts
    assert call.id == "live_call_a_call"  # the pair keeps its ORIGINAL position/id
    assert call.call_id == "call_c"  # ...owned by the newest attempt
    assert call.metadata["attempts"] == 3
    assert result.id == "live_call_a_result"
    assert result.metadata["attempts"] == 3
    assert result.metadata["total_wait_ms"] == 90000.0
    assert result.content[0].text == "completed"  # newest result VERBATIM
    assert narration1.text == "Still waiting on task_1..."
    assert narration2.text == "Task_1 is close, polling again..."
    kinds = [e for e, _ in events]
    assert kinds.count("message.part.added") == 4  # one pair + the two narrations
    assert kinds.count("message.part.updated") == 4  # 2 re-polls x (call + result)


def test_thinking_and_text_between_waits_collapses_and_keeps_both_verbatim() -> None:
    """LIVE evidence (rerun sess_c6241fc8906f, msg_asst_8894cb745b15): the
    provider-thinking lane came alive alongside narration text, so the stored
    shape between two same-args re-polls is tool_call, tool_result,
    THINKING, text, tool_call(same args)... — not just text. A ``thinking``
    part is the same narration lane as ``text`` (both stay exactly where they
    streamed, never absorbed): it must not break the collapse chain either."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    transcript.append_text_delta("main", "provider_thinking:main", "Checking on task_1...")
    transcript.append_text_delta("main", "next_thought", "Still waiting on task_1...")
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result", "thinking", "text"]
    call, result, thinking, narration = parts
    assert call.id == "live_call_a_call"  # the pair keeps its ORIGINAL position/id
    assert call.call_id == "call_b"  # ...owned by the newest attempt
    assert call.metadata["attempts"] == 2
    assert result.id == "live_call_a_result"
    assert result.metadata["attempts"] == 2
    assert result.metadata["total_wait_ms"] == 60000.0
    assert result.content[0].text == "completed"  # newest result VERBATIM
    assert thinking.text == "Checking on task_1..."
    assert narration.text == "Still waiting on task_1..."
    kinds = [e for e, _ in events]
    assert kinds.count("message.part.added") == 4  # one pair + thinking + text
    assert kinds.count("message.part.updated") == 2  # 1 re-poll x (call + result)


def test_interleaved_other_tool_call_breaks_the_collapse_chain() -> None:
    """A DIFFERENT tool's call/result pair between same-args waits BREAKS the
    chain — collapsing across another tool's activity would reorder reality."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_x", tool_name="read_file", filepath="x.h5")
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_result("call_x", "bytes", 5.0, tool_name="read_file")
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
    ]
    assert "attempts" not in parts[4].metadata
    assert [e for e, _ in events].count("message.part.updated") == 0


def test_interleaved_expert_handoff_breaks_the_collapse_chain() -> None:
    """A spawn (expert_handoff) between same-args waits BREAKS the chain — the
    wait after a new delegation is a new activity, never a re-poll of the old."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_a", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_a", "running", 30000.0))
    _append_live_assistant_part(
        app,
        "sess_x",
        Part(
            id="p_handoff",
            type="expert_handoff",
            agent_id="main",
            child_agent="data_expert",
            stage="delegate.started",
            handle_id="task_9",
            status="running",
        ),
    )
    _append_live_assistant_part(
        app, "sess_x", _collector_call("call_b", task_ids=["task_1"], timeout_s=30.0)
    )
    _append_live_assistant_part(app, "sess_x", _collector_result("call_b", "completed", 30000.0))

    parts = transcript.snapshot()
    assert [p.type for p in parts] == [
        "tool_call",
        "tool_result",
        "expert_handoff",
        "tool_call",
        "tool_result",
    ]
    assert [e for e, _ in events].count("message.part.updated") == 0


def test_non_collector_tools_never_collapse() -> None:
    """Scope is STRICTLY the two collector tools by name — an identical-args
    re-run of any other tool appends normally (no generic tool collapsing)."""

    from clio_agent.gact.tool_observer import _append_live_assistant_part

    app, transcript, events = _collector_transcript_app()
    for call_id in ("call_a", "call_b"):
        _append_live_assistant_part(
            app, "sess_x", _collector_call(call_id, tool_name="read_file", filepath="x.h5")
        )
        _append_live_assistant_part(
            app, "sess_x", _collector_result(call_id, "bytes", 5.0, tool_name="read_file")
        )

    parts = transcript.snapshot()
    assert [p.type for p in parts] == ["tool_call", "tool_result", "tool_call", "tool_result"]
    assert [e for e, _ in events].count("message.part.updated") == 0


def test_collector_tools_notify_the_live_observer() -> None:
    """wait/check are REAL tool calls the model makes; they must reach the
    observer (started + completed with the verbatim result) instead of being
    invisible mechanism the narration references (owner, 2026-08-05). The
    per-tool ``_observed_collector`` shim is generalized into the default-on
    ``observed_tool_callable`` wrapper every seam-instrumented native gets."""

    from clio_agent.gact.agents.tool_instrumentation import observed_tool_callable
    from clio_agent.tools import execution as _execution

    calls: list[tuple[str, dict, str, str | None, object]] = []

    def _capture(name, args, phase, error=None, result=None):
        calls.append((name, args, phase, error, result))

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _capture
    try:

        def fake_wait(task_ids: list[str], timeout_s: float) -> str:
            return '{"results": []}'

        wrapped = observed_tool_callable(fake_wait, "wait_agent_tasks")
        out = wrapped(["task_1"], 30.0)
        assert out == '{"results": []}'
        assert [(c[0], c[2]) for c in calls] == [
            ("wait_agent_tasks", "started"),
            ("wait_agent_tasks", "completed"),
        ]
        assert calls[0][1] == {"task_ids": ["task_1"], "timeout_s": 30.0}
        assert calls[1][4] == '{"results": []}'

        calls.clear()

        def boom(task_ids: list[str] | None = None) -> str:
            raise RuntimeError("registry gone")

        wrapped_boom = observed_tool_callable(boom, "check_agent_tasks")
        with pytest.raises(RuntimeError):
            wrapped_boom()
        assert [(c[0], c[2], c[3]) for c in calls] == [
            ("check_agent_tasks", "started", None),
            ("check_agent_tasks", "completed", "registry gone"),
        ]
        assert calls[0][1] == {"task_ids": None}
    finally:
        _execution.notify_global_tool_observer = original


# ---------------------------------------------------------------------------
# Turn-end artifact rollup (owner ask 2026-08-06): "show at the end of the turn
# ALL artifacts that have been generated in that turn by any of the agents or
# subagents". A child's mints only ever chipped into the CHILD's own transcript
# (its per-session turn buffer is drained by ITS OWN finalize); these tests drive
# ``artifacts/wire.append_turn_child_resource_links`` — the finalize seam that
# rolls a spawned child's (and its descendants') registry-sourced mints onto the
# PARENT's settled message — directly, matching the ``test_finalize_append_helper
# _builds_resource_link_parts`` unit idiom (test_artifacts_s2.py) rather than a
# live turn, so mint ORDER and the turn/session scoping are exactly controlled.
# ---------------------------------------------------------------------------


class _FakeTranscript:
    """Minimal transcript stand-in: records appended parts and lets a test seed
    parts already "on the message" (the exactly-once dedup precondition)."""

    def __init__(self, parts: list[Part] | None = None) -> None:
        self._parts = list(parts or [])

    def snapshot(self) -> list[Part]:
        return list(self._parts)

    def append_part(self, part: Part, *, stream_source: str = "batch") -> Part:
        del stream_source
        self._parts.append(part)
        return part


def _rollup_app(tmp_path: Path) -> SimpleNamespace:
    """A real SessionStore + AgentTaskRegistry + ArtifactRegistry, no semantic
    sink wired (``mint_artifact``'s ``_emit_semantic_event`` short-circuits
    harmlessly with no sink — the rollup reads the registry + agent-task index
    directly, never the sink)."""

    from clio_agent.gact.artifacts.registry import ArtifactRegistry
    from clio_agent.gact.sessions import SessionStore

    store = SessionStore(path=tmp_path / "sessions.json")
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions=store,
            agent_task_registry=AgentTaskRegistry(),
            artifact_registry=ArtifactRegistry(),
        )
    )


def _rollup_mint(app: Any, sid: str, name: str, sha: str, *, call_id: str = "c1") -> Any:
    """Mint one synthetic artifact version, producer-stamped to ``sid``."""

    from clio_agent.gact.artifacts.minting import mint_artifact
    from clio_agent.gact.artifacts.records import ArtifactKind, IdentityEvidence, Mechanism

    return mint_artifact(
        app,
        sid,
        name=name,
        workspace_id="ws1",
        evidence=IdentityEvidence.hashed_at_use(sha256=sha, size_bytes=10),
        kind=ArtifactKind.IMAGE,
        mechanism=Mechanism.TOOL_SCHEMA,
        producer={"tool": "plot", "call_id": call_id, "session_id": sid},
    )


def _rollup_task(app: Any, *, parent_sid: str, child_sid: str, parent_turn_id: str) -> None:
    """Register a child AgentTask directly — the rollup only reads the registry
    projection, so a full ``persist_agent_task`` round-trip is unnecessary."""

    app.state.agent_task_registry.register(
        AgentTask(
            task_id=f"task_{child_sid}",
            parent_session_id=parent_sid,
            child_session_id=child_sid,
            parent_turn_id=parent_turn_id,
            agent_ref={"expert_id": "worker", "requesting_expert_id": "main"},
            status="completed",
            created_at="2026-08-05T00:00:00+00:00",
            updated_at="2026-08-05T00:00:00+00:00",
        )
    )


def test_child_rollup_appends_child_mints_ordered_by_mint_time(tmp_path: Path) -> None:
    """A turn whose child mints two artifacts: the settled parent message ends
    with exactly those two ``resource_link`` parts, metadata intact, oldest first."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_c", parent_turn_id="T1")

    # Minted in this order; named so a name-sort (instead of mint-time-sort)
    # would flip the assertion below — the lock proves ORDER, not naming luck.
    older = _rollup_mint(app, "sess_c", "zzz_first_minted.png", "a" * 64, call_id="c1")
    time.sleep(0.005)
    newer = _rollup_mint(app, "sess_c", "aaa_second_minted.png", "b" * 64, call_id="c2")

    transcript = _FakeTranscript()
    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    parts = transcript.snapshot()
    # Sabotage: sort by name instead of created_at -> this order flips, red.
    assert [p.name for p in parts] == ["zzz_first_minted.png", "aaa_second_minted.png"]
    assert [p.type for p in parts] == ["resource_link", "resource_link"]
    assert parts[0].metadata["artifact_id"] == older.artifact_id
    assert parts[1].metadata["artifact_id"] == newer.artifact_id
    assert parts[0].agent_id == "main"
    # The full #966.9 metadata block rides verbatim (11 keys), not a subset.
    assert set(parts[0].metadata) == {
        "artifact_id",
        "sha256",
        "size_bytes",
        "kind",
        "version",
        "custody",
        "fetch_url",
        "producer_activity_id",
        "mechanism",
        "workspace_id",
        "name",
    }
    assert parts[0].uri == "artifact://ws1/zzz_first_minted.png@v1"


def test_child_rollup_includes_grandchild_mints(tmp_path: Path) -> None:
    """Any of the agents or subagents (owner ask): a grandchild — spawned by the
    CHILD, not the parent — still rolls up, because the grandchild session exists
    solely because of this turn's spawn chain."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_c", parent_turn_id="T1")
    _rollup_task(app, parent_sid="sess_c", child_sid="sess_gc", parent_turn_id="child_own_turn")
    grand = _rollup_mint(app, "sess_gc", "deep.png", "c" * 64, call_id="c3")

    transcript = _FakeTranscript()
    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    parts = transcript.snapshot()
    # Sabotage: scope to direct children only (skip descendant_session_ids) -> [], red.
    assert len(parts) == 1
    assert parts[0].metadata["artifact_id"] == grand.artifact_id


def test_child_rollup_preserves_same_named_artifacts_from_distinct_children(tmp_path: Path) -> None:
    """Parallel child results retain their immutable causal identities."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_a", parent_turn_id="T1")
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_b", parent_turn_id="T1")
    first = _rollup_mint(app, "sess_a", "shared_catalog.csv", "a" * 64, call_id="c1")
    latest = _rollup_mint(app, "sess_b", "shared_catalog.csv", "b" * 64, call_id="c2")

    transcript = _FakeTranscript()
    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    parts = transcript.snapshot()
    assert len(parts) == 2
    assert [part.name for part in parts] == ["shared_catalog.csv", "shared_catalog.csv"]
    assert [part.metadata["version"] for part in parts] == [1, 2]
    assert [part.metadata["artifact_id"] for part in parts] == [
        first.artifact_id,
        latest.artifact_id,
    ]


def test_child_rollup_skips_artifact_id_already_on_the_message(tmp_path: Path) -> None:
    """The exactly-once guard: an artifact_id already riding a ``resource_link``
    part on this message (the parent's own live-chipped mint) is never re-added."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links, resource_link_part

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_c", parent_turn_id="T1")
    minted = _rollup_mint(app, "sess_c", "already.png", "d" * 64, call_id="c4")
    existing_part = resource_link_part(
        "ws1", "already.png", minted, part_id="part_existing", agent_id="main"
    )
    transcript = _FakeTranscript(parts=[existing_part])

    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    parts = transcript.snapshot()
    # Sabotage: drop the "already present" dedup set -> len becomes 2, red.
    assert len(parts) == 1
    assert parts[0] is existing_part


def test_child_rollup_excludes_a_child_spawned_in_a_previous_turn(tmp_path: Path) -> None:
    """A mint from a PREVIOUS turn does not leak in: the child was spawned under
    an earlier ``parent_turn_id`` (T0), not the turn finalizing now (T1)."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="sess_p", child_sid="sess_c", parent_turn_id="T0")
    _rollup_mint(app, "sess_c", "stale.png", "e" * 64, call_id="c5")

    transcript = _FakeTranscript()
    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    # Sabotage: drop the parent_turn_id filter -> the stale child's mint leaks in, red.
    assert transcript.snapshot() == []


def test_child_rollup_appends_nothing_when_no_child_spawned_this_turn(tmp_path: Path) -> None:
    """No descendant spawned this turn -> no registry scan, no parts (never an
    empty grid marker)."""

    from clio_agent.gact.artifacts.wire import append_turn_child_resource_links

    app = _rollup_app(tmp_path)

    transcript = _FakeTranscript()
    append_turn_child_resource_links(app, "sess_p", "T1", transcript, agent_id="main")

    assert transcript.snapshot() == []
