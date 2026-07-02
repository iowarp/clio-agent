"""Workspace CRUD through the SDK (SPEC §6.1)."""

from __future__ import annotations

import pytest

from clio_agent.sdk import ClioClient, NotFoundError, Workspace


def test_workspace_round_trip(client: ClioClient, tmp_path) -> None:
    listed = client.workspaces.list()
    assert "ws_default" in [w.id for w in listed], "the implicit default workspace exists"

    created = client.workspaces.create("proj", root_path=str(tmp_path), metadata={"k": "v"})
    assert isinstance(created, Workspace)
    assert created.id.startswith("ws_")
    assert created.name == "proj"
    assert created.storage_root, "workspace rows always carry a derived storage_root"

    fetched = client.workspaces.get(created.id)
    assert fetched.name == "proj"
    assert fetched.metadata.get("k") == "v"

    renamed = client.workspaces.update(created.id, name="proj2", metadata={"extra": 1})
    assert renamed.name == "proj2"
    # PATCH merges metadata — no key removal (SPEC §6.1).
    assert renamed.metadata.get("k") == "v"
    assert renamed.metadata.get("extra") == 1

    client.workspaces.delete(created.id)
    with pytest.raises(NotFoundError):
        client.workspaces.get(created.id)


def test_sessions_scope_to_workspace(client: ClioClient, tmp_path) -> None:
    ws = client.workspaces.create("scoped", root_path=str(tmp_path))
    sess = client.sessions.create(workspace_id=ws.id, title="scoped session")

    assert sess.workspace_id == ws.id
    scoped_ids = [s.id for s in client.sessions.list(workspace_id=ws.id)]
    assert scoped_ids == [sess.id]
    default_ids = [s.id for s in client.sessions.list()]
    assert sess.id not in default_ids, "default listing is ws_default-scoped"
    all_ids = [s.id for s in client.sessions.list(include_all_workspaces=True)]
    assert sess.id in all_ids
