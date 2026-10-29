"""iowarp/clio-agent#14: backend slash commands."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._plugin_fixtures import custom_marketplace

# Default sessions run their plugin agent; route it to each test's
# ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, *args, **kwargs):
        return _Pred()


COMMAND_AGENTS = """
from clio_agent.sdk import Agent


class Reviewer(Agent):
    id = "reviewer"
    version = "1.0.0"
    role = "a code reviewer"
    plugins = Agent.plugins + ["workspace", "review"]


class Caller(Agent):
    id = "caller"
    version = "1.0.0"
    role = "an agent that calls commands"
    plugins = Agent.plugins + ["workspace", "summaries"]
"""

#: Command dispatch runs the resolved agent through this seam.
RUNNER = "clio_agent.gact.routes.catalog.run_resolved_agent"


def _command(name: str, body: str, **meta: Any) -> str:
    lines = ["---", f"name: {name}"]
    for key, value in meta.items():
        key = key.replace("_", "-") if key in {"agent_invocable"} else key
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join([*lines, "---", body, ""])


def _command_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugins that ship commands (``review``, ``summaries``) and agents that carry them."""

    summaries = {
        "ai.iowarp.clio/commands/summarize.md": _command(
            "summarize",
            "Summarize {{args.path}}.",
            description="Summarize a dataset",
            agent_invocable="true",
            arguments=["path"],
        ),
        "ai.iowarp.clio/commands/user_only.md": _command(
            "user-only", "User only.", agent_invocable="false"
        ),
        "ai.iowarp.clio/commands/future.md": _command(
            "future",
            "Later.",
            status="unavailable",
            enabled="false",
            error="not_implemented",
            disabled_reason="not ready yet",
        ),
    }
    review = {
        "ai.iowarp.clio/commands/review.md": _command(
            "review", "Review this: {{input}}", description="Review the supplied change"
        )
    }
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={"review": {"files": review}, "summaries": {"files": summaries}},
        agents={"command_agents.py": COMMAND_AGENTS},
    )


def _session(client: TestClient, agent_ref: str) -> str:
    response = client.post(
        "/v1/sessions", json={"title": "t", "metadata": {"agent_ref": agent_ref}}
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    # ENTERED: test_dispatch_clear_drops_messages / test_dispatch_clear_obeys_
    # permission_policy drive a real POST /messages turn via complete_turn. An
    # un-entered TestClient gives every request its own transient anyio portal,
    # which is torn down (cancelling the still-running turn) the instant the ack
    # lands -- before the assistant reply ever settles.
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent())) as test_client:
        yield test_client


def test_goal_command_retains_a_clickable_notice_without_starting_twice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json
    import os

    from clio_agent.gact.protocol.v3.message import message_to_v3

    launches: list[str] = []

    async def launch(app: Any, sid: str) -> None:
        launches.append(sid)

    monkeypatch.setattr("clio_agent.gact.goal_control.launch_pending_goal", launch)
    sid = client.post("/v1/sessions", json={"title": "Goal presentation"}).json()["id"]
    response = client.post(
        f"/v1/sessions/{sid}/commands/goal", json={"input": "Continue until the report is ready"}
    )
    assert response.status_code == 200
    assert launches == [sid]
    messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
    receipt = next(m for m in messages if m.get("metadata", {}).get("command") == "/goal")
    assert receipt["parts"][0]["type"] == "notice"
    projected = message_to_v3(receipt)
    assert projected["blocks"][0]["code"] == "goal_started"
    assert projected["blocks"][0]["text"] == "Continue until the report is ready"
    assert "[/goal]" not in json.dumps(projected)
    work = client.get(f"/v1/sessions/{sid}/work")
    assert work.status_code == 200
    review = {"message": projected, "work": work.json()}
    (tmp_path / "goal-notice-review.json").write_text(json.dumps(review), encoding="utf-8")
    if destination := os.environ.get("CLIO_GOAL_REVIEW_OUTPUT"):
        Path(destination).write_text(json.dumps(review, indent=2), encoding="utf-8")
    cleared = client.post(f"/v1/sessions/{sid}/commands/goal", json={"input": "clear"})
    assert cleared.status_code == 200
    latest = next(
        row
        for row in client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        if row["id"] != receipt["id"] and row.get("metadata", {}).get("command") == "/goal"
    )
    assert message_to_v3(latest)["blocks"][0]["code"] == "goal_stopped"
    assert launches == [sid]


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
    # #801: uniform structured not-implemented — shared reason code + #633 pointer.
    assert optimize["error"] == "optimizer_not_implemented"
    assert "633" in optimize["disabled_reason"]


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


