"""S7 (#948 / #955, prep for #671): the ``ExpertInvoker`` seam.

Parity suite — proves the in-process invoker is a THIN, behavior-preserving seam
over the spawn substrate: every operation (invoke / wait / check / cancel) produces
the SAME records, the SAME events (count + order + payloads modulo ids/timestamps),
the SAME typed errors, and the SAME run_index / notify semantics as the direct
substrate calls the spawn-runtime tools use today.

The parity tests drive BOTH paths side by side in ONE app and diff the outcomes
structurally: a child spawned through :class:`InProcessExpertInvoker` under one
parent vs a child spawned through ``spawn_child_turn_threadsafe`` under another,
same task text, then a normalized diff of their child-session event streams and
projected records.

Uses the S3 stub agent (the child turn RUNS a real turn cycle; the LM answer is
orthogonal to the substrate). Declared-children resolution is monkeypatched.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    AgentTask,
)
from clio_agent.gact.agents.invoker import (
    RELAY_STATE_MAP,
    TASK_CONSUMED_EVENT,
    ExpertInvoker,
    InProcessExpertInvoker,
    InvokerError,
    RelayExpertInvoker,
    TaskEvent,
    TaskHandle,
    TaskResult,
    spec_from_wire,
    spec_to_wire,
    task_event_type,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.turn_spawn import (
    MAX_SPAWN_DEPTH,
    SpawnError,
    TaskSpec,
    cancel_agent_task,
    spawn_child_turn_threadsafe,
)
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, resolve_store
from clio_agent.tools.relay_transport import RelayTaskIdentity

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.mark.parametrize(
    "invoker_type",
    [InProcessExpertInvoker, RelayExpertInvoker],
    ids=["in-process", "relay"],
)
def test_both_expert_invoker_implementations_satisfy_protocol(invoker_type: type) -> None:
    """The S7 implementation matrix contains both protocol implementations."""

    assert all(
        callable(getattr(invoker_type, method, None))
        for method in ("invoke", "wait", "check", "cancel", "message")
    )


class _Agent:
    """S3 stub: a real child turn whose answer is deterministic in the task text."""

    def __init__(self, sleep_s: float = 0.0) -> None:
        self.sleep_s = sleep_s

    def forward(self, question: str, session_id: str, **_kw):
        if self.sleep_s:
            time.sleep(self.sleep_s)
        return type(
            "P",
            (),
            {
                "answer": f"child did: {question[:20]}",
                "selected_expert": "",
                "routing_rationale": "",
            },
        )()


class _FakeRelayBackend:
    """Deterministic relay state shared by every reconstructed fake client."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.submissions: list[dict[str, Any]] = []
        self.messages: list[tuple[str, str]] = []
        self.client_count = 0
        self._next_id = 0
        self._lock = threading.Lock()

    def client(self, owner_session_id: str) -> "_FakeRelayClient":
        """Return a fresh owner-bound client."""

        return _FakeRelayClient(self, owner_session_id)

    def create(self, owner_session_id: str, arguments: dict[str, Any]) -> RelayTaskIdentity:
        """Admit one job and persist the exact reconnect key."""

        with self._lock:
            self._next_id += 1
            task_id = f"task_relay_{self._next_id:04d}"
            context = dict(arguments["context"])
            task = {
                "task_id": task_id,
                "owner_session_id": owner_session_id,
                "arguments": dict(arguments),
                "context": context,
                "polls": 0,
                "cancel_requested": False,
                "terminal": threading.Event(),
                "stream_closed": threading.Event(),
                "state": "queued",
            }
            self.tasks[task_id] = task
            self.submissions.append(dict(arguments))
        key = TaskKey(
            server_id="fake-relay-server",
            session_id=owner_session_id,
            task_id=task_id,
        )
        resolve_store(None).put(
            TaskRecord(
                key=key,
                tool="relay_submit_remote_agent",
                backend={"transport": "fake-relay"},
                status="working",
                created_at="2026-08-01T00:00:00+00:00",
            )
        )
        return RelayTaskIdentity.from_key(key)

    def current(self, task_id: str, *, terminal: bool = False) -> SimpleNamespace:
        """Return one SEP task observation projected from canonical fake relay state."""

        task = self.tasks[task_id]
        task["polls"] += 1
        if task["cancel_requested"]:
            observation = "canceled"
        elif terminal:
            observation = "succeeded"
        elif task["polls"] == 1:
            observation = "queued"
        else:
            observation = "running"
        task["state"] = observation
        if observation == "canceled":
            task["terminal"].set()
            resolve_store(None).drop(self._key(task))
            return _relay_current("canceled")
        if observation == "succeeded":
            task["terminal"].set()
            resolve_store(None).drop(self._key(task))
            return _relay_current(
                "succeeded",
                result={"isError": False, "task_result": self.task_result(task)},
            )
        return _relay_current(observation)

    @staticmethod
    def _key(task: dict[str, Any]) -> TaskKey:
        return TaskKey(
            server_id="fake-relay-server",
            session_id=str(task["owner_session_id"]),
            task_id=str(task["task_id"]),
        )

    @staticmethod
    def task_result(task: dict[str, Any]) -> dict[str, Any]:
        """Build the remote TaskResult boundary record."""

        spec = task["context"]
        text = str(spec["task_text"])
        return {
            "task_id": task["task_id"],
            "parent_session_id": task["owner_session_id"],
            "child_session_id": "",
            "agent_ref": {
                "expert_id": spec["child_expert_id"],
                "requesting_expert_id": spec["requesting_expert_id"],
            },
            "depth": spec["depth"],
            "run_index": 0,
            "status": "completed",
            "queued_reason": "",
            "error_reason": "",
            "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": "2026-08-01T00:00:01+00:00",
            "result": {
                "message_ref": "remote-message",
                "answer_excerpt": f"child did: {text[:20]}",
                "workflow_state": {},
            },
            "artifact_ref": "",
        }


