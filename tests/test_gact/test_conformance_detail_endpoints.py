"""Regression coverage for GACT read/detail endpoints used by conformance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

from ._writing_agent import WritingAgent, workspace_session
from .conftest import complete_turn

# Every session runs its pinned plugin agent (the hermetic ``standard``); route its
# module to each test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""


class _Agent:
    def __init__(self) -> None:
        self._pred = _Pred()

    def forward(self, question: str, session_id: str) -> _Pred:
        return self._pred


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))


def test_message_detail_returns_stored_message(tmp_path: Path) -> None:
    # ENTERED: complete_turn drives a real POST /messages turn; an un-entered
    # TestClient's per-request transient portal is torn down (cancelling the turn)
    # the instant the ack lands, before the assistant reply ever settles.
    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assistant = complete_turn(client, sid, "hello")

        resp = client.get(f"/v1/sessions/{sid}/messages/{assistant['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == assistant["id"]
    assert body["role"] == "assistant"


def test_session_agent_detail_returns_default_standard_agent(tmp_path: Path) -> None:
    """A bare session (no agent requested) pins the default ``clio/standard`` plugin
    agent, and the session-agent detail route returns that pinned resolution."""

    with _client(tmp_path) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        resp = client.get(f"/v1/sessions/{sid}/agent")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["subagent"] is False
    resolution = body["resolution"]
    assert resolution["agent"]["id"] == "standard"
    assert resolution["agent"]["ref"] == "clio/standard"
    assert "core" in [row["id"] for row in resolution["plugins"]]
    assert resolution["tool_names"]


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


def test_session_and_message_diffs_return_conformance_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "main.py").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(tmp_path))
    agent = WritingAgent()
    agent.calls = [
        (
            "fs_edit_file",
            {"filepath": str(root / "main.py"), "old_string": "old", "new_string": "new"},
        )
    ]
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as client:
        sid = workspace_session(client, root, approval_mode="auto-edits")
        assistant = complete_turn(client, sid, "edit main.py")

        session_body = client.get(f"/v1/sessions/{sid}/diffs").json()
        message_body = client.get(f"/v1/sessions/{sid}/messages/{assistant['id']}/diffs").json()

        assert session_body["diffs"][0]["path"] == str(root / "main.py")
        assert session_body["diffs"][0]["status"] == "applied"
        assert session_body["diffs"][0]["undo_available"] is True
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