class _Recorder:
    """A host agent that records what the model is asked, optionally held until released."""

    def __init__(self, answer: str, hold: threading.Event | None = None) -> None:
        self.answer = answer
        self.hold = hold
        self.questions: list[str] = []
        self.started = threading.Event()

    def forward(self, question: str = "", *args: Any, **kwargs: Any) -> _Pred:
        self.questions.append(question)
        self.started.set()
        if self.hold is not None:
            assert self.hold.wait(timeout=30), "the test never released the command turn"
        return _Pred(answer=self.answer)


def _answer(client: TestClient, sid: str) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        messages = client.get(f"/v1/sessions/{sid}/messages").json()["messages"]
        done = [m for m in messages if m["role"] == "assistant" and m.get("stop_reason")]
        if done:
            return done[0]
        time.sleep(0.05)
    raise AssertionError("the command turn never settled")


def test_plugin_command_listed_and_runs_as_a_turn_of_the_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The person's command is their message; the model receives the rendered recipe."""

    _command_marketplace(tmp_path, monkeypatch)
    agent = _Recorder("REVIEW_OK")
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = _session(c, "clio/reviewer")

        listed = c.get("/v1/commands", params={"session_id": sid}).json()["commands"]
        review = next(command for command in listed if command["id"] == "/review")
        assert review["source"] == "user"
        assert review["command_scope"] == "plugin" and review["plugin"] == "review"
        assert review["status"] == "available"
        # Without a session, no agent's plugins are in scope.
        assert "/review" not in {row["id"] for row in c.get("/v1/commands").json()["commands"]}

        resp = c.post(f"/v1/sessions/{sid}/commands/review", json={"input": "diff --git"}).json()

        assert resp.get("result", {}).get("type") == "turn", resp
        answer = _answer(c, sid)
        assert any(p.get("text") == "REVIEW_OK" for p in answer["parts"])
        assert agent.questions == ["Review this: diff --git"]
        user = next(
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "user"
        )
        assert [p["text"] for p in user["parts"] if p["type"] == "text"] == ["/review diff --git"]


def test_a_command_turn_is_accepted_at_once_while_the_agent_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """iowarp/clio-agent#755: a slow command never holds the request (or the server)."""

    _command_marketplace(tmp_path, monkeypatch)
    release = threading.Event()
    agent = _Recorder("SLOW_OK", hold=release)
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = _session(c, "clio/reviewer")
        try:
            resp = c.post(f"/v1/sessions/{sid}/commands/review", json={"input": "diff --git"})
            assert resp.status_code == 200 and resp.json()["result"]["type"] == "turn"
            assert agent.started.wait(timeout=30), "the command turn never started"
            assert c.get("/v1/health").status_code in {200, 503}
        finally:
            release.set()
        answer = _answer(c, sid)
        assert any(p.get("text") == "SLOW_OK" for p in answer["parts"])


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
    # Since #918 skills are not agents: skill commands dispatch to `main`, and
    # the declared template is COMPOSED with the skill body (the procedure the
    # old skill-agent carried as its system prompt) — never instead of it.
    assert command["agent_id"] == "main"
    assert command["agent_source"] == "skill"
    assert command["prompt_template"].startswith("Explain {{input}}")
    assert "Use concise explanations." in command["prompt_template"]


