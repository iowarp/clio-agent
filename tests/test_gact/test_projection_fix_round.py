"""A2 adversarial-review fix round for the campaign's projection lanes.

One test per reviewed finding, each written failing-first. The lanes under test
are the pending-interaction projection (``routes.interactions``), the child-work
provenance projection (``provenance.child_projection`` / ``routes.provenance``),
and the descendant substrate both of them read (``agent_tasks``).

Every test here drives the REAL registries through ``build_app`` rather than a
``SimpleNamespace`` stand-in: the findings below are all disagreements BETWEEN
substrates, which a hand-built stub cannot reproduce.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_tasks import AgentTask
from clio_agent.gact.app import build_app


def _seed_task(app: Any, *, parent: str, child: str, task_id: str, depth: int = 1) -> AgentTask:
    """Register one delegated child through the authoritative registry."""

    return app.state.agent_task_registry.register(
        AgentTask(
            task_id=task_id,
            parent_session_id=parent,
            child_session_id=child,
            agent_ref={"expert_id": "analyst"},
            depth=depth,
            status="running",
            created_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-03T00:00:00+00:00",
        )
    )


# --------------------------------------------------------------------------- #
# A2-F4: a session-store child with no task row is a FORK, not the root
# --------------------------------------------------------------------------- #


def test_a_fork_is_attributed_to_itself_not_silently_to_the_root(tmp_path) -> None:
    """``by_session.get(sid, by_session[root])`` invented ownership.

    A session the user forked exists in the session store with a
    ``parent_session_id`` but has no ``AgentTask``. Its spans fell through to the
    root's lineage row, so they inherited the ROOT's ``task_id`` and ``task_path``
    -- a fabricated attribution -- and the ``contains`` edge they minted pointed
    at a ``session:<fork>`` node that was never in the graph.
    """

    from clio_agent.gact.provenance.child_projection import project_child_execution

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    delegated = app.state.sessions.create(workspace_id="ws_default", title="delegated")
    fork = app.state.sessions.create(
        workspace_id="ws_default", title="fork", parent_session_id=root.id
    )
    _seed_task(app, parent=root.id, child=delegated.id, task_id="task_delegated")

    result = project_child_execution(
        app,
        root.id,
        {
            "spans": [
                {"id": "span_fork", "session_id": fork.id, "event_type": "tool.call.completed"}
            ],
            "nodes": [],
            "edges": [],
        },
    )

    span = result["spans"][0]
    assert span["owner_session_id"] == fork.id
    assert span["task_id"] == "", "a fork must not inherit the root's task identity"
    assert span["task_path"] == [], "a fork must not inherit the root's task path"

    node_ids = {node["id"] for node in result["nodes"]}
    assert f"session:{fork.id}" in node_ids, "the contains edge would dangle without this node"
    fork_node = next(node for node in result["nodes"] if node["id"] == f"session:{fork.id}")
    assert fork_node["attributes"]["attribution"] == "session_fork"

    # Every contains edge must land on a node the graph actually has.
    for edge in result["edges"]:
        if edge["kind"] == "contains":
            assert edge["source"] in node_ids, f"dangling contains edge: {edge['id']}"


def test_a_span_from_an_unknown_session_is_reported_not_reattributed(tmp_path) -> None:
    """A session id in NEITHER substrate is a gap in the read model, not the root."""

    from clio_agent.gact.provenance.child_projection import project_child_execution

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")

    result = project_child_execution(
        app,
        root.id,
        {
            "spans": [{"id": "span_ghost", "session_id": "sess_ghost", "event_type": "tool.call"}],
            "nodes": [],
            "edges": [],
        },
    )

    span = result["spans"][0]
    assert span["owner_session_id"] == "sess_ghost"
    assert span["task_id"] == ""
    ghost = next(node for node in result["nodes"] if node["id"] == "session:sess_ghost")
    assert ghost["attributes"]["attribution"] == "unattributed_session"
    assert any(
        row["reason"] == "unattributed_session" and row["session_id"] == "sess_ghost"
        for row in result["degradations"]
    )


def test_the_descendant_scope_carries_forks_with_a_typed_marker(tmp_path) -> None:
    """One walk over BOTH substrates, so a permission raised in a fork is listable.

    The task registry alone cannot see a fork; the session store alone cannot say
    which child a task delegated. Reading only one of them is what let a fork's
    pending permission be invisible to every interactions poll.
    """

    from clio_agent.gact.session_descendants import descendant_session_ids, descendant_sessions

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    delegated = app.state.sessions.create(workspace_id="ws_default", title="delegated")
    fork = app.state.sessions.create(
        workspace_id="ws_default", title="fork", parent_session_id=root.id
    )
    grandchild = app.state.sessions.create(workspace_id="ws_default", title="grandchild")
    _seed_task(app, parent=root.id, child=delegated.id, task_id="task_a")
    _seed_task(app, parent=delegated.id, child=grandchild.id, task_id="task_b", depth=2)

    rows = {row.session_id: row for row in descendant_sessions(app, root.id)}
    assert set(rows) == {delegated.id, fork.id, grandchild.id}
    assert rows[delegated.id].attribution == "agent_task"
    assert rows[delegated.id].task_id == "task_a"
    assert rows[fork.id].attribution == "session_fork"
    assert rows[fork.id].task_id == ""
    assert rows[grandchild.id].depth == 2
    assert set(descendant_session_ids(app, root.id)) == set(rows)


def test_a_forks_pending_permission_is_listable_from_the_root(tmp_path) -> None:
    """The user-visible half of the same finding."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        root = app.state.sessions.create(workspace_id="ws_default", title="root")
        fork = app.state.sessions.create(
            workspace_id="ws_default", title="fork", parent_session_id=root.id
        )
        app.state.permissions["perm_fork"] = {
            "id": "perm_fork",
            "session_id": fork.id,
            "status": "pending",
            "summary": "Allow the fork to write",
            "created_at": "2026-09-03T00:00:00+00:00",
            "tool_call": {"tool_name": "fs_apply_edit_write"},
        }
        listed = client.get(
            f"/v1/sessions/{root.id}/interactions", params={"include_children": True}
        )

    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()["interactions"]]
    assert "permission:perm_fork" in ids


