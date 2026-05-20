"""iowarp/clio-agent#14: backend slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, *args, **kwargs):
        return _Pred()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    )


def test_commands_listed(client: TestClient) -> None:
    body = client.get("/v1/commands").json()
    ids = {c["id"] for c in body["commands"]}
    assert "/clear" in ids
    assert "/cache-stats" in ids
    optimize = next(c for c in body["commands"] if c["id"] == "/optimize")
    assert optimize["status"] == "unavailable"
    assert optimize["error"] == "not_implemented"


def test_commands_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["commands"] is True


def test_dispatch_clear_drops_messages(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "first")
    assert (
        len(client.get(f"/v1/sessions/{sid}/messages").json()["messages"]) == 2
    )

    resp = client.post(f"/v1/sessions/{sid}/commands/clear").json()
    assert resp["command"] == "/clear"
    assert "cleared" in resp["result"]["text"]
    # /clear drops the conversation but leaves a synthetic command_result
    # message so the TUI shows visible "Cleared." confirmation
    # (commit 6b01c39 — "backend cmds materialise"). Real user/assistant
    # turns must be gone; only the synthetic confirmation may remain.
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    real_msgs = [m for m in msgs if m.get("metadata", {}).get("synthetic") != "command_result"]
    assert real_msgs == [], f"non-synthetic messages survived /clear: {real_msgs}"
    # message_count tracks real turns; synthetic confirmations don't count.
    sess = client.get(f"/v1/sessions/{sid}").json()
    assert sess["message_count"] == 0


def test_dispatch_cache_stats_returns_arc_numbers(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = client.post(f"/v1/sessions/{sid}/commands/cache-stats").json()
    # ARC isn't wired in this fixture so all zeros — just assert
    # the line shape.
    text = resp["result"]["text"]
    assert "hits=" in text
    assert "misses=" in text


def test_dispatch_optimize_returns_structured_not_implemented(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = client.post(f"/v1/sessions/{sid}/commands/optimize")

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["command"] == "/optimize"
    assert body["error"]["details"]["recovery_actions"] == [
        "retry_after_optimizer_support_lands",
        "exit",
    ]
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert msgs == []


def test_dispatch_unknown_command_404s(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = client.post(f"/v1/sessions/{sid}/commands/nonsense")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "internal_error"


def test_dispatch_unknown_session_404s(client: TestClient) -> None:
    resp = client.post("/v1/sessions/sess_nope/commands/clear")
    assert resp.status_code == 404