def test_workspace_command_file_is_listed_and_dispatches_to_the_session_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _Recorder("FILE_REVIEW_OK")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".clio" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "review.md").write_text(
        "\n".join(
            [
                "---",
                "name: review",
                "title: Review",
                "description: Review a supplied path",
                "argument-hint: <path>",
                "arguments:",
                "- path",
                "---",
                "Review $ARGUMENTS at {{args.path}} with {{agent_id}}.",
            ]
        ),
        encoding="utf-8",
    )
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

        listed = c.get("/v1/commands").json()["commands"]
        command = next(row for row in listed if row["id"] == "/review")
        assert command["agent_source"] == "command_file"
        assert command["command_source"] == "clio_workspace"
        assert command["argument_hint"] == "<path>"

        missing = c.post(f"/v1/sessions/{sid}/commands/review", json={"args": {}})
        assert missing.status_code == 422
        assert missing.json()["error"]["details"]["missing"] == ["path"]

        resp = c.post(
            f"/v1/sessions/{sid}/commands/review",
            json={"args": {"path": "src/app.py"}},
        ).json()

        assert resp["result"]["type"] == "turn"
        _answer(c, sid)
        # No ``agent:`` field: the command runs on the session's agent.
        assert agent.questions == ["Review src/app.py at src/app.py with standard."]


