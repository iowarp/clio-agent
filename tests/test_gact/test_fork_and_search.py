"""CLIO-BBBBBBBBBB26+27: session fork + message search.

Fork copies stored messages from one session into a new session
that carries ``parent_session_id`` pointing at the source. Search
is case-insensitive substring match over every text part in the
stored log, returning ``{matches}`` with message_id, part_id,
snippet, and a recency-biased score.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "analysis reply"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def forward(self, question: str, session_id: str):
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))


def _turn(client: TestClient, sid: str, text: str) -> dict:
    from .conftest import complete_turn

    return complete_turn(client, sid, text)


def test_fork_copies_messages_and_sets_parent(tmp_path: Path) -> None:
    client = _client(tmp_path)
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
    client = _client(tmp_path)
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
    client = _client(tmp_path)
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
    client = _client(tmp_path)
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
    client = _client(tmp_path)
    sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
    _turn(client, sid, "hello")
    body = client.get(f"/v1/sessions/{sid}/messages/search?q=").json()
    assert body["matches"] == []


def test_fork_unknown_session_404s(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.post("/v1/sessions/sess_nope/fork", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"
