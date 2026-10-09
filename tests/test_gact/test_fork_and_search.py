"""session fork + message search.

Fork copies stored messages from one session into a new session
that carries ``parent_session_id`` pointing at the source. Search
is case-insensitive substring match over every text part in the
stored log, returning ``{matches}`` with message_id, part_id,
snippet, and a recency-biased score.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "analysis reply"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str) -> _Pred:
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))


def _turn(client: TestClient, sid: str, text: str) -> dict[str, Any]:
    from .conftest import complete_turn

    return complete_turn(client, sid, text)


def test_fork_copies_messages_and_sets_parent(tmp_path: Path) -> None:
    # ENTERED: _turn() drives a real POST /messages turn via complete_turn; an
    # un-entered TestClient's per-request transient portal is torn down (cancelling
    # the turn) the instant the ack lands, before the assistant reply ever settles.
    with _client(tmp_path) as client:
        src = client.post("/v1/sessions", json={"title": "src"}).json()["id"]
        _turn(client, src, "analyze /tmp/one.parquet")
        _turn(client, src, "analyze /tmp/two.parquet")

        resp = client.post(f"/v1/sessions/{src}/fork", json={})
        assert resp.status_code == 201
        new = resp.json()
        assert new["parent_session_id"] == src
        assert new["title"].endswith("(fork)")

        rows = client.get(f"/v1/sessions/{new['id']}/messages").json()["messages"]
    assert len(rows) == 4  # 2 turns × (user + assistant)


def test_fork_copies_context_files(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        src = client.post("/v1/sessions", json={"title": "src"}).json()["id"]
        target = tmp_path / "notes.md"
        target.write_text("important context\n")
        client.post(
            f"/v1/sessions/{src}/context/files",
            json={"path": str(target), "mode": "read"},
        )

        new = client.post(f"/v1/sessions/{src}/fork", json={}).json()

        original = client.get(f"/v1/sessions/{src}/context/files").json()["files"]
        forked = client.get(f"/v1/sessions/{new['id']}/context/files").json()["files"]
        assert forked == original
        forked[0]["mode"] = "edit"
        assert client.app.state.context_files[src][str(target)]["mode"] == "read"


def test_fork_truncates_at_message_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        src = client.post("/v1/sessions", json={"title": "src"}).json()["id"]
        t1_assistant = _turn(client, src, "one")
        _turn(client, src, "two")
        cutoff = t1_assistant["id"]

        fork = client.post(
            f"/v1/sessions/{src}/fork",
            json={"at_message_id": cutoff, "title": "fork-at-one"},
        ).json()
        rows = client.get(f"/v1/sessions/{fork['id']}/messages").json()["messages"]
    # Newest-first: assistant_t1, user_t1 — only the first turn.
    assert len(rows) == 2
    ids = {r["id"] for r in rows}
    assert cutoff in ids


def test_search_returns_ranked_snippets(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        _turn(client, sid, "load /tmp/alpha.parquet")
        _turn(client, sid, "compare /tmp/alpha.parquet to /tmp/beta.parquet")

        body = client.get(f"/v1/sessions/{sid}/messages/search?q=alpha.parquet").json()
    matches = body["matches"]
    assert len(matches) >= 2
    for m in matches:
        assert "alpha.parquet" in m["snippet"].lower()
    # Score is recency-biased — the second turn's match outranks the first.
    assert matches[0]["score"] >= matches[-1]["score"]


def test_search_empty_query_returns_no_matches(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
        _turn(client, sid, "hello")
        body = client.get(f"/v1/sessions/{sid}/messages/search?q=").json()
    assert body["matches"] == []


def test_fork_unknown_session_404s(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        resp = client.post("/v1/sessions/sess_nope/fork", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_v3_fork_is_writable_and_leaves_original_unchanged(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        original = client.post("/v1/sessions", json={"title": "Original"}).json()["id"]
        _turn(client, original, "Baseline")
        before = client.get(f"/v1/sessions/{original}/messages").json()
        response = client.post(
            f"/v1/sessions/{original}/fork", json={}, headers={"X-GACT-Version": "0.3"}
        )
        assert response.status_code == 201
        branch = response.json()
        assert branch["session_kind"] == "branch"
        assert branch["state"] == "completed"
        assert branch["message_count"] == 2
        _turn(client, branch["id"], "Independent follow-up")
        assert client.get(f"/v1/sessions/{original}/messages").json() == before
        assert len(client.get(f"/v1/sessions/{branch['id']}/messages").json()["messages"]) == 4
        listed = client.get("/v1/sessions", headers={"X-GACT-Version": "0.3"}).json()["sessions"]
        assert next(row for row in listed if row["id"] == branch["id"])["session_kind"] == "branch"


def test_fork_preserves_configuration_and_narrowing_policies(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        source = client.app.state.sessions.create(
            workspace_id="ws_default",
            title="Configured",
            model={"provider_id": "codex", "model_id": "model", "variant": "direct"},
            mode="plan",
            edit_mode="patch",
            routing_mode="reasoning_only",
            approval_mode="bypass",
            agent={"id": "expert", "mode": "subagent"},
            metadata={
                "effort": "high",
                "effort_source": "user",
                "active_agent_blueprint_id": "blueprint",
                "session_type": "agent_task",
                "agent_task": {"pending": True},
                "pinned": True,
            },
        )
        client.app.state.permission_policies = [
            {
                "scope": "session",
                "scope_id": source.id,
                "action": "deny",
                "tool": "bash",
            }
        ]
        branch = client.post(
            f"/v1/sessions/{source.id}/fork", json={}, headers={"X-GACT-Version": "0.3"}
        ).json()
        record = client.app.state.sessions.get(branch["id"])
        assert record.model == source.model and record.model is not source.model
        assert record.mode == "plan"
        assert record.edit_mode == "patch"
        assert record.routing_mode == "reasoning_only"
        assert record.approval_mode == "bypass"
        assert record.approval_profile == source.approval_profile
        assert record.agent == {"id": "expert"}
        assert branch["session_kind"] == "branch"
        assert branch["effort"] == "high"
        assert branch["active_blueprint_id"] == "blueprint"
        assert not branch["pinned"]
        assert "agent_task" not in record.metadata
        assert any(
            row["scope_id"] == record.id and row["action"] == "deny"
            for row in client.app.state.permission_policies
        )


def test_branch_interactions_are_independent_and_include_its_workers(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        source = client.app.state.sessions.create(workspace_id="ws_default", title="Original")
        branch = client.post(f"/v1/sessions/{source.id}/fork", json={}).json()["id"]
        worker = client.app.state.sessions.create(
            workspace_id="ws_default",
            title="Branch worker",
            parent_session_id=branch,
            agent={"id": "worker", "mode": "subagent"},
        )
        for owner in (source.id, branch, worker.id):
            client.app.state.permissions[owner] = {
                "id": owner,
                "session_id": owner,
                "status": "pending",
                "summary": owner,
                "created_at": source.created_at,
                "tool_call": {"tool_name": "bash", "input": {"command": "pwd"}},
            }
        original_rows = client.get(
            f"/v1/sessions/{source.id}/interactions", params={"include_children": True}
        ).json()["interactions"]
        branch_rows = client.get(
            f"/v1/sessions/{branch}/interactions", params={"include_children": True}
        ).json()["interactions"]
        assert {row["owner_session_id"] for row in original_rows} == {source.id}
        assert {row["owner_session_id"] for row in branch_rows} == {branch, worker.id}
        assert {row["attended_session_id"] for row in branch_rows} == {branch}