def test_agent_invocable_command_planner_visibility_and_allowed_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent may invoke exactly the agent-invocable commands its plugins ship."""

    calls: list[tuple[str, str, str]] = []

    def fake_runner(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return _Pred(answer="SUMMARY_OK", selected_expert=agent_def.id)

    _command_marketplace(tmp_path, monkeypatch)
    monkeypatch.setattr(RUNNER, fake_runner)
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    sid = _session(c, "clio/caller")

    for params in ({"agent_id": "clio/caller"}, {"session_id": sid}):
        planner = c.get("/v1/commands", params={"planner": "true", **params}).json()["commands"]
        visible_ids = {row["id"] for row in planner}
        assert "/summarize" in visible_ids
        assert "/user-only" not in visible_ids
    other = c.get("/v1/commands", params={"planner": "true", "agent_id": "clio/reviewer"})
    assert "/summarize" not in {row["id"] for row in other.json()["commands"]}

    resp = c.post(
        f"/v1/sessions/{sid}/commands/summarize",
        json={
            "caller": {"type": "agent", "agent_id": "caller"},
            "args": {"path": "data.csv"},
        },
    ).json()

    assert resp["result"]["text"] == "SUMMARY_OK"
    assert resp["result"]["audit"]["caller_type"] == "agent"
    assert resp["result"]["audit"]["caller_agent_id"] == "caller"
    assert resp["result"]["audit"]["args"] == {"path": "data.csv"}
    assert calls == [("caller", "Summarize data.csv.", sid)]
    assert c.app.state.command_audit[-1]["status"] == "completed"


def test_agent_invocable_command_denies_missing_allowlist_and_records_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".clio" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "summarize.md").write_text(
        "\n".join(
            [
                "---",
                "name: summarize",
                "agent-invocable: true",
                "---",
                "Summarize.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    # The session's agent ships no commands, so it may invoke none.
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/commands/summarize",
        json={"caller": {"type": "agent", "agent_id": "standard"}},
    )

    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["error"] == "command_denied"
    assert body["details"]["audit"]["status"] == "denied"
    assert (
        c.app.state.command_audit[-1]["error"]
        == "command /summarize is not allowed for agent standard"
    )


def test_agent_invocable_command_invalid_args_records_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _command_marketplace(tmp_path, monkeypatch)
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    sid = _session(c, "clio/caller")

    resp = c.post(
        f"/v1/sessions/{sid}/commands/summarize",
        json={"caller": {"type": "agent", "agent_id": "caller"}, "args": {}},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "invalid_arguments"
    assert c.app.state.command_audit[-1]["status"] == "failed"
    assert c.app.state.command_audit[-1]["error"] == "invalid_arguments"


def test_workspace_commands_do_not_leak_between_workspace_requests(tmp_path: Path) -> None:
    ws_a = tmp_path / "workspace-a"
    ws_b = tmp_path / "workspace-b"
    for workspace, command_id in ((ws_a, "a-command"), (ws_b, "b-command")):
        command_dir = workspace / ".clio" / "commands"
        command_dir.mkdir(parents=True)
        (command_dir / f"{command_id}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"name: {command_id}",
                    "agent: main",
                    "agent-invocable: true",
                    "---",
                    f"Run {command_id}.",
                ]
            ),
            encoding="utf-8",
        )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    wid_a = c.post(
        "/v1/workspaces",
        json={"name": "A", "root_path": str(ws_a), "storage_root": str(ws_a / ".clio")},
    ).json()["id"]
    wid_b = c.post(
        "/v1/workspaces",
        json={"name": "B", "root_path": str(ws_b), "storage_root": str(ws_b / ".clio")},
    ).json()["id"]

    ids_a = {
        row["id"]
        for row in c.get("/v1/commands", params={"workspace_id": wid_a}).json()["commands"]
    }
    ids_b = {
        row["id"]
        for row in c.get("/v1/commands", params={"workspace_id": wid_b}).json()["commands"]
    }

    assert "/a-command" in ids_a
    assert "/b-command" not in ids_a
    assert "/b-command" in ids_b
    assert "/a-command" not in ids_b


def test_compatible_claude_command_file_is_listed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".claude" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "summarize.md").write_text(
        "\n".join(
            [
                "---",
                "name: summarize",
                "user-invocable: true",
                "agent-invocable: false",
                "---",
                "Summarize {{input}}",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))

    listed = c.get("/v1/commands").json()["commands"]

    command = next(row for row in listed if row["id"] == "/summarize")
    assert command["command_source"] == "claude_workspace"
    assert command["user_invocable"] is True
    assert command["agent_invocable"] is False


def test_command_file_with_shell_field_is_visible_but_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".clio" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "unsafe.md").write_text(
        "\n".join(
            [
                "---",
                "name: unsafe",
                "shell: rm -rf /tmp/nope",
                "---",
                "This must not execute.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    listed = c.get("/v1/commands").json()["commands"]
    command = next(row for row in listed if row["id"] == "/unsafe")
    assert command["status"] == "unsupported"
    assert command["enabled"] is False

    resp = c.post(f"/v1/sessions/{sid}/commands/unsafe")

    assert resp.status_code == 501
    assert resp.json()["error"]["error"] == "not_supported"


def test_disabled_plugin_command_is_visible_but_not_runnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _command_marketplace(tmp_path, monkeypatch)
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    sid = _session(c, "clio/caller")

    listed = c.get("/v1/commands", params={"session_id": sid}).json()["commands"]
    command = next(row for row in listed if row["id"] == "/future")
    assert command["enabled"] is False

    resp = c.post(f"/v1/sessions/{sid}/commands/future")

    assert resp.status_code == 501
    body = resp.json()
    assert body["error"]["error"] == "not_implemented"
    assert body["error"]["details"]["disabled_reason"] == "not ready yet"


def test_dispatch_cache_stats_arc_failure_returns_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_stats() -> dict[str, object]:
        raise RuntimeError("ARC stats unavailable")

    app = build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    with TestClient(app) as client:
        created = client.post("/v1/sessions", json={"title": "t"})
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        monkeypatch.setattr(app.state.arc, "get_cache_stats", broken_stats)
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
    # #801: uniform structured not-implemented — shared reason code + #633 pointer.
    assert body["error"]["error"] == "optimizer_not_implemented"
    assert "633" in body["error"]["message"]
    assert body["error"]["details"]["command"] == "/optimize"
    assert body["error"]["details"]["status"] == "unavailable"
    assert "633" in body["error"]["details"]["disabled_reason"]
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
