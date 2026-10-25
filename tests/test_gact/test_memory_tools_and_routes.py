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
    PastSessionSearchWithheld,
    session_policy,
    withheld_memory_tools,
)
from clio_agent.gact.memory.presentation import present_long_term_memory
from clio_agent.gact.memory.snapshot import MARKER, session_snapshot
from clio_agent.gact.task_access import TaskCaller


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

    def held_fact(*, fact: str, facts: list[tuple[str, str]]) -> str:
        calls.append({"held": fact, "facts": facts})
        return next((fid for fid, text in facts if text.casefold() == fact.casefold()), "")

    monkeypatch.setattr("clio_agent.gact.memory.routing.route_fact", route_fact)
    monkeypatch.setattr("clio_agent.gact.memory.routing.held_fact", held_fact)
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
        assert forgotten["fact"]["text"] == "My Frontier project id is CSC456"
        # Each write's row names the fact itself; the link opens its document.
        summary, blocks, subject = present_long_term_memory("create_memory", {}, created)
        assert (summary, subject) == ("Memory updated", "fact")
        assert blocks[0] == {"id": "fact", "type": "text", "text": created["fact"]["text"]}
        assert blocks[1]["label"] == "Edit in memory"
        assert blocks[1]["uri"] == f"{name}#{fact_id}"  # opens the page on this fact
        summary, blocks, subject = present_long_term_memory("forget_memory", {}, forgotten)
        assert (summary, subject) == ("Memory forgotten", "fact")
        assert forgotten["document_removed"] is True
        assert blocks == [{"id": "fact", "type": "text", "text": forgotten["fact"]["text"]}]
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
    # The session-agent route describes what the session runs with, and says why not more.
    agent = client.get(f"/v1/sessions/{session.id}/agent").json()
    withheld = {row["name"]: row["reason"] for row in agent["withheld_tools"]}
    assert withheld["memory_search"] == withheld["session_search"] == "session_memory_off"
    assert not set(withheld) & set(agent["resolution"]["tool_names"])


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


