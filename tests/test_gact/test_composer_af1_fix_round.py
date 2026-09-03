"""AF1 adversarial-review fix round for the campaign-integration composer wave.

One test per reviewed finding, each written failing-first against the reviewed
head (58547561) before its fix landed.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.ask_user_tool import arm_ask_user_deadline
from clio_agent.gact.protocol_v3 import CLIO_A2UI_CATALOG_ID
from clio_agent.gact.types import UserQuestion
from tests._config_layer import set_config

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}


def _armed_question(app: object, sid: str, question_id: str, *, ttl_s: int = 3600) -> UserQuestion:
    now = datetime.now(timezone.utc)
    row = UserQuestion(
        id=question_id,
        session_id=sid,
        owner_session_id=sid,
        attended_session_id=sid,
        prompt="Which dataset?",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_s)).isoformat(),
    )
    app.state.user_questions[row.id] = row
    return row


# --------------------------------------------------------------------------- #
# Finding 2: the ask_user expiry timer is retained and cancelled when settled
# --------------------------------------------------------------------------- #


def test_answering_a_question_cancels_its_armed_expiry_timer(tmp_path) -> None:
    """A settled question must not leave a live daemon timer for its whole TTL."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ask")
    row = _armed_question(app, session.id, "q_settled")
    app.state.sessions.update(
        session.id,
        status="waiting_user",
        metadata_patch={"pending_user_question_id": row.id},
    )

    threads_before = threading.active_count()
    arm_ask_user_deadline(app, row)
    assert row.id in app.state.ask_user_deadlines
    timer = app.state.ask_user_deadlines[row.id]

    with TestClient(app) as client:
        answered = client.post(
            f"/v1/sessions/{session.id}/questions/{row.id}/answer",
            headers=HEADERS,
            json={"answer": "the beam"},
        )
        assert answered.status_code == 200

    assert app.state.user_questions[row.id].status == "answered"
    assert row.id not in app.state.ask_user_deadlines
    timer.join(timeout=5.0)
    assert not timer.is_alive()
    assert threading.active_count() <= threads_before


def test_cancelling_a_question_cancels_its_armed_expiry_timer(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ask")
    row = _armed_question(app, session.id, "q_cancelled")

    arm_ask_user_deadline(app, row)
    timer = app.state.ask_user_deadlines[row.id]

    with TestClient(app) as client:
        cancelled = client.post(
            f"/v1/sessions/{session.id}/questions/{row.id}/cancel", headers=HEADERS
        )
        assert cancelled.status_code == 200

    assert row.id not in app.state.ask_user_deadlines
    timer.join(timeout=5.0)
    assert not timer.is_alive()


def _a2ui_surface(client: TestClient, sid: str, surface_id: str) -> dict[str, object]:
    """Produce one approval surface whose button dispatches ``approval.respond``."""

    create = {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": surface_id, "catalogId": CLIO_A2UI_CATALOG_ID},
    }
    components = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": surface_id,
            "components": [
                {"id": "label", "component": "Text", "text": "Approve"},
                {
                    "id": "approve",
                    "component": "Button",
                    "child": "label",
                    "action": {
                        "event": {
                            "name": "approval.respond",
                            "context": {"permission_id": "perm_other", "action": "allow"},
                        }
                    },
                },
            ],
        },
    }
    produced = client.post(
        f"/v1/sessions/{sid}/a2ui/messages",
        headers=HEADERS,
        json={"messages": [create, components]},
    )
    assert produced.status_code == 200, produced.text
    return produced.json()


# --------------------------------------------------------------------------- #
# Finding 3: approval.respond is exact-owner scoped
# --------------------------------------------------------------------------- #


def test_approval_respond_context_action_is_data_not_a_nested_action_envelope() -> None:
    """``approval.respond`` was unroutable: its own context.action failed validation."""

    from clio_agent.gact.a2ui import A2UIValidationError, _validate_value, validate_client_action

    message = {
        "version": "v0.9.1",
        "action": {
            "name": "approval.respond",
            "surfaceId": "s",
            "sourceComponentId": "b",
            "timestamp": "2026-09-03T10:00:00Z",
            "context": {"permission_id": "p", "action": "allow"},
        },
    }
    parsed = validate_client_action(message, surface_id="s")
    assert parsed["context"] == {"permission_id": "p", "action": "allow"}

    # The SAFETY rules still apply inside a free-form action context.
    for unsafe in ({"style": "x"}, {"call": "x"}, {"url": "http://evil"}):
        with pytest.raises(A2UIValidationError):
            _validate_value(
                {
                    "id": "b",
                    "component": "Button",
                    "action": {
                        "event": {
                            "name": "approval.respond",
                            "context": {"permission_id": "p", "action": "allow", **unsafe},
                        }
                    },
                }
            )


