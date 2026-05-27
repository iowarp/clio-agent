"""iowarp/clio-agent#14: backend slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class _BrokenARC:
    def get_cache_stats(self) -> dict[str, object]:
        raise RuntimeError("ARC stats unavailable")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))


def test_commands_listed(client: TestClient) -> None:
    body = client.get("/v1/commands").json()
    ids = {c["id"] for c in body["commands"]}
    assert "/clear" in ids
    assert "/cache-stats" in ids
    clear = next(c for c in body["commands"] if c["id"] == "/clear")
    assert clear["status"] == "available"
    assert clear["enabled"] is True
    assert clear["error"] == ""
    optimize = next(c for c in body["commands"] if c["id"] == "/optimize")
    assert optimize["status"] == "unavailable"
    assert optimize["enabled"] is False
    assert optimize["error"] == "not_implemented"
    assert optimize["disabled_reason"]


def test_commands_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["commands"] is True


def test_dispatch_clear_drops_messages(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "first")
    assert len(client.get(f"/v1/sessions/{sid}/messages").json()["messages"]) == 2

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
    permission = next(iter(client.app.state.permissions.values()))
    assert permission["tool_call"]["tool_name"] == "gact.session.clear"
    assert permission["reason"] == "user_requested_session_clear"


def test_dispatch_clear_obeys_permission_policy(client: TestClient) -> None:
    from .conftest import complete_turn

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    complete_turn(client, sid, "first")
    client.put(
        "/v1/policies",
        json={
            "policies": [
                {
                    "scope": "session",
                    "scope_id": sid,
                    "tool_name_pattern": "gact.session.clear",
                    "action": "deny",
                }
            ]
        },
    )

    resp = client.post(f"/v1/sessions/{sid}/commands/clear")

    assert resp.status_code == 403
    assert len(client.get(f"/v1/sessions/{sid}/messages").json()["messages"]) == 2
    permission = next(iter(client.app.state.permissions.values()))
    assert permission["status"] == "auto_denied"
    assert permission["tool_call"]["tool_name"] == "gact.session.clear"


def test_dispatch_cache_stats_returns_arc_numbers(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = client.post(f"/v1/sessions/{sid}/commands/cache-stats").json()
    # ARC isn't wired in this fixture so all zeros — just assert
    # the line shape.
    text = resp["result"]["text"]
    assert "hits=" in text
    assert "misses=" in text


def test_user_agent_command_listed_and_dispatches_to_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return _Pred(answer="REVIEW_OK", selected_expert=agent_def.id)

    def fail_tool_agent(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("prompt-only user command should not use tool runner")

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)
    monkeypatch.setattr("clio_agent.gact.app._run_tool_user_agent", fail_tool_agent)
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    c.post(
        "/v1/agents",
        json={
            "id": "reviewer",
            "title": "Reviewer",
            "description": "Review code changes",
            "system_prompt": "Review carefully.",
            "metadata": {
                "commands": [
                    {
                        "id": "/review",
                        "description": "Review the supplied change",
                        "prompt_template": "Review this: {{input}}",
                    }
                ]
            },
        },
    )
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    listed = c.get("/v1/commands").json()["commands"]
    review = next(command for command in listed if command["id"] == "/review")
    assert review["source"] == "user"
    assert review["agent_id"] == "reviewer"
    assert review["status"] == "available"

    resp = c.post(f"/v1/sessions/{sid}/commands/review", json={"input": "diff --git"}).json()

    assert resp["result"]["type"] == "agent_message"
    assert resp["result"]["text"] == "REVIEW_OK"
    assert calls == [("reviewer", "Review this: diff --git", sid)]
    msgs = c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert msgs[0]["metadata"]["command"] == "/review"
    assert msgs[0]["metadata"]["agent_id"] == "reviewer"


def test_skill_frontmatter_command_is_listed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / ".codex" / "skills" / "explain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: explain-skill",
                "description: Explain a selected topic",
                "command: /explain",
                "prompt-template: Explain {{input}}",
                "---",
                "Use concise explanations.",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))

    commands = c.get("/v1/commands").json()["commands"]

    command = next(row for row in commands if row["id"] == "/explain")
    assert command["agent_id"] == "explain-skill"
    assert command["agent_source"] == "skill"
    assert command["prompt_template"] == "Explain {{input}}"


def test_disabled_user_agent_command_is_visible_but_not_runnable(tmp_path: Path) -> None:
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    c.post(
        "/v1/agents",
        json={
            "id": "future_agent",
            "title": "Future Agent",
            "metadata": {
                "commands": [
                    {
                        "id": "/future",
                        "status": "unavailable",
                        "enabled": False,
                        "error": "not_implemented",
                        "disabled_reason": "not ready yet",
                    }
                ]
            },
        },
    )
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    listed = c.get("/v1/commands").json()["commands"]
    command = next(row for row in listed if row["id"] == "/future")
    assert command["enabled"] is False

    resp = c.post(f"/v1/sessions/{sid}/commands/future")

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["disabled_reason"] == "not ready yet"


def test_dispatch_cache_stats_arc_failure_returns_structured_error(
    tmp_path: Path,
) -> None:
    client = TestClient(
        build_app(
            sessions_path=tmp_path / "s.json",
            agent=_Agent(),
            arc=_BrokenARC(),
        )
    )
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = client.post(f"/v1/sessions/{sid}/commands/cache-stats")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["error"] == "command_error"
    assert body["error"]["message"] == (
        "Backend command /cache-stats could not read ARC cache statistics."
    )
    assert body["error"]["details"]["command"] == "/cache-stats"
    assert body["error"]["details"]["original_error"] == "ARC stats unavailable"
    assert body["error"]["details"]["recovery_actions"] == [
        "retry",
        "reconfigure_provider",
        "exit",
    ]
    msgs = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    assert msgs == []


def test_dispatch_optimize_returns_structured_not_implemented(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = client.post(f"/v1/sessions/{sid}/commands/optimize")

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["command"] == "/optimize"
    assert body["error"]["details"]["status"] == "unavailable"
    assert body["error"]["details"]["disabled_reason"]
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
    assert body["error"]["error"] == "not_found"


def test_dispatch_unknown_session_404s(client: TestClient) -> None:
    resp = client.post("/v1/sessions/sess_nope/commands/clear")
    assert resp.status_code == 404
