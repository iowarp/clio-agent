"""Long-term memory in a running server: tools, policy, snapshot, routes and switches."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.memory import tools as memory_tools
from clio_agent.gact.memory.policy import (
    SESSION_MEMORY_OFF,
    session_policy,
    withheld_memory_tools,
)
from clio_agent.gact.memory.snapshot import MARKER, session_snapshot


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as started:
        yield started


@pytest.fixture(autouse=True)
def routing(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The routing model call, answered deterministically (a new document per subject)."""
    calls: list[dict[str, Any]] = []

    def route_fact(**kwargs: Any) -> tuple[str, str]:
        calls.append(kwargs)
        existing = [name for name, _ in kwargs["documents"]]
        slug = kwargs["subject"].lower().replace(" ", "-")
        return (slug, "") if slug in existing else (slug, f"About {kwargs['subject']}")

    monkeypatch.setattr("clio_agent.gact.memory.routing.route_fact", route_fact)
    return calls


def _in_session(app: Any, sid: str) -> list[Any]:
    return [context.set_app(app), context.set_session_id(sid)]


def _leave(tokens: list[Any]) -> None:
    for token in reversed(tokens):
        context.reset(token)


def _root(app: Any, **metadata: Any) -> Any:
    return app.state.sessions.create(workspace_id="ws_global", title="t", metadata=metadata)


def test_remember_correct_and_forget_in_a_root_conversation(client: TestClient) -> None:
    app = client.app
    session = _root(app)
    tokens = _in_session(app, session.id)
    try:
        created = memory_tools.create_memory(
            "Frontier allocation", "My Frontier project id is CSC123", "environments"
        )
        name = created["memory"]["name"]
        assert name == "user/environments/frontier-allocation"
        assert created["fact"]["text"] == "My Frontier project id is CSC123"
        hits = memory_tools.memory_search("Frontier project id")["hits"]
        assert [hit["name"] for hit in hits] == [name]
        fact_id = created["fact"]["id"]

        updated = memory_tools.update_memory(fact_id, "My Frontier project id is CSC456")
        read = memory_tools.memory_read(name)
        assert [(f["text"], f["live"]) for f in read["facts"]] == [
            ("My Frontier project id is CSC123", False),
            ("My Frontier project id is CSC456", True),
        ]
        forgotten = memory_tools.forget_memory(fact_id=updated["fact"]["id"])
        # The correction's earlier value goes too, and the emptied document with it.
        assert forgotten["forgotten"] == [fact_id, updated["fact"]["id"]]
        assert memory_tools.memory_search("Frontier project id")["hits"] == []
    finally:
        _leave(tokens)
    assert client.get(f"/v1/memory/documents/{name}").status_code == 404
    versions = client.get(f"/v1/memory/documents/{name}/versions").json()["versions"]
    assert len(versions) == 3 and versions[0]["author"]["kind"] == "tool_call"
    profile = client.get("/v1/memory/documents/user/profile/profile").json()
    assert {fact_id, updated["fact"]["id"]} <= set(profile["forgotten"])


def test_a_delegated_child_reads_but_never_writes(client: TestClient) -> None:
    app = client.app
    root = _root(app)
    child = app.state.sessions.create(
        workspace_id="ws_global",
        title="child",
        parent_session_id=root.id,
        agent={"id": "explore"},
        metadata={"session_type": "agent_task"},
    )
    assert session_policy(app, root).may_write
    assert not session_policy(app, child).may_write
    tokens = _in_session(app, child.id)
    try:
        withheld = withheld_memory_tools({"memory_search", "create_memory", "forget_memory"})
        assert withheld == {
            "create_memory": "memory_write_root_only",
            "forget_memory": "memory_write_root_only",
        }
        with pytest.raises(memory_tools.MemoryToolError) as caught:
            memory_tools.create_memory("x", "y", "topics")
        assert caught.value.reason == "memory_write_root_only"
        assert memory_tools.memory_search("anything")["hits"] == []
    finally:
        _leave(tokens)


def test_a_fork_of_a_root_conversation_may_write(client: TestClient) -> None:
    app = client.app
    root = _root(app)
    fork = app.state.sessions.create(
        workspace_id="ws_global",
        title="fork",
        parent_session_id=root.id,
        agent={"id": "main"},
        metadata={"session_kind": "branch", "fork_origin": {"session_id": root.id}},
    )
    assert session_policy(app, fork).may_write