def test_a2ui_approval_respond_refuses_a_permission_outside_its_session_scope(
    tmp_path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    dispatcher = app.state.sessions.create(workspace_id="ws_default", title="dispatcher")
    victim = app.state.sessions.create(workspace_id="ws_default", title="victim")
    app.state.permissions["perm_other"] = {
        "id": "perm_other",
        "session_id": victim.id,
        "status": "pending",
        "summary": "Allow victim write",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_call": {"tool_name": "fs_apply_edit_write", "input": {"path": "x"}},
    }
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "approval.respond",
            "surfaceId": "approve-form",
            "sourceComponentId": "approve",
            "timestamp": "2026-09-03T10:00:00Z",
            "context": {"permission_id": "perm_other", "action": "allow"},
        },
    }

    with TestClient(app) as client:
        _a2ui_surface(client, dispatcher.id, "approve-form")
        crossed = client.post(
            f"/v1/sessions/{dispatcher.id}/a2ui/actions",
            headers=HEADERS,
            json={"message": action},
        )

    assert crossed.status_code == 404
    assert app.state.permissions["perm_other"]["status"] == "pending"


def test_a2ui_approval_respond_still_resolves_its_own_session_permission(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    owner = app.state.sessions.create(workspace_id="ws_default", title="owner")
    app.state.permissions["perm_other"] = {
        "id": "perm_other",
        "session_id": owner.id,
        "status": "pending",
        "summary": "Allow own write",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_call": {"tool_name": "fs_apply_edit_write", "input": {"path": "x"}},
    }
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "approval.respond",
            "surfaceId": "approve-form",
            "sourceComponentId": "approve",
            "timestamp": "2026-09-03T10:00:00Z",
            "context": {"permission_id": "perm_other", "action": "allow"},
        },
    }

    with TestClient(app) as client:
        _a2ui_surface(client, owner.id, "approve-form")
        allowed = client.post(
            f"/v1/sessions/{owner.id}/a2ui/actions",
            headers=HEADERS,
            json={"message": action},
        )

    assert allowed.status_code == 200
    assert app.state.permissions["perm_other"]["status"] == "resolved"


