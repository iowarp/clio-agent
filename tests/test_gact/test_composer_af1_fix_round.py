"""AF1 adversarial-review fix round for the campaign-integration composer wave.

One test per reviewed finding, each written failing-first against the reviewed
head (58547561) before its fix landed.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.arc.task_types import QuestionDefinition, QuestionReply
from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id
from clio_agent.gact.app import build_app
from clio_agent.gact.question_surface import question_row
from clio_agent.gact.types import UserQuestion
from tests._config_layer import set_config
from tests.test_gact.task_records import record_assignment_fixture

HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
CLIO_A2UI_CATALOG_ID = workspace_catalog_id()


def _armed_question(app: Any, sid: str, question_id: str, *, ttl_s: int = 3600) -> UserQuestion:
    """Post one actual Core Question and let the application own its timer."""
    assignment = app.state.task_history.latest_assignment(sid) or record_assignment_fixture(
        app, sid
    )
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat() if ttl_s else None
    task = app.state.questions.create(
        assignment.owner,
        QuestionDefinition(prompt="Which dataset?", expires_at=expires),
        invocation_id=question_id,
        handle=question_id,
    )
    return question_row(app, task.handle)


# --------------------------------------------------------------------------- #
# Finding 2: the ask_user expiry timer is retained and cancelled when settled
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("resolution", ["answer", "cancel"])
def test_question_settlement_retires_its_application_timer(tmp_path: Path, resolution: str) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="expiry")
        row = _armed_question(app, session.id, "task_expiry")
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), app.state.question_events.loop).result(
            timeout=5
        )
        timer = app.state.question_events._timers[row.id]
        if resolution == "answer":
            response = client.post(
                f"/v1/sessions/{session.id}/questions/{row.id}/answer", json={"answer": "A"}
            )
        else:
            response = client.post(f"/v1/sessions/{session.id}/questions/{row.id}/cancel")
        assert response.status_code in {200, 204}, response.text
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), app.state.question_events.loop).result(
            timeout=5
        )
        assert timer.cancelled()
        assert row.id not in app.state.question_events._timers
        assert app.state.task_history.task_snapshot(row.id).terminal_at is not None


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

    from clio_agent.gact.a2ui import A2UIValidationError, validate_client_action
    from clio_agent.gact.a2ui_catalogs.validation import validate_value

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
            validate_value(
                {
                    "id": "b",
                    "component": "Button",
                    "action": {
                        "event": {
                            "name": "approval.respond",
                            "context": {"permission_id": "p", "action": "allow", **unsafe},
                        }
                    },
                },
                entry=None,
                max_depth=20,
                max_string=16 * 1024,
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
        "tool_call": {"tool_name": "fs_write_file", "input": {"path": "x"}},
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


def test_a2ui_approval_respond_still_resolves_its_own_session_permission(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S5 moved the ``permission`` destination's resolve_permission call out of
    # routes/a2ui.py into the owner dispatcher (docs/design/a2ui-compat-
    # campaign-2026-09.md S5) -- the seam to audit is now
    # gact.a2ui_actions.dispatcher, which imports resolve_permission directly
    # and calls it via run_off_loop from ``_deliver_permission``.
    from clio_agent.gact.a2ui_actions import dispatcher as a2ui_dispatcher

    original = a2ui_dispatcher.resolve_permission
    called_off_loop: list[bool] = []

    def audited_resolve(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            called_off_loop.append(True)
        else:
            called_off_loop.append(False)
        return original(*args, **kwargs)

    monkeypatch.setattr(a2ui_dispatcher, "resolve_permission", audited_resolve)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    owner = app.state.sessions.create(workspace_id="ws_default", title="owner")
    app.state.permissions["perm_other"] = {
        "id": "perm_other",
        "session_id": owner.id,
        "status": "pending",
        "summary": "Allow own write",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool_call": {"tool_name": "fs_write_file", "input": {"path": "x"}},
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
    assert called_off_loop == [True]


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
        "tool_call": {"tool_name": "fs_write_file", "input": {"path": "x"}},
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


def test_queue_promotion_authorizes_referenced_parts_exactly_once(tmp_path, monkeypatch) -> None:
    """The promote door resolves references off-loop ONCE.

    Before this fix, ``prepare_references`` authorized a queued message's
    ``context_ref`` parts off-loop, and the synchronous ``accept_message`` it
    handed off to re-authorized the SAME parts again on the loop thread -- the
    digest work F1 moved off-loop ran twice per promotion instead of once.

    The row is seeded directly through the store rather than the HTTP create
    route: the route's own ``redrive_queue`` call (an unconditional
    auto-promotion attempt, a no-op only while the session is busy) and any
    live-turn/busy-vs-idle machinery are both irrelevant to what this test
    checks. Earlier versions held a real turn open with a blocking fake agent
    to keep the session busy across the create+promote calls; under enough
    concurrent machine load that live turn's own executor scheduling proved
    unable to guarantee the row survived to the manual promote call (observed
    live, repeatedly, even with an unbounded wait -- a real race in the
    turn-scheduling machinery this test has no business depending on). Seeding
    through the store sidesteps that entirely: nothing calls
    ``redrive_queue``/``promote_queue_head`` before this test's own explicit
    promote, so the row's presence is a certainty, not a timing bet.
    """

    from clio_agent.gact import message_submission
    from clio_agent.gact.types import Part
    from tests.test_gact.task_records import record_composer_fixture
    from tests.test_gact.test_post_messages import FakeClioAgent

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent(answer="ok"))
    workspace_id, sid = _workspace_session(app, tmp_path, name="promote")
    (tmp_path / "promote" / "notes.txt").write_text("hello reference", encoding="utf-8")

    with TestClient(app) as client:
        listed = client.get(
            f"/v1/workspaces/{workspace_id}/references",
            params={"kinds": "workspace_file"},
            headers=HEADERS,
        ).json()["references"]
        row = next(item for item in listed if item["id"] == "notes.txt")

        app.state.sessions.update(sid, metadata_patch={"composer_queue_paused": True})
        record_composer_fixture(
            app,
            sid,
            "queued_ref",
            parts=[
                Part(type="text", text="look at this"),
                Part(
                    type="context_ref",
                    ref_kind="workspace_file",
                    ref_id="notes.txt",
                    revision=row["revision"],
                ),
            ],
            client_message_id="msg_ref_future",
        )

        calls: list[int] = []
        real_authorize = message_submission.authorize_context_reference_parts_sync

        def counting_authorize(app_, session_, parts_):
            calls.append(1)
            return real_authorize(app_, session_, parts_)

        monkeypatch.setattr(
            message_submission, "authorize_context_reference_parts_sync", counting_authorize
        )

        promoted = client.post(
            f"/v1/sessions/{sid}/queued-messages/queued_ref/promote",
            json={"revision": 1, "delivery": "auto"},
        )
        assert promoted.status_code == 200, promoted.text

    assert len(calls) == 1


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

    # A still-uploading resource is refused at the SAME gate the resource_ref
    # admission path uses, on both acceptance doors.
    uploading, _resumed = app.state.resource_store.create_or_resume(
        workspace_id=workspace_id, name="wip.md", declared_size=32
    )
    with pytest.raises(Exception) as refused:  # noqa: PT011 - HTTPException envelope
        authorize_context_reference_parts_sync(
            app,
            session,
            [Part(type="context_ref", ref_kind="resource", ref_id=uploading.id)],
        )
    assert refused.value.detail["error"]["error"] == "resource_not_ready"


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
    app.state.file_changes = {
        sid: [
            {
                "path": "src/app.py",
                "part_id": "d1",
                "message_id": "msg_1",
                "status": "applied",
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
    # The change is undone -- exactly what can happen between the send and the retry.
    app.state.file_changes[sid][0]["status"] = "reverted"

    enriched = enrich_with_context_references(app, sid, "retry me", message)

    assert "as-of snapshot" in enriched
    assert row["id"] in enriched
    assert '"status": "applied"' in enriched


# --------------------------------------------------------------------------- #
# Finding 7: an init failure releases parked inputs instead of stranding them
# --------------------------------------------------------------------------- #


def test_init_failure_retains_confirmed_inputs_for_later_admission(tmp_path: Path) -> None:
    from clio_agent.gact.agent_initialization import record_init_failure
    from clio_agent.gact.session_input import accept_session_input

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="deferred")
        app.state.agent = None
        identity = accept_session_input(
            app, session.id, "retained answer", metadata={"ask_user_resume": True}
        )
        original = app.state.task_history.mail_item(session.id, identity)
        record_init_failure(app, RuntimeError("provider is not configured"), stage="init")
        assert app.state.task_history.pending_mail(session.id) == (original,)
        assert identity not in app.state.task_history.acknowledged_notifications(session.id)
        assert app.state.agent_init_error == "RuntimeError('provider is not configured')"


def test_agent_ready_signals_the_existing_core_pending_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agent_initialization import mark_agent_ready, record_init_failure
    from clio_agent.gact.session_input import accept_session_input

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="rebind")
        app.state.agent = None
        identity = accept_session_input(app, session.id, "retained answer")
        record_init_failure(app, RuntimeError("not configured"), stage="init")
        signalled = threading.Event()
        monkeypatch.setattr(app.state.conversations, "signal_pending", signalled.set)
        mark_agent_ready(app, object())
        assert signalled.wait(5)
        assert identity in {item.id for item in app.state.task_history.pending_mail(session.id)}
        assert identity not in app.state.task_history.acknowledged_notifications(session.id)


def test_the_session_store_refuses_an_out_of_vocabulary_status(tmp_path) -> None:
    """``status`` is a closed vocabulary on the wire; the store never checked it.

    ``SessionStore.update`` guards ``mode``/``edit_mode``/``routing_mode``/
    ``approval_mode`` against their vocabularies and then assigns ``status``
    unchecked onto a plain dataclass -- so an invented status like ``"failed"``
    persists silently and only detonates at the WIRE boundary, where
    ``ListSessionsResponse`` validates ``types.Session``: one bad row 500s
    ``GET /v1/sessions`` for every OTHER session in the store. Refuse the write,
    and make a store that already holds a bad row still listable.
    """

    import json

    from clio_agent.gact.sessions import SessionStore

    path = tmp_path / "sessions.json"
    store = SessionStore(path=path)
    session = store.create(workspace_id="ws_default", title="vocabulary")

    with pytest.raises(ValueError, match="failed"):
        store.update(session.id, status="failed")
    assert store.get(session.id).status == "idle"
    # Every legal status still writes.
    assert store.update(session.id, status="error").status == "error"

    # A store that ALREADY holds a bad row (an older build, a hand-edited file)
    # must not take the listing down with it.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[session.id]["status"] = "failed"
    raw["sess_good"] = dict(raw[session.id], id="sess_good", status="idle")
    path.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = SessionStore(path=path)
    app = build_app(sessions_path=path)
    app.state.sessions = reloaded
    with TestClient(app) as client:
        listed = client.get("/v1/sessions")
        assert listed.status_code == 200, listed.text
        by_id = {row["id"]: row for row in listed.json()["sessions"]}
        assert set(by_id) == {session.id, "sess_good"}
        # An unreadable status resolves to ``error``, never to a healthier one.
        assert by_id[session.id]["status"] == "error"
        assert by_id["sess_good"]["status"] == "idle"


# --------------------------------------------------------------------------- #
# Finding 8: a terminally-failing queue head says so instead of looping
# --------------------------------------------------------------------------- #


def test_blocked_queue_head_retains_its_typed_failure_without_repeated_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from clio_agent.gact.routes.mailbox import promote_next
    from tests.test_gact.input_capture import capture_inputs
    from tests.test_gact.task_records import record_composer_fixture
    from tests.test_gact.test_post_messages import FakeClioAgent

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=FakeClioAgent())
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="blocked")
        capture_inputs(app, monkeypatch)
        attempts: list[str] = []

        def denied(*args: Any, **kwargs: Any) -> Any:
            attempts.append("preflight")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "context_ref_stale",
                    "message": "revision changed",
                    "details": {"actual_revision": "sha256:new"},
                },
            )

        monkeypatch.setattr("clio_agent.gact.message_submission.prepare_references_sync", denied)
        original = record_composer_fixture(app, session.id, "queued_head", text="read the file")
        promote_next(app, session.id)
        current = app.state.task_history.mail_item(session.id, original.id)
        assert current.delivery == "held"
        failure = app.state.task_history.read_content(current.content)["promotion_error"]
        assert failure["status_code"] == 409
        assert failure["detail"]["error"] == "context_ref_stale"
        assert failure["detail"]["details"]["actual_revision"] == "sha256:new"
        promote_next(app, session.id)
        promote_next(app, session.id)
        assert attempts == ["preflight"]
        assert app.state.task_history.mail_item(session.id, original.id) == current
        assert original.id not in app.state.task_history.acknowledged_notifications(session.id)


def test_editing_blocked_queue_head_keeps_original_evidence_and_releases_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_gact.input_capture import capture_inputs
    from tests.test_gact.task_records import record_composer_fixture

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="edit")
        capture_inputs(app, monkeypatch)
        original = record_composer_fixture(app, session.id, "queued_head", text="original")
        old_value = app.state.task_history.read_content(original.content)
        held = original.model_copy(
            update={
                "delivery": "held",
                "revision": 2,
                "content": app.state.task_history.write_content(
                    session.id,
                    {**old_value, "promotion_error": {"status_code": 409, "detail": "stale"}},
                ),
            }
        )
        app.state.task_history.update_mail(held, expected_revision=1)
        response = client.patch(
            f"/v1/sessions/{session.id}/queued-messages/queued_head",
            json={"revision": 2, "text": "revised"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["revision"] == 3
        assert response.json()["promotion_error"] is None
        current = app.state.task_history.mail_item(session.id, original.id)
        assert current.delivery == "queued"
        assert (
            app.state.task_history.read_content(original.content)["request"]["text"] == "original"
        )
        assert app.state.task_history.read_content(current.content)["request"]["text"] == "revised"


# --------------------------------------------------------------------------- #
# Finding 9: honest a2ui status, app-scoped MCP store, bounded projection
# --------------------------------------------------------------------------- #


def test_a2ui_surface_stops_reporting_pending_once_it_was_responded_to(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="surface")
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

    with TestClient(app) as client:
        client.post(
            f"/v1/sessions/{session.id}/a2ui/messages",
            headers=HEADERS,
            json={"messages": [create, components]},
        )
        before = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]
        assert [row["status"] for row in before if row["kind"] == "a2ui"] == ["pending"]

        responded = client.post(
            f"/v1/sessions/{session.id}/a2ui/actions",
            headers=HEADERS,
            json={"message": action},
        )
        assert responded.status_code == 200
        after = client.get(
            f"/v1/sessions/{session.id}/interactions",
            params={"include_recent_resolved": True},
        ).json()["interactions"]

    settled = [row for row in after if row["kind"] == "a2ui"]
    assert [row["status"] for row in settled] == ["answered"]
    assert settled[0]["payload"]["last_action"]["name"] == "form.submit"


def test_interactions_read_only_the_owning_app_core_history(tmp_path: Path) -> None:
    first = build_app(sessions_path=tmp_path / "first.json")
    second = build_app(sessions_path=tmp_path / "second.json")
    with TestClient(first) as first_client, TestClient(second) as second_client:
        left = first.state.sessions.create(workspace_id="ws_default", title="left")
        right = second.state.sessions.create(workspace_id="ws_default", title="right")
        q = _armed_question(first, left.id, "task_left", ttl_s=0)
        rows = first_client.get(f"/v1/sessions/{left.id}/interactions").json()["interactions"]
        assert any(row["id"] == "question:" + q.id for row in rows)
        assert (
            second_client.get(f"/v1/sessions/{right.id}/interactions").json()["interactions"] == []
        )
        # The apps share one clio-core store, and clio-core is the memory: the second
        # app reads the same task, yet nothing surfaces it in another session's list.
        shared = second.state.task_history.task_snapshot(q.id)
        assert shared == first.state.task_history.task_snapshot(q.id)


def test_interaction_projection_is_bounded(tmp_path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    set_config("gact.interactions.projection_limit", 3)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="many")
    for index in range(10):
        app.state.permissions[f"perm_{index}"] = {
            "id": f"perm_{index}",
            "session_id": session.id,
            "status": "pending",
            "summary": f"Allow {index}",
            "created_at": now,
            "tool_call": {"tool_name": "fs_write_file"},
        }

    with TestClient(app) as client:
        rows = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]

    assert len(rows) == 3


def test_core_question_history_retains_terminal_and_pending_rows(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="retained")
        handles = []
        for index in range(12):
            row = _armed_question(app, session.id, f"task_retained_{index}", ttl_s=0)
            handles.append(row.id)
            if index < 11:
                app.state.questions.answer(row.id, QuestionReply(answer=str(index)))
        assert all(app.state.task_history.task_snapshot(handle) is not None for handle in handles)
        assert app.state.task_history.task_snapshot(handles[-1]).raw_status == "pending"
        for index, handle in enumerate(handles[:-1]):
            task = app.state.task_history.task_snapshot(handle)
            assert QuestionReply.model_validate(
                app.state.task_history.read_content(task.result)
            ).answer == str(index)


# --------------------------------------------------------------------------- #
# Finding 10: the vocabulary names families that are actually emitted
# --------------------------------------------------------------------------- #


def test_provenance_kinds_classify_the_families_that_are_actually_emitted() -> None:
    from clio_agent.gact.provenance.normalization import _event_kind

    assert _event_kind("user_question.answered") == "interaction"
    assert _event_kind("permission.resolved") == "interaction"
    assert _event_kind("mcp_task.updated") == "interactive_work"
    assert _event_kind("a2ui.action.received") == "interactive_work"
    assert _event_kind("resource.ready") == "resource"
    # The four prefixes nothing emits no longer claim a classification, and the
    # ask-user family is not spelled ``question.`` (that is a v3 wire projection).
    for dead in (
        "interaction.opened",
        "mcp.task.updated",
        "context.reference.resolved",
        "evidence.added",
        "question.upserted",
    ):
        assert _event_kind(dead) == "event"


def test_bare_question_ids_no_longer_route_through_the_question_branch(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ids")
    with TestClient(app) as client:
        _armed_question(app, session.id, "q_bare")
        bare = client.post(
            f"/v1/sessions/{session.id}/interactions/q_bare/respond",
            headers=HEADERS,
            json={"action": "answer", "answer": "no"},
        )
        # The day-one /response alias is gone; only /respond is served.
        alias = client.post(
            f"/v1/sessions/{session.id}/interactions/question:q_bare/response",
            headers=HEADERS,
            json={"action": "answer", "answer": "no"},
        )
        prefixed = client.post(
            f"/v1/sessions/{session.id}/interactions/question:q_bare/respond",
            headers=HEADERS,
            json={"action": "answer", "answer": "yes"},
        )

    assert bare.status_code == 404
    assert alias.status_code == 404
    assert prefixed.status_code == 200
    assert question_row(app, "q_bare").status == "answered"


def test_submit_repair_attempt_is_declared_trace_only() -> None:
    from clio_agent.gact.semantic_events import (
        SSE_TRACE_ONLY_EVENT_TYPES,
        event_reaches_ui,
    )

    assert "agent.submit_repair.attempted" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("agent.submit_repair.attempted", "running") is False
    # Even a failed emit stays off the served wire.
    assert event_reaches_ui("agent.submit_repair.attempted", "failed") is False


def test_registering_interaction_routes_preserves_core_question_history(tmp_path: Path) -> None:
    from clio_agent.gact.routes.interactions import register_interaction_routes

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="retained")
        row = _armed_question(app, session.id, "task_route_registration", ttl_s=0)
        original = app.state.task_history.task_snapshot(row.id)
        revision = app.state.task_history.revision
        register_interaction_routes(app, app.state.gact_deps)
        assert app.state.task_history.revision == revision
        assert app.state.task_history.task_snapshot(row.id) == original


# --------------------------------------------------------------------------- #
# Finding 11: typed degradations instead of silent ones
# --------------------------------------------------------------------------- #


def test_inline_mcp_question_retains_identity_without_inventing_a_backend_task(
    tmp_path: Path,
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="mcp")
        owner = record_assignment_fixture(app, session.id).owner
        task = app.state.questions.create(
            owner,
            QuestionDefinition(
                prompt="Select format",
                source="mcp_elicitation",
                metadata={"elicitation": {"tool_name": "earthscope_query", "invocation_id": "c1"}},
            ),
            invocation_id="uncorrelated-question",
        )
        rows = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]
        row = next(row for row in rows if row["id"] == "question:" + task.handle)
        assert row["kind"] == "question"
        assert row["source"]["protocol"] == "mcp"
        assert row["payload"]["tool_name"] == "earthscope_query"
        assert row["payload"]["invocation_id"] == "c1"
        assert row["payload"]["task_handle"] == task.handle
        assert row["payload"].get("task_id") is None
        assert "degraded" not in row["payload"]


def test_unreadable_resource_store_surfaces_a_typed_discovery_degradation(tmp_path) -> None:
    from clio_agent.gact.context_reference_search import _resource_results

    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace = app.state.workspaces.create(name="ws", root_path=str(tmp_path / "ws"))

    class _Broken:
        def list(self, workspace_id: str):
            raise ValueError("index quarantined")

    app.state.resource_store = _Broken()

    assert _resource_results(app, workspace.id) == []
    degradations = app.state.reference_search_degradations[workspace.id]
    assert [row["reason"] for row in degradations] == ["resource_store_unreadable"]
    assert "index quarantined" in degradations[0]["detail"]

    # And it reaches the client, not just the log.
    with TestClient(app) as client:
        body = client.get(
            f"/v1/workspaces/{workspace.id}/references",
            params={"kinds": "resource"},
            headers=HEADERS,
        ).json()
    assert body["references"] == []
    assert [row["reason"] for row in body["degradations"]] == ["resource_store_unreadable"]


def test_content_addressed_duplicate_drop_is_reported(tmp_path) -> None:
    from types import SimpleNamespace

    from clio_agent.gact.context_reference_search import _resource_results

    app = build_app(sessions_path=tmp_path / "sessions.json")
    workspace = app.state.workspaces.create(name="ws", root_path=str(tmp_path / "ws"))
    digest = "a" * 64

    class _Store:
        def list(self, workspace_id: str):
            return [
                SimpleNamespace(
                    id="res_named",
                    name="paper.md",
                    sha256=digest,
                    revision="1",
                    detected_mime="text/markdown",
                ),
                SimpleNamespace(
                    id="res_hashed",
                    name=f"{digest}.md",
                    sha256=digest,
                    revision="1",
                    detected_mime="text/markdown",
                ),
            ]

    app.state.resource_store = _Store()

    rows = _resource_results(app, workspace.id)

    assert [row["id"] for row in rows] == ["res_named"]
    reasons = [row["reason"] for row in app.state.reference_search_degradations[workspace.id]]
    assert reasons == ["resource_content_addressed_name_hidden"]


# --------------------------------------------------------------------------- #
# Review finding: reference-discovery degradations must not leak or go stale
# --------------------------------------------------------------------------- #


def test_reference_discovery_degradations_are_scoped_and_reset_per_workspace(
    tmp_path,
) -> None:
    """``reference_search_degradations`` used to be one flat, never-reset,
    process-lifetime list, and the route served the WHOLE thing regardless of
    which workspace was searched: workspace B's client saw workspace A's
    degradation, and would keep seeing it forever -- even after A's store
    recovered, since nothing ever cleared the accumulator.
    """

    app = build_app(sessions_path=tmp_path / "sessions.json")
    broken_ws = app.state.workspaces.create(name="broken", root_path=str(tmp_path / "broken"))
    clean_ws = app.state.workspaces.create(name="clean", root_path=str(tmp_path / "clean"))

    class _SelectivelyBroken:
        healthy = False

        def list(self, workspace_id: str):
            if workspace_id == broken_ws.id and not self.healthy:
                raise ValueError("index quarantined")
            return []

    store = _SelectivelyBroken()
    app.state.resource_store = store

    with TestClient(app) as client:
        broken_body = client.get(
            f"/v1/workspaces/{broken_ws.id}/references",
            params={"kinds": "resource"},
            headers=HEADERS,
        ).json()
        clean_body = client.get(
            f"/v1/workspaces/{clean_ws.id}/references",
            params={"kinds": "resource"},
            headers=HEADERS,
        ).json()

        # Cross-workspace: the clean workspace must never see the broken one's
        # degradation, even though both searches ran against the SAME store.
        assert [row["reason"] for row in broken_body["degradations"]] == [
            "resource_store_unreadable"
        ]
        assert clean_body["degradations"] == []

        # Staleness: once the store recovers, a fresh search of the SAME
        # workspace must stop reporting the old failure rather than repeating it
        # forever.
        store.healthy = True
        recovered_body = client.get(
            f"/v1/workspaces/{broken_ws.id}/references",
            params={"kinds": "resource"},
            headers=HEADERS,
        ).json()
        assert recovered_body["degradations"] == []


@pytest.mark.parametrize("lifetime", [0, 90_000])
def test_ask_user_has_no_default_or_clamped_lifetime(tmp_path: Path, lifetime: int) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app):
        session = app.state.sessions.create(workspace_id="ws_default", title="expiry")
        row = _armed_question(app, session.id, "task_explicit_expiry", ttl_s=lifetime)
        task = app.state.task_history.task_snapshot(row.id)
        definition = QuestionDefinition.model_validate(
            app.state.task_history.read_content(task.details)
        )
        if lifetime == 0:
            assert definition.expires_at is None
        else:
            remaining = (
                datetime.fromisoformat(definition.expires_at) - datetime.now(timezone.utc)
            ).total_seconds()
            assert 89_990 < remaining <= 90_000
