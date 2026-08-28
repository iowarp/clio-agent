from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import Message, Part


def _message(message_id: str, sid: str, text: str) -> Message:
    return Message(
        id=message_id,
        session_id=sid,
        role="assistant",
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:00:00+00:00",
        parts=[Part(id=f"part_{message_id}", type="text", text=text)],
    )


def _seed_messages(client: TestClient, sid: str, message_ids: list[str]) -> None:
    messages = [_message(message_id, sid, message_id) for message_id in message_ids]
    client.app.state.messages[sid] = messages
    client.app.state.sessions.update(sid, message_count=len(messages))


def test_undo_removes_last_messages_and_audits_destructive_action(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rollback"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2", "msg_3"])

        resp = client.post(f"/v1/sessions/{sid}/undo", json={"count": 2})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_message_ids"] == ["msg_2", "msg_3"]
        assert body["deleted_messages"] == ["msg_2", "msg_3"]
        assert body["reverted_message_ids"] == ["msg_2", "msg_3"]
        assert body["message_count"] == 1
        assert [m.id for m in app.state.messages[sid]] == ["msg_1"]
        sess = app.state.sessions.get(sid)
        assert sess.message_count == 1
        assert sess.metadata["last_rollback"]["memory_scope"] == "gact_visible_transcript_only"
        permission = next(iter(app.state.permissions.values()))
        assert permission["tool_call"]["tool_name"] == "gact.session.undo"
        assert permission["reason"] == "user_requested_session_undo"
        history = app.state.bus._history[sid]
        assert [event.type for event in history[-4:]] == [
            "message.deleted",
            "message.deleted",
            "session.undo",
            "session.updated",
        ]


def test_undo_preserves_ready_a2ui_surface_from_removed_message(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rollback surface"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2"])
        app.state.a2ui_store.apply_batch(
            sid,
            [
                {
                    "version": "v0.9.1",
                    "createSurface": {
                        "surfaceId": "analysis_surface",
                        "catalogId": CLIO_A2UI_CATALOG_ID,
                    },
                },
                {
                    "version": "v0.9.1",
                    "updateComponents": {
                        "surfaceId": "analysis_surface",
                        "components": [
                            {
                                "id": "status",
                                "component": "clio.status.v1",
                                "label": "Analysis",
                                "state": "completed",
                            }
                        ],
                    },
                },
            ],
        )

        response = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})

        assert response.status_code == 200, response.text
        surface = app.state.a2ui_store.get(sid, "analysis_surface")
        assert surface is not None
        assert surface.state == "ready"
        assert app.state.a2ui_store.projection_degradations(sid) == []
        assert app.state.messages[sid][-1].metadata == {
            "synthetic": "a2ui_preservation",
            "preserved_by": "undo",
        }
        assert app.state.messages[sid][-1].parts[0].type == "a2ui"


def test_rewind_removes_messages_after_target_by_default(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2", "msg_3", "msg_4"])

        resp = client.post(f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_2"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_message_ids"] == ["msg_3", "msg_4"]
        assert [m.id for m in app.state.messages[sid]] == ["msg_1", "msg_2"]
        permission = next(iter(app.state.permissions.values()))
        assert permission["tool_call"]["tool_name"] == "gact.session.rewind"


def test_rewind_accepts_gact_to_message_id_alias(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2", "msg_3"])

        resp = client.post(f"/v1/sessions/{sid}/rewind", json={"to_message_id": "msg_1"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_message_ids"] == ["msg_2", "msg_3"]
        assert body["deleted_messages"] == ["msg_2", "msg_3"]
        assert [m.id for m in app.state.messages[sid]] == ["msg_1"]


def test_rewind_can_include_target_message(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2", "msg_3"])

        resp = client.post(
            f"/v1/sessions/{sid}/rewind",
            json={"message_id": "msg_2", "include_target": True},
        )

        assert resp.status_code == 200
        assert resp.json()["deleted_message_ids"] == ["msg_2", "msg_3"]
        assert [m.id for m in app.state.messages[sid]] == ["msg_1"]


def test_rewind_unknown_target_returns_not_found_without_mutation(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2"])

        resp = client.post(f"/v1/sessions/{sid}/rewind", json={"message_id": "msg_missing"})

        assert resp.status_code == 404
        assert [m.id for m in app.state.messages[sid]] == ["msg_1", "msg_2"]
        assert app.state.permissions == {}


def test_rollback_permission_deny_blocks_mutation(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rollback"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2"])
        client.put(
            "/v1/policies",
            json={
                "policies": [
                    {
                        "scope": "session",
                        "scope_id": sid,
                        "tool_name_pattern": "gact.session.undo",
                        "action": "deny",
                    }
                ]
            },
        )

        resp = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})

        assert resp.status_code == 403
        assert [m.id for m in app.state.messages[sid]] == ["msg_1", "msg_2"]
        permission = next(iter(app.state.permissions.values()))
        assert permission["status"] == "auto_denied"
        assert permission["tool_call"]["tool_name"] == "gact.session.undo"


def test_rollback_rejects_running_session(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rollback"}).json()["id"]
        _seed_messages(client, sid, ["msg_1", "msg_2"])
        app.state.sessions.update(sid, status="running")

        resp = client.post(f"/v1/sessions/{sid}/undo", json={"count": 1})

        assert resp.status_code == 409
        assert [m.id for m in app.state.messages[sid]] == ["msg_1", "msg_2"]