# --------------------------------------------------------------------------- #
# A2-F2: computed degradations must reach the client, not be dropped
# --------------------------------------------------------------------------- #


def test_the_interactions_projection_serves_the_a2ui_quarantine_reasons(tmp_path) -> None:
    """The A2UI store COMPUTES typed quarantine rows; the projection threw them away.

    A surface whose persisted part cannot be replayed is silently absent from the
    attention lane, so the client sees a shorter list and no reason for it.
    """

    from clio_agent.gact.types import Message, Part

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="root")
        app.state.messages.setdefault(session.id, []).append(
            Message(
                id="msg_bad",
                turn_id="turn_bad",
                session_id=session.id,
                role="assistant",
                created_at="2026-09-03T00:00:00+00:00",
                updated_at="2026-09-03T00:00:00+00:00",
                parts=[Part(type="a2ui", id="part_bad", a2ui={"version": "v0.9.1"})],
            )
        )
        quarantined = app.state.a2ui_store.projection_degradations(session.id)
        assert quarantined, "fixture must produce a real quarantine row"
        listed = client.get(f"/v1/sessions/{session.id}/interactions")

    assert listed.status_code == 200, listed.text
    reasons = {row["reason"] for row in listed.json()["degradations"]}
    assert reasons & {row["reason"] for row in quarantined}


def test_the_interactions_projection_bounds_the_owners_it_walks(tmp_path) -> None:
    """Per-owner A2UI derivation re-walks a whole message ledger, per poll.

    ``include_children`` on a wide tree multiplied that by the descendant count
    with no bound of its own -- the projection_limit only trimmed the ROWS, after
    every ledger had already been walked. Exceeding the bound is a typed
    degradation, never a quietly shorter answer.
    """

    from clio_agent.gact.routes.interactions import project_pending_interactions
    from tests._config_layer import set_config

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    for index in range(6):
        child = app.state.sessions.create(
            workspace_id="ws_default", title=f"child{index}", parent_session_id=root.id
        )
        app.state.permissions[f"perm_{index}"] = {
            "id": f"perm_{index}",
            "session_id": child.id,
            "status": "pending",
            "summary": "write",
            "created_at": f"2026-09-03T00:00:0{index}+00:00",
            "tool_call": {},
        }

    set_config("gact.interactions.projection_limit", 3)
    projection = project_pending_interactions(app, root.id, include_children=True)

    assert len(projection.rows) <= 3
    assert any(
        row["reason"] == "interaction_projection_truncated" for row in projection.degradations
    )
    assert projection.degradations[0]["detail"]