def test_manual_memory_creation_is_user_authored_and_logged_without_a_model(
    client: TestClient,
) -> None:
    body = {
        "scope": "user",
        "category": "topics",
        "slug": "writing-style",
        "description": "Writing preferences",
        "facts": ["Use short paragraphs", "Prefer concrete examples"],
    }
    created = client.post("/v1/memory/documents", json=body)
    assert created.status_code == 200, created.text
    document = created.json()
    assert document["name"] == "user/topics/writing-style"
    assert {fact["source"] for fact in document["facts"]} == {"user"}
    assert {fact["session"] for fact in document["facts"]} == {"memory_page"}

    duplicate = client.post("/v1/memory/documents", json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["error"] == "memory_conflict"

    response = client.get("/v1/memory/creation-runs")
    assert response.status_code == 200, response.text
    runs = response.json()
    manual_rows = [row for row in runs["runs"] if row["origin"] == "manual"]
    manual = next(row for row in manual_rows if row["reason"] == "created_by_user")
    assert manual["state"] == "completed" and manual["reason"] == "created_by_user"
    assert manual["models"] == {"extract": None, "curator": None}
    assert manual["targets"] == ["user/topics/writing-style"]
    assert manual["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cost_known": True,
    }
    assert manual["proposal_counts"] == {"accepted": 0, "pending": 0, "rejected": 0}
    assert any(row["state"] == "failed" for row in manual_rows)
    assert "background" in runs and runs["next_cursor"] is None

    workspace = client.app.state.workspaces.create(name="memory-project", root_path=str(Path.cwd()))
    project = client.post(
        "/v1/memory/documents",
        json={
            "scope": f"project/{workspace.id}",
            "category": "topics",
            "slug": "cluster-plan",
            "facts": ["Use the shared GPU queue"],
        },
    )
    assert project.status_code == 200, project.text
    project_log = client.get("/v1/memory/creation-runs").json()["runs"]
    linked = next(
        row
        for row in project_log
        if row["targets"] == [f"project/{workspace.id}/topics/cluster-plan"]
    )
    assert linked["workspace_id"] == workspace.id


def test_manual_memory_creation_rejects_empty_or_duplicate_facts(client: TestClient) -> None:
    base = {
        "scope": "user",
        "category": "topics",
        "slug": "invalid",
        "facts": [],
    }
    assert client.post("/v1/memory/documents", json=base).status_code == 422
    base["facts"] = ["Same fact", " same  fact "]
    assert client.post("/v1/memory/documents", json=base).status_code == 422
    base["scope"] = "project/ws_global"
    base["facts"] = ["A valid fact"]
    assert client.post("/v1/memory/documents", json=base).status_code == 422


def test_creation_run_cursor_is_validated(client: TestClient) -> None:
    response = client.get("/v1/memory/creation-runs", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400


def test_manual_singleton_entry_appends_without_replacing_existing_facts(
    client: TestClient,
) -> None:
    first = client.post(
        "/v1/memory/documents",
        json={
            "scope": "user",
            "category": "preferences",
            "slug": "ignored-name",
            "description": "Preferences written by the user",
            "facts": ["Use concise answers"],
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["name"] == "user/preferences/preferences"
    second = client.post(
        "/v1/memory/documents",
        json={
            "scope": "user",
            "category": "preferences",
            "slug": "also-ignored",
            "description": "Must not replace the existing description",
            "facts": ["Use concise answers", "Show concrete examples"],
        },
    )
    assert second.status_code == 200, second.text
    document = second.json()
    assert document["description"] == "Preferences written by the user"
    assert {fact["text"] for fact in document["facts"]} == {
        "Use concise answers",
        "Show concrete examples",
    }
    assert {fact["source"] for fact in document["facts"]} == {"user"}


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


def test_memory_off_also_stops_searching_other_conversations_through_tasks(
    client: TestClient,
) -> None:
    """``query_tasks`` with a query over authorized sessions reads their transcripts:
    the same switches as ``session_search`` apply, with the typed reason."""
    app = client.app
    off = _root(app, **{SESSION_MEMORY_OFF: True})
    on = _root(app)
    human = str(app.state.human_principal)
    tasks = app.state.task_operations
    with pytest.raises(PastSessionSearchWithheld) as refused:
        tasks.query(TaskCaller(off.id, human), kind="Session", scope="authorized", query="plots")
    assert refused.value.reason == "session_memory_off"
    # Its own tree, a listing without text, and the person's own search are not recall.
    tasks.query(TaskCaller(off.id, human), kind="Session", scope="tree", query="plots")
    tasks.query(TaskCaller(off.id, human), kind="Session", scope="authorized")
    tasks.query(TaskCaller(on.id, human), kind="Session", scope="authorized", query="plots")


def test_asking_twice_to_remember_a_held_fact_files_it_once(client: TestClient) -> None:
    """The comparison step names the fact the document already holds; nothing is written."""
    app = client.app
    session = _root(app)
    tokens = _in_session(app, session.id)
    try:
        first = memory_tools.create_memory(
            "alcf account", "ALCF username is jcernuda", "environments"
        )
        again = memory_tools.create_memory(
            "alcf account", "ALCF username is jcernuda", "environments"
        )
    finally:
        _leave(tokens)
    assert first["memory"]["action"] == "created"
    assert again["memory"]["action"] == "already_remembered"
    assert again["fact"]["id"] == first["fact"]["id"]
    doc = client.get(f"/v1/memory/documents/{first['memory']['name']}").json()
    assert [f["text"] for f in doc["facts"] if f["live"]] == ["ALCF username is jcernuda"]


def test_the_comparison_answer_must_name_a_listed_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """``held_fact`` refuses an id it did not list (typed), and skips the call for none."""
    import dspy

    from clio_agent.gact.memory import routing

    monkeypatch.undo()  # the real comparison, not the autouse stub
    assert routing.held_fact(fact="x", facts=[]) == ""
    lm = dspy.utils.DummyLM(
        [
            {"held": "true", "fact_id": "f_missing"},
            {"held": "true", "fact_id": "fa1"},
            {"held": "false", "fact_id": "(empty)"},
        ]
    )
    with dspy.context(lm=lm):
        with pytest.raises(routing.MemoryRouteError) as refused:
            routing.held_fact(fact="Uses ALMA", facts=[("fa1", "Uses ALMA")])
        assert refused.value.error_type == "memory_route_invalid"
        assert routing.held_fact(fact="Uses ALMA", facts=[("fa1", "Uses ALMA")]) == "fa1"
        # Not held: whatever the model put in the id field is not read.
        assert routing.held_fact(fact="Uses VLA", facts=[("fa1", "Uses ALMA")]) == ""


def test_routing_reads_the_typed_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing document is used only when the model says so; a filler such as
    "(empty)" in a field the decision does not use is never read as a name."""
    import dspy

    from clio_agent.gact.memory import routing

    monkeypatch.undo()
    docs = [("python", "Python setup")]
    lm = dspy.utils.DummyLM(
        [
            {"use_existing": "true", "existing": "python", "new_name": "", "new_description": ""},
            {
                "use_existing": "false",
                "existing": "(empty)",
                "new_name": "dark-mode",
                "new_description": "How the user likes interfaces to look",
            },
        ]
    )
    with dspy.context(lm=lm):
        assert routing.route_fact(
            fact="Uses 3.13", subject="python", category="environments", documents=docs
        ) == ("python", "")
        assert routing.route_fact(
            fact="Prefers dark mode", subject="ui", category="preferences", documents=docs
        ) == ("dark-mode", "How the user likes interfaces to look")
