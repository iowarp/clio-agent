"""Negotiated GACT 0.3 projection and event contract tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent import __version__ as clio_agent_version
from clio_agent.gact.agent_tasks import seed_agent_task
from clio_agent.gact.app import build_app
from clio_agent.gact.events import Event
from clio_agent.gact.parts import Part
from clio_agent.gact.protocol_v3 import event_to_v3, part_to_v3_block
from clio_agent.gact.types import Message, Tokens

V3_HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def test_capabilities_negotiate_v3_without_changing_v2(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        legacy = client.get("/v1/capabilities")
        v3 = client.get("/v1/capabilities", headers=V3_HEADERS)

    assert legacy.status_code == 200
    assert legacy.json()["contract_version"] == "0.2"
    assert v3.status_code == 200
    assert v3.json()["service"] == {
        "name": "clio-agent",
        "version": clio_agent_version,
    }
    assert v3.json()["gact_versions"] == ["0.3", "0.2"]
    assert v3.json()["a2ui_versions"] == ["0.9.1"]
    assert v3.json()["replay"] == {"supported": True, "retention": 256}
    assert v3.json()["model_catalog"]["source"] == "unavailable"
    assert v3.json()["model_catalog"]["reason"]


def test_capabilities_report_the_effective_active_model(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    app.state.lm_config = {
        "provider": "codex",
        "model": "gpt-5.6-luna",
        "thinking_level": "medium",
    }

    response = TestClient(app).get("/v1/capabilities", headers=V3_HEADERS)

    assert response.status_code == 200
    assert response.json()["active_model"] == {
        "provider_id": "codex",
        "model_id": "gpt-5.6-luna",
        "effort": "medium",
    }
    assert response.json()["model_catalog"]["source"] == "provider"
    assert response.json()["model_catalog"]["stale"] is False


def test_unknown_protocol_is_rejected(tmp_path: Path) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    response = client.get("/v1/capabilities", headers={"X-GACT-Version": "9.9"})

    assert response.status_code == 406
    assert response.json()["error"]["error"] == "unsupported_protocol"


def test_workspace_v3_uses_short_server_label_and_keeps_path_secondary(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace = app.state.workspaces.create(
        name=r"D:\science\flat-ndp",
        root_path=r"D:\science\flat-ndp",
    )
    client = TestClient(app)

    response = client.get("/v1/workspaces", headers=V3_HEADERS)

    assert response.status_code == 200
    row = next(item for item in response.json()["workspaces"] if item["id"] == workspace.id)
    assert row["display_name"] == "flat-ndp"
    assert row["path"] == r"D:\science\flat-ndp"
    assert row["display_name"] != row["path"]


def test_v3_lifecycle_mutations_return_canonical_workspace_and_session_rows(
    tmp_path: Path,
) -> None:
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    workspace_response = client.post(
        "/v1/workspaces",
        headers=V3_HEADERS,
        json={
            "name": "flat-NDP",
            "root_path": str(tmp_path / "flat-NDP"),
            "metadata": {"pinned": True},
        },
    )
    assert workspace_response.status_code == 201
    workspace = workspace_response.json()
    assert workspace["display_name"] == "flat-NDP"
    assert workspace["path"] == str(tmp_path / "flat-NDP")
    assert workspace["pinned"] is True
    assert "root_path" not in workspace

    renamed_workspace = client.patch(
        f"/v1/workspaces/{workspace['id']}",
        headers=V3_HEADERS,
        json={"name": "NDP review", "metadata": {"pinned": False}},
    ).json()
    assert renamed_workspace["display_name"] == "NDP review"
    assert renamed_workspace["pinned"] is False

    session_response = client.post(
        "/v1/sessions",
        headers=V3_HEADERS,
        json={
            "workspace_id": workspace["id"],
            "title": "Evidence review",
            "metadata": {"pinned": True},
        },
    )
    assert session_response.status_code == 201
    session = session_response.json()
    assert session["title"] == "Evidence review"
    assert session["pinned"] is True
    assert session["archived"] is False
    assert session["last_interaction_at"] == session["created_at"]
    assert "status" not in session

    updated_session = client.patch(
        f"/v1/sessions/{session['id']}",
        headers=V3_HEADERS,
        json={"title": "Reviewed evidence", "archived": True},
    ).json()
    assert updated_session["title"] == "Reviewed evidence"
    assert updated_session["pinned"] is True
    assert updated_session["archived"] is True
    assert updated_session["last_interaction_at"] == session["last_interaction_at"]


def test_v3_session_and_transcript_are_normalized(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Working SPOTTER")
    message = Message(
        id="msg_1",
        session_id=session.id,
        turn_id="run_1",
        role="assistant",
        created_at="2026-08-22T00:00:00+00:00",
        updated_at="2026-08-22T00:00:01+00:00",
        parts=[
            Part(
                id="part_reasoning",
                type="thinking",
                text="Comparing the observed campaigns before choosing an action.",
                agent_id="main",
                sequence=1,
                metadata={
                    "stream_source": "live",
                    "signature_field_name": "provider_thinking:codex_sdk_reasoning",
                    "thinking_source": "provider",
                    "provider_source": "codex_sdk_reasoning",
                    "default_collapsed": True,
                },
            ),
            Part(
                id="part_text",
                type="text",
                text="Watching campaign evidence.",
                agent_id="spotter",
                sequence=2,
                metadata={
                    "stream_source": "live",
                    "signature_field_name": "next_thought",
                },
            ),
            Part(
                id="part_call",
                type="tool_call",
                call_id="call_1",
                tool_name="spotter_campaign_health",
                tool_title="Check campaign health",
                input={"campaign": "phenotype-2026"},
                agent_id="spotter",
                sequence=3,
            ),
            Part(
                id="part_result",
                type="tool_result",
                call_id="call_1",
                tool_name="spotter_campaign_health",
                structured_content={"runs_checked": 4, "anomalous": []},
                agent_id="spotter",
                sequence=4,
            ),
        ],
        tokens=Tokens(input=120, output=45, cache_read=30),
        cost_usd=0.0125,
        stop_reason="end_turn",
        metadata={
            "reasoning_log": [
                {
                    "model": "openai/gpt-5.6-luna",
                    "question": "Inspect campaign state.",
                    "reasoning": "Full model-call reasoning retained by the provider bridge.",
                    "response": "Watching campaign evidence.",
                    "reasoning_chars": 58,
                    "timestamp": "2026-08-22T00:00:00.500000+00:00",
                }
            ]
        },
    )
    app.state.messages[session.id] = [message]
    client = TestClient(app)

    sessions = client.get(
        f"/v1/sessions?workspace_id={session.workspace_id}", headers=V3_HEADERS
    ).json()["sessions"]
    transcript = client.get(f"/v1/sessions/{session.id}/messages", headers=V3_HEADERS).json()

    session_row = next(row for row in sessions if row["id"] == session.id)
    assert transcript["cursor"].isdigit()
    assert session_row["state"] == "completed"
    assert session_row["agent_id"] == "main"
    projected = transcript["messages"][0]
    assert [block["type"] for block in projected["blocks"]] == ["reasoning", "text", "tool"]
    assert projected["blocks"][0] == {
        "id": "part_reasoning",
        "type": "reasoning",
        "text": "Comparing the observed campaigns before choosing an action.",
        "source": "provider",
        "provider_source": "codex_sdk_reasoning",
        "default_collapsed": True,
        "agent_id": "main",
        "sequence": 1,
        "stream_source": "live",
        "channel": "provider_thinking:codex_sdk_reasoning",
    }
    assert projected["blocks"][1]["channel"] == "next_thought"
    assert projected["blocks"][1]["agent_id"] == "spotter"
    assert projected["usage"] == {
        "input": 120,
        "output": 45,
        "cache_read": 30,
        "cache_write": 0,
    }
    assert projected["cost_usd"] == 0.0125
    assert projected["stop_reason"] == "end_turn"
    assert projected["reasoning_calls"] == [
        {
            "id": "reasoning_call_1",
            "model": "openai/gpt-5.6-luna",
            "question": "Inspect campaign state.",
            "reasoning": "Full model-call reasoning retained by the provider bridge.",
            "response": "Watching campaign evidence.",
            "reasoning_chars": 58,
            "timestamp": "2026-08-22T00:00:00.500000+00:00",
        }
    ]
    assert transcript["tools"] == [
        {
            "id": "call_1",
            "session_id": session.id,
            "run_id": "run_1",
            "name": "spotter_campaign_health",
            "title": "Check campaign health",
            "state": "succeeded",
            "input": {"campaign": "phenotype-2026"},
            "output": {"runs_checked": 4, "anomalous": []},
            "duration_ms": None,
        }
    ]


def test_v3_preserves_rowless_tool_thought_fallback() -> None:
    block = part_to_v3_block(
        Part(
            id="part_call",
            type="tool_call",
            call_id="call_1",
            tool_name="geo_geocode",
            thought="Resolve the place name before searching the station catalog.",
            input={"query": "Chicago"},
            agent_id="geospatial",
            sequence=5,
        ).to_wire()
    )

    assert block == {
        "id": "part_call",
        "type": "tool",
        "tool_id": "call_1",
        "thought": "Resolve the place name before searching the station catalog.",
        "agent_id": "geospatial",
        "sequence": 5,
    }


def test_v3_transcript_preserves_navigable_child_agent_semantics(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    parent = app.state.sessions.create(workspace_id="ws_default", title="Flat NDP")
    child = seed_agent_task(
        app,
        parent_session_id=parent.id,
        parent_turn_id="run_1",
        agent_ref={"expert_id": "geospatial"},
        task_id="task_geo",
        status="completed",
        run_label="Resolve station region",
    )
    app.state.messages[parent.id] = [
        Message(
            id="msg_subagent",
            session_id=parent.id,
            turn_id="run_1",
            role="assistant",
            created_at="2026-08-22T00:00:00+00:00",
            updated_at="2026-08-22T00:00:01+00:00",
            parts=[
                Part(
                    id="handoff_geo",
                    type="expert_handoff",
                    handle_id="task_geo",
                    child_agent="geospatial",
                    run_label="Resolve station region",
                    live_state="completed",
                    status="completed",
                    duration_ms=12_500.0,
                    text="main <- geospatial",
                    metadata={
                        "question": "Ground the requested region before catalog search.",
                        "output": "Resolved the region with authoritative coordinates.",
                    },
                )
            ],
        )
    ]

    transcript = (
        TestClient(app).get(f"/v1/sessions/{parent.id}/messages", headers=V3_HEADERS).json()
    )

    assert transcript["subagents"] == [
        {
            "id": "task_geo",
            "session_id": parent.id,
            "parent_run_id": "run_1",
            "child_session_id": child.child_session_id,
            "agent_id": "geospatial",
            "title": "Resolve station region",
            "state": "completed",
            "summary": "main <- geospatial",
            "task": "Ground the requested region before catalog search.",
            "result": "Resolved the region with authoritative coordinates.",
            "duration_ms": 12_500.0,
        }
    ]


def test_v3_action_card_preserves_labels_and_safe_client_behavior(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Working SPOTTER")
    message = Message(
        id="msg_action",
        session_id=session.id,
        role="assistant",
        created_at="2026-08-22T00:00:00+00:00",
        updated_at="2026-08-22T00:00:01+00:00",
        parts=[
            Part(
                id="card_1",
                type="action_card",
                source="spotter-ai",
                severity="critical",
                status="active",
                title="Campaign quarantined",
                body="One run needs review.",
                actions=[
                    {
                        "id": "discuss",
                        "label": "Discuss",
                        "enabled": True,
                        "behavior": {"kind": "focus_session", "handle_id": "task_child"},
                    },
                    {
                        "id": "address",
                        "label": "Address",
                        "enabled": False,
                        "behavior": {"kind": "stub", "reason": "Not available yet"},
                    },
                ],
            )
        ],
    )
    app.state.messages[session.id] = [message]

    transcript = (
        TestClient(app).get(f"/v1/sessions/{session.id}/messages", headers=V3_HEADERS).json()
    )

    card = transcript["messages"][0]["blocks"][0]
    assert card == {
        "id": "card_1",
        "type": "action_card",
        "title": "Campaign quarantined",
        "detail": "One run needs review.",
        "source": "spotter-ai",
        "severity": "critical",
        "status": "active",
        "actions": [
            {
                "id": "discuss",
                "label": "Discuss",
                "enabled": True,
                "behavior": {"kind": "focus_session", "handle_id": "task_child"},
            },
            {
                "id": "address",
                "label": "Address",
                "enabled": False,
                "behavior": {"kind": "stub", "reason": "Not available yet"},
            },
        ],
    }


def test_v3_artifact_preserves_registry_identity_and_retrieval_metadata(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="Artifact review")
    app.state.messages[session.id] = [
        Message(
            id="msg_artifact",
            session_id=session.id,
            role="assistant",
            created_at="2026-08-22T00:00:00+00:00",
            updated_at="2026-08-22T00:00:01+00:00",
            parts=[
                Part(
                    id="part_artifact",
                    type="resource_link",
                    uri="artifact://ws_default/result.py@v1",
                    name="result.py",
                    mime_type="text/x-python",
                    metadata={
                        "artifact_id": "artifact_1",
                        "workspace_id": "ws_default",
                        "fetch_url": "/v1/artifacts/artifact_1/bytes",
                        "custody": "cas",
                        "sha256": "abc123",
                        "size_bytes": 42,
                    },
                )
            ],
        )
    ]

    transcript = (
        TestClient(app).get(f"/v1/sessions/{session.id}/messages", headers=V3_HEADERS).json()
    )

    assert transcript["messages"][0]["blocks"][0]["artifact_id"] == "artifact_1"
    assert transcript["artifacts"] == [
        {
            "id": "artifact_1",
            "session_id": session.id,
            "workspace_id": "ws_default",
            "name": "result.py",
            "media_type": "text/x-python",
            "uri": "artifact://ws_default/result.py@v1",
            "fetch_path": "/v1/artifacts/artifact_1/bytes",
            "custody": "cas",
            "sha256": "abc123",
            "size": 42,
            "created_at": "2026-08-22T00:00:00+00:00",
        }
    ]


def test_event_projection_scopes_session_and_normalizes_delta() -> None:
    event = Event(
        type="message.part.delta",
        session_id="sess_1",
        payload={
            "message_id": "msg_1",
            "part_id": "part_1",
            "turn_id": "run_1",
            "delta": {"text_append": "next"},
        },
    )

    envelope = event_to_v3(event, workspace_id="ws_1")

    assert envelope["protocol_version"] == "0.3"
    assert envelope["type"] == "message.block.delta"
    assert envelope["scope"] == {
        "connection_id": "local",
        "workspace_id": "ws_1",
        "session_id": "sess_1",
    }
    assert envelope["payload"] == {
        "message_id": "msg_1",
        "block_id": "part_1",
        "delta": "next",
    }


def test_connection_event_does_not_inherit_focused_session_scope() -> None:
    event = Event(type="lm.provider.changed", session_id="", payload={"provider": "codex"})

    envelope = event_to_v3(event, workspace_id="ws_focused")

    assert envelope["scope"] == {"connection_id": "local"}


def test_v3_normalizes_question_permission_and_child_agent_events() -> None:
    question = event_to_v3(
        Event(
            type="user_question.created",
            session_id="sess_parent",
            payload={
                "id": "ques_1",
                "session_id": "sess_parent",
                "prompt": "Continue?",
                "status": "pending",
                "kind": "confirmation",
                "options": [],
                "created_at": "2026-08-22T00:00:00+00:00",
                "updated_at": "2026-08-22T00:00:00+00:00",
            },
        )
    )
    permission = event_to_v3(
        Event(
            type="permission.requested",
            session_id="sess_parent",
            payload={
                "id": "perm_1",
                "session_id": "sess_parent",
                "tool_call": {"tool_name": "shell.exec", "input": {"cmd": "inspect"}},
                "summary": "Run a protected command",
                "created_at": "2026-08-22T00:00:00+00:00",
            },
        )
    )
    child = event_to_v3(
        Event(
            type="agent.task.started",
            session_id="sess_parent",
            payload={
                "task_id": "task_1",
                "parent_session_id": "sess_parent",
                "parent_turn_id": "turn_1",
                "child_session_id": "sess_child",
                "agent_ref": {"expert_id": "data_expert"},
                "run_index": 1,
                "status": "running",
                "live_state": "working",
            },
        )
    )

    assert question["type"] == "question.upserted"
    assert question["entity_id"] == "ques_1"
    assert permission["type"] == "approval.upserted"
    assert permission["payload"]["tool_name"] == "shell.exec"
    assert permission["payload"]["input"] == {"cmd": "inspect"}
    assert child["type"] == "subagent.upserted"
    assert child["payload"] == {
        "id": "task_1",
        "session_id": "sess_parent",
        "parent_run_id": "turn_1",
        "title": "data_expert #2",
        "state": "running",
        "agent_id": "data_expert",
        "child_session_id": "sess_child",
    }
