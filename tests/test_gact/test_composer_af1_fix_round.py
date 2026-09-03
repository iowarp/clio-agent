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


def test_queue_promotion_authorizes_referenced_parts_exactly_once(
    tmp_path, monkeypatch
) -> None:
    """The promote door resolves references off-loop ONCE.

    Before this fix, ``prepare_references`` authorized a queued message's
    ``context_ref`` parts off-loop, and the synchronous ``accept_message`` it
    handed off to re-authorized the SAME parts again on the loop thread -- the
    digest work F1 moved off-loop ran twice per promotion instead of once.
    """

    import threading
    import time

    from clio_agent.gact import message_submission
    from tests.test_gact.test_post_messages import FakeClioAgent, FakePrediction

    # A wall-clock delay races the auto-promotion idle hook under machine load:
    # if the turn happens to settle before this test reaches the manual promote
    # call, composer_runtime's own idle-transition auto-promotion consumes the
    # queued row first, and the manual call 404s on a row that is already gone.
    # Holding the turn open on an Event the test releases itself makes "the turn
    # is still busy when we promote" a certainty, not a timing bet -- so
    # ``forward`` must NOT time out its own wait: a bounded wait reintroduces
    # exactly the race under enough load (observed live: a full-suite run slow
    # enough to blow a 10s ceiling let the held turn settle on its own, and the
    # queue's own auto-promotion consumed the row before the manual call ran).
    # The test's own ``finally`` is what prevents an actual hang, not this wait.
    release_turn = threading.Event()

    class HoldingAgent(FakeClioAgent):
        def forward(self, question: str, session_id: str) -> object:
            self.calls.append((question, session_id))
            release_turn.wait()
            return FakePrediction(answer=self.answer)

    app = build_app(sessions_path=tmp_path / "sessions.json", agent=HoldingAgent())
    workspace_id, sid = _workspace_session(app, tmp_path, name="promote")
    (tmp_path / "promote" / "notes.txt").write_text("hello reference", encoding="utf-8")

    calls: list[int] = []
    real_authorize = message_submission.authorize_context_reference_parts_sync

    def counting_authorize(app_, session_, parts_):
        calls.append(1)
        return real_authorize(app_, session_, parts_)

    monkeypatch.setattr(
        message_submission, "authorize_context_reference_parts_sync", counting_authorize
    )

    try:
        with TestClient(app) as client:
            started = client.post(
                f"/v1/sessions/{sid}/messages",
                json={"parts": [{"type": "text", "text": "hold the slot"}]},
            )
            assert started.status_code == 200, started.text
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and not app.state.turn_runner.busy(sid):
                time.sleep(0.02)
            assert app.state.turn_runner.busy(sid)

            listed = client.get(
                f"/v1/workspaces/{workspace_id}/references",
                params={"kinds": "workspace_file"},
                headers=HEADERS,
            ).json()["references"]
            row = next(item for item in listed if item["id"] == "notes.txt")

            created = client.post(
                f"/v1/sessions/{sid}/queued-messages",
                json={
                    "parts": [
                        {"type": "text", "text": "look at this"},
                        {
                            "type": "context_ref",
                            "ref_kind": "workspace_file",
                            "ref_id": "notes.txt",
                            "revision": row["revision"],
                        },
                    ],
                    "client_message_id": "msg_ref_future",
                    "idempotency_key": "queue-ref-1",
                },
            )
            assert created.status_code == 201, created.text
            queued = created.json()

            # The turn must still be busy: promotion is meant to run through the
            # explicit manual door below, not the idle-hook auto-promoter.
            assert app.state.turn_runner.busy(sid)

            # Isolate the PROMOTE call's own authorization count; queueing
            # legitimately authorized the reference once already, at a different
            # point in time.
            calls.clear()
            promoted = client.post(
                f"/v1/sessions/{sid}/queued-messages/{queued['id']}/promote",
                json={"revision": queued["revision"], "delivery": "auto"},
            )
            assert promoted.status_code == 200, promoted.text
    finally:
        # Guaranteed even on assertion failure: never leave the held turn (and
        # its blocked thread) hanging past this test.
        release_turn.set()

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


