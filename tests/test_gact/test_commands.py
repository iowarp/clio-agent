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
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_prompt_agent)
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


def test_workspace_command_file_is_listed_and_dispatches_to_builtin_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return _Pred(answer="FILE_REVIEW_OK", selected_expert=agent_def.id)

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_prompt_agent)
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
                "agent: main",
                "argument-hint: <path>",
                "arguments:",
                "- path",
                "---",
                "Review $ARGUMENTS at {{args.path}} with {{agent_id}}.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
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

    assert resp["result"]["text"] == "FILE_REVIEW_OK"
    assert calls == [("main", "Review src/app.py at src/app.py with main.", sid)]


def test_agent_invocable_command_planner_visibility_and_allowed_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        calls.append((agent_def.id, question, session_id))
        return _Pred(answer="SUMMARY_OK", selected_expert=agent_def.id)

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)
    monkeypatch.setattr("clio_agent.gact.app._run_blueprint_dspy_agent", fake_prompt_agent)
    monkeypatch.chdir(tmp_path)
    command_dir = tmp_path / ".clio" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "summarize.md").write_text(
        "\n".join(
            [
                "---",
                "name: summarize",
                "title: Summarize",
                "description: Summarize a dataset",
                "agent: main",
                "agent-invocable: true",
                "arguments:",
                "- path",
                "---",
                "Summarize {{args.path}}.",
            ]
        ),
        encoding="utf-8",
    )
    (command_dir / "user_only.md").write_text(
        "\n".join(
            [
                "---",
                "name: user-only",
                "title: User Only",
                "agent: main",
                "agent-invocable: false",
                "---",
                "User only.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    c.post(
        "/v1/agents",
        json={
            "id": "caller",
            "title": "Caller",
            "system_prompt": "Call allowed commands.",
            "metadata": {"commands": ["/summarize"]},
        },
    )
    sid = c.post("/v1/sessions", json={"title": "t", "agent": {"id": "caller"}}).json()["id"]

    planner = c.get("/v1/commands", params={"planner": "true", "agent_id": "caller"}).json()[
        "commands"
    ]
    visible_ids = {row["id"] for row in planner}
    assert "/summarize" in visible_ids
    assert "/user-only" not in visible_ids

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
    assert calls == [("main", "Summarize data.csv.", sid)]
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
                "agent: main",
                "agent-invocable: true",
                "---",
                "Summarize.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    c.post(
        "/v1/agents",
        json={"id": "caller", "title": "Caller", "system_prompt": "No commands."},
    )
    sid = c.post("/v1/sessions", json={"title": "t", "agent": {"id": "caller"}}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/commands/summarize",
        json={"caller": {"type": "agent", "agent_id": "caller"}},
    )

    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["error"] == "command_denied"
    assert body["details"]["audit"]["status"] == "denied"
    assert c.app.state.command_audit[-1]["error"] == "command /summarize is not allowed for agent caller"


def test_agent_invocable_command_invalid_args_records_audit(
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
                "agent: main",
                "agent-invocable: true",
                "arguments:",
                "- path",
                "---",
                "Summarize {{args.path}}.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    c.post(
        "/v1/agents",
        json={
            "id": "caller",
            "title": "Caller",
            "system_prompt": "Call allowed commands.",
            "metadata": {"commands": ["/summarize"]},
        },
    )
    sid = c.post("/v1/sessions", json={"title": "t", "agent": {"id": "caller"}}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/commands/summarize",
        json={"caller": {"type": "agent", "agent_id": "caller"}, "args": {}},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "invalid_arguments"
    assert c.app.state.command_audit[-1]["status"] == "failed"
    assert c.app.state.command_audit[-1]["error"] == "invalid_arguments"


def test_expert_pack_command_allowlist_controls_planner_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".clio" / "experts").mkdir(parents=True)
    (tmp_path / ".clio" / "commands").mkdir(parents=True)
    (tmp_path / ".clio" / "experts" / "dataset.md").write_text(
        """---
id: dataset_expert
title: Dataset Expert
parent_id: main
tier: 2
commands: [summarize-dataset]
---
Work with datasets.
""",
        encoding="utf-8",
    )
    (tmp_path / ".clio" / "commands" / "summarize-dataset.md").write_text(
        "\n".join(
            [
                "---",
                "name: summarize-dataset",
                "agent: main",
                "agent-invocable: true",
                "---",
                "Summarize.",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".clio" / "commands" / "other.md").write_text(
        "\n".join(
            [
                "---",
                "name: other",
                "agent: main",
                "agent-invocable: true",
                "---",
                "Other.",
            ]
        ),
        encoding="utf-8",
    )
    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))

    planner = c.get(
        "/v1/commands",
        params={"planner": "true", "agent_id": "dataset_expert"},
    ).json()["commands"]

    assert {row["id"] for row in planner} == {"/summarize-dataset"}


def test_agent_blueprint_packaged_command_discovery_dispatch_and_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_prompt_agent(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent
        calls.append((agent_def.id, question, session_id))
        return _Pred(answer="VALIDATION_OK", selected_expert=agent_def.id)

    monkeypatch.setattr("clio_agent.gact.app._run_prompt_user_agent", fake_prompt_agent)
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    blueprint_root = workspace / ".clio" / "agent-blueprints" / "qc-agent"
    (blueprint_root / "experts").mkdir(parents=True)
    (blueprint_root / "commands").mkdir()
    (blueprint_root / "AGENT.md").write_text(
        """---
id: qc-agent
version: 0.1.0
title: QC Agent
root_expert: root
blueprint:
  format: agent-blueprint-v1
---
Quality-control agent.
""",
        encoding="utf-8",
    )
    (blueprint_root / "experts" / "root.md").write_text(
        """---
id: root
title: QC Root
tier: 1
commands:
  - validate-dataset
---
Coordinate dataset checks.
""",
        encoding="utf-8",
    )
    (blueprint_root / "commands" / "validate-dataset.md").write_text(
        """---
name: validate-dataset
title: Validate Dataset
description: Validate a dataset before analysis
agent: root
agent-invocable: true
arguments:
  - path
---
Validate {{args.path}} for this QC workflow.
""",
        encoding="utf-8",
    )

    c = TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent()))
    wid = c.post(
        "/v1/workspaces",
        json={
            "name": "Workspace",
            "root_path": str(workspace),
            "storage_root": str(workspace / ".clio"),
        },
    ).json()["id"]
    sid = c.post("/v1/sessions", json={"title": "t", "workspace_id": wid}).json()["id"]
    assert c.post(
        f"/v1/sessions/{sid}/agent-blueprint",
        json={"blueprint_id": "qc-agent"},
    ).status_code == 200

    workspace_commands = c.get("/v1/commands", params={"workspace_id": wid}).json()["commands"]
    assert "/validate-dataset" not in {row["id"] for row in workspace_commands}

    session_commands = c.get("/v1/commands", params={"session_id": sid}).json()["commands"]
    packaged = next(row for row in session_commands if row["id"] == "/validate-dataset")
    assert packaged["command_source"] == "agent_blueprint"
    assert packaged["agent_blueprint_id"] == "qc-agent"
    assert packaged["command_scope"] == "agent_blueprint"
    assert packaged["command_path"].endswith("commands/validate-dataset.md")

    planner_commands = c.get(
        "/v1/commands",
        params={"session_id": sid, "planner": "true", "agent_id": "root"},
    ).json()["commands"]
    assert {row["id"] for row in planner_commands} == {"/validate-dataset"}

    denied = c.post(
        f"/v1/sessions/{sid}/commands/validate-dataset",
        json={
            "caller": {"type": "agent", "agent_id": "missing"},
            "args": {"path": "data.csv"},
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["audit"]["status"] == "denied"

    resp = c.post(
        f"/v1/sessions/{sid}/commands/validate-dataset",
        json={
            "caller": {"type": "agent", "agent_id": "root"},
            "args": {"path": "data.csv"},
        },
    ).json()

    assert resp["result"]["text"] == "VALIDATION_OK"
    assert resp["result"]["audit"]["caller_type"] == "agent"
    assert resp["result"]["audit"]["caller_agent_id"] == "root"
    assert resp["result"]["audit"]["command_source"] == "agent_blueprint"
    assert calls == [("root", "Validate data.csv for this QC workflow.", sid)]


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
        row["id"] for row in c.get("/v1/commands", params={"workspace_id": wid_a}).json()["commands"]
    }
    ids_b = {
        row["id"] for row in c.get("/v1/commands", params={"workspace_id": wid_b}).json()["commands"]
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
