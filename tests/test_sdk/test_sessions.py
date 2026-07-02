"""Session lifecycle round-trip through the SDK (SPEC §6.2)."""

from __future__ import annotations

import pytest

from clio_agent.sdk import ClioClient, NotFoundError, Session


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
