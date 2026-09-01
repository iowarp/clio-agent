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


def test_session_scoped_delete_removes_only_target_session_message(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid_1 = client.post("/v1/sessions", json={"title": "one"}).json()["id"]
        sid_2 = client.post("/v1/sessions", json={"title": "two"}).json()["id"]
        app.state.messages[sid_1] = [_message("msg_1", sid_1, "one")]
        app.state.messages[sid_2] = [_message("msg_2", sid_2, "two")]
        app.state.sessions.update(sid_1, message_count=1)
        app.state.sessions.update(sid_2, message_count=1)

        resp = client.delete(f"/v1/sessions/{sid_1}/messages/msg_1")

        assert resp.status_code == 204
        assert app.state.messages[sid_1] == []
        assert [m.id for m in app.state.messages[sid_2]] == ["msg_2"]
        assert app.state.sessions.get(sid_1).message_count == 0
        assert app.state.sessions.get(sid_2).message_count == 1
        history = app.state.bus._history.get(sid_1, [])
        assert history[-1].type == "message.deleted"
        assert history[-1].payload == {"message_id": "msg_1", "session_id": sid_1}


def test_session_scoped_delete_does_not_scan_other_sessions(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid_1 = client.post("/v1/sessions", json={"title": "one"}).json()["id"]
        sid_2 = client.post("/v1/sessions", json={"title": "two"}).json()["id"]
        app.state.messages[sid_2] = [_message("msg_2", sid_2, "two")]
        app.state.sessions.update(sid_2, message_count=1)

        resp = client.delete(f"/v1/sessions/{sid_1}/messages/msg_2")

        assert resp.status_code == 404
        detail = resp.json()["error"]
        assert detail["error"] == "not_found"
        assert detail["details"]["session_id"] == sid_1
        assert [m.id for m in app.state.messages[sid_2]] == ["msg_2"]
        assert app.state.sessions.get(sid_2).message_count == 1
        assert app.state.bus._history.get(sid_2, []) == []


def test_legacy_delete_can_be_scoped_with_session_query(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "one"}).json()["id"]
        app.state.messages[sid] = [_message("msg_1", sid, "one")]
        app.state.sessions.update(sid, message_count=1)

        resp = client.delete(f"/v1/messages/msg_1?session_id={sid}")

        assert resp.status_code == 204
        assert app.state.messages[sid] == []
        assert app.state.sessions.get(sid).message_count == 0


def test_legacy_delete_without_session_remains_compatible(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "one"}).json()["id"]
        app.state.messages[sid] = [_message("msg_1", sid, "one")]
        app.state.sessions.update(sid, message_count=1)

        resp = client.delete("/v1/messages/msg_1")

        assert resp.status_code == 204
        assert app.state.messages[sid] == []
        assert app.state.sessions.get(sid).message_count == 0


def _a2ui_batch(surface_id: str) -> list[dict[str, object]]:
    return [
        {
            "version": "v0.9.1",
            "createSurface": {"surfaceId": surface_id, "catalogId": CLIO_A2UI_CATALOG_ID},
        },
        {
            "version": "v0.9.1",
            "updateComponents": {
                "surfaceId": surface_id,
                "components": [
                    {
                        "id": "root",
                        "component": "clio.status.v1",
                        "label": "Analysis",
                        "state": "completed",
                    }
                ],
            },
        },
    ]


def test_deleting_an_a2ui_message_preserves_its_ready_surface(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "surface"}).json()["id"]
        app.state.a2ui_store.apply_batch(sid, _a2ui_batch("kept_surface"))
        target = app.state.messages[sid][-1].id

        resp = client.delete(f"/v1/sessions/{sid}/messages/{target}")

        assert resp.status_code == 204
        assert [m.id for m in app.state.messages[sid]] != [target]
        surface = app.state.a2ui_store.get(sid, "kept_surface")
        assert surface is not None
        assert surface.state == "ready"
        assert app.state.a2ui_store.projection_degradations(sid) == []
        assert app.state.messages[sid][-1].metadata == {
            "synthetic": "a2ui_preservation",
            "preserved_by": "message_delete",
        }
        assert app.state.sessions.get(sid).message_count == len(app.state.messages[sid])