# --------------------------------------------------------------------------- #
# A2-F9/F10/F11: lineage honesty, lifecycle, liveness
# --------------------------------------------------------------------------- #


def test_child_lineage_is_actually_bounded_and_says_when_it_truncates(tmp_path) -> None:
    """The docstring promised "bounded"; the walk had no depth cap at all.

    ``descendant_session_ids`` stops at ``MAX_SPAWN_DEPTH``; ``child_session_lineage``
    walked the same graph with nothing but a ``seen`` set, so the two substrates
    disagreed about what the tree even is.
    """

    from clio_agent.gact.provenance.child_projection import child_session_lineage
    from clio_agent.gact.session_descendants import _DEFAULT_DESCENDANT_DEPTH, MAX_SPAWN_DEPTH

    assert _DEFAULT_DESCENDANT_DEPTH == MAX_SPAWN_DEPTH, (
        "the aggregation cap must track the spawn backstop, not drift from it"
    )

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    parent_id = root.id
    for depth in range(1, MAX_SPAWN_DEPTH + 3):
        child = app.state.sessions.create(workspace_id="ws_default", title=f"d{depth}")
        _seed_task(app, parent=parent_id, child=child.id, task_id=f"task_{depth}", depth=depth)
        parent_id = child.id

    lineage, truncated = child_session_lineage(app, root.id)
    assert truncated is True
    assert max(row["depth"] for row in lineage) == MAX_SPAWN_DEPTH


def test_the_execution_projection_reports_its_own_depth_and_truncation(tmp_path) -> None:
    """A truncated tree served as if complete is the same lie one layer up."""

    from clio_agent.gact.provenance.child_projection import project_child_execution

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    child = app.state.sessions.create(workspace_id="ws_default", title="child")
    _seed_task(app, parent=root.id, child=child.id, task_id="task_a")

    result = project_child_execution(app, root.id, {"spans": [], "nodes": [], "edges": []})
    assert result["lineage_depth"] == 1
    assert result["lineage_truncated"] is False
    assert result["degradations"] == []


