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
            "message_agent",
            "observe_agent_tasks",
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


# ---------------------------------------------------------------------------
# Fix 1 (#880 verbatim output): the delegation ``output`` is the child's FULL
# answer byte-for-byte, re-read at wait-time — NOT the registry's bounded excerpt.
# ---------------------------------------------------------------------------


def test_wait_returns_child_answer_verbatim_past_the_excerpt_bound(monkeypatch) -> None:
    # No leading/trailing whitespace: _message_text strips (as it does when minting
    # the excerpt), so the verbatim contract is byte-for-byte on the stripped body.
    big = " | ".join(f"line-{i:04d} the child's deliverable" for i in range(400))
    assert len(big) > 2000, "fixture must exceed the 2000-char excerpt bound"
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


def test_orchestrator_max_iters_scales_with_declared_children() -> None:
    """#948 S4: the react iteration default pays spawn+wait per child — the old
    flat 5 starved every orchestrator into a forced no-evidence extract (live)."""

    from types import SimpleNamespace

    from clio_agent.gact.agents.builders import _tool_user_agent_max_iters

    leaf = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(leaf) == 5
    assert _tool_user_agent_max_iters(leaf, declared_children=0) == 5
    four_children = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(four_children, declared_children=4) == 22
    many = SimpleNamespace(parameters={})
    assert _tool_user_agent_max_iters(many, declared_children=10) == 24  # capped
    # An explicit blueprint param always wins, both directions.
    pinned = SimpleNamespace(parameters={"max_iters": 7})
    assert _tool_user_agent_max_iters(pinned, declared_children=4) == 7


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


def _collector_call(call_id: str, tool_name: str = "wait_agent_tasks", **args: Any) -> Part:
    """A live tool_call Part shaped exactly like the observer's started append."""

    return Part(
        id=f"live_{call_id}_call",
        type="tool_call",
        agent_id="main",
        call_id=call_id,
        tool_name=tool_name,
        input=dict(args),
        metadata={"stream_source": "live", "telemetry_source": "live_observer"},
    )


def _collector_result(
    call_id: str,
    text: str,
    duration_ms: float,
    tool_name: str = "wait_agent_tasks",
    is_error: bool = False,
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
    invisible mechanism the narration references (owner, 2026-08-05)."""

    from clio_agent.gact.agents.spawn_runtime import _observed_collector
    from clio_agent.tools import execution as _execution

    calls: list[tuple[str, dict, str, str | None, object]] = []

    def _capture(name, args, phase, error=None, result=None):
        calls.append((name, args, phase, error, result))

    original = _execution.notify_global_tool_observer
    _execution.notify_global_tool_observer = _capture
    try:

        def fake_wait(task_ids: list[str], timeout_s: float) -> str:
            return '{"results": []}'

        wrapped = _observed_collector(fake_wait, "wait_agent_tasks")
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

        wrapped_boom = _observed_collector(boom, "check_agent_tasks")
        with pytest.raises(RuntimeError):
            wrapped_boom()
        assert [(c[0], c[2], c[3]) for c in calls] == [
            ("check_agent_tasks", "started", None),
            ("check_agent_tasks", "completed", "registry gone"),
        ]
        assert calls[0][1] == {"task_ids": None}
    finally:
        _execution.notify_global_tool_observer = original
