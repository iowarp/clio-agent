"""CLIO-BBBBBBBBBB19: text parts stream via message.part.delta events."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = ""
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def __init__(self, answer):
        self._answer = answer

    def forward(self, question: str, session_id: str):
        return _Pred(answer=self._answer)


@pytest.fixture()
def app_client(tmp_path: Path):
    answer = "X" * 200  # 200 chars -> 4 chunks at 64-char window.
    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent(answer))
    return app, TestClient(app), answer


def test_text_parts_stream_as_deltas(app_client) -> None:
    app, client, answer = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "stream me"}]},
    )

    history = app.state.bus._history.get(sid, [])
    added = [
        e for e in history
        if e.type == "message.part.added"
        and e.payload["part"]["type"] == "text"
    ]
    deltas = [e for e in history if e.type == "message.part.delta"]
    completed = [e for e in history if e.type == "message.part.completed"]

    # The text part arrives as one .added (empty text) + N .deltas +
    # one .completed. The other part types (routing_decision) use
    # .added with full content and emit no deltas.
    assert len(added) == 1
    assert added[0].payload["part"]["text"] == ""
    assert len(deltas) == 4
    assert len(completed) == 1

    # Concatenated deltas reconstruct the full answer.
    chunks = [d.payload["delta"]["text_append"] for d in deltas]
    assert "".join(chunks) == answer


def test_non_text_parts_skip_deltas(app_client) -> None:
    app, client, _ = app_client
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    )
    history = app.state.bus._history.get(sid, [])
    # routing_decision arrives via .added, not .delta.
    routing_added = [
        e for e in history
        if e.type == "message.part.added"
        and e.payload["part"]["type"] == "routing_decision"
    ]
    assert len(routing_added) == 1
    assert routing_added[0].payload["part"]["selected_agent"] == "data_expert"