class _FakeRelayClient:
    """Async RelayTransportClient surface over the shared fake backend."""

    def __init__(self, backend: _FakeRelayBackend, owner_session_id: str) -> None:
        self.backend = backend
        self.owner_session_id = owner_session_id

    async def __aenter__(self) -> "_FakeRelayClient":
        self.backend.client_count += 1
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def submit(self, tool_name: str, arguments: dict[str, Any]) -> RelayTaskIdentity:
        assert tool_name == "relay_submit_remote_agent"
        return self.backend.create(self.owner_session_id, arguments)

    async def poll(self, identity: RelayTaskIdentity) -> SimpleNamespace:
        return self.backend.current(identity.task_id)

    async def resume(
        self, key: TaskKey, *, timeout_seconds: float | None = None
    ) -> SimpleNamespace:
        del timeout_seconds
        return self.backend.current(key.task_id, terminal=True)

    async def cancel(self, identity: RelayTaskIdentity) -> dict[str, bool]:
        task = self.backend.tasks[identity.task_id]
        task["cancel_requested"] = True
        record = resolve_store(None).get(identity.key)
        assert record is not None
        from dataclasses import replace

        resolve_store(None).put(replace(record, cancel_requested=True))
        return {"acknowledged": True}

    async def message(self, identity: RelayTaskIdentity, text: str) -> None:
        self.backend.messages.append((identity.task_id, text))

    async def stream_events(
        self,
        identity: RelayTaskIdentity,
        *,
        cursor: int = 1,
    ) -> AsyncIterator[dict[str, Any]]:
        assert cursor == 1
        task = self.backend.tasks[identity.task_id]
        result = self.backend.task_result(task)
        try:
            yield {
                "task_id": identity.task_id,
                "event_type": "agent.task.started",
                "session_id": "",
                "status": "running",
                "payload": {**result, "status": "running", "result": None},
            }
            while not task["terminal"].is_set():
                await asyncio.sleep(0.01)
            status = "cancelled" if task["state"] == "canceled" else "completed"
            event_type = f"agent.task.{status}"
            yield {
                "task_id": identity.task_id,
                "event_type": event_type,
                "session_id": "",
                "status": status,
                "payload": {**result, "status": status, "result": result["result"]},
            }
        finally:
            task["stream_closed"].set()