def test_deleting_a_session_purges_its_agent_task_rows(tmp_path) -> None:
    """A deleted child left its task row behind, so the lineage kept naming it."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        root = app.state.sessions.create(workspace_id="ws_default", title="root")
        child = app.state.sessions.create(workspace_id="ws_default", title="child")
        _seed_task(app, parent=root.id, child=child.id, task_id="task_gone")
        assert app.state.agent_task_registry.get("task_gone") is not None

        deleted = client.delete(f"/v1/sessions/{child.id}")
        assert deleted.status_code in {200, 204}, deleted.text

    assert app.state.agent_task_registry.get("task_gone") is None
    assert app.state.agent_task_registry.for_parent(root.id) == []


def test_agent_task_events_reach_the_attended_root(tmp_path) -> None:
    """Permission events already mirror to the watched root; task events did not.

    A nested spawn published only to its own parent and child, so the human
    watching the root session's stream never saw a grandchild start or finish.
    """

    from clio_agent.gact.agent_tasks import publish_agent_task_event

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    middle = app.state.sessions.create(
        workspace_id="ws_default", title="middle", parent_session_id=root.id
    )
    leaf = app.state.sessions.create(
        workspace_id="ws_default", title="leaf", parent_session_id=middle.id
    )
    task = _seed_task(app, parent=middle.id, child=leaf.id, task_id="task_deep", depth=2)

    published: list[tuple[str, dict[str, Any]]] = []
    original = app.state.bus.publish

    def capture(event: Any) -> Any:
        published.append((event.session_id, dict(event.payload)))
        return original(event)

    app.state.bus.publish = capture
    publish_agent_task_event(app, task, "agent.task.started")

    targets = {session_id for session_id, _payload in published}
    assert targets == {middle.id, leaf.id, root.id}
    mirrored = next(payload for session_id, payload in published if session_id == root.id)
    assert mirrored["forwarded_from_session_id"] == middle.id
    assert mirrored["attended_session_id"] == root.id


def test_the_subagent_projection_carries_the_task_path(tmp_path) -> None:
    """Without it a client cannot place a nested run in the tree it belongs to."""

    from dataclasses import asdict

    from clio_agent.gact import context as _ctx
    from clio_agent.gact.events import Event
    from clio_agent.gact.protocol.v3.event import event_to_v3

    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = app.state.sessions.create(workspace_id="ws_default", title="root")
    middle = app.state.sessions.create(workspace_id="ws_default", title="middle")
    leaf = app.state.sessions.create(workspace_id="ws_default", title="leaf")
    _seed_task(app, parent=root.id, child=middle.id, task_id="task_outer")
    task = _seed_task(app, parent=middle.id, child=leaf.id, task_id="task_inner", depth=2)

    token = _ctx.set_app(app)
    try:
        projected = event_to_v3(
            Event(type="agent.task.started", session_id=middle.id, payload=asdict(task)),
            session=app.state.sessions.get(middle.id),
        )
    finally:
        _ctx.reset(token)
    assert projected["payload"]["task_path"] == ["task_outer", "task_inner"]


def test_pending_interaction_rows_carry_a_revision(tmp_path) -> None:
    """A client cannot discard a stale poll response without one."""

    from clio_agent.gact.types import UserQuestion

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        session = app.state.sessions.create(workspace_id="ws_default", title="root")
        app.state.user_questions["q_rev"] = UserQuestion(
            id="q_rev",
            session_id=session.id,
            owner_session_id=session.id,
            attended_session_id=session.id,
            prompt="Which dataset?",
            created_at="2026-09-03T00:00:00+00:00",
            updated_at="2026-09-03T00:00:00+00:00",
        )
        listed = client.get(f"/v1/sessions/{session.id}/interactions")

    row = listed.json()["interactions"][0]
    assert row["revision"] == "2026-09-03T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# A2-F13: the projection routes, driven against the REAL registries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("include_children", [True, False])
def test_the_execution_route_honours_include_children(tmp_path, include_children: bool) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        root = app.state.sessions.create(workspace_id="ws_default", title="root")
        child = app.state.sessions.create(workspace_id="ws_default", title="child")
        _seed_task(app, parent=root.id, child=child.id, task_id="task_a")
        response = client.get(
            f"/v1/sessions/{root.id}/provenance/execution",
            params={"include_children": include_children},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    lineage = [row["session_id"] for row in body["session_lineage"]]
    assert lineage == ([root.id, child.id] if include_children else [root.id])
    assert body["lineage_truncated"] is False


def test_one_unreadable_child_does_not_cost_the_parent_its_timeline() -> None:
    """The ARC read raised straight out of the route: a 500 for the whole query.

    A child whose event segments cannot be read is one gap, not a failed query --
    and it must be NAMED, or the parent's timeline silently loses a branch.
    """

    from types import SimpleNamespace

    from clio_agent.gact.routes.provenance import _native_events

    def iterate(session_id: str) -> list[Any]:
        if session_id == "child":
            raise OSError("segment file is unreadable")
        return []

    app = SimpleNamespace(
        state=SimpleNamespace(
            arc=SimpleNamespace(_live=SimpleNamespace(iter_session_event_segments=iterate))
        )
    )

    events, degradations = _native_events(app, ["root", "child"])

    assert events == []
    assert [row["reason"] for row in degradations] == ["child_events_unreadable"]
    assert degradations[0]["session_id"] == "child"
    assert "unreadable" in degradations[0]["detail"]


def test_the_execution_route_survives_a_deleted_child(tmp_path) -> None:
    """The registry can outlive the session store between two writes."""

    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        root = app.state.sessions.create(workspace_id="ws_default", title="root")
        child = app.state.sessions.create(workspace_id="ws_default", title="child")
        _seed_task(app, parent=root.id, child=child.id, task_id="task_a")
        # Drop the session WITHOUT the route's purge, reproducing a torn write.
        app.state.sessions._sessions.pop(child.id, None)
        response = client.get(f"/v1/sessions/{root.id}/provenance/execution")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["session_id"] for row in body["session_lineage"]] == [root.id, child.id]
    assert any(
        row["reason"] == "lineage_session_missing" and row["session_id"] == child.id
        for row in body["degradations"]
    )
