"""tier-3 nanoagent spawns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    nanoagents_spawned: list = field(default_factory=list)


class _Agent:
    def __init__(self, spawns):
        self._pred = _Pred(nanoagents_spawned=spawns)

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, spawns) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(spawns)))


def test_no_spawns_no_children(tmp_path: Path) -> None:
    client = _client(tmp_path, spawns=[])
    sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    )
    all_sessions = client.get("/v1/sessions").json()["sessions"]
    assert all(s["parent_session_id"] == "" for s in all_sessions)
