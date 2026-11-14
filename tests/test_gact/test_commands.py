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
    plugins = Agent.plugins + ["workspace", "review", "summaries"]
"""


def _command(name: str, body: str, **meta: Any) -> str:
    lines = ["---", f"name: {name}"]
    for key, value in meta.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    return "\n".join([*lines, "---", body, ""])


def _command_marketplace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugins that ship commands (``review``, ``summaries``) and an agent that carries both."""

    summaries = {
        "ai.iowarp.clio/commands/data/summarize.md": _command(
            "summarize", "Summarize $1 as $2.", description="Summarize a dataset"
        ),
        "ai.iowarp.clio/commands/notes.md": _command("notes", "Summaries notes."),
        "ai.iowarp.clio/commands/goal.md": _command("goal", "Not the built-in."),
    }
    review = {
        "ai.iowarp.clio/commands/review.md": _command(
            "review", "Review this: $ARGUMENTS", description="Review the supplied change"
        ),
        "ai.iowarp.clio/commands/notes.md": _command("notes", "Review notes."),
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
    """The built-ins are the core plugin's command files; ``/optimize`` is gone."""

    rows = {c["id"]: c for c in client.get("/v1/commands").json()["commands"]}
    assert {"/clear", "/cache-stats", "/dump-trace", "/cron", "/loop", "/goal"} <= set(rows)
    assert "/optimize" not in rows
    clear = rows["/clear"]
    assert clear["kind"] == "builtin" and clear["handler"] == "clear"
    assert clear["qualified_id"] == "/core:clear" and clear["plugin"] == "core"
    assert clear["status"] == "available" and clear["enabled"] is True
    assert clear["path"].endswith("core/ai.iowarp.clio/commands/clear.md")
    assert rows["/cron"]["aliases"] == ["/schedule"]
    assert "prompt_template" not in clear and "root" not in clear


def test_an_alias_runs_its_builtin(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = client.post(f"/v1/sessions/{sid}/commands/schedule", json={"input": "list"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["command"] == "/cron"


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
        assert review["kind"] == "command" and review["source"] == "plugin"
        assert review["plugin"] == "review" and review["qualified_id"] == "/review:review"
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


def test_workspace_command_file_is_listed_and_dispatches_to_the_session_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _Recorder("FILE_REVIEW_OK")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    # The project is the conversations' folder (a first run's default is CLIO's data folder).
    monkeypatch.setenv("CLIO_DEFAULT_WORKSPACE", str(tmp_path))
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
                "- name: path",
                "  required: true",
                "---",
                "Review $1 ($ARGUMENTS).",
            ]
        ),
        encoding="utf-8",
    )
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

        listed = c.get("/v1/commands").json()["commands"]
        command = next(row for row in listed if row["id"] == "/review")
        assert command["source"] == "clio_workspace"
        assert command["qualified_id"] == "/project:review"
        assert command["argument_hint"] == "<path>"

        missing = c.post(f"/v1/sessions/{sid}/commands/review", json={})
        assert missing.status_code == 422
        assert missing.json()["error"]["details"]["missing"] == ["path"]

        resp = c.post(
            f"/v1/sessions/{sid}/commands/review", json={"input": '"src/my app.py" fast'}
        ).json()

        assert resp["result"]["type"] == "turn"
        _answer(c, sid)
        # No ``agent:`` field: the command runs on the session's agent.
        assert agent.questions == ['Review src/my app.py ("src/my app.py" fast).']


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
    # The project is the conversations' folder (a first run's default is CLIO's data folder).
    monkeypatch.setenv("CLIO_DEFAULT_WORKSPACE", str(tmp_path))
    command_dir = tmp_path / ".claude" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "summarize.md").write_text(
        "\n".join(
            [
                "---",
                "description: Summarize a file",
                "argument-hint: <file>",
                "allowed-tools: Read, Grep",
                "model: claude-sonnet",
                "---",
                "Summarize $ARGUMENTS",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))

    listed = c.get("/v1/commands").json()["commands"]

    command = next(row for row in listed if row["id"] == "/summarize")
    assert command["source"] == "claude_workspace"
    assert command["argument_hint"] == "<file>"
    # Listed, never acted on: they do not change the turn's model or tools.
    assert command["unsupported_fields"] == ["model", "allowed-tools"]
    assert "user_invocable" not in command and "agent_invocable" not in command


def test_command_file_with_shell_field_is_visible_but_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    # The project is the conversations' folder (a first run's default is CLIO's data folder).
    monkeypatch.setenv("CLIO_DEFAULT_WORKSPACE", str(tmp_path))
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

    assert command["disabled_reason"] == "command_shell_unsupported"

    resp = c.post(f"/v1/sessions/{sid}/commands/unsafe")

    assert resp.status_code == 409
    assert resp.json()["error"]["error"] == "command_shell_unsupported"


def test_plugin_commands_clash_reserve_and_label_their_subfolders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two plugins' ``/notes`` are ambiguous; a plugin's ``/goal`` is reserved; subfolders label."""

    _command_marketplace(tmp_path, monkeypatch)
    agent = _Recorder("OK")
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = _session(c, "clio/reviewer")
        body = c.get("/v1/commands", params={"session_id": sid}).json()
        rows = body["commands"]
        assert body["ambiguous"] == {"/notes": ["/review:notes", "/summaries:notes"]}
        notes = [row for row in rows if row["name"] == "/notes"]
        assert {row["id"] for row in notes} == {"/review:notes", "/summaries:notes"}
        assert all(row["ambiguous"] for row in notes)
        goal = next(row for row in rows if row["plugin"] == "summaries" and row["name"] == "/goal")
        assert goal["enabled"] is False and goal["disabled_reason"] == "command_name_reserved"
        summarize = next(row for row in rows if row["id"] == "/summarize")
        assert summarize["label"] == "data"

        refused = c.post(f"/v1/sessions/{sid}/commands/notes", json={})
        assert refused.status_code == 409
        assert refused.json()["error"]["error"] == "command_ambiguous"
        assert refused.json()["error"]["details"]["qualified"] == [
            "/review:notes",
            "/summaries:notes",
        ]

        ran = c.post(f"/v1/sessions/{sid}/commands/summaries:notes", json={})
        assert ran.status_code == 200, ran.text
        _answer(c, sid)
        assert agent.questions == ["Summaries notes."]
        user = next(
            m
            for m in c.get(f"/v1/sessions/{sid}/messages").json()["messages"]
            if m["role"] == "user"
        )
        assert [p["text"] for p in user["parts"] if p["type"] == "text"] == ["/notes"]


def test_positional_arguments_fill_a_subfolder_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _command_marketplace(tmp_path, monkeypatch)
    agent = _Recorder("OK")
    with TestClient(build_app(sessions_path=tmp_path / "s.json", agent=agent)) as c:
        sid = _session(c, "clio/reviewer")
        ran = c.post(
            f"/v1/sessions/{sid}/commands/summarize", json={"input": "'runs/a b.h5' a table"}
        )
        assert ran.status_code == 200, ran.text
        _answer(c, sid)
        assert agent.questions == ["Summarize runs/a b.h5 as a."]


def test_a_workspace_goal_file_is_reserved_and_goal_still_runs_the_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    # The project is the conversations' folder (a first run's default is CLIO's data folder).
    monkeypatch.setenv("CLIO_DEFAULT_WORKSPACE", str(tmp_path))
    (tmp_path / ".clio" / "commands").mkdir(parents=True)
    (tmp_path / ".clio" / "commands" / "goal.md").write_text("Not the goal.", encoding="utf-8")
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    rows = c.get("/v1/commands").json()["commands"]
    mine = next(row for row in rows if row["source"] == "clio_workspace")
    assert mine["name"] == "/goal" and mine["disabled_reason"] == "command_name_reserved"
    goal = next(row for row in rows if row["id"] == "/goal" and row["kind"] == "builtin")
    assert goal["enabled"] is True


def test_workspace_clio_file_shadows_claude_file_and_user_files_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    # The project is the conversations' folder (a first run's default is CLIO's data folder).
    monkeypatch.setenv("CLIO_DEFAULT_WORKSPACE", str(tmp_path))
    for folder, text in (
        (tmp_path / ".clio" / "commands", "From clio."),
        (tmp_path / ".claude" / "commands", "From claude."),
    ):
        folder.mkdir(parents=True)
        (folder / "hello.md").write_text(text, encoding="utf-8")
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "standup.md").write_text("Standup.", encoding="utf-8")
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    rows = c.get("/v1/commands").json()["commands"]
    hello = [row for row in rows if row["name"] == "/hello"]
    winner = next(row for row in hello if row["enabled"])
    loser = next(row for row in hello if not row["enabled"])
    assert winner["source"] == "clio_workspace"
    assert loser["source"] == "claude_workspace"
    assert loser["disabled_reason"] == "command_shadowed"
    assert loser["shadowed_by"] == winner["path"]
    standup = next(row for row in rows if row["id"] == "/standup")
    assert standup["source"] == "claude_user" and standup["qualified_id"] == "/user:standup"


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


def test_dispatch_unknown_command_404s(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    for name in ("nonsense", "optimize"):
        resp = client.post(f"/v1/sessions/{sid}/commands/{name}")
        assert resp.status_code == 404
        assert resp.json()["error"]["error"] == "command_unknown"


def test_dispatch_unknown_session_404s(client: TestClient) -> None:
    resp = client.post("/v1/sessions/sess_nope/commands/clear")
    assert resp.status_code == 404