def test_the_snapshot_is_pinned_and_carries_profile_preferences_and_an_index(
    client: TestClient,
) -> None:
    app = client.app
    writer = _root(app)
    tokens = _in_session(app, writer.id)
    try:
        memory_tools.create_memory("prefs", "Prefers SI units", "preferences")
        memory_tools.create_memory("HDF5 paper", "Draft due in November", "areas")
    finally:
        _leave(tokens)
    session = _root(app)
    snapshot = session_snapshot(app, session)
    assert snapshot is not None and snapshot["text"].startswith(MARKER)
    assert "Prefers SI units" in snapshot["text"]
    assert "user/areas/hdf5-paper (areas): About HDF5 paper" in snapshot["text"]
    assert snapshot["loaded"] == ["user/preferences/preferences"]
    pinned = app.state.sessions.get(session.id).metadata["memory_snapshot"]
    assert pinned == snapshot
    tokens = _in_session(app, writer.id)
    try:
        memory_tools.create_memory("prefs", "Prefers short answers", "preferences")
    finally:
        _leave(tokens)
    assert session_snapshot(app, app.state.sessions.get(session.id)) == snapshot  # pinned


def test_memory_off_withholds_the_tools_and_the_snapshot(client: TestClient) -> None:
    app = client.app
    session = _root(app)
    assert client.put(f"/v1/sessions/{session.id}/memory", json={"off": True}).json() == {
        "memory_off": True
    }
    refreshed = app.state.sessions.get(session.id)
    assert refreshed.metadata[SESSION_MEMORY_OFF] is True
    assert session_snapshot(app, refreshed) is None
    tokens = _in_session(app, session.id)
    try:
        assert withheld_memory_tools({"memory_search", "session_search"}) == {
            "memory_search": "session_memory_off",
            "session_search": "session_memory_off",
        }
    finally:
        _leave(tokens)
    again = client.put(f"/v1/sessions/{session.id}/memory", json={"off": False})
    assert again.status_code == 409 and again.json()["error"]["error"] == "memory_off_is_final"


def test_a_workspace_without_account_memory_sees_only_its_project(client: TestClient) -> None:
    app = client.app
    workspace = app.state.workspaces.create(name="lab", root_path=str(Path.cwd()))
    resp = client.put(f"/v1/workspaces/{workspace.id}/memory", json={"use_account_memory": False})
    assert resp.json() == {"use_account_memory": False}
    session = app.state.sessions.create(workspace_id=workspace.id, title="t")
    assert session_policy(app, session).scopes == (f"project/{workspace.id}",)


def test_the_memory_page_edits_facts_with_a_hash_precondition(client: TestClient) -> None:
    app = client.app
    session = _root(app)
    tokens = _in_session(app, session.id)
    try:
        created = memory_tools.create_memory("telescopes", "Uses the VLA", "topics")
    finally:
        _leave(tokens)
    name = created["memory"]["name"]
    doc = client.get(f"/v1/memory/documents/{name}").json()
    fact_id = doc["facts"][0]["id"]
    stale = client.post(
        f"/v1/memory/documents/{name}/facts/{fact_id}",
        json={"fact": "Uses ALMA", "expected_sha": "0" * 64},
    )
    assert stale.status_code == 409 and stale.json()["error"]["error"] == "memory_conflict"
    edited = client.post(
        f"/v1/memory/documents/{name}/facts/{fact_id}",
        json={"fact": "Uses ALMA", "expected_sha": doc["sha"]},
    )
    assert edited.status_code == 200, edited.text
    live = [f["text"] for f in edited.json()["facts"] if f["live"]]
    assert live == ["Uses ALMA"]
    listed = client.get("/v1/memory/documents").json()["documents"]
    assert name in [row["name"] for row in listed]
    summary = client.get("/v1/memory/summary").json()
    assert summary["enabled"] is True and summary["inbox"] == 0
    assert client.get("/v1/memory/search", params={"q": "ALMA"}).json()["hits"][0]["name"] == name


def test_a_withheld_write_tool_takes_its_prompt_fragment_along(client: TestClient) -> None:
    from clio_agent.gact.agents.resolution import session_agent_def

    app = client.app
    root = _root(app)
    child = app.state.sessions.create(
        workspace_id="ws_global",
        title="child",
        parent_session_id=root.id,
        agent={"id": "explore"},
        metadata={"session_type": "agent_task"},
    )
    assert "create_memory" in session_agent_def(app, root.id).system_prompt
    assert "create_memory" not in session_agent_def(app, child.id).system_prompt


