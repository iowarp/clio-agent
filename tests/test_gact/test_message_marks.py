"""Conversation landmarks persist without changing the transcript or model context."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.message_marks import merge_session_metadata
from clio_agent.gact.protocol_v3 import event_to_v3
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.types import Message, Part


@pytest.fixture()
def marked_client(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Seed a recorded transcript without invoking a model."""
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Calibration review"}).json()["id"]
        app.state.messages[sid] = [
            Message(
                id=f"msg_{index}",
                session_id=sid,
                role="user" if index % 2 else "assistant",
                created_at=f"2026-10-11T12:00:0{index}+00:00",
                updated_at=f"2026-10-11T12:00:0{index}+00:00",
                parts=[Part(id=f"part_{index}", type="text", text=f"Calibration step {index}")],
            )
            for index in range(1, 5)
        ]
        app.state.sessions.update(sid, message_count=4)
        yield client, sid


def _mark(client: TestClient, sid: str, marks: dict[str, bool]) -> dict[str, Any]:
    response = client.patch(f"/v1/sessions/{sid}", json={"metadata": {"message_marks": marks}})
    assert response.status_code == 200, response.text
    return response.json()["metadata"]["message_marks"]


def test_marks_persist_and_publish_without_changing_transcript(marked_client: Any) -> None:
    client, sid = marked_client
    before = client.get(f"/v1/sessions/{sid}/messages").json()
    assert _mark(client, sid, {"msg_1": True}) == {"msg_1": True}
    assert _mark(client, sid, {"msg_3": True}) == {"msg_1": True, "msg_3": True}
    assert _mark(client, sid, {"msg_1": False}) == {"msg_3": True}
    store = SessionStore(path=client.app.state.sessions._path, store=client.app.state.arc._store)
    assert store.get(sid).metadata["message_marks"] == {"msg_3": True}
    assert client.get(f"/v1/sessions/{sid}/messages").json() == before
    event = client.app.state.bus._history[sid][-1]
    assert event.type == "session.updated"
    assert event.payload["metadata"]["message_marks"] == {"msg_3": True}


def test_v3_reads_and_events_include_public_marks_but_not_private_metadata(
    marked_client: Any,
) -> None:
    client, sid = marked_client
    client.app.state.sessions.update(
        sid, metadata_patch={"private_internal": "hidden", "composer_queue_paused": False}
    )
    response = client.patch(
        f"/v1/sessions/{sid}",
        headers={"X-GACT-Version": "0.3"},
        json={"metadata": {"message_marks": {"msg_1": True}}},
    )
    assert response.status_code == 200, response.text
    expected = {"composer_queue_paused": False, "message_marks": {"msg_1": True}}
    assert response.json()["metadata"] == expected
    assert (
        client.get(f"/v1/sessions/{sid}", headers={"X-GACT-Version": "0.3"}).json()["metadata"]
        == expected
    )
    event = client.app.state.bus._history[sid][-1]
    projected = event_to_v3(event, session=client.app.state.sessions.get(sid))
    assert projected["payload"]["metadata"] == expected


@pytest.mark.parametrize("marks", [None, [], True, {"msg_1": 1}, {"msg_1": "true"}, {"": True}])
def test_bad_marks_reject_entire_update(marked_client: Any, marks: Any) -> None:
    client, sid = marked_client
    response = client.patch(
        f"/v1/sessions/{sid}",
        json={"title": "Must not change", "metadata": {"message_marks": marks}},
    )
    assert response.status_code == 422
    assert client.app.state.sessions.get(sid).title == "Calibration review"
    assert "message_marks" not in client.app.state.sessions.get(sid).metadata


def test_foreign_or_missing_message_rejects_entire_mark_patch(marked_client: Any) -> None:
    client, sid = marked_client
    other = client.post("/v1/sessions", json={"title": "Other"}).json()["id"]
    client.app.state.messages[other] = [
        client.app.state.messages[sid][0].model_copy(update={"id": "foreign", "session_id": other})
    ]
    for message_id in ["foreign", "missing"]:
        response = client.patch(
            f"/v1/sessions/{sid}",
            json={"metadata": {"message_marks": {"msg_1": True, message_id: True}}},
        )
        assert response.status_code == 404
        assert "message_marks" not in client.app.state.sessions.get(sid).metadata


def test_fork_copies_only_retained_marks_and_edits_them_independently(marked_client: Any) -> None:
    client, sid = marked_client
    _mark(client, sid, {"msg_1": True, "msg_4": True})
    fork = client.post(f"/v1/sessions/{sid}/fork", json={"at_message_id": "msg_2"})
    assert fork.status_code == 201, fork.text
    branch = fork.json()
    assert branch["metadata"]["message_marks"] == {"msg_1": True}
    assert _mark(client, branch["id"], {"msg_1": False, "msg_2": True}) == {"msg_2": True}
    assert client.app.state.sessions.get(sid).metadata["message_marks"] == {
        "msg_1": True,
        "msg_4": True,
    }


@pytest.mark.parametrize("operation", ["rewind", "undo"])
def test_history_removal_prunes_marks_only_for_deleted_messages(
    marked_client: Any, operation: str
) -> None:
    client, sid = marked_client
    _mark(client, sid, {"msg_1": True, "msg_2": True, "msg_4": True})
    data = {"message_id": "msg_2"} if operation == "rewind" else {"count": 2}
    response = client.post(f"/v1/sessions/{sid}/{operation}", json=data)
    assert response.status_code == 200, response.text
    assert client.app.state.sessions.get(sid).metadata["message_marks"] == {
        "msg_1": True,
        "msg_2": True,
    }


def test_export_import_preserves_marks(marked_client: Any) -> None:
    client, sid = marked_client
    _mark(client, sid, {"msg_2": True})
    exported = client.get(f"/v1/sessions/{sid}/export")
    assert exported.status_code == 200, exported.text
    assert exported.json()["session"]["metadata"]["message_marks"] == {"msg_2": True}
    imported = client.post("/v1/sessions/import", json=exported.json())
    assert imported.status_code == 200, imported.text
    assert imported.json()["metadata"]["message_marks"] == {"msg_2": True}
    assert _mark(client, imported.json()["id"], {"msg_2": False}) == {}
    assert client.app.state.sessions.get(sid).metadata["message_marks"] == {"msg_2": True}


def test_concurrent_mark_deltas_do_not_overwrite_each_other(tmp_path: Path, arc: Any) -> None:
    store = SessionStore(path=tmp_path / "sessions.json", store=arc._store)
    sid = store.create(workspace_id="ws_default", title="Shared").id

    def mark(index: int) -> None:
        store.update(sid, metadata_patch={"message_marks": {f"message_{index}": True}})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(mark, range(12)))
    assert SessionStore(path=tmp_path / "sessions.json", store=arc._store).get(sid).metadata[
        "message_marks"
    ] == {f"message_{index}": True for index in range(12)}
    assert merge_session_metadata({"pinned": True}, {"message_marks": {}}) == {
        "pinned": True,
        "message_marks": {},
    }
    with pytest.raises(ValueError, match="message IDs to booleans"):
        merge_session_metadata({}, {"message_marks": {"message": 1}})
