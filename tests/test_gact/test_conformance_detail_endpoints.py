"""Regression coverage for GACT read/detail endpoints used by conformance."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

from .conftest import complete_turn


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    file_diffs: list[dict[str, Any]] = field(default_factory=list)


class _Agent:
    def __init__(self, diffs: list[dict[str, Any]] | None = None) -> None:
        self._pred = _Pred(file_diffs=list(diffs or []))

    def forward(self, question: str, session_id: str) -> _Pred:
        return self._pred


def _client(tmp_path: Path, diffs: list[dict[str, Any]] | None = None) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(diffs)))


def test_message_detail_returns_stored_message(tmp_path: Path) -> None:
    client = _client(tmp_path)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "hello")

    resp = client.get(f"/v1/sessions/{sid}/messages/{assistant['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == assistant["id"]
    assert body["role"] == "assistant"


def test_agent_detail_returns_builtin_agent(tmp_path: Path) -> None:
    client = _client(tmp_path)

    resp = client.get("/v1/agents/main")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "main"
    assert body["source"] == "builtin"
    assert body["title"]


def test_mcp_server_detail_returns_listed_server(tmp_path: Path) -> None:
    client = _client(tmp_path)
    listed = client.get("/v1/mcp/servers").json()["servers"]
    server_id = listed[0]["id"]

    resp = client.get(f"/v1/mcp/servers/{server_id}")

    assert resp.status_code == 200
    assert resp.json()["id"] == server_id


def test_provider_detail_returns_listed_provider(tmp_path: Path) -> None:
    client = _client(tmp_path)
    listed = client.get("/v1/providers").json()["providers"]
    provider_id = listed[0]["id"]

    resp = client.get(f"/v1/providers/{provider_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == provider_id
    assert body["name"]


def test_session_and_message_diffs_return_conformance_shape(tmp_path: Path) -> None:
    client = _client(
        tmp_path,
        diffs=[
            {
                "path": "main.py",
                "unified_diff": "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new\n",
            }
        ],
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    assistant = complete_turn(client, sid, "propose edit")

    session_body = client.get(f"/v1/sessions/{sid}/diffs").json()
    message_body = client.get(f"/v1/sessions/{sid}/messages/{assistant['id']}/diffs").json()

    assert session_body["diffs"][0]["path"] == "main.py"
    assert session_body["diffs"][0]["applied"] is False
    assert message_body["diffs"][0]["message_id"] == assistant["id"]


def test_workspace_repo_map_returns_tree_envelope(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("hello", encoding="utf-8")
    client = _client(tmp_path)
    workspace_id = client.get("/v1/workspaces").json()["workspaces"][0]["id"]

    resp = client.get(f"/v1/workspaces/{workspace_id}/repo_map")

    assert resp.status_code == 200
    body = resp.json()
    assert "tree" in body
    assert "tokens" in body