def test_interaction_permission_response_forwards_the_intercept_payload(tmp_path) -> None:
    """Parity with POST /v1/permissions/{pid}: approve-with-modified-args survives."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="intercept")
    app.state.permissions["perm_intercept"] = {
        "id": "perm_intercept",
        "session_id": session.id,
        "status": "pending",
        "summary": "Allow write",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_call": {"tool_name": "fs_apply_edit_write", "input": {"path": "x"}},
    }

    with TestClient(app) as client:
        allowed = client.post(
            f"/v1/sessions/{session.id}/interactions/permission:perm_intercept/respond",
            headers=HEADERS,
            json={"action": "allow", "metadata": {"input": {"path": "safe.txt"}}},
        )

    assert allowed.status_code == 200
    assert app.state.permissions["perm_intercept"]["resolution_input"] == {"path": "safe.txt"}


# --------------------------------------------------------------------------- #
# Finding 4: the interaction responder enforces A2UI negotiation
# --------------------------------------------------------------------------- #


def test_interaction_a2ui_response_requires_the_negotiated_protocol_version(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="surface")
    action = {
        "version": "v0.9.1",
        "action": {
            "name": "form.submit",
            "surfaceId": "form",
            "sourceComponentId": "submit",
            "timestamp": "2026-09-03T10:00:00Z",
            "context": {"value": "x"},
        },
    }
    create = {
        "version": "v0.9.1",
        "createSurface": {"surfaceId": "form", "catalogId": CLIO_A2UI_CATALOG_ID},
    }
    components = {
        "version": "v0.9.1",
        "updateComponents": {
            "surfaceId": "form",
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

    with TestClient(app) as client:
        produced = client.post(
            f"/v1/sessions/{session.id}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [create, components]},
        )
        assert produced.status_code == 200
        interaction_id = f"a2ui:{session.id}:form"
        unnegotiated = client.post(
            f"/v1/sessions/{session.id}/interactions/{interaction_id}/respond",
            json={"message": action},
        )
        negotiated = client.post(
            f"/v1/sessions/{session.id}/interactions/{interaction_id}/respond",
            headers=HEADERS,
            json={"message": action},
        )

    assert unnegotiated.status_code == 406
    assert unnegotiated.json()["error"]["error"] == "unsupported_protocol"
    assert negotiated.status_code == 200


# --------------------------------------------------------------------------- #
# Finding 1: reference resolution never runs on the event loop
# --------------------------------------------------------------------------- #


def _workspace_session(app: object, tmp_path, name: str = "ws") -> tuple[str, str]:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    workspace = app.state.workspaces.create(name=name, root_path=str(root))
    session = app.state.sessions.create(workspace_id=workspace.id, title=name)
    return workspace.id, session.id


def test_post_messages_never_hashes_a_referenced_file_on_the_loop_thread(
    tmp_path, monkeypatch
) -> None:
    """The digest is real work; it must happen in a worker, not on the loop."""

    from clio_agent.gact import context_reference_file_io, context_references

    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace_id, sid = _workspace_session(app, tmp_path)
    target = tmp_path / "ws" / "notes.txt"
    target.write_text("hello reference", encoding="utf-8")

    loop_thread: dict[str, int] = {}
    hashing_threads: list[int] = []
    real_sha = context_reference_file_io.sha256_file

    def recording_sha(path):
        hashing_threads.append(threading.get_ident())
        return real_sha(path)

    monkeypatch.setattr(context_references, "_sha256_file", recording_sha)

    with TestClient(app) as client:

        @app.get("/_af1/loop-thread")
        async def _loop_thread() -> dict[str, int]:
            loop_thread["ident"] = threading.get_ident()
            return loop_thread

        loop_ident = client.get("/_af1/loop-thread").json()["ident"]
        listed = client.get(
            f"/v1/workspaces/{workspace_id}/references",
            params={"kinds": "workspace_file"},
            headers=HEADERS,
        ).json()["references"]
        row = next(item for item in listed if item["id"] == "notes.txt")
        assert row["part_type"] == "context_ref"
        posted = client.post(
            f"/v1/sessions/{sid}/messages",
            headers=HEADERS,
            json={
                "parts": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "context_ref",
                        "ref_kind": "workspace_file",
                        "ref_id": "notes.txt",
                        "revision": row["revision"],
                    },
                ]
            },
        )

    assert posted.status_code in (200, 202, 503), posted.text
    assert hashing_threads, "the reference was never resolved"
    assert loop_ident not in hashing_threads


def test_oversized_workspace_file_is_refused_with_a_typed_limit(tmp_path) -> None:
    from clio_agent.gact.context_reference_domain import ContextReferenceError
    from clio_agent.gact.context_references import authorize_context_reference_parts_sync
    from clio_agent.gact.types import Part

    set_config("gact.context_references.max_hashable_bytes", 8)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace_id, sid = _workspace_session(app, tmp_path, name="big")
    (tmp_path / "big" / "large.txt").write_text("x" * 64, encoding="utf-8")
    session = app.state.sessions.get(sid)
    assert session.workspace_id == workspace_id

    with pytest.raises(Exception) as refused:  # noqa: PT011 - HTTPException envelope
        authorize_context_reference_parts_sync(
            app,
            session,
            [Part(type="context_ref", ref_kind="workspace_file", ref_id="large.txt")],
        )
    detail = getattr(refused.value, "detail", {})
    assert not isinstance(refused.value, ContextReferenceError)
    assert detail["error"]["error"] == "context_ref_too_large"
    assert detail["error"]["details"]["max_bytes"] == 8
    assert detail["error"]["details"]["size_bytes"] == 64


# --------------------------------------------------------------------------- #
# Finding 5: a picked resource is attachable end to end
# --------------------------------------------------------------------------- #


def test_resource_reference_search_row_attaches_as_a_resource_ref_part(tmp_path) -> None:
    from clio_agent.gact.context_references import authorize_context_reference_parts_sync
    from clio_agent.gact.types import Part

    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace_id, sid = _workspace_session(app, tmp_path, name="res")
    payload = b"# paper\n"
    record, _resumed = app.state.resource_store.create_or_resume(
        workspace_id=workspace_id,
        name="paper.md",
        declared_size=len(payload),
        claimed_mime="text/markdown",
    )
    ready = app.state.resource_store.append(record.id, offset=0, data=payload)
    assert ready.state == "ready"

    with TestClient(app) as client:
        listed = client.get(
            f"/v1/workspaces/{workspace_id}/references",
            params={"kinds": "resource"},
            headers=HEADERS,
        ).json()["references"]
    row = next(item for item in listed if item["id"] == ready.id)
    assert row["part_type"] == "resource_ref"

    session = app.state.sessions.get(sid)
    attached = authorize_context_reference_parts_sync(
        app,
        session,
        [
            Part(
                type="context_ref",
                ref_kind="resource",
                ref_id=ready.id,
                revision=row["revision"],
            )
        ],
    )
    assert [part.type for part in attached] == ["resource_ref"]
    assert attached[0].resource_id == ready.id
    assert attached[0].resource_revision == str(ready.revision)
    assert attached[0].media_type == ready.detected_mime


def test_capability_documents_the_part_type_mapping_and_the_agent_mechanism() -> None:
    from clio_agent.gact.context_reference_domain import CONTEXT_REFERENCE_CAPABILITY

    mapping = CONTEXT_REFERENCE_CAPABILITY["part_type_by_kind"]
    assert mapping["resource"] == "resource_ref"
    assert mapping["workspace_file"] == "context_ref"
    assert set(mapping) == set(CONTEXT_REFERENCE_CAPABILITY["search_kinds"])
    agents = CONTEXT_REFERENCE_CAPABILITY["alternate_mechanisms"]["agents"]
    assert agents["mechanism"] == "message_request_field"
    assert agents["field"] == "agent"
    assert "agents" not in CONTEXT_REFERENCE_CAPABILITY["kinds"]


# --------------------------------------------------------------------------- #
# Finding 6: an admitted message stays retryable after its evidence moves
# --------------------------------------------------------------------------- #


def test_admitted_diff_reference_redelivers_its_snapshot_with_a_stale_marker(
    tmp_path,
) -> None:
    from clio_agent.gact.context_reference_delivery import enrich_with_context_references
    from clio_agent.gact.context_references import authorize_context_reference_parts_sync
    from clio_agent.gact.types import Message, Part

    app = build_app(sessions_path=tmp_path / "sessions.json")
    _workspace_id, sid = _workspace_session(app, tmp_path, name="diffs")
    app.state.pending_diffs = {
        sid: [
            {
                "path": "src/app.py",
                "part_id": "d1",
                "message_id": "msg_1",
                "status": "pending",
                "unified_diff": "@@ -1 +1 @@\n-a\n+b\n",
            }
        ]
    }
    session = app.state.sessions.get(sid)
    from clio_agent.gact.context_reference_evidence import evidence_reference_snapshots

    row, _payload = evidence_reference_snapshots(app, session.workspace_id, ["diff"])[0]
    admitted = authorize_context_reference_parts_sync(
        app,
        session,
        [
            Part(
                type="context_ref",
                ref_kind="diff",
                ref_id=row["id"],
                revision=row["revision"],
            )
        ],
    )
    message = Message(
        id="msg_user",
        turn_id="msg_user",
        session_id=sid,
        role="user",
        created_at="",
        updated_at="",
        parts=admitted,
    )
    # The diff settles -- exactly what happens between the send and the retry.
    app.state.pending_diffs[sid][0]["status"] = "applied"

    enriched = enrich_with_context_references(app, sid, "retry me", message)

    assert "as-of snapshot" in enriched
    assert row["id"] in enriched
    assert '"status": "pending"' in enriched


def test_ask_user_ttl_default_and_clamp_are_config_resolved() -> None:
    from clio_agent.gact import ask_user_tool

    set_config("gact.ask_user.ttl_s", 42)
    set_config("gact.ask_user.max_ttl_s", 120)
    assert ask_user_tool.ask_user_ttl_bounds() == (42, 120)
