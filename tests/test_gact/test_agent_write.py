"""iowarp/clio-agent#19: dynamic agent registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_capability_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    assert body["capabilities"]["agent_write"] is True


def test_post_agent_then_list(client: TestClient) -> None:
    new = client.post(
        "/v1/agents",
        json={
            "id": "code_reviewer",
            "title": "Code Reviewer",
            "description": "Reviews diffs for style + correctness",
            "system_prompt": "Review the diff and report correctness issues.",
            "default_provider": "lm_studio",
            "default_model": "qwopus3.5-9b-v3",
            "parameters": {"temperature": 0.1, "max_tokens": 2048},
            "tier": 2,
            "specialization": "code_editing",
            "keywords": ["review", "lint"],
            "tools": ["fs_read_file"],
            "skills": ["code-review"],
            "commands": ["/review"],
            "capability_refs": [
                {
                    "kind": "command",
                    "id": "/review",
                    "title": "Review current change",
                    "source": "user",
                    "status": "available",
                }
            ],
        },
    )
    assert new.status_code == 201
    body = new.json()
    assert body["id"] == "code_reviewer"
    assert body["source"] == "user"
    assert body["tier"] == 2
    assert body["system_prompt"] == "Review the diff and report correctness issues."
    assert body["default_provider"] == "lm_studio"
    assert body["default_model"] == "qwopus3.5-9b-v3"
    assert body["parameters"] == {"temperature": 0.1, "max_tokens": 2048}
    assert body["skills"] == ["code-review"]
    assert body["commands"] == ["/review"]
    assert (
        body["capability_refs"][0]["kind"],
        body["capability_refs"][0]["id"],
        body["capability_refs"][0]["status"],
    ) == ("tool", "fs_read_file", "available")
    assert any(
        ref["kind"] == "command" and ref["id"] == "/review"
        for ref in body["capability_refs"]
    )

    # GET /v1/agents now includes it (and the built-ins).
    rows = client.get("/v1/agents").json()["agents"]
    ids = {a["id"] for a in rows}
    assert "code_reviewer" in ids
    assert "main" in ids  # built-in still listed
    listed = next(a for a in rows if a["id"] == "code_reviewer")
    assert listed["system_prompt"] == "Review the diff and report correctness issues."
    assert listed["default_model"] == "qwopus3.5-9b-v3"
    assert listed["skills"] == ["code-review"]
    assert listed["commands"] == ["/review"]


def test_put_agent_replaces_existing(client: TestClient) -> None:
    client.post(
        "/v1/agents",
        json={
            "id": "code_reviewer",
            "title": "Code Reviewer",
        },
    )
    resp = client.put(
        "/v1/agents/code_reviewer",
        json={
            "id": "ignored-by-server",
            "title": "Strict Code Reviewer",
            "description": "now stricter",
            "tier": 2,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # URL id wins over body id (server enforces).
    assert body["id"] == "code_reviewer"
    assert body["title"] == "Strict Code Reviewer"


def test_post_agent_refuses_builtin_id(client: TestClient) -> None:
    resp = client.post(
        "/v1/agents",
        json={
            "id": "data",
            "title": "Steal the built-in id",
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["error"] == "permission_error"
    assert "built-in" in body["error"]["message"]


def test_put_agent_refuses_builtin(client: TestClient) -> None:
    resp = client.put("/v1/agents/data", json={"id": "data"})
    assert resp.status_code == 409


def test_delete_user_agent_works(client: TestClient) -> None:
    client.post(
        "/v1/agents",
        json={
            "id": "to_drop",
            "title": "Drop me",
        },
    )
    resp = client.delete("/v1/agents/to_drop")
    assert resp.status_code == 204
    rows = client.get("/v1/agents").json()["agents"]
    assert all(a["id"] != "to_drop" for a in rows)


def test_delete_unknown_agent_404s(client: TestClient) -> None:
    resp = client.delete("/v1/agents/never_existed")
    assert resp.status_code == 404


def test_delete_builtin_refused(client: TestClient) -> None:
    resp = client.delete("/v1/agents/data")
    assert resp.status_code == 409


def test_persistence_round_trip(tmp_path: Path) -> None:
    """First app instance creates an agent; a second instance at
    the same path sees it."""

    c1 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    c1.post(
        "/v1/agents",
        json={
            "id": "persisted",
            "title": "x",
            "system_prompt": "Persist this prompt.",
            "default_provider": "openai",
            "default_model": "gpt-4o-mini",
            "parameters": {"temperature": 0},
            "skills": ["persisted-skill"],
            "commands": ["/persisted"],
        },
    )
    c2 = TestClient(build_app(sessions_path=tmp_path / "s.json"))
    rows = c2.get("/v1/agents").json()["agents"]
    restored = next(a for a in rows if a["id"] == "persisted")
    assert restored["system_prompt"] == "Persist this prompt."
    assert restored["default_provider"] == "openai"
    assert restored["default_model"] == "gpt-4o-mini"
    assert restored["parameters"] == {"temperature": 0}
    assert restored["skills"] == ["persisted-skill"]
    assert restored["commands"] == ["/persisted"]


def test_builtin_agents_surface_prompts(client: TestClient) -> None:
    rows = client.get("/v1/agents").json()["agents"]
    main = next(a for a in rows if a["id"] == "main")
    data = next(a for a in rows if a["id"] == "data")

    assert "CLIO's agent planner" in main["system_prompt"]
    assert "CLIO Data Expert" in data["system_prompt"]


def test_skill_files_do_not_materialize_agents(monkeypatch, tmp_path: Path) -> None:
    """Sabotage twin (#918): a SKILL.md on disk must not appear as an agent,
    and using its id as an agent id is a typed 400, not a silent not-found."""

    skill_dir = tmp_path / ".claude" / "skills" / "tui-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tui-test\ndescription: Tests TUI behavior\n---\nBody.",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    local = TestClient(build_app(sessions_path=tmp_path / "s.json"))

    listed = {a["id"] for a in local.get("/v1/agents").json()["agents"]}
    assert "tui-test" not in listed

    resp = local.get("/v1/commands", params={"planner": "true", "agent_id": "tui-test"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["error"] == "skill_not_delegatable"
    assert "skills:" in body["error"]["message"]
    assert body["error"]["details"]["skill_id"] == "tui-test"


def test_skill_frontmatter_parses_via_catalog(monkeypatch, tmp_path: Path) -> None:
    """The parsing surface the old skill-agents carried now lives on SkillRef."""

    from clio_agent.gact.skills import SkillCatalog, _skill_list_field

    codex_skill = tmp_path / ".codex" / "skills" / "tui-test"
    codex_skill.mkdir(parents=True)
    (codex_skill / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: tui-test",
                "description: Tests Bubbletea UI behavior",
                "allowed-tools:",
                "- fs_read_file",
                "- shell_bash",
                "keywords: tui,testing",
                "---",
                "Use deterministic TUI testing workflows.",
            ]
        ),
        encoding="utf-8",
    )
    nested = tmp_path / ".agents" / "skills" / "source-command" / "wtfp-help"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "Help users with source-command workflows.", encoding="utf-8"
    )

    refs = {r.id: r for r in SkillCatalog(home=tmp_path / "no-home", cwd=tmp_path).discover()}
    codex_ref = refs["tui-test"]
    assert codex_ref.description == "Tests Bubbletea UI behavior"
    assert _skill_list_field(codex_ref.meta, "allowed-tools") == ["fs_read_file", "shell_bash"]
    assert _skill_list_field(codex_ref.meta, "keywords") == ["tui", "testing"]
    assert codex_ref.layout == "skill_md"
    assert codex_ref.source == "codex"
    assert codex_ref.body == "Use deterministic TUI testing workflows."
    assert refs["wtfp-help"].description == "Help users with source-command workflows."
    assert refs["wtfp-help"].source == "agents"

