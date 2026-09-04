"""Runtime-tool and normalized interaction contract tests."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import dspy
from dspy.utils.dummies import DummyLM
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.agent_initialization import mark_agent_ready, record_init_failure
from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.builders import _dynamic_agent_tools
from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls
from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import arm_ask_user_deadline
from clio_agent.gact.elicitation_bridge import (
    claim_question_transition,
    invocation_with_request_correlation,
)
from clio_agent.gact.loop_inbox import InboxEvent, LoopInbox
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import AgentDef, UserQuestion, UserQuestionOption
from clio_agent.gact.user_question_ledger import record_user_question
from clio_agent.tools.mcp_handlers import MCPInvocationContext
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, resolve_store

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def test_agent_init_failure_surfaces_a_deferred_question_resume() -> None:
    """An answered question may remain queued, but it must not look silently idle.

    A failed construction is NOT terminal for the process: ``PUT /v1/providers/lm``
    rebinds and calls ``mark_agent_ready``, which drains exactly these inboxes.
    Destroying the parked items to "release" them therefore throws away work the
    one recovery path could still deliver.
    """

    class Sessions:
        def __init__(self) -> None:
            self.updates: list[tuple[str, dict[str, object]]] = []

        def update(self, session_id: str, **changes: object) -> None:
            self.updates.append((session_id, changes))

    class Bus:
        def __init__(self) -> None:
            self.events: list[object] = []

        def publish(self, event: object) -> None:
            self.events.append(event)

    inbox = LoopInbox()
    inbox.put(
        InboxEvent(
            kind="user_message",
            task_id="",
            metadata={"ask_user_resume": True, "question_id": "question_1"},
        )
    )
    sessions = Sessions()
    bus = Bus()
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_init_error=None,
            bus=bus,
            loop_inboxes={"child_session": inbox},
            sessions=sessions,
        )
    )

    record_init_failure(app, RuntimeError("provider unavailable"), stage="init")

    assert inbox.peek_nonempty(), "the recoverable answer should remain queued"
    assert sessions.updates[0][0] == "child_session"
    # "failed" is out of ``Session.status``'s vocabulary; "error" is the real one.
    assert sessions.updates[0][1]["status"] == "error"
    event = bus.events[0]
    assert event.type == "session.input_refused"
    assert event.payload["question_id"] == "question_1"
    assert event.payload["reason"] == "agent_init_failed"
    # The item is retained, so the refusal must say so and name the door that
    # delivers it -- publishing ``recoverable: False`` next to recovery actions
    # (which is what the drain-everything version did) is self-contradicting.
    assert event.payload["recoverable"] is True
    assert event.payload["retained"] is True
    assert event.payload["recovery_actions"] == ["rebind_lm_provider"]


def _root_and_child(app: object) -> tuple[str, str]:
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    child = app.state.sessions.create(
        workspace_id="ws_default", title="child", parent_session_id=root.id
    )
    app.state.agent_task_registry.register(
        AgentTask(
            task_id="task_child",
            parent_session_id=root.id,
            child_session_id=child.id,
            agent_ref={"expert_id": "child"},
            status="running",
            created_at="2026-09-02T10:00:00+00:00",
        )
    )
    return root.id, child.id


def test_declared_native_tools_are_selective_and_root_a2ui_remains_compatible() -> None:
    ask = AgentDef(id="asker", title="Asker", parent_id="root", tools=["ask_user"])
    visual = AgentDef(id="visual", title="Visual", parent_id="root", tools=["create_a2ui_surface"])
    plain_child = AgentDef(id="plain", title="Plain", parent_id="root")
    root = AgentDef(id="root", title="Root")

    assert [tool.name for tool in _dynamic_agent_tools(SimpleNamespace(), ask, {})] == ["ask_user"]
    assert [tool.name for tool in _dynamic_agent_tools(SimpleNamespace(), visual, {})] == [
        "create_a2ui_surface"
    ]
    assert "ask_user" not in {tool.name for tool in build_auto_react_tools(plain_child)}
    assert "create_a2ui_surface" not in {tool.name for tool in build_auto_react_tools(plain_child)}
    assert "create_a2ui_surface" in {tool.name for tool in build_auto_react_tools(root)}


def test_ask_user_runtime_tool_injects_owner_task_and_attended_correlation(
    tmp_path, monkeypatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root, child = _root_and_child(app)
    agent = AgentDef(id="child", title="Child", parent_id="root", tools=["ask_user"])
    tool = _dynamic_agent_tools(SimpleNamespace(), agent, {})[0]
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: child)
    monkeypatch.setattr(gact_context, "active_turn_id", lambda: "turn_child")

    result = tool(
        question="Which dataset should I use?",
        kind="choice",
        options=[{"label": "A", "value": "a"}],
        allowFreeform=True,
    )

    pending = app.state.sessions.get(child).metadata["pending_ask_user"]
    assert "END YOUR TURN" in result
    assert pending["owner_session_id"] == child
    assert pending["attended_session_id"] == root
    assert pending["task_id"] == "task_child"
    assert pending["invocation_id"] == "turn_child:child:ask_user"
    assert pending["allow_freeform"] is True


def test_ask_user_success_ends_react_turn_before_another_model_step(tmp_path) -> None:
    """A native question is a runtime yield, not advice the model may ignore."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ask")
    agent_def = AgentDef(id="asker", title="Asker", tools=["ask_user"])
    ask_tool = _dynamic_agent_tools(SimpleNamespace(), agent_def, {})[0]
    react = retaining_reactv2_cls()(
        "question -> answer",
        tools=[ask_tool],
        max_iters=0,
    )
    lm = DummyLM(
        [
            {
                "next_thought": "The requested study lacks a defined objective.",
                "tool_calls": {
                    "tool_calls": [
                        {
                            "name": "ask_user",
                            "args": {
                                "question": "What outcome should the simulation estimate?",
                                "kind": "freeform",
                            },
                        }
                    ]
                },
            },
            {
                "next_thought": "This second model step must never run.",
                "tool_calls": {"tool_calls": [{"name": "submit", "args": {"answer": "wrong"}}]},
            },
        ]
    )
    app_token = gact_context.set_app(app)
    session_token = gact_context.set_session_id(session.id)
    turn_token = gact_context.set_turn_id_token("turn_ask")
    try:
        with dspy.context(lm=lm, adapter=dspy.ChatAdapter()):
            prediction = react(question="Help me design a simulation study.")
    finally:
        gact_context.reset(turn_token)
        gact_context.reset(session_token)
        gact_context.reset(app_token)

    assert prediction.termination_reason == "ask_user_yield"
    assert not getattr(prediction, "answer", "")
    assert len(lm.history) == 1
    pending = app.state.sessions.get(session.id).metadata["pending_ask_user"]
    assert pending["question"] == "What outcome should the simulation estimate?"
    assert pending["surfaced"] is False