def test_a_fork_keeps_memory_as_of_the_fork(client: TestClient) -> None:
    app = client.app
    writer = _root(app)
    tokens = _in_session(app, writer.id)
    try:
        memory_tools.create_memory("prefs", "Prefers SI units", "preferences")
    finally:
        _leave(tokens)
    root = _root(app)
    snapshot = session_snapshot(app, root)
    from clio_agent.gact.parts import Part
    from clio_agent.gact.session_store import _append_session_message
    from clio_agent.gact.types import Message

    stamp = "2026-10-20T10:00:00Z"
    _append_session_message(
        app,
        root.id,
        Message(
            id="msg_fork_1",
            session_id=root.id,
            role="user",
            created_at=stamp,
            updated_at=stamp,
            parts=[Part(id="p1", type="text", text="hello")],
        ),
    )
    forked = client.post(f"/v1/sessions/{root.id}/fork", json={})
    assert forked.status_code == 201, forked.text
    fork = app.state.sessions.get(forked.json()["id"])
    assert fork.metadata["memory_snapshot"] == snapshot
    assert session_policy(app, fork).may_write


def test_an_answer_names_the_memory_it_used_on_the_public_message() -> None:
    from clio_agent.gact.protocol.v3.message import _message_presentation_metadata

    projected = _message_presentation_metadata(
        {"memory_used": {"read": ["user/topics/hdf5"], "loaded": []}, "tools_called": [{}]}
    )
    assert projected == {"memory_used": {"read": ["user/topics/hdf5"], "loaded": []}}
    assert _message_presentation_metadata({"memory_used": {"read": [], "loaded": []}}) == {}


def test_the_curator_is_never_offered_for_delegation(client: TestClient) -> None:
    from clio_agent.gact.agents.resolution import session_resolution, spawn_targets
    from clio_agent.gact.spawn_context import resolve_child_agent

    app = client.app
    root = _root(app)
    resolution = session_resolution(app, root.id)
    assert "curator" in [row["id"] for row in resolution.subagents]  # it still resolves
    assert "curator" not in [target["id"] for target in spawn_targets(resolution)]
    with pytest.raises(ValueError, match="neither a subagent"):
        resolve_child_agent(app, root.id, "curator")


def test_the_turn_path_withholds_tools_and_their_guidance(client: TestClient) -> None:
    from clio_agent.gact.agents.prompt_basis import turn_agent_def
    from clio_agent.gact.agents.resolution import session_resolution, withhold_session_tools

    app = client.app
    root = _root(app)
    child = app.state.sessions.create(
        workspace_id="ws_global",
        title="child",
        parent_session_id=root.id,
        agent={"id": "explore"},
        metadata={"session_type": "agent_task"},
    )
    for session, kept, gone in (
        (root, {"create_memory", "memory_search"}, {"propose_memory_change"}),
        (child, {"memory_search"}, {"create_memory", "propose_memory_change"}),
    ):
        resolution = session_resolution(app, session.id)
        turn_def, built = turn_agent_def(app, session.id, resolution)
        agent = withhold_session_tools(app, session.id, turn_def, resolution, built)
        assert kept <= set(agent.tools) and not gone & set(agent.tools)
        assert ("create_memory" in agent.system_prompt) == ("create_memory" in kept)


def test_a_scope_is_read_exactly_and_one_document_exports_alone(client: TestClient) -> None:
    import io
    import zipfile

    app = client.app
    session = _root(app)
    tokens = _in_session(app, session.id)
    try:
        memory_tools.create_memory("telescopes", "Uses the VLA", "topics")
        memory_tools.create_memory("cluster", "Runs on Polaris", "environments")
    finally:
        _leave(tokens)
    bad = client.get("/v1/memory/documents", params={"scope": "project:ws_x"})
    assert bad.status_code == 400 and bad.json()["error"]["error"] == "memory_scope_invalid"
    one = client.get("/v1/memory/export", params={"name": "user/topics/telescopes"})
    names = zipfile.ZipFile(io.BytesIO(one.content)).namelist()
    assert sorted(names) == ["manifest.json", "user/topics/telescopes.md"]