def _relay_current(
    observation: str,
    *,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build one fake ClientGetTaskResult for a committed map row."""

    projection = RELAY_STATE_MAP[observation]
    return SimpleNamespace(
        status=projection["status"],
        relay_state=observation,
        status_message=f"Relay job is {observation}",
        result=result,
        error=error,
    )


def _relay_invoker(app: Any, backend: _FakeRelayBackend) -> RelayExpertInvoker:
    """Construct the relay implementation with fixed remote execution inputs."""

    return RelayExpertInvoker(
        app,
        backend.client,
        cluster="ares",
        prompt_path="/shared/clio/relay-expert.md",
        mcp_config_path="/shared/clio/mcp.toml",
        model="anthropic/claude",
        workdir="/shared/work",
    )


def _declare(monkeypatch, *child_ids: str) -> None:
    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda app, pid, session_id="", **_bindings: set(child_ids),
    )


def _wait_terminal(app, task_id: str, timeout: float = 10.0) -> AgentTask:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = app.state.agent_task_registry.get(task_id)
        if t is not None and t.is_terminal:
            return t
        time.sleep(0.05)
    return app.state.agent_task_registry.get(task_id)


# Volatile fields that legitimately differ between two independent children (unique
# ids, wall-clock timestamps, per-child session ids). A structural parity diff
# normalizes them out; everything else must match byte-for-byte.
_VOLATILE = frozenset(
    {
        "task_id",
        "child_session_id",
        "parent_session_id",
        "created_at",
        "updated_at",
        "consumed_at",
        # P2.10 parity is modulo executor-specific run-handle identity/placement.
        "handle_id",
        "host",
        "placement",
    }
)
_VOLATILE_RESULT = frozenset({"message_ref"})


def _norm_payload(payload: dict) -> dict:
    out = {k: v for k, v in payload.items() if k not in _VOLATILE}
    result = out.get("result")
    if isinstance(result, dict):
        out["result"] = {k: v for k, v in result.items() if k not in _VOLATILE_RESULT}
    return out


def _norm_events(app, sid: str) -> list[tuple[str, dict]]:
    """The normalized (event_type, payload) stream for a session — ids/timestamps
    stripped so two independent children are structurally comparable."""

    return [
        (e.type, _norm_payload(dict(e.payload or {})))
        for e in app.state.bus._history.get(sid, [])
        if e.type.startswith("agent.task.")
    ]


_TERMINAL_TASK_EVENTS = frozenset(
    {"agent.task.completed", "agent.task.failed", "agent.task.cancelled"}
)


def _wait_task_events_settled(app, sid: str, timeout: float = 10.0) -> None:
    """Wait until a session's ``agent.task.*`` stream ENDS on a terminal event.

    ``_wait_terminal`` only waits for the registry RECORD to reach a terminal status;
    the terminal ``agent.task.*`` BUS event is published on a separate step and can lag
    that transition. An event-stream parity diff must wait for the terminal EVENT too, or
    it races (one child's ``completed`` already appended, the other's not yet)."""

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        events = _norm_events(app, sid)
        if events and events[-1][0] in _TERMINAL_TASK_EVENTS:
            return
        time.sleep(0.05)


def _spec(parent: str, task_text: str = "analyze the dataset") -> TaskSpec:
    return TaskSpec(
        child_expert_id="main",
        task_text=task_text,
        parent_session_id=parent,
        requesting_expert_id="main",
        workflow_state={"plan": "P1"},
    )


# ---------------------------------------------------------------------------
# Reused request shape: TaskSpec is already fully JSON-serializable (no twin)
# ---------------------------------------------------------------------------


def test_taskspec_json_roundtrips_verbatim() -> None:
    """The seam REUSES ``turn_spawn.TaskSpec`` rather than minting a twin — prove it
    is fully JSON-serializable in AND out (the #671 request contract)."""

    spec = TaskSpec(
        child_expert_id="worker",
        task_text="do the thing",
        parent_session_id="sess_p",
        requesting_expert_id="orchestrator",
        parent_turn_id="turn_1",
        depth=2,
        mode="async",
        workflow_state={"plan": "P1", "steps": [1, 2, 3]},
        fanout_bound=4,
        workspace_id="ws_science",
        session_mode="architect",
        session_scope_metadata={
            "active_agent_blueprint_id": "science-blueprint",
            "active_agent_blueprint_path": "/blueprints/science",
            "active_expert_pack_id": "science-pack",
            "active_expert_pack_path": "/packs/science",
            "expert_pack_id": "legacy-science-pack",
        },
    )
    wire = spec_to_wire(spec)
    # Genuinely JSON (survives a dumps/loads with no custom encoder).
    assert json.loads(json.dumps(wire)) == wire
    assert spec_from_wire(wire) == spec
    # Unknown keys (a cross-release field) are tolerated, not fatal.
    assert spec_from_wire({**wire, "future_field": 7}) == spec


@pytest.mark.parametrize("implementation", ["in-process", "relay"])
def test_s7_record_parity_across_both_invokers(
    implementation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both implementations produce the direct substrate TaskResult record."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        invoker: ExpertInvoker = (
            InProcessExpertInvoker(app)
            if implementation == "in-process"
            else _relay_invoker(app, backend)
        )
        p_inv = client.post("/v1/sessions", json={"title": "inv"}).json()["id"]
        p_dir = client.post("/v1/sessions", json={"title": "dir"}).json()["id"]
        handle = invoker.invoke(_spec(p_inv))
        direct = spawn_child_turn_threadsafe(app, _spec(p_dir))
        if implementation == "relay":
            invoker = _relay_invoker(app, backend)
        result = invoker.wait(handle, timeout_s=10.0)
        assert app.state.agent_task_registry.event(direct.task_id).wait(timeout=10.0)
        direct_result = TaskResult.from_task(app.state.agent_task_registry.get(direct.task_id))

        assert _norm_payload(result.to_wire()) == _norm_payload(direct_result.to_wire())
        assert result.status == direct_result.status == STATUS_COMPLETED
        assert result.result["answer_excerpt"] == direct_result.result["answer_excerpt"]


@pytest.mark.parametrize("implementation", ["in-process", "relay"])
def test_s7_event_parity_across_both_invokers(
    implementation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both implementations publish the same ordered lifecycle event family."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        invoker: ExpertInvoker = (
            InProcessExpertInvoker(app)
            if implementation == "in-process"
            else _relay_invoker(app, backend)
        )
        p_inv = client.post("/v1/sessions", json={"title": "inv"}).json()["id"]
        p_dir = client.post("/v1/sessions", json={"title": "dir"}).json()["id"]
        handle = invoker.invoke(_spec(p_inv))
        direct = spawn_child_turn_threadsafe(app, _spec(p_dir))
        invoker.wait(handle, timeout_s=10.0)
        _wait_terminal(app, direct.task_id)
        _wait_task_events_settled(app, handle.child_session_id)
        _wait_task_events_settled(app, direct.child_session_id)

        actual = _norm_events(app, handle.child_session_id)
        expected = _norm_events(app, direct.child_session_id)
        assert actual == expected
        assert [event_type for event_type, _payload in actual] == [
            "agent.task.queued",
            "agent.task.started",
            "agent.task.completed",
        ]


@pytest.mark.parametrize("implementation", ["in-process", "relay"])
def test_s7_typed_error_parity_across_both_invokers(
    implementation: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both implementations preserve the direct typed undeclared-child refusal."""

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app):
        invoker: ExpertInvoker = (
            InProcessExpertInvoker(app)
            if implementation == "in-process"
            else _relay_invoker(app, backend)
        )
        parent = app.state.sessions.create(workspace_id="ws_default", title="p")
        spec = TaskSpec(
            child_expert_id="hpc_expert",
            task_text="x",
            parent_session_id=parent.id,
        )
        with pytest.raises(SpawnError) as inv_exc:
            invoker.invoke(spec)
        with pytest.raises(SpawnError) as direct_exc:
            spawn_child_turn_threadsafe(app, spec)

        assert inv_exc.value.reason == direct_exc.value.reason == "undeclared_child"
        depth_spec = TaskSpec(
            child_expert_id="data_expert",
            task_text="x",
            parent_session_id=parent.id,
            depth=MAX_SPAWN_DEPTH + 1,
        )
        with pytest.raises(SpawnError) as inv_depth:
            invoker.invoke(depth_spec)
        with pytest.raises(SpawnError) as direct_depth:
            spawn_child_turn_threadsafe(app, depth_spec)
        assert inv_depth.value.reason == direct_depth.value.reason == "spawn_depth_exceeded"
        assert backend.submissions == []


def test_taskspec_maps_to_remote_agent_task_spec_verbatim(
    tmp_path: Path,
) -> None:
    """Remote execution fields are configured and context is the exact TaskSpec wire."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    invoker = _relay_invoker(app, backend)
    spec = TaskSpec(
        child_expert_id="worker",
        task_text="inspect",
        parent_session_id="session-parent",
        requesting_expert_id="main",
        workspace_id="ws-science",
        session_mode="architect",
        session_scope_metadata={"active_agent_blueprint_id": "science"},
    )

    assert invoker.remote_agent_task_spec(spec) == {
        "prompt_path": "/shared/clio/relay-expert.md",
        "mcp_config_path": "/shared/clio/mcp.toml",
        "model": "anthropic/claude",
        "workdir": "/shared/work",
        "context": spec_to_wire(spec),
        "request_followup_message": True,
    }


def test_relay_message_answers_post_admission_input_on_retained_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay invoker carries a message over the retained durable identity."""

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "relay message"}).json()["id"]
        invoker = _relay_invoker(app, backend)
        handle = invoker.invoke(
            TaskSpec(
                child_expert_id="data_expert",
                task_text="initial",
                parent_session_id=parent,
                placement="relay:ares",
            )
        )
        invoker.message(handle, "Use the new boundary condition.")
        result = invoker.wait(handle, timeout_s=1.0)
        assert result.status == "completed"
        assert backend.tasks[handle.task_id]["stream_closed"].wait(1.0)

    assert backend.messages == [(handle.task_id, "Use the new boundary condition.")]


@pytest.mark.parametrize("observation", sorted(RELAY_STATE_MAP))
def test_every_committed_relay_state_row_is_used(observation: str, tmp_path: Path) -> None:
    """Every committed source row translates through the sole table."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    invoker = _relay_invoker(app, _FakeRelayBackend())
    result = None
    error = None
    if observation == "succeeded":
        result = {"isError": False}
    elif observation == "tool-fail":
        result = {"isError": True}
    elif observation == "protocol":
        error = {"reason": "agent_error"}
    current = _relay_current(observation, result=result, error=error)

    actual_observation, projection = invoker._relay_projection(current)

    assert actual_observation == observation
    assert projection is RELAY_STATE_MAP[observation]


def test_relay_detach_new_invoker_reconnects_by_task_id_and_streams_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new invoker/client resumes the retained task id and its event stream."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        originating = _relay_invoker(app, backend)
        handle = originating.invoke(_spec(parent))
        assert handle.task_id == next(iter(backend.tasks))
        assert resolve_store(None).get(originating._task_key(handle)) is not None
        deadline = time.monotonic() + 5.0
        while backend.client_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.client_count >= 2

        rebuilt = _relay_invoker(app, backend)
        result = rebuilt.wait(handle, timeout_s=10.0)
        _wait_task_events_settled(app, handle.child_session_id)

    assert result.status == STATUS_COMPLETED
    assert result.task_id == handle.task_id
    assert backend.client_count >= 3
    assert _norm_events(app, handle.child_session_id)[-1][0] == "agent.task.completed"


def test_relay_live_task_events_use_committed_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSE door feeds TaskEvent objects through fold_agent_task_event."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    seen = threading.Event()
    from clio_agent.gact.agents import invoker as invoker_module

    original = invoker_module.fold_agent_task_event

    def recording_fold(app_arg: Any, observation: Any, **kwargs: Any) -> Any:
        if isinstance(observation, TaskEvent):
            seen.set()
        return original(app_arg, observation, **kwargs)

    monkeypatch.setattr(invoker_module, "fold_agent_task_event", recording_fold)
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        handle = _relay_invoker(app, backend).invoke(_spec(parent))
        assert seen.wait(timeout=5.0)
        assert (
            _wait_terminal(
                app,
                _relay_invoker(app, backend).wait(handle, timeout_s=10.0).task_id,
            ).status
            == STATUS_COMPLETED
        )


def test_relay_cancel_acknowledges_now_and_settles_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation ack keeps the task running until a later canonical poll."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    backend = _FakeRelayBackend()
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        invoker = _relay_invoker(app, backend)
        handle = invoker.invoke(_spec(parent))
        assert invoker.cancel(handle) is True
        acknowledged = app.state.agent_task_registry.get(handle.task_id)
        assert acknowledged.status != STATUS_CANCELLED
        record = resolve_store(None).get(invoker._task_key(handle))
        assert record is not None and record.cancel_requested is True

        settled = invoker.check([handle])[0]

    assert settled.status == STATUS_CANCELLED
    assert invoker.cancel(handle) is False


# ---------------------------------------------------------------------------
# invoke parity: same record + same started events
# ---------------------------------------------------------------------------


def test_build_app_binds_in_process_expert_invoker(tmp_path: Path) -> None:
    """P2.6: app assembly has one explicit default executor binding."""

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())

    assert isinstance(app.state.expert_invoker, InProcessExpertInvoker)
    assert app.state.expert_invoker.app is app


def test_invoke_parity_records_and_events(tmp_path: Path, monkeypatch) -> None:
    """A child spawned through the invoker is record- and event-identical to one
    spawned through the direct substrate (same status, run_index, agent_ref, depth;
    same normalized event stream).

    Sabotage: make ``InProcessExpertInvoker.invoke`` mint its own record (bypass
    ``spawn_child_turn_threadsafe``) and the event streams / run_index diverge.
    """

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        invoker: ExpertInvoker = InProcessExpertInvoker(app)
        assert isinstance(invoker, ExpertInvoker)  # runtime-checkable Protocol
        p_inv = client.post("/v1/sessions", json={"title": "inv"}).json()["id"]
        p_dir = client.post("/v1/sessions", json={"title": "dir"}).json()["id"]

        handle = invoker.invoke(_spec(p_inv))
        direct = spawn_child_turn_threadsafe(app, _spec(p_dir))

        # Handle mirrors the freshly-spawned record.
        assert isinstance(handle, TaskHandle)
        assert handle.status == direct.status == STATUS_RUNNING
        assert handle.run_index == direct.run_index == 0
        assert handle.depth == direct.depth == 1

        inv_task = app.state.agent_task_registry.get(handle.task_id)
        assert inv_task.agent_ref == direct.agent_ref
        assert inv_task.queued_reason == direct.queued_reason == ""

        _wait_terminal(app, handle.task_id)
        _wait_terminal(app, direct.task_id)
        # Wait for the terminal BUS event too (it lags the registry transition), or the
        # stream diff races on a not-yet-appended ``completed``.
        _wait_task_events_settled(app, handle.child_session_id)
        _wait_task_events_settled(app, direct.child_session_id)

        # Structural diff of the two children's own event streams (queued→started→
        # completed), ids/timestamps normalized out.
        inv_events = _norm_events(app, handle.child_session_id)
        dir_events = _norm_events(app, direct.child_session_id)
        assert inv_events == dir_events, (inv_events, dir_events)
        assert [t for t, _ in inv_events] == [
            "agent.task.queued",
            "agent.task.started",
            "agent.task.completed",
        ]


# ---------------------------------------------------------------------------
# wait parity: same terminal result projection
# ---------------------------------------------------------------------------


def test_wait_parity_result_matches_projection(tmp_path: Path, monkeypatch) -> None:
    """``invoker.wait(handle, t)`` returns a :class:`TaskResult` equal to projecting
    the record the direct substrate wait (Event.wait + registry.get) resolves."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        p_inv = client.post("/v1/sessions", json={"title": "inv"}).json()["id"]
        p_dir = client.post("/v1/sessions", json={"title": "dir"}).json()["id"]

        handle = invoker.invoke(_spec(p_inv))
        direct = spawn_child_turn_threadsafe(app, _spec(p_dir))

        result = invoker.wait(handle, timeout_s=10.0)
        assert isinstance(result, TaskResult)
        assert result.is_terminal and result.status == STATUS_COMPLETED

        # Direct substrate wait, then project — the exact same values.
        reg = app.state.agent_task_registry
        assert reg.event(direct.task_id).wait(timeout=10.0)
        dir_task = reg.get(direct.task_id)
        dir_result = TaskResult.from_task(dir_task)

        # Compare modulo volatile identity (ids/timestamps/session).
        inv_wire = _norm_payload(result.to_wire())
        dir_wire = _norm_payload(dir_result.to_wire())
        assert inv_wire == dir_wire, (inv_wire, dir_wire)
        # The result payload carried the child's answer + its own workflow_state
        # (empty for the stub child, which produces none — the PARENT's injected
        # plan rides the task text, not the child's returned state).
        assert "child did" in result.result["answer_excerpt"]
        assert result.result["workflow_state"] == {}


def test_wait_on_timeout_returns_current_nonterminal(tmp_path: Path, monkeypatch) -> None:
    """A wait that times out returns the CURRENT (running) record, not an error —
    the caller decides how to proceed (parity with ``registry.event.wait`` → False,
    then ``registry.get``)."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=3.0))
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        handle = invoker.invoke(_spec(parent))
        result = invoker.wait(handle, timeout_s=0.1)
        assert not result.is_terminal
        assert result.status == STATUS_RUNNING


# ---------------------------------------------------------------------------
# check parity: non-blocking poll projection
# ---------------------------------------------------------------------------


def test_check_parity_polls_current_records(tmp_path: Path, monkeypatch) -> None:
    """``invoker.check([...])`` returns, in order, the projection of each handle's
    current record — matching a direct ``registry.get`` + project."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        h1 = invoker.invoke(_spec(parent, "task one"))
        h2 = invoker.invoke(_spec(parent, "task two"))
        _wait_terminal(app, h1.task_id)
        _wait_terminal(app, h2.task_id)

        rows = invoker.check([h1, h2])
        assert [r.task_id for r in rows] == [h1.task_id, h2.task_id]  # order preserved
        reg = app.state.agent_task_registry
        for handle, row in zip((h1, h2), rows, strict=True):
            assert row.to_wire() == TaskResult.from_task(reg.get(handle.task_id)).to_wire()


# ---------------------------------------------------------------------------
# cancel parity: same effect + same cancel event
# ---------------------------------------------------------------------------


def test_cancel_parity_effect_and_event(tmp_path: Path, monkeypatch) -> None:
    """``invoker.cancel(handle)`` cancels the child turn and publishes the cascade
    cancel event exactly as ``cancel_agent_task`` — one via the invoker, one direct,
    same terminal status + same parent-visible ``agent.task.cancelled`` event."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=5.0))
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        p_inv = client.post("/v1/sessions", json={"title": "inv"}).json()["id"]
        p_dir = client.post("/v1/sessions", json={"title": "dir"}).json()["id"]
        h_inv = invoker.invoke(_spec(p_inv))
        t_dir = spawn_child_turn_threadsafe(app, _spec(p_dir))

        assert invoker.cancel(h_inv) is True
        assert cancel_agent_task(app, t_dir.task_id) is True

        inv_settled = _wait_terminal(app, h_inv.task_id, timeout=6.0)
        dir_settled = _wait_terminal(app, t_dir.task_id, timeout=6.0)
        assert inv_settled.status == dir_settled.status == "cancelled"
        # The terminal BUS event lags the registry transition; settle on it before diffing.
        _wait_task_events_settled(app, h_inv.child_session_id)
        _wait_task_events_settled(app, t_dir.child_session_id)

        inv_events = _norm_events(app, h_inv.child_session_id)
        dir_events = _norm_events(app, t_dir.child_session_id)
        assert inv_events == dir_events
        assert any(t == "agent.task.cancelled" for t, _ in inv_events)


# ---------------------------------------------------------------------------
# typed-error parity: invoke refusals raise the SAME SpawnError
# ---------------------------------------------------------------------------


def test_invoke_undeclared_child_parity(tmp_path: Path, monkeypatch) -> None:
    """Both the invoker and the direct substrate raise ``SpawnError`` with reason
    ``undeclared_child`` for a child the parent did not declare."""

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        invoker = InProcessExpertInvoker(app)
        parent = app.state.sessions.create(workspace_id="ws_default", title="p")
        spec = TaskSpec(child_expert_id="hpc_expert", task_text="x", parent_session_id=parent.id)
        with pytest.raises(SpawnError) as inv_exc:
            invoker.invoke(spec)
        with pytest.raises(SpawnError) as dir_exc:
            spawn_child_turn_threadsafe(app, spec)
        assert inv_exc.value.reason == dir_exc.value.reason == "undeclared_child"


def test_invoke_depth_cap_parity(tmp_path: Path, monkeypatch) -> None:
    """Both paths raise ``SpawnError`` reason ``spawn_depth_exceeded`` past the
    runaway backstop."""

    _declare(monkeypatch, "data_expert")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        invoker = InProcessExpertInvoker(app)
        spec = TaskSpec(
            child_expert_id="data_expert",
            task_text="x",
            parent_session_id="sess_p",
            depth=MAX_SPAWN_DEPTH + 1,
        )
        with pytest.raises(SpawnError) as inv_exc:
            invoker.invoke(spec)
        with pytest.raises(SpawnError) as dir_exc:
            spawn_child_turn_threadsafe(app, spec)
        assert inv_exc.value.reason == dir_exc.value.reason == "spawn_depth_exceeded"


def test_queue_at_cap_parity(tmp_path: Path, monkeypatch) -> None:
    """At the concurrency cap the second spawn QUEUES (not an error) with typed
    ``queued_reason`` — identical through the invoker handle and the direct record."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(sleep_s=1.0))
    with TestClient(app) as client:
        app.state.max_concurrent_agent_tasks = 1
        invoker = InProcessExpertInvoker(app)
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        first = invoker.invoke(_spec(parent))
        second = invoker.invoke(_spec(parent))
        assert first.status == STATUS_RUNNING
        assert second.status == "queued"
        assert second.queued_reason == "concurrency_cap"
        # Both still drive to terminal (FIFO admission on the freed slot).
        assert _wait_terminal(app, first.task_id).status == STATUS_COMPLETED
        assert _wait_terminal(app, second.task_id).status == STATUS_COMPLETED


# ---------------------------------------------------------------------------
# unknown-handle guard: typed InvokerError, never a block-forever
# ---------------------------------------------------------------------------


def test_wait_unknown_handle_raises_typed(tmp_path: Path, monkeypatch) -> None:
    """Waiting on a handle whose task is unknown raises ``InvokerError``
    (``unknown_task``) IMMEDIATELY — never blocks the full budget on a fresh
    never-set Event (the ``wait_agent_tasks`` footgun).

    Sabotage: drop the ``reg.get(...) is None`` guard in ``wait`` and this test
    hangs for the timeout instead of raising.
    """

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        invoker = InProcessExpertInvoker(app)
        bogus = TaskHandle(task_id="task_nope", parent_session_id="p", child_session_id="c")
        start = time.monotonic()
        with pytest.raises(InvokerError) as exc:
            invoker.wait(bogus, timeout_s=30.0)
        assert exc.value.reason == "unknown_task"
        assert time.monotonic() - start < 1.0, "unknown handle must not block the budget"


def test_check_unknown_handle_raises_typed(tmp_path: Path, monkeypatch) -> None:
    """An unknown handle in a check batch raises typed rather than being silently
    dropped (no-silent-fallback)."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app):
        invoker = InProcessExpertInvoker(app)
        bogus = TaskHandle(task_id="task_nope", parent_session_id="p", child_session_id="c")
        with pytest.raises(InvokerError) as exc:
            invoker.check([bogus])
        assert exc.value.reason == "unknown_task"


# ---------------------------------------------------------------------------
# run_index / notify semantics parity (ensemble)
# ---------------------------------------------------------------------------


def test_run_index_and_notify_parity(tmp_path: Path, monkeypatch) -> None:
    """Spawning the SAME child twice in one parent turn assigns run_index 0,1 through
    the invoker exactly as the direct path; an async child sets ``notify_pending`` on
    completion (observe-later), and the invoker does NOT consume it (that stays a
    parent-side concern above the seam)."""

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]

        def _turn_spec():
            return TaskSpec(
                child_expert_id="main",
                task_text="x",
                parent_session_id=parent,
                requesting_expert_id="main",
                parent_turn_id="turn_1",
                mode="async",
            )

        h0 = invoker.invoke(_turn_spec())
        h1 = invoker.invoke(_turn_spec())
        assert (h0.run_index, h1.run_index) == (0, 1)

        _wait_terminal(app, h0.task_id)
        settled = _wait_terminal(app, h1.task_id)
        assert settled.status == STATUS_COMPLETED
        # Async completion set notify_pending; the invoker.wait did NOT consume it.
        result = invoker.wait(h1, timeout_s=5.0)
        assert result.is_terminal
        assert app.state.agent_task_registry.get(h1.task_id).notify_pending is True


# ---------------------------------------------------------------------------
# result shape: drops internal bookkeeping; relay-compatible vocabulary
# ---------------------------------------------------------------------------


def test_taskresult_drops_internal_bookkeeping(tmp_path: Path, monkeypatch) -> None:
    """The boundary :class:`TaskResult` omits EVERY :class:`AgentTask` field that is not
    part of the executor boundary — all eight, in three classes: parent-side observe-later /
    wire-dedup bookkeeping (``notify_pending`` / ``consumed_at`` / ``delegation_reported``)
    and spawn-request / topology fields the parent already holds on its ``TaskSpec``
    (``parent_turn_id`` / ``child_turn_id`` / ``fanout_bound``), plus parent-side run-list
    display state (``detached`` / ``dismissed``).

    Adversarial-review finding [5]: the drop-list must be EXHAUSTIVE against the code —
    ``AgentTask`` minus ``TaskResult`` is exactly these eight, no more, no less."""

    task_fields = set(AgentTask.__dataclass_fields__)
    result_fields = set(TaskResult.__dataclass_fields__)
    dropped = {
        "notify_pending",
        "consumed_at",
        "delegation_reported",
        "parent_turn_id",
        "child_turn_id",
        "fanout_bound",
        "detached",
        "dismissed",
    }
    assert dropped.isdisjoint(result_fields)  # none of the eight survive the projection
    # EXHAUSTIVE: the eight named above are exactly the fields AgentTask has and
    # TaskResult drops — a newly-added dropped/carried field must update this + the docs.
    assert task_fields - result_fields == dropped
    # But it DOES carry the durable, relay-compatible record vocabulary.
    assert {
        "task_id",
        "status",
        "created_at",
        "updated_at",
        "error_reason",
        "result",
        "artifact_ref",
    } <= result_fields


# ---------------------------------------------------------------------------
# event vocabulary + serializable boundary event
# ---------------------------------------------------------------------------


def test_task_event_vocabulary_and_projection(tmp_path: Path, monkeypatch) -> None:
    """``task_event_type`` resolves the agent.task.* family, and :class:`TaskEvent`
    projects a published bus event into a serializable boundary shape."""

    assert task_event_type("completed") == "agent.task.completed"
    assert TASK_CONSUMED_EVENT == "agent.task.consumed"
    with pytest.raises(InvokerError) as exc:
        task_event_type("bogus")
    assert exc.value.reason == "unknown_status"

    _declare(monkeypatch, "main")
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        invoker = InProcessExpertInvoker(app)
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        handle = invoker.invoke(_spec(parent))
        _wait_terminal(app, handle.task_id)
        # Terminal STATUS lands one step before the completion hook publishes the
        # terminal bus event — poll the bus rather than racing it (the _wait_bus
        # pattern; a bounded wait still fails if the event is truly absent).
        deadline = time.monotonic() + 15.0
        completed: list = []
        while time.monotonic() < deadline and not completed:
            completed = [
                e
                for e in app.state.bus._history.get(handle.child_session_id, [])
                if e.type == "agent.task.completed"
            ]
            if not completed:
                time.sleep(0.05)
        assert completed
        ev = TaskEvent.from_bus_event(completed[0])
        assert ev.event_type == "agent.task.completed"
        assert ev.status == "completed"
        assert ev.task_id == handle.task_id
        assert json.loads(json.dumps(ev.to_wire())) == ev.to_wire()  # JSON-serializable