# --------------------------------------------------------------------------- #
# Finding 7: an init failure releases parked inputs instead of stranding them
# --------------------------------------------------------------------------- #


def test_init_failure_refuses_inputs_parked_for_an_agent_that_never_arrives(
    tmp_path,
) -> None:
    """A restart-shaped scenario: the answer landed, then construction failed."""

    from clio_agent.gact.agent_initialization import record_init_failure
    from clio_agent.gact.loop_inbox import enqueue_user_steer

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="stranded")
    app.state.agent = None
    enqueue_user_steer(
        app,
        session.id,
        "the answer the user already gave",
        {"question_id": "q_parked", "ask_user_resume": True},
    )
    app.state.sessions.update(session.id, status="idle")
    refusals: list[dict[str, object]] = []
    original_publish = app.state.bus.publish

    def capture(event):
        if event.type == "session.input_refused":
            refusals.append(dict(event.payload))
        return original_publish(event)

    app.state.bus.publish = capture

    record_init_failure(app, RuntimeError("provider is not configured"), stage="init")

    assert len(refusals) == 1
    assert refusals[0]["reason"] == "agent_init_failed"
    assert refusals[0]["question_id"] == "q_parked"
    assert refusals[0]["recoverable"] is False
    assert app.state.sessions.get(session.id).status == "error"
    assert not app.state.loop_inboxes[session.id].peek_nonempty()


def test_mark_agent_ready_drains_through_the_running_loop(tmp_path) -> None:
    """The production branch (loop.call_soon), not the monkeypatched drain."""

    import asyncio

    from clio_agent.gact.agent_initialization import mark_agent_ready
    from clio_agent.gact.loop_inbox import enqueue_user_steer

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="deferred")
    app.state.agent = None
    enqueue_user_steer(app, session.id, "deferred answer", {"ask_user_resume": True})

    async def drive() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        app.state.turn_runner.bind_loop(app.state.mcp_app_loop)
        mark_agent_ready(app, object())
        # call_soon schedules on the loop; one yield runs it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(drive())

    assert app.state.agent is not None
    # The drain ran on the loop and promoted the parked steer into a real turn.
    assert not app.state.loop_inboxes[session.id].peek_nonempty()


# --------------------------------------------------------------------------- #
# Finding 8: a terminally-failing queue head says so instead of looping
# --------------------------------------------------------------------------- #


def test_queue_head_that_fails_terminally_reports_its_typed_cause_once(
    tmp_path, monkeypatch
) -> None:
    from fastapi import HTTPException

    from clio_agent.gact import composer_runtime, message_submission
    from clio_agent.gact.message_intents import QueuedMessage
    from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Part

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="queue")
    app.state.message_intents.create_queued(
        QueuedMessage(
            id="queued_head",
            session_id=session.id,
            parts=[Part(type="text", text="stale reference")],
        )
    )

    def stale_accept(*_args, **_kwargs):
        raise HTTPException(
            status_code=409,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="context_ref_stale",
                    message="workspace file changed after the reference was selected",
                    details={
                        "requested_revision": "sha256:old",
                        "actual_revision": "sha256:new",
                    },
                    recoverable=True,
                )
            ).model_dump(exclude_none=True),
        )

    monkeypatch.setattr(message_submission, "accept_message", stale_accept)
    published: list[tuple[str, dict[str, object]]] = []
    original_publish = app.state.bus.publish

    def capture(event):
        published.append((event.type, dict(event.payload)))
        return original_publish(event)

    app.state.bus.publish = capture

    composer_runtime.promote_queue_head(app, object(), session.id)
    first = list(published)
    composer_runtime.promote_queue_head(app, object(), session.id)
    composer_runtime.promote_queue_head(app, object(), session.id)

    failures = [row for row in first if row[0] == "queued_message.promotion_failed"]
    assert len(failures) == 1
    payload = failures[0][1]
    assert payload["cause"]["error"] == "context_ref_stale"
    assert payload["cause"]["status_code"] == 409
    assert payload["cause"]["details"]["actual_revision"] == "sha256:new"
    assert payload["recoverable"] is False
    assert "retry_on" not in payload
    blocked = [row for row in first if row[0] == "queued_message.head_blocked"]
    assert len(blocked) == 1
    assert blocked[0][1]["queued_message_id"] == "queued_head"
    assert blocked[0][1]["recovery_actions"] == [
        "edit_queued_message",
        "delete_queued_message",
    ]
    # Re-drives do NOT re-attempt the same head at the same revision.
    assert published == first
    # Nothing was deleted: the row is still there for the client to edit.
    assert [row.id for row in app.state.message_intents.list_queued(session.id)] == ["queued_head"]
    assert composer_runtime.blocked_queue_head(app, session.id)["queued_message_id"] == (
        "queued_head"
    )


