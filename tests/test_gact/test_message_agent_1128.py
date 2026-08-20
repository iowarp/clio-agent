"""P2.11 message-an-agent: placement-selected queue/steer/wake semantics."""

from __future__ import annotations

import json
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
    persist_agent_task,
    seed_agent_task,
)
from clio_agent.gact.agents.invoker import TaskHandle
from clio_agent.gact.app import build_app
from clio_agent.gact.types import Part

FIXTURE = Path(__file__).parents[1] / "fixtures" / "message_agent_parts_1128.json"


class _Agent:
    def forward(self, question: str, session_id: str, **_kwargs: Any) -> Any:
        del session_id
        return type(
            "Prediction",
            (),
            {
                "answer": f"child consumed: {question}",
                "selected_expert": "",
                "routing_rationale": "",
            },
        )()


class _RelayMessageInvoker:
    def __init__(self) -> None:
        self.messages: list[tuple[TaskHandle, str, dict[str, Any]]] = []
        self.specs: list[Any] = []

    def message(
        self,
        handle: TaskHandle,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.messages.append((handle, text, dict(metadata or {})))

    def invoke(self, spec: Any) -> TaskHandle:
        self.specs.append(spec)
        return TaskHandle(
            task_id="task_relay_woken",
            parent_session_id=spec.parent_session_id,
            child_session_id="sess_relay_woken",
            status=STATUS_RUNNING,
            handle_id="task_relay_woken",
            run_label=f"{spec.child_expert_id} #2",
            live_state=STATUS_RUNNING,
            host="ares",
            placement="relay:ares",
        )


def _seed(
    app: Any,
    parent: str,
    *,
    status: str,
    placement: str,
    task_id: str,
) -> AgentTask:
    task = seed_agent_task(
        app,
        parent_session_id=parent,
        agent_ref={"expert_id": "data_expert", "requesting_expert_id": "main"},
        status=status,
        placement=placement,
        host="ares" if placement.startswith("relay:") else "local",
        task_id=task_id,
    )
    if status == STATUS_COMPLETED:
        completed = AgentTask(
            **{
                **task.__dict__,
                "result": {"message_ref": "msg_old", "answer_excerpt": "old return"},
            }
        )
        app.state.agent_task_registry.register(completed)
        persist_agent_task(app, completed)
        return completed
    return task


def test_relay_running_message_arrives_via_tasks_update_and_child_consumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILING-FIRST: relay messaging uses the parked input round, never local inbox."""

    from clio_agent.gact.agent_messaging import message_agent_task

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    relay = _RelayMessageInvoker()
    parts: list[Part] = []
    monkeypatch.setattr(
        "clio_agent.gact.agent_messaging._append_live_assistant_part",
        lambda _app, _sid, part: parts.append(part),
    )
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        app.state.relay_expert_invokers = {"ares": relay}
        task = _seed(
            app,
            parent,
            status=STATUS_RUNNING,
            placement="relay:ares",
            task_id="task_relay_running",
        )
        result = message_agent_task(app, task.task_id, "Use the new boundary condition.")

    assert result.action == "queue"
    assert result.transport == "tasks/update"
    assert [(text, metadata) for _handle, text, metadata in relay.messages] == [
        ("Use the new boundary condition.", {})
    ]
    assert app.state.loop_inboxes.get(task.child_session_id) is None
    assert parts[0].type == "agent_message"
    assert parts[0].stage == "message.queued"


def test_route_selects_relay_without_transport_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    relay = _RelayMessageInvoker()
    monkeypatch.setattr(
        "clio_agent.gact.agent_messaging._append_live_assistant_part",
        lambda _app, _sid, _part: None,
    )
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        app.state.relay_expert_invokers = {"ares": relay}
        task = _seed(
            app,
            parent,
            status=STATUS_RUNNING,
            placement="relay:ares",
            task_id="task_relay_route",
        )
        response = client.post(
            f"/v1/agent-tasks/{task.task_id}/steer",
            json={"text": "route this from placement"},
        )

    assert response.status_code == 202
    assert response.json()["transport"] == "tasks/update"
    assert relay.messages[0][1] == "route this from placement"


def test_message_agent_tool_has_no_transport_argument() -> None:
    from clio_agent.gact.agent_messaging import build_message_agent_tool

    tool = build_message_agent_tool(SimpleNamespace(id="main"))
    assert tool.name == "message_agent"
    assert set(tool.args) == {"task_id", "message"}


def test_local_and_relay_running_messages_have_shape_identical_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agent_messaging import message_agent_task

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    relay = _RelayMessageInvoker()
    parts: list[Part] = []
    monkeypatch.setattr(
        "clio_agent.gact.agent_messaging._append_live_assistant_part",
        lambda _app, _sid, part: parts.append(part),
    )
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        app.state.relay_expert_invokers = {"ares": relay}
        local = _seed(
            app,
            parent,
            status=STATUS_RUNNING,
            placement="local",
            task_id="task_local_running",
        )
        remote = _seed(
            app,
            parent,
            status=STATUS_RUNNING,
            placement="relay:ares",
            task_id="task_relay_running",
        )
        monkeypatch.setattr(
            app.state.turn_runner,
            "busy",
            lambda sid: sid == local.child_session_id,
        )
        local_result = message_agent_task(app, local.task_id, "focus locally")
        relay_result = message_agent_task(app, remote.task_id, "focus remotely")

    assert local_result.transport == "step_boundary"
    assert relay_result.transport == "tasks/update"
    assert len(app.state.loop_inboxes[local.child_session_id].drain()) == 1
    assert len(parts) == 2
    assert set(parts[0].to_wire()) == set(parts[1].to_wire())


@pytest.mark.parametrize("placement", ["local", "relay:ares"])
def test_finished_child_wake_emits_parent_supersede_event_and_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    placement: str,
) -> None:
    from clio_agent.gact.agent_messaging import message_agent_task

    monkeypatch.setattr(
        "clio_agent.gact.agents.resolution._runtime_declared_child_ids",
        lambda _app, _pid, session_id="", **_bindings: {"data_expert"},
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    relay = _RelayMessageInvoker()
    parts: list[Part] = []
    monkeypatch.setattr(
        "clio_agent.gact.agent_messaging._append_live_assistant_part",
        lambda _app, _sid, part: parts.append(part),
    )
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        app.state.relay_expert_invokers = {"ares": relay}
        old = _seed(
            app,
            parent,
            status=STATUS_COMPLETED,
            placement=placement,
            task_id=f"task_old_{placement.replace(':', '_')}",
        )
        result = message_agent_task(app, old.task_id, "Recheck with the new constraint.")

    assert result.action == "wake"
    assert result.task_id != old.task_id
    assert result.supersedes_task_id == old.task_id
    events = [
        event
        for event in app.state.bus._history.get(parent, [])
        if event.type == "agent.task.superseded"
    ]
    assert len(events) == 1
    assert events[0].payload["supersedes_task_id"] == old.task_id
    assert events[0].payload["superseded_by_task_id"] == result.task_id
    assert len(parts) == 1
    assert parts[0].type == "expert_handoff"
    assert parts[0].stage == "delegate.superseded"
    assert parts[0].supersedes_handle_id == old.task_id
    assert parts[0].superseded_by_handle_id == result.task_id


@pytest.mark.parametrize(
    ("task_id", "status", "reason"),
    [
        ("task_unknown", None, "unknown_task"),
        ("task_cancelled", STATUS_CANCELLED, "task_unwakeable"),
    ],
)
def test_unknown_and_terminal_unwakeable_edges_are_typed(
    tmp_path: Path,
    task_id: str,
    status: str | None,
    reason: str,
) -> None:
    from clio_agent.gact.agent_messaging import MessageAgentError, message_agent_task

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        if status is not None:
            _seed(app, parent, status=status, placement="local", task_id=task_id)
        with pytest.raises(MessageAgentError) as excinfo:
            message_agent_task(app, task_id, "hello")
    assert excinfo.value.reason == reason


@pytest.mark.parametrize("index", [0, 1, 2])
def test_message_agent_part_matches_committed_fixture(index: int) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert Part(**rows[index]).to_wire() == rows[index]


# --------------------------------------------------------------------------- #
# Declared structured_content (P5 wire semantics) — the wait_agent_tasks       #
# treatment extended to message_agent, via the REAL tool (not message_agent_task #
# directly), so the closure's own declare_structured_content call is under test. #
# --------------------------------------------------------------------------- #


def test_message_agent_tool_declares_typed_structured_content_for_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agent_messaging import build_message_agent_tool

    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    monkeypatch.setattr(
        "clio_agent.gact.agent_messaging._append_live_assistant_part",
        lambda _app, _sid, _part: None,
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        task = _seed(
            app, parent, status=STATUS_RUNNING, placement="local", task_id="task_queue_msg"
        )
        monkeypatch.setattr(app.state.turn_runner, "busy", lambda sid: sid == task.child_session_id)
        tool = build_message_agent_tool(SimpleNamespace(id="main"))
        token_a = _ctx.set_app(app)
        token_s = _ctx.set_session_id(parent)
        try:
            out = tool.func(task_id=task.task_id, message="focus locally")
        finally:
            _ctx.reset(token_s)
            _ctx.reset(token_a)

    wire = json.loads(out)
    assert wire["accepted"] is True
    assert len(declared) == 1
    shape = declared[0]
    assert next(iter(shape)) == "message"
    assert shape["message"] == (
        f"queued message to task {task.task_id} (transport={wire['transport']})"
    )
    assert {k: v for k, v in shape.items() if k != "message"} == wire


def test_message_agent_tool_declares_typed_structured_content_for_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import context as _ctx
    from clio_agent.gact.agent_messaging import build_message_agent_tool

    declared: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "clio_agent.gact.agents.tool_instrumentation.declare_structured_content",
        lambda value: declared.append(dict(value)),
    )
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        parent = client.post("/v1/sessions", json={"title": "p"}).json()["id"]
        tool = build_message_agent_tool(SimpleNamespace(id="main"))
        token_a = _ctx.set_app(app)
        token_s = _ctx.set_session_id(parent)
        try:
            out = tool.func(task_id="task_never_existed", message="hello")
        finally:
            _ctx.reset(token_s)
            _ctx.reset(token_a)

    wire = json.loads(out)
    assert wire["error"] == "unknown_task"
    assert len(declared) == 1
    shape = declared[0]
    assert next(iter(shape)) == "message"
    assert shape["message"] == f"message rejected: unknown_task — {wire['message']}"
    # The raw exception text is preserved (never dropped) — renamed to "detail" in
    # the structured payload only, since "message" now carries our composed
    # presentation summary; the model-facing wire's own "message" is untouched.
    assert shape["detail"] == wire["message"]
    assert {k: v for k, v in shape.items() if k not in ("message", "detail")} == {
        k: v for k, v in wire.items() if k != "message"
    }
