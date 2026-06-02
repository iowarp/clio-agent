from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.types import Message, Part


class _FakeARC:
    def get_cache_stats(self) -> dict[str, Any]:
        return {
            "hits": 0,
            "misses": 0,
            "hit_rate": 0.0,
            "capacity": 1000,
            "conv_index_size": 0,
            "inv_index_size": 0,
        }


def _write_workspace_pack(root: Path, *, pack_id: str, marker: str) -> None:
    pack = root / ".clio" / "agent-blueprints" / pack_id
    _write_pack_at(pack, pack_id=pack_id, marker=marker)


def _write_global_pack(config_root: Path, *, pack_id: str, marker: str) -> None:
    pack = config_root / "agent-blueprints" / pack_id
    _write_pack_at(pack, pack_id=pack_id, marker=marker)


def _write_pack_at(pack: Path, *, pack_id: str, marker: str) -> None:
    (pack / "experts").mkdir(parents=True)
    pack.joinpath("AGENT.md").write_text(
        f"""---
id: {pack_id}
version: 0.1.0
title: Workspace Scoped Agent
root_expert: main
blueprint:
  format: agent-blueprint-v1
---
{marker}
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "main.md").write_text(
        f"""---
id: main
title: Workspace Main
tier: 1
---
{marker} root prompt.
""",
        encoding="utf-8",
    )
    pack.joinpath("experts", "domain.md").write_text(
        f"""---
id: domain
title: Workspace Domain Expert
parent_id: main
tier: 2
tools:
  - memory_search_sessions
---
{marker} domain prompt.
""",
        encoding="utf-8",
    )


def _write_workspace_command(root: Path, *, name: str, marker: str) -> None:
    command_dir = root / ".clio" / "commands"
    _write_command_at(command_dir, name=name, marker=marker)


def _write_global_command(config_root: Path, *, name: str, marker: str) -> None:
    command_dir = config_root / "commands"
    _write_command_at(command_dir, name=name, marker=marker)


def _write_command_at(command_dir: Path, *, name: str, marker: str) -> None:
    command_dir.mkdir(parents=True)
    command_dir.joinpath(f"{name}.md").write_text(
        f"""---
name: {name}
agent: main
agent-invocable: true
---
{marker} command body.
""",
        encoding="utf-8",
    )


def _add_text_message(
    client: TestClient,
    session_id: str,
    *,
    message_id: str,
    text: str,
    created_at: str,
) -> None:
    client.app.state.messages.setdefault(session_id, []).append(
        Message(
            id=message_id,
            session_id=session_id,
            role="assistant",
            created_at=created_at,
            updated_at=created_at,
            parts=[Part(id=f"{message_id}_part", type="text", text=text)],
        )
    )
    client.app.state.sessions.update(
        session_id,
        message_count=len(client.app.state.messages[session_id]),
    )


def test_workspace_local_global_blueprints_commands_and_memory_do_not_leak(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Pre-benchmark proof for local/global workspace scoping semantics."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    ws_a_root = tmp_path / "workspace-a"
    ws_b_root = tmp_path / "workspace-b"
    ws_a_root.mkdir()
    ws_b_root.mkdir()
    global_root = home / ".config" / "clio-agent"
    _write_workspace_pack(ws_a_root, pack_id="shared-agent", marker="WORKSPACE_A")
    _write_workspace_pack(ws_b_root, pack_id="shared-agent", marker="WORKSPACE_B")
    _write_global_pack(global_root, pack_id="global-agent", marker="GLOBAL")
    _write_workspace_command(ws_a_root, name="shared-command", marker="WORKSPACE_A")
    _write_workspace_command(ws_b_root, name="shared-command", marker="WORKSPACE_B")
    _write_global_command(global_root, name="global-command", marker="GLOBAL")

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=_FakeARC())
    with TestClient(app) as client:
        ws_a = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace A",
                "root_path": str(ws_a_root),
                "storage_root": str(ws_a_root / ".clio"),
            },
        ).json()["id"]
        ws_b = client.post(
            "/v1/workspaces",
            json={
                "name": "Workspace B",
                "root_path": str(ws_b_root),
                "storage_root": str(ws_b_root / ".clio"),
            },
        ).json()["id"]
        sid_a = client.post(
            "/v1/sessions",
            json={"title": "A current", "workspace_id": ws_a},
        ).json()["id"]
        sid_a_prior = client.post(
            "/v1/sessions",
            json={"title": "A prior", "workspace_id": ws_a},
        ).json()["id"]
        sid_b = client.post(
            "/v1/sessions",
            json={"title": "B current", "workspace_id": ws_b},
        ).json()["id"]
        sid_global = client.app.state.sessions.create(
            title="Global reference",
            workspace_id="ws_global",
        ).id

        assert client.post(
            f"/v1/sessions/{sid_a}/agent-blueprint",
            json={"blueprint_id": "shared-agent"},
        ).status_code == 200
        assert client.post(
            f"/v1/sessions/{sid_b}/agent-blueprint",
            json={"blueprint_id": "shared-agent"},
        ).status_code == 200

        prompts_a = client.get("/v1/agents", params={"session_id": sid_a}).json()["agents"]
        prompts_b = client.get("/v1/agents", params={"session_id": sid_b}).json()["agents"]
        commands_a = client.get("/v1/commands", params={"workspace_id": ws_a}).json()[
            "commands"
        ]
        commands_b = client.get("/v1/commands", params={"workspace_id": ws_b}).json()[
            "commands"
        ]
        commands_default = client.get("/v1/commands", params={"workspace_id": ws_a}).json()[
            "commands"
        ]
        listed_a = client.get("/v1/agent-blueprints", params={"workspace_id": ws_a}).json()[
            "agent_blueprints"
        ]
        listed_b = client.get("/v1/agent-blueprints", params={"workspace_id": ws_b}).json()[
            "agent_blueprints"
        ]
        listed_global = client.get("/v1/agent-blueprints").json()["agent_blueprints"]

        _add_text_message(
            client,
            sid_a_prior,
            message_id="msg_a_prior",
            text="Workspace A pressure dataset alpha should be reusable with intent.",
            created_at="2026-05-25T12:00:00+00:00",
        )
        _add_text_message(
            client,
            sid_b,
            message_id="msg_b",
            text="Workspace B pressure dataset beta must not leak into workspace A.",
            created_at="2026-05-26T12:00:00+00:00",
        )
        _add_text_message(
            client,
            sid_global,
            message_id="msg_global",
            text="Global pressure dataset gamma requires explicit global scope.",
            created_at="2026-05-27T12:00:00+00:00",
        )
        memory_a = client.get(
            "/v1/memory/search",
            params={
                "query": "pressure dataset",
                "session_id": sid_a,
                "workspace_id": ws_a,
                "include_cross_session": "true",
                "limit": 10,
            },
        )
        denied_b_from_a = client.get(
            "/v1/memory/search",
            params={
                "query": "pressure dataset",
                "session_id": sid_b,
                "workspace_id": ws_a,
                "include_cross_session": "true",
            },
        )
        global_with_intent = client.post(
            f"/v1/sessions/{sid_a}/memory/tools/search-sessions",
            json={
                "query": "pressure dataset",
                "scope": "global",
                "limit": 10,
            },
        )
        memory_tool_audit = list(client.app.state.memory_tool_audit)

    agents_a = {row["id"]: row for row in prompts_a}
    agents_b = {row["id"]: row for row in prompts_b}
    assert agents_a["main"]["metadata"]["definition_path"].startswith(str(ws_a_root))
    assert agents_b["main"]["metadata"]["definition_path"].startswith(str(ws_b_root))
    assert "WORKSPACE_A" in agents_a["main"]["system_prompt"]
    assert "WORKSPACE_B" in agents_b["main"]["system_prompt"]

    blueprint_a = next(row for row in listed_a if row["id"] == "shared-agent")
    blueprint_b = next(row for row in listed_b if row["id"] == "shared-agent")
    global_blueprint_a = next(row for row in listed_a if row["id"] == "global-agent")
    global_blueprint_b = next(row for row in listed_b if row["id"] == "global-agent")
    global_blueprint_default = next(row for row in listed_global if row["id"] == "global-agent")
    assert blueprint_a["definition_path"].startswith(str(ws_a_root))
    assert blueprint_b["definition_path"].startswith(str(ws_b_root))
    assert global_blueprint_a["scope"] == "global"
    assert global_blueprint_b["scope"] == "global"
    assert global_blueprint_default["definition_path"].startswith(str(global_root))

    command_a = next(row for row in commands_a if row["id"] == "/shared-command")
    command_b = next(row for row in commands_b if row["id"] == "/shared-command")
    global_command_a = next(row for row in commands_default if row["id"] == "/global-command")
    assert command_a["source"] == "user"
    assert command_b["source"] == "user"
    assert command_a["command_path"].startswith(str(ws_a_root))
    assert command_b["command_path"].startswith(str(ws_b_root))
    assert command_a["command_path"] != command_b["command_path"]
    assert global_command_a["command_path"].startswith(str(global_root))
    assert global_command_a["command_source"] == "clio_user"

    assert memory_a.status_code == 200, memory_a.text
    memory_body = memory_a.json()
    assert memory_body["metadata"]["workspace_scope"] == "workspace"
    assert set(memory_body["searched_sessions"]) == {sid_a, sid_a_prior}
    assert {hit["session_id"] for hit in memory_body["hits"]} == {sid_a_prior}
    assert all(hit["workspace_id"] == ws_a for hit in memory_body["hits"])

    assert denied_b_from_a.status_code == 403
    denied = denied_b_from_a.json()["error"]
    assert denied["error"] == "permission_error"
    assert denied["details"]["scope"] == "other_workspace"

    assert global_with_intent.status_code == 200, global_with_intent.text
    global_body = global_with_intent.json()
    assert global_body["metadata"]["workspace_scope"] == "global"
    assert global_body["metadata"]["policy_decision"] == "allow_global_user_intent"
    assert global_body["searched_sessions"] == [sid_global]
    assert {hit["session_id"] for hit in global_body["hits"]} == {sid_global}

    global_audit = next(
        row
        for row in memory_tool_audit
        if row.get("id") == global_body["metadata"]["audit_id"]
    )
    assert global_audit["tool_name"] == "memory_search_sessions"
    assert global_audit["status"] == "completed"
    assert global_audit["policy_decision"] == "allow_global_user_intent"
    assert global_audit["scope"] == "global"
    assert global_audit["session_id"] == sid_a
