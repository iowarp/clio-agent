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

from fastapi.testclient import TestClient

from clio_agent.gact import context as ctx
from clio_agent.gact.agent_tasks import AgentTask, AgentTaskRegistry
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

    return SimpleNamespace(
        state=SimpleNamespace(
            agent_task_registry=registry or AgentTaskRegistry(),
            sessions=_StubSessions(),
            messages=dict(messages or {}),
        )
    )


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
        "clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe",
        lambda a, spec: SimpleNamespace(
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

    def _fake_spawn(a: Any, spec: Any) -> Any:
        spawn_calls.append(spec.child_expert_id)
        return SimpleNamespace(
            task_id=f"task_{spec.child_expert_id}", status="running", run_index=0, queued_reason=""
        )

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
            {
                "task_id": "task_data_expert",
                "status": "running",
                "run_index": 0,
                "queued_reason": "",
            },
            {
                "task_id": "task_hpc_expert",
                "status": "running",
                "run_index": 0,
                "queued_reason": "",
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

    def _capture_spawn(a: Any, spec: Any) -> Any:
        captured.append(spec)
        return SimpleNamespace(
            task_id=f"task_d{spec.depth}", status="running", run_index=0, queued_reason=""
        )

    monkeypatch.setattr("clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe", _capture_spawn)
    # Stub the started-Part append (the bare app has no transcript/bus); this test
    # asserts on computed depth, not on the Part.
    _capture_parts(monkeypatch)

    app = _fake_app()
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
        "clio_agent.gact.turn_spawn.spawn_child_turn_threadsafe",
        lambda a, spec: SimpleNamespace(
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