def test_editing_a_blocked_queue_head_unfreezes_the_queue(tmp_path, monkeypatch) -> None:
    from fastapi import HTTPException

    from clio_agent.gact import composer_runtime, message_submission
    from clio_agent.gact.message_intents import QueuedMessage
    from clio_agent.gact.types import Part, PostMessageResponse

    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="queue")
    app.state.message_intents.create_queued(
        QueuedMessage(
            id="queued_head",
            session_id=session.id,
            parts=[Part(type="text", text="stale reference")],
        )
    )
    monkeypatch.setattr(
        message_submission,
        "accept_message",
        lambda *_a, **_k: (_ for _ in ()).throw(HTTPException(status_code=409, detail={})),
    )
    composer_runtime.promote_queue_head(app, object(), session.id)
    assert composer_runtime.blocked_queue_head(app, session.id) is not None

    accepted: list[str] = []

    def good_accept(*_args, **_kwargs):
        accepted.append("promoted")
        return (
            PostMessageResponse(
                message_id="msg_1", accepted_at="now", delivery="start", state="started"
            ),
            200,
        )

    monkeypatch.setattr(message_submission, "accept_message", good_accept)
    app.state.message_intents.update_queued(
        session.id,
        "queued_head",
        1,
        parts=[Part(type="text", text="fixed")],
    )
    composer_runtime.promote_queue_head(app, object(), session.id)

    assert accepted == ["promoted"]
    assert composer_runtime.blocked_queue_head(app, session.id) is None


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
        after = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]

    settled = [row for row in after if row["kind"] == "a2ui"]
    assert [row["status"] for row in settled] == ["answered"]
    assert settled[0]["payload"]["last_action"]["name"] == "form.submit"


def test_interaction_projection_reads_this_apps_task_store_not_the_process_global(
    tmp_path,
) -> None:
    """Two apps in one process: the newest SessionStore owns the module global."""

    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord, resolve_store

    first = build_app(sessions_path=tmp_path / "first.json")
    first_session = first.state.sessions.create(workspace_id="ws_default", title="first")
    first.state.sessions.task_store.put(
        TaskRecord(
            key=TaskKey(server_id="srv", session_id=first_session.id, task_id="task_first"),
            tool="earthscope_query",
            status="input_required",
        )
    )
    # Building a SECOND app republishes the process-global onto its own registry.
    second = build_app(sessions_path=tmp_path / "second.json")
    assert resolve_store(None) is second.state.sessions.task_store

    with TestClient(first) as client:
        rows = client.get(f"/v1/sessions/{first_session.id}/interactions").json()["interactions"]

    assert [row["id"] for row in rows if row["kind"] == "mcp_task_input"] == [
        "mcp_task_input:srv:task_first"
    ]


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
            "tool_call": {"tool_name": "fs_apply_edit_write"},
        }

    with TestClient(app) as client:
        rows = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]

    assert len(rows) == 3