def test_interactions_aggregate_children_and_route_question_and_permission(
    tmp_path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root, child = _root_and_child(app)
    now = datetime.now(timezone.utc).isoformat()
    native = UserQuestion(
        id="q_native",
        session_id=root,
        owner_session_id=root,
        attended_session_id=root,
        prompt="Proceed?",
        kind="confirmation",
        options=[UserQuestionOption(label="Yes", value="yes")],
        allow_freeform=True,
        created_at=now,
        updated_at=now,
    )
    mcp = UserQuestion(
        id="q_mcp",
        session_id=root,
        owner_session_id=child,
        attended_session_id=root,
        prompt="Select format",
        kind="choice",
        options=[UserQuestionOption(label="CSV", value="csv")],
        source="mcp_elicitation",
        created_at=now,
        updated_at=now,
        metadata={
            "elicitation": {
                "tool_name": "earthscope_query",
                "invocation_id": "call_7",
                "task_id": "task_child",
                "input_key": "format",
            }
        },
    )
    app.state.user_questions = {native.id: native, mcp.id: mcp}
    app.state.permissions["perm_child"] = {
        "id": "perm_child",
        "session_id": child,
        "status": "pending",
        "summary": "Allow child write",
        "created_at": now,
        "tool_call": {"tool_name": "fs_apply_edit_write", "input": {"path": "x"}},
    }
    calls: list[tuple[str, str]] = []
    original_answer = app.state.answer_user_question

    async def record_answer(session_id: str, question_id: str, request: object) -> UserQuestion:
        calls.append((session_id, question_id))
        return await original_answer(session_id, question_id, request)

    app.state.answer_user_question = record_answer

    with TestClient(app) as client:
        direct = client.get(f"/v1/sessions/{root}/interactions").json()["interactions"]
        assert {row["id"] for row in direct} == {
            "question:q_native",
            "mcp_task_input:q_mcp",
        }
        aggregate = client.get(
            f"/v1/sessions/{root}/interactions", params={"include_children": True}
        ).json()["interactions"]
        rows = {row["id"]: row for row in aggregate}
        assert {row["status"] for row in aggregate} == {"pending"}
        assert rows["mcp_task_input:q_mcp"]["owner_session_id"] == child
        assert rows["mcp_task_input:q_mcp"]["task_id"] == "task_child"
        assert rows["mcp_task_input:q_mcp"]["source"] == {
            "protocol": "mcp",
            "tool_name": "earthscope_query",
            "invocation_id": "call_7",
        }
        assert rows["question:q_native"]["payload"]["allow_freeform"] is True
        assert rows["permission:perm_child"]["task_id"] == "task_child"

        answered = client.post(
            f"/v1/sessions/{root}/interactions/question:q_native/respond",
            json={"action": "answer", "selected_options": ["yes"]},
        )
        assert answered.status_code == 200
        assert calls == [(root, "q_native")]
        duplicate = client.post(
            f"/v1/sessions/{root}/interactions/question:q_native/respond",
            json={"action": "answer", "selected_options": ["yes"]},
        )
        assert duplicate.status_code == 409

        allowed = client.post(
            f"/v1/sessions/{root}/interactions/permission:perm_child/respond",
            json={"action": "allow"},
        )
        assert allowed.status_code == 200
        assert app.state.permissions["perm_child"]["status"] == "resolved"

        cancellable = UserQuestion(
            id="q_cancel",
            session_id=root,
            prompt="Cancel me",
            created_at=now,
            updated_at=now,
        )
        app.state.user_questions[cancellable.id] = cancellable
        cancelled = client.post(
            f"/v1/sessions/{root}/interactions/question:q_cancel/respond",
            json={"action": "cancel"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["interaction"]["status"] == "cancelled"

        # The day-one ``/response`` alias is gone: ``/respond`` is the one door.
        retired = client.post(
            f"/v1/sessions/{root}/interactions/question:q_cancel/response",
            json={"action": "answer", "answer": "kept"},
        )
        assert retired.status_code == 404

        expired = UserQuestion(
            id="q_expired",
            session_id=root,
            prompt="Too late",
            status="expired",
            created_at=now,
            updated_at=now,
        )
        app.state.user_questions[expired.id] = expired
        late = client.post(
            f"/v1/sessions/{root}/interactions/question:q_expired/respond",
            json={"action": "answer", "answer": "late"},
        )
        assert late.status_code == 409


def test_pending_native_question_survives_backend_restart(tmp_path) -> None:
    sessions_path = tmp_path / "sessions.json"
    first_app = build_app(sessions_path=sessions_path)
    session = first_app.state.sessions.create(workspace_id="ws_default", title="restart")
    now = datetime.now(timezone.utc)
    question_id = "ques_restart"
    first_app.state.sessions.update(
        session.id,
        status="waiting_user",
        metadata_patch={
            "pending_user_question_id": question_id,
            "pending_ask_user": {
                "action": "ask_user",
                "question": "Which physical system should I simulate?",
                "kind": "freeform",
                "choices": [],
                "allow_freeform": True,
                "reason": "The study needs a physical system.",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
                "owner_session_id": session.id,
                "attended_session_id": session.id,
                "task_id": "",
                "invocation_id": "turn_before_restart:main:ask_user",
                "caller": {"agent_id": "main"},
                "surfaced": True,
                "question_id": question_id,
            },
        },
    )

    restarted_app = build_app(sessions_path=sessions_path)
    routed: list[tuple[str, str]] = []
    original_answer = restarted_app.state.answer_user_question

    async def record_answer(
        session_id: str, restored_question_id: str, request: object
    ) -> UserQuestion:
        routed.append((session_id, restored_question_id))
        return await original_answer(session_id, restored_question_id, request)

    restarted_app.state.answer_user_question = record_answer

    with TestClient(restarted_app) as client:
        interactions = client.get(
            f"/v1/sessions/{session.id}/interactions",
            headers=HEADERS,
        )
        assert interactions.status_code == 200
        rows = interactions.json()["interactions"]
        assert [row["id"] for row in rows] == [f"question:{question_id}"]
        assert rows[0]["prompt"] == "Which physical system should I simulate?"
        assert rows[0]["source"]["invocation_id"] == "turn_before_restart:main:ask_user"

        answered = client.post(
            f"/v1/sessions/{session.id}/interactions/question:{question_id}/respond",
            headers=HEADERS,
            json={"action": "answer", "answer": "A cantilever beam."},
        )
        assert answered.status_code == 200

    assert routed == [(session.id, question_id)]
    assert restarted_app.state.user_questions[question_id].status == "answered"
    assert restarted_app.state.sessions.get(session.id).status == "idle"
    restarted = restarted_app.state.sessions.get(session.id)
    assert restarted.metadata["pending_user_question_id"] == ""
    assert restarted.metadata["pending_ask_user"]["resolved_status"] == "answered"
    assert restarted_app.state.loop_inboxes[session.id].peek_nonempty()


def test_agent_ready_promotes_inputs_deferred_during_initialization(tmp_path, monkeypatch) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="deferred")
    app.state.loop_inboxes[session.id] = object()
    app.state.mcp_app_loop = None
    drained: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.loop_inbox.drain_inbox_to_new_turn",
        lambda _app, session_id: drained.append(session_id),
    )
    agent = object()

    mark_agent_ready(app, agent)

    assert app.state.agent is agent
    assert drained == [session.id]


def test_legacy_forwarded_question_hydrates_owner_and_attended_ids() -> None:
    row = UserQuestion.model_validate(
        {
            "id": "legacy",
            "session_id": "root",
            "prompt": "Legacy forwarded question",
            "created_at": "2026-09-02T10:00:00+00:00",
            "updated_at": "2026-09-02T10:00:00+00:00",
            "metadata": {"forwarded_from_session": "child"},
        }
    )

    assert row.owner_session_id == "child"
    assert row.attended_session_id == "root"


def test_native_question_deadline_atomically_expires_and_releases_root(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    now = datetime.now(timezone.utc)
    row = UserQuestion(
        id="q_deadline",
        session_id=root.id,
        owner_session_id=root.id,
        attended_session_id=root.id,
        prompt="Timed question",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    app.state.user_questions[row.id] = row
    app.state.sessions.update(
        root.id,
        status="waiting_user",
        metadata_patch={"pending_user_question_id": row.id},
    )

    arm_ask_user_deadline(app, row)
    deadline = time.monotonic() + 2.0
    while app.state.user_questions[row.id].status == "pending" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert app.state.user_questions[row.id].status == "expired"
    assert app.state.sessions.get(root.id).status == "idle"
    assert app.state.sessions.get(root.id).metadata["pending_user_question_id"] == ""


def test_mcp_task_request_id_correlates_hyphenated_task_exactly(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root, child = _root_and_child(app)
    del root
    resolve_store(None).put(
        TaskRecord(
            key=TaskKey(server_id="srv", session_id=child, task_id="abc-def"),
            tool="earthscope_query",
            status="input_required",
        )
    )
    invocation = MCPInvocationContext(
        invocation_id="call_7",
        session_id=child,
        namespace="earthscope",
        tool_name="earthscope_query",
    )

    correlated = invocation_with_request_correlation(
        invocation,
        SimpleNamespace(request_id="task-abc-def-output_format"),
    )

    assert correlated.task_id == "abc-def"
    assert correlated.input_key == "output_format"


def test_child_a2ui_interaction_routes_to_owning_surface(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root, child = _root_and_child(app)
    create = {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": "child-form", "catalogId": CLIO_A2UI_CATALOG_ID},
    }
    components = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "child-form",
            "components": [
                {"id": "label", "component": "Text", "text": "Submit"},
                {
                    "id": "submit",
                    "component": "Button",
                    "child": "label",
                    "action": {"event": {"name": "form.submit", "context": {"value": "x"}}},
                },
            ],
        },
    }
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "form.submit",
            "surfaceId": "child-form",
            "sourceComponentId": "submit",
            "timestamp": "2026-09-02T10:00:00Z",
            "context": {"value": "x"},
        },
    }
    with TestClient(app) as client:
        produced = client.post(
            f"/v1/sessions/{child}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [create, components]},
        )
        assert produced.status_code == 200
        listed = client.get(
            f"/v1/sessions/{root}/interactions", params={"include_children": True}
        ).json()["interactions"]
        row = next(item for item in listed if item["kind"] == "a2ui")
        assert row["owner_session_id"] == child
        assert row["source"]["surface_id"] == "child-form"
        mismatched = {
            **action,
            "action": {**action["action"], "surfaceId": "different-surface"},
        }
        rejected = client.post(
            f"/v1/sessions/{root}/interactions/{row['id']}/respond",
            headers=HEADERS,
            json={"message": mismatched},
        )
        assert rejected.status_code == 422
        # The a2ui branch of the interaction responder enforces the SAME
        # negotiation the canonical /a2ui/actions route does, so the headers are
        # not decoration here.
        responded = client.post(
            f"/v1/sessions/{root}/interactions/{row['id']}/respond",
            headers=HEADERS,
            json={"message": action},
        )
        assert responded.status_code == 200
        assert responded.json()["result"]["submitted"] == {"value": "x"}


def test_capabilities_advertise_normalized_interactions(tmp_path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert client.get("/v1/capabilities").json()["capabilities"]["x_clio_interactions"] is True


def test_agent_routed_questions_project_without_human_actions_and_keep_resolved_history(
    tmp_path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="routing")
    now = datetime.now(timezone.utc).isoformat()
    fields = [
        {
            "name": "count",
            "type": "integer",
            "title": "Sample count",
            "description": "How many samples should be retained?",
            "required": True,
            "default": 3,
            "min_length": None,
            "max_length": None,
            "min_items": None,
            "max_items": None,
            "multi": False,
        }
    ]
    app.state.user_questions = {
        "agent_pending": UserQuestion(
            id="agent_pending",
            session_id=session.id,
            prompt="Choose the sample count",
            source="mcp_elicitation",
            created_at=now,
            updated_at=now,
            answer_metadata={"count": 3},
            metadata={
                "elicitation": {
                    "mode": "form",
                    "fields": fields,
                    "additional_properties": False,
                    "invocation_id": "call_pending",
                },
                "agent_answer_task": {
                    "task_id": "task_answer",
                    "child_session_id": "sess_answer",
                },
            },
            audience="agent",
            agent_elicitation_routing="elicitation_routed_to_agent",
        ),
        "human_fallback": UserQuestion(
            id="human_fallback",
            session_id=session.id,
            prompt="Choose the sample count",
            source="mcp_elicitation",
            created_at=now,
            updated_at=now,
            metadata={"elicitation": {"mode": "form", "fields": fields}},
            audience="agent",
            agent_elicitation_routing="agent_elicitation_fallback_to_human",
            agent_elicitation_fallback_detail="agent_answer_timeout",
        ),
        "agent_answered": UserQuestion(
            id="agent_answered",
            session_id=session.id,
            prompt="Choose the output format",
            status="answered",
            source="mcp_elicitation",
            created_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            updated_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
            metadata={"elicitation": {"invocation_id": "call_pending"}},
            audience="agent",
            agent_elicitation_routing="elicitation_routed_to_agent",
            answered_by="agent",
        ),
    }
    app.state.agent_task_registry.register(
        AgentTask(
            task_id="task_answer",
            parent_session_id=session.id,
            child_session_id="sess_answer",
            run_label="agent-elicitation answer",
            project_to_parent=False,
            status="completed",
            created_at=now,
            updated_at=now,
        )
    )

    with TestClient(app) as client:
        response = client.get(
            f"/v1/sessions/{session.id}/interactions",
            params={"include_recent_resolved": True, "resolved_limit": 10},
            headers=HEADERS,
        )
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["interactions"]}

        answering = rows["question:agent_pending"]
        assert answering["requires_human_response"] is False
        assert answering["actions"] == []
        assert answering["routing_state"] == "elicitation_routed_to_agent"
        assert answering["payload"]["fields"] == fields
        assert answering["payload"]["answer_metadata"] == {"count": 3}
        assert answering["payload"]["agent_answer_task"] == {
            "task_id": "task_answer",
            "child_session_id": "sess_answer",
            "status": "completed",
            "live_state": "completed",
            "created_at": now,
            "updated_at": now,
            "run_label": "agent-elicitation answer",
        }
        assert answering["payload"]["request_index"] == 1
        assert answering["payload"]["request_count"] == 2

        fallback = rows["question:human_fallback"]
        assert fallback["requires_human_response"] is True
        assert fallback["actions"] == ["answer", "cancel"]
        assert fallback["fallback_detail"] == "agent_answer_timeout"

        answered = rows["question:agent_answered"]
        assert answered["status"] == "answered"
        assert answered["answered_by"] == "agent"
        assert answered["requires_human_response"] is False
        assert answered["payload"]["request_index"] == 2
        assert answered["payload"]["request_count"] == 2

        rejected = client.post(
            f"/v1/sessions/{session.id}/interactions/question:agent_pending/respond",
            json={"action": "answer", "metadata": {"count": 4}},
            headers=HEADERS,
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["error"] == "interaction_not_human_addressed"


def test_resolved_agent_question_survives_process_restart(tmp_path) -> None:
    sessions_path = tmp_path / "sessions.json"
    first = build_app(sessions_path=sessions_path)
    session = first.state.sessions.create(workspace_id="ws_default", title="restart")
    now = datetime.now(timezone.utc).isoformat()
    record_user_question(
        first,
        UserQuestion(
            id="agent_restart",
            session_id=session.id,
            prompt="What nonce did the user provide?",
            source="mcp_elicitation",
            created_at=now,
            updated_at=now,
            metadata={
                "elicitation": {
                    "invocation_id": "call_restart",
                    "tool_name": "v2ex_agent_guarded_input",
                }
            },
            audience="agent",
            agent_elicitation_routing="elicitation_routed_to_agent",
        ),
    )
    answered = claim_question_transition(
        first,
        "agent_restart",
        "answered",
        answer_metadata={"nonce": "restart-visible"},
        answered_by="agent",
    )
    assert answered is not None

    restarted = build_app(sessions_path=sessions_path)
    with TestClient(restarted) as client:
        response = client.get(
            f"/v1/sessions/{session.id}/interactions",
            params={"include_recent_resolved": True},
            headers=HEADERS,
        )

    assert response.status_code == 200
    [row] = response.json()["interactions"]
    assert row["id"] == "question:agent_restart"
    assert row["status"] == "answered"
    assert row["answered_by"] == "agent"
    assert row["source"] == {
        "protocol": "mcp",
        "tool_name": "v2ex_agent_guarded_input",
        "invocation_id": "call_restart",
    }
    assert row["payload"]["answer_metadata"] == {"nonce": "restart-visible"}


def test_structured_form_422_keeps_prefill_and_question_pending(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="form")
    now = datetime.now(timezone.utc).isoformat()
    question = UserQuestion(
        id="form_required",
        session_id=session.id,
        prompt="Configure the guarded input",
        source="mcp_elicitation",
        created_at=now,
        updated_at=now,
        answer_metadata={"count": 3},
        metadata={
            "elicitation": {
                "mode": "form",
                "fields": [
                    {
                        "name": "count",
                        "type": "integer",
                        "title": "Sample count",
                        "required": True,
                        "default": 3,
                    },
                    {
                        "name": "label",
                        "type": "string",
                        "title": "Label",
                        "required": True,
                        "min_length": 2,
                        "max_length": 8,
                    },
                ],
                "additional_properties": False,
            }
        },
    )
    app.state.user_questions[question.id] = question

    with TestClient(app) as client:
        invalid = client.post(
            f"/v1/sessions/{session.id}/interactions/question:{question.id}/respond",
            json={"action": "answer", "metadata": {"count": 3, "label": "x"}},
            headers=HEADERS,
        )
        assert invalid.status_code == 422
        retained = app.state.user_questions[question.id]
        assert retained.status == "pending"
        assert retained.answer_metadata == {"count": 3}
