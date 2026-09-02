"""Runtime-tool and normalized interaction contract tests."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import dspy
from dspy.utils.dummies import DummyLM
from fastapi.testclient import TestClient

from clio_agent.gact import context as gact_context
from clio_agent.gact.agent_initialization import mark_agent_ready
from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.agents.builders import _dynamic_agent_tools
from clio_agent.gact.agents.reactv2 import retaining_reactv2_cls
from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import arm_ask_user_deadline
from clio_agent.gact.elicitation_bridge import invocation_with_request_correlation
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import AgentDef, UserQuestion, UserQuestionOption
from clio_agent.tools.mcp_handlers import MCPInvocationContext
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, resolve_store

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


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

        compatibility = UserQuestion(
            id="q_compatibility",
            session_id=root,
            prompt="Answer through the compatibility route",
            created_at=now,
            updated_at=now,
        )
        app.state.user_questions[compatibility.id] = compatibility
        compatible = client.post(
            f"/v1/sessions/{root}/interactions/question:q_compatibility/response",
            json={"action": "answer", "answer": "kept"},
        )
        assert compatible.status_code == 200

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
            json={"message": mismatched},
        )
        assert rejected.status_code == 422
        responded = client.post(
            f"/v1/sessions/{root}/interactions/{row['id']}/respond",
            json={"message": action},
        )
        assert responded.status_code == 200
        assert responded.json()["result"]["submitted"] == {"value": "x"}


def test_capabilities_advertise_normalized_interactions(tmp_path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert client.get("/v1/capabilities").json()["capabilities"]["x_clio_interactions"] is True