def test_user_questions_ledger_evicts_terminal_rows_before_pending_ones(tmp_path) -> None:
    from clio_agent.gact.runtime import retention
    from clio_agent.gact.user_question_ledger import record_user_question

    set_config("gact.ledger_retention.user_questions.max", 2)
    set_config("gact.ledger_retention.user_questions.hard", 3)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="ledger")
    rebuilt = retention.build_ledger_bounds()
    retention.LEDGER_BOUNDS["user_questions"] = rebuilt["user_questions"]

    now = datetime.now(timezone.utc).isoformat()
    for index in range(4):
        record_user_question(
            app,
            UserQuestion(
                id=f"q_{index}",
                session_id=session.id,
                prompt=f"question {index}",
                status="answered" if index < 3 else "pending",
                created_at=now,
                updated_at=now,
            ),
        )

    assert "q_3" in app.state.user_questions
    assert len(app.state.user_questions) <= 3
    reasons = [row["reason"] for row in app.state.ledger_evictions]
    assert reasons and all(reason.startswith("capacity_") for reason in reasons)
    assert all(row["ledger"] == "user_questions" for row in app.state.ledger_evictions)


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
    _armed_question(app, session.id, "q_bare")

    with TestClient(app) as client:
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
    assert app.state.user_questions["q_bare"].status == "answered"


def test_submit_repair_attempt_is_declared_trace_only() -> None:
    from clio_agent.gact.semantic_events import (
        SSE_TRACE_ONLY_EVENT_TYPES,
        event_reaches_ui,
    )

    assert "agent.submit_repair.attempted" in SSE_TRACE_ONLY_EVENT_TYPES
    assert event_reaches_ui("agent.submit_repair.attempted", "running") is False
    # Even a failed emit stays off the served wire.
    assert event_reaches_ui("agent.submit_repair.attempted", "failed") is False


def test_registering_interaction_routes_does_not_mutate_live_state(tmp_path) -> None:
    """Route registration is wiring; crash recovery is app assembly."""

    import inspect

    from clio_agent.gact.routes import interactions

    source = inspect.getsource(interactions.register_interaction_routes)
    assert "restore_pending_ask_user_questions" not in source
    # ...but build_app still restores, so the behaviour did not move out of reach.
    sessions_path = tmp_path / "sessions.json"
    first = build_app(sessions_path=sessions_path)
    session = first.state.sessions.create(workspace_id="ws_default", title="restart")
    now = datetime.now(timezone.utc)
    first.state.sessions.update(
        session.id,
        status="waiting_user",
        metadata_patch={
            "pending_user_question_id": "q_restored",
            "pending_ask_user": {
                "action": "ask_user",
                "question": "Which system?",
                "kind": "freeform",
                "choices": [],
                "allow_freeform": True,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
                "owner_session_id": session.id,
                "attended_session_id": session.id,
                "caller": {"agent_id": "main"},
                "surfaced": True,
                "question_id": "q_restored",
            },
        },
    )

    restarted = build_app(sessions_path=sessions_path)

    assert "q_restored" in restarted.state.user_questions


# --------------------------------------------------------------------------- #
# Finding 11: typed degradations instead of silent ones
# --------------------------------------------------------------------------- #


def test_uncorrelated_mcp_question_states_its_kind_downgrade(tmp_path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    session = app.state.sessions.create(workspace_id="ws_default", title="mcp")
    now = datetime.now(timezone.utc).isoformat()
    app.state.user_questions["q_mcp"] = UserQuestion(
        id="q_mcp",
        session_id=session.id,
        owner_session_id=session.id,
        attended_session_id=session.id,
        prompt="Select format",
        source="mcp_elicitation",
        created_at=now,
        updated_at=now,
        metadata={"elicitation": {"tool_name": "earthscope_query", "invocation_id": "c1"}},
    )

    with TestClient(app) as client:
        rows = client.get(f"/v1/sessions/{session.id}/interactions").json()["interactions"]

    row = next(item for item in rows if item["id"] == "question:q_mcp")
    assert row["kind"] == "question"
    assert row["payload"]["degraded"] == {
        "reason": "mcp_task_correlation_missing",
        "declared_kind": "mcp_task_input",
        "projected_kind": "question",
    }


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
    reasons = [
        row["reason"] for row in app.state.reference_search_degradations[workspace.id]
    ]
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


def test_ask_user_ttl_default_and_clamp_are_config_resolved() -> None:
    from clio_agent.gact import ask_user_tool

    set_config("gact.ask_user.ttl_s", 42)
    set_config("gact.ask_user.max_ttl_s", 120)
    assert ask_user_tool.ask_user_ttl_bounds() == (42, 120)
