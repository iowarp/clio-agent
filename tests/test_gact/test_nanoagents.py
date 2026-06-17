"""CLIO-BBBBBBBBBB25: tier-3 nanoagent spawns."""

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


def test_nanoagent_spawns_child_session(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        spawns=[
            {
                "agent_id": "code_reviewer",
                "input": {"files": ["main.go"]},
                "answer": "looks good; one nit on line 42",
                "tools_called": [{"name": "fs_read_file", "args": {"filepath": "main.go"}}],
                "duration_ms": 145.0,
                "cost_usd": 0.0009,
            }
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "split this"}]},
    )

    all_sessions = client.get("/v1/sessions").json()["sessions"]
    children = [s for s in all_sessions if s["parent_session_id"] == sid]
    assert len(children) == 1
    child = children[0]
    assert child["title"] == "code_reviewer subagent"
    assert child["agent"]["id"] == "code_reviewer"
    assert child["agent"]["mode"] == "subagent"
    assert child["metadata"]["session_type"] == "nanoagent"
    assert child["metadata"]["parent_session_id"] == sid
    assert child["metadata"]["tool_count"] == 1

    # Child has its own user + assistant messages.
    msgs = client.get(f"/v1/sessions/{child['id']}/messages").json()["messages"]
    assert len(msgs) == 2
    user = [m for m in msgs if m["role"] == "user"][0]
    assert "Subagent input:" in user["parts"][0]["text"]
    assert '"files": [' in user["parts"][0]["text"]
    assistant = [m for m in msgs if m["role"] == "assistant"][0]
    assert any("looks good" in p.get("text", "") for p in assistant["parts"])
    assert assistant["metadata"]["tools_called"][0]["name"] == "fs_read_file"


def test_no_spawns_no_children(tmp_path: Path) -> None:
    client = _client(tmp_path, spawns=[])
    sid = client.post("/v1/sessions", json={"title": "parent"}).json()["id"]
    client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hi"}]},
    )
    all_sessions = client.get("/v1/sessions").json()["sessions"]
    assert all(s["parent_session_id"] == "" for s in all_sessions)
