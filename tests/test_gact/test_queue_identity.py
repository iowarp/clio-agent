"""Queue promotion preserves accepted transcript identities across admission races."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Part, PostMessageRequest
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _app(client: TestClient) -> FastAPI:
    """Narrow the actual ASGI application, without a replacement test implementation."""
    assert isinstance(client.app, FastAPI)
    return client.app


@pytest.fixture
def paused(tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    """Run the actual REST/Core owners while inference remains held."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="unused"))
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "Queue identity"}).json()["id"]
        app.state.sessions.update(sid, metadata_patch={"agent_paused": True})
        yield client, sid


def test_different_queue_keys_cannot_accept_the_same_client_message_id(
    paused: tuple[TestClient, str],
) -> None:
    client, sid = paused
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    original = client.post(
        endpoint, json={"text": "first", "client_message_id": "shared", "idempotency_key": "one"}
    )
    assert original.status_code == 201, original.text
    before = _app(client).state.task_history.pending_mail(sid)
    refused = client.post(
        endpoint, json={"text": "second", "client_message_id": "shared", "idempotency_key": "two"}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["error"] == "client_message_id_conflict"
    assert _app(client).state.task_history.pending_mail(sid) == before
    assert client.get(endpoint).json()["queued_messages"] == [original.json()]
    replay = client.post(
        endpoint, json={"text": "first", "client_message_id": "different", "idempotency_key": "one"}
    )
    assert replay.status_code == 201 and replay.json() == original.json()
    assert not _app(client).state.task_history.acknowledged_notifications(sid)


def test_failed_core_promotion_has_no_conflicting_transcript_and_can_retry(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault before the authoritative mail update cannot strand its original draft."""
    client, sid = paused
    app = _app(client)
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    response = client.post(endpoint, json={"text": "Retained draft", "client_message_id": "stable"})
    assert response.status_code == 201, response.text
    queued = response.json()
    original = app.state.task_history.update_mail

    def fail_promotion(item: Any, **kwargs: Any) -> Any:
        if item.id == queued["id"] and item.delivery == "eligible":
            raise RuntimeError("Core promotion fault")
        return original(item, **kwargs)

    monkeypatch.setattr(app.state.task_history, "update_mail", fail_promotion)
    failed = client.post(
        endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
    )
    assert failed.status_code == 500, failed.text
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
    assert client.get(endpoint).json()["queued_messages"] == [queued]
    monkeypatch.setattr(app.state.task_history, "update_mail", original)
    retried = client.post(
        endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["acceptance"]["message_id"] == "stable"
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert len(messages) == 1 and messages[0]["id"] == "stable"
    assert not app.state.task_history.acknowledged_notifications(sid)


def test_projection_failure_preserves_confirmed_promotion_for_startup_repair(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault after Core acceptance leaves the original typed input recoverable."""
    from clio_agent.gact import session_store, turn

    client, sid = paused
    app = _app(client)
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    response = client.post(endpoint, json={"text": "Accepted draft", "client_message_id": "stable"})
    assert response.status_code == 201, response.text
    queued = response.json()
    original = turn._append_session_message

    def fail_projection(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Transcript projection fault")

    monkeypatch.setattr(turn, "_append_session_message", fail_projection)
    failed = client.post(
        endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
    )
    assert failed.status_code == 500, failed.text
    canonical = app.state.task_history.mail_item(sid, queued["id"])
    assert canonical is not None and canonical.delivery == "eligible"
    value = app.state.task_history.read_content(canonical.content)
    assert value["message_id"] == value["message"]["id"] == "stable"
    assert client.get(endpoint).json()["queued_messages"] == []
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == []
    monkeypatch.setattr(turn, "_append_session_message", original)
    session_store.restore_pending_mail_messages(app, app.state.task_history)
    session_store.restore_pending_mail_messages(app, app.state.task_history)
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert len(messages) == 1 and messages[0]["id"] == "stable"
    assert app.state.task_history.mail_item(sid, queued["id"]) == canonical
    assert not app.state.task_history.acknowledged_notifications(sid)


def test_queue_rejects_an_identity_already_owned_by_the_transcript(
    paused: tuple[TestClient, str],
) -> None:
    client, sid = paused
    response = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"text": "Original human input", "client_message_id": "shared"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["task_handle"]
    canonical_id = response.json()["message_id"]
    before = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    refused = client.post(
        f"/v1/sessions/{sid}/queued-messages",
        json={
            "text": "Would overwrite",
            "client_message_id": canonical_id,
            "idempotency_key": "new",
        },
    )
    assert refused.status_code == 409, refused.text
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == before
    assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == []


def test_message_replay_repairs_projection_from_its_original_core_acceptance(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed transcript write must not turn retry into a second accepted instruction."""
    from clio_agent.gact import turn

    client, sid = paused
    app = _app(client)
    endpoint = f"/v1/sessions/{sid}/messages"
    request = {"text": "Retain the original acceptance", "client_message_id": "retryable"}
    original = turn._append_session_message

    def fail_projection(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Transcript projection fault")

    monkeypatch.setattr(turn, "_append_session_message", fail_projection)
    failed = client.post(endpoint, json=request)
    assert failed.status_code == 500, failed.text
    pending = app.state.task_history.pending_mail(sid)
    assert len(pending) == 1
    accepted = pending[0]
    saved = app.state.task_history.read_content(accepted.content)
    assignment = app.state.task_history.latest_assignment(sid)
    assert assignment is not None and saved["assignment_id"] == assignment.handle
    assert client.get(endpoint).json()["messages"] == []

    monkeypatch.setattr(turn, "_append_session_message", original)
    for _ in range(2):
        replay = client.post(endpoint, json=request)
        assert replay.status_code == 200, replay.text
        receipt = replay.json()
        assert receipt["idempotent_replay"] is True
        assert receipt["message_id"] == accepted.id == saved["message"]["id"]
        assert receipt["accepted_at"] == saved["message"]["created_at"]
        assert receipt["task_handle"] == assignment.handle
        assert receipt["effective_model"] == saved["message"]["metadata"]["effective_model"]
        rows = client.get(endpoint).json()["messages"]
        assert len(rows) == 1 and rows[0]["id"] == accepted.id
        assert app.state.task_history.pending_mail(sid) == (accepted,)
        assert app.state.task_history.latest_assignment(sid) == assignment
        assert not app.state.task_history.acknowledged_notifications(sid)


def test_promotion_rechecks_identity_after_an_intervening_public_acceptance(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import message_submission

    client, sid = paused
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    response = client.post(
        endpoint,
        json={"text": "Queued input", "client_message_id": "shared", "idempotency_key": "q"},
    )
    assert response.status_code == 201, response.text
    queued = response.json()
    original = message_submission.prepare_references_sync
    inserted: list[dict[str, Any]] = []

    def accept_during_preflight(app: Any, session: Any, request: PostMessageRequest) -> Any:
        prepared = original(app, session, request)
        if not inserted:
            # Restore before the nested request uses this same preparation seam.
            monkeypatch.setattr(message_submission, "prepare_references_sync", original)
            with monkeypatch.context() as identities:
                # Make an opaque server ID collision deterministic; acceptance
                # still goes through the real public route and Core owner.
                identities.setattr(message_submission, "_new_message_id", lambda _role: "shared")
                accepted = client.post(
                    f"/v1/sessions/{sid}/messages", json={"text": "Intervening human input"}
                )
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["task_handle"]
            inserted.extend(client.get(f"/v1/sessions/{sid}/messages").json()["messages"])
        return prepared

    monkeypatch.setattr(message_submission, "prepare_references_sync", accept_during_preflight)
    refused = client.post(
        endpoint + "/" + queued["id"] + "/promote", json={"revision": queued["revision"]}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["error"] == "client_message_id_conflict"
    assert inserted and client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == inserted
    assert client.get(endpoint).json()["queued_messages"] == [queued]
    assert not _app(client).state.task_history.acknowledged_notifications(sid)


def test_legacy_transcript_identity_cannot_be_replaced_by_new_acceptance(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical transcript presence cannot imply permission to replay or overwrite it."""
    from clio_agent.gact import message_submission, session_store
    from clio_agent.gact.types import Message, Part

    client, sid = paused
    app = _app(client)
    legacy = Message(
        id="legacy-user",
        session_id=sid,
        role="user",
        created_at="2026-10-01T12:00:00Z",
        updated_at="2026-10-01T12:00:00Z",
        parts=[Part(type="text", text="Retained historical instruction")],
    )
    session_store._append_session_message(app, sid, legacy)
    before = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assignment = app.state.task_history.latest_assignment(sid)
    monkeypatch.setattr(message_submission, "_new_message_id", lambda _role: legacy.id)
    refused = client.post(
        f"/v1/sessions/{sid}/messages", json={"text": "Would rewrite existing history"}
    )
    assert refused.status_code == 409, refused.text
    assert "without a confirmed acceptance" in refused.json()["error"]["message"]
    assert client.get(f"/v1/sessions/{sid}/messages").json()["messages"] == before
    assert not app.state.task_history.pending_mail(sid)
    assert app.state.task_history.latest_assignment(sid) == assignment


@pytest.mark.parametrize("key", ["idempotency_key", "client_message_id"])
def test_queue_replay_returns_the_accepted_revision_before_resolving_references_again(
    paused: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Replay preserves accepted identity even after an edit or reference expiry."""
    from clio_agent.gact.routes import mailbox

    client, sid = paused
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    request = {"text": "original", key: "retained"}
    accepted = client.post(endpoint, json=request)
    assert accepted.status_code == 201, accepted.text
    row = accepted.json()
    edited = client.patch(
        endpoint + "/" + row["id"], json={"revision": row["revision"], "text": "edited"}
    )
    assert edited.status_code == 200, edited.text

    def refuse_resolution(*_args: Any, **_kwargs: Any) -> list[Part]:
        raise AssertionError("An accepted request must not be resolved or accepted twice")

    monkeypatch.setattr(mailbox, "authorize_context_reference_parts_sync", refuse_resolution)
    replay = client.post(endpoint, json=request)
    assert replay.status_code == 201, replay.text
    assert replay.json() == edited.json()
    assert client.get(endpoint).json()["queued_messages"] == [edited.json()]
    assert not _app(client).state.task_history.acknowledged_notifications(sid)
    changed = client.post(endpoint, json={**request, "text": "different submission"})
    if key == "idempotency_key":
        assert changed.status_code == 201 and changed.json() == edited.json()
    else:
        assert changed.status_code == 409, changed.text


@pytest.mark.parametrize("operation", ["create", "edit"])
@pytest.mark.parametrize("change", ["session_deleted", "workspace_root"])
def test_queue_reference_preflight_cannot_commit_after_its_workspace_changes(
    paused: tuple[TestClient, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    change: str,
) -> None:
    """Authorization of one workspace cannot admit content into a different scope."""
    from clio_agent.gact.routes import mailbox

    client, sid = paused
    app = _app(client)
    root = tmp_path / "original"
    root.mkdir()
    (root / "input.txt").write_text("private original workspace", encoding="utf-8")
    app.state.workspaces.update("ws_default", root_path=str(root))
    # The scope change: the workspace moves to a folder no other workspace pins (#1771).
    moved = tmp_path / "other"
    endpoint = f"/v1/sessions/{sid}/queued-messages"
    before: list[dict[str, Any]] = []
    body: dict[str, Any] = {
        "parts": [{"type": "context_ref", "ref_kind": "workspace_file", "ref_id": "input.txt"}]
    }
    if operation == "edit":
        accepted = client.post(endpoint, json={"text": "retain this draft"})
        assert accepted.status_code == 201, accepted.text
        before = [accepted.json()]
        endpoint += "/" + before[0]["id"]
        body["revision"] = before[0]["revision"]
    original = mailbox.authorize_context_reference_parts_sync
    resolved: list[Part] = []
    pending_after_deletion: tuple[Any, ...] = ()

    def change_after_authorization(app: Any, session: Any, parts: list[Part]) -> list[Part]:
        nonlocal pending_after_deletion
        canonical = original(app, session, parts)
        resolved.extend(canonical)
        if change == "session_deleted":
            removed = client.delete(f"/v1/sessions/{sid}")
            assert removed.status_code == 204, removed.text
            pending_after_deletion = app.state.task_history.pending_mail(sid)
        else:
            app.state.workspaces.update("ws_default", root_path=str(moved))
        return canonical

    monkeypatch.setattr(
        mailbox, "authorize_context_reference_parts_sync", change_after_authorization
    )
    response = (
        client.post(endpoint, json=body)
        if operation == "create"
        else client.patch(endpoint, json=body)
    )
    assert resolved and resolved[0].revision.startswith("sha256:")
    if change == "session_deleted":
        assert response.status_code == 404, response.text
        assert client.get(f"/v1/sessions/{sid}/queued-messages").status_code == 404
        assert app.state.task_history.pending_mail(sid) == pending_after_deletion
    else:
        assert response.status_code == 409, response.text
        assert client.get(f"/v1/sessions/{sid}/queued-messages").json()["queued_messages"] == before
    assert not app.state.task_history.acknowledged_notifications(sid)
