"""Session lifecycle round-trip through the SDK (SPEC §6.2)."""

from __future__ import annotations

import pytest

from clio_agent.sdk import ClioClient, NotFoundError, Session, UserQuestion


def test_session_lifecycle_round_trip(client: ClioClient) -> None:
    created = client.sessions.create(title="sdk lifecycle", mode="plan", routing_mode="chat")

    assert isinstance(created, Session)
    assert created.id.startswith("sess_")
    assert created.workspace_id == "ws_default"
    assert created.title == "sdk lifecycle"
    assert created.mode == "plan"
    assert created.routing_mode == "chat"
    assert created.status == "idle"

    fetched = client.sessions.get(created.id)
    assert fetched.id == created.id
    assert fetched.title == "sdk lifecycle"

    listed = client.sessions.list()
    assert created.id in [s.id for s in listed]

    # PATCH: partial update; metadata merges shallowly server-side.
    patched = client.sessions.update(created.id, title="renamed", metadata={"pinned": True})
    assert patched.title == "renamed"
    assert patched.metadata.get("pinned") is True
    patched_again = client.sessions.update(created.id, metadata={"other": 1})
    assert patched_again.metadata.get("pinned") is True, "metadata must merge, not replace"
    assert patched_again.metadata.get("other") == 1

    # Archive: hidden from the active list, visible via archived=True.
    archived = client.sessions.update(created.id, archived=True)
    assert archived.archived is True
    assert created.id not in [s.id for s in client.sessions.list()]
    assert created.id in [s.id for s in client.sessions.list(archived=True)]

    client.sessions.delete(created.id)
    with pytest.raises(NotFoundError):
        client.sessions.get(created.id)


def test_session_fork_sets_parent_and_store_defaults(client: ClioClient) -> None:
    parent = client.sessions.create(title="parent", mode="plan", edit_mode="whole")

    fork = client.sessions.fork(parent.id, title="the fork")

    assert fork.id != parent.id
    assert fork.parent_session_id == parent.id
    assert fork.title == "the fork"
    # SPEC §6.2: modes are NOT inherited — the fork gets store defaults.
    assert fork.mode == "chat"
    assert fork.edit_mode == "diff"

    # Both remain listed and independently fetchable.
    ids = [s.id for s in client.sessions.list()]
    assert parent.id in ids and fork.id in ids


def test_fork_unknown_session_raises_not_found(client: ClioClient) -> None:
    with pytest.raises(NotFoundError):
        client.sessions.fork("sess_missing")


def test_questions_and_answer_round_trip(client: ClioClient) -> None:
    """The ask-user question surface round-trips through the SDK: a seeded
    pending question is listed, answered, and then no longer pending."""

    sess = client.sessions.create(title="ask-user round trip")
    # Seed a pending question the way the orchestrator would (no
    # resume_on_answer, so answering just settles the session — no agent
    # needed for this SDK-level check).
    created = client._request(
        "POST",
        f"/v1/sessions/{sess.id}/questions",
        json={"prompt": "Which dataset should I analyze?", "kind": "freeform"},
    )
    assert created.status_code == 201

    pending = client.sessions.questions(sess.id, status="pending")
    assert len(pending) == 1
    assert isinstance(pending[0], UserQuestion)
    assert pending[0].prompt == "Which dataset should I analyze?"
    assert pending[0].status == "pending"

    answered = client.sessions.answer_question(sess.id, pending[0].id, answer="the HDF5 one")
    assert isinstance(answered, UserQuestion)
    assert answered.status == "answered"
    assert answered.answer == "the HDF5 one"

    assert client.sessions.questions(sess.id, status="pending") == []


def test_answer_question_with_option_selection(client: ClioClient) -> None:
    """``option_id`` is sent as a selected option and validated server-side."""

    sess = client.sessions.create(title="choice question")
    created = client._request(
        "POST",
        f"/v1/sessions/{sess.id}/questions",
        json={
            "prompt": "Pick a compressor",
            "kind": "choice",
            "options": [
                {"label": "GZIP", "value": "gzip"},
                {"label": "LZF", "value": "lzf"},
            ],
        },
    )
    assert created.status_code == 201
    qid = created.json()["id"]

    answered = client.sessions.answer_question(sess.id, qid, option_id="lzf")
    assert answered.status == "answered"
    assert "lzf" in answered.selected_options
