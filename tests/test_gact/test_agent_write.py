"""iowarp/clio-agent#19: user-defined agents.

A user agent is a parent agent plus a delta (``extends``, ``add``, ``remove``),
saved to the user's own marketplace (``user/<id>``) from the + menu, never a
flattened ``AgentDef`` row. ``POST /v1/agents/user`` saves (or replaces) one,
``DELETE /v1/agents/user/{id}`` removes it, and ``GET /v1/agents`` lists it
next to the installed agents.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        yield client


def _plugins(client: TestClient, ref: str) -> list[str]:
    resp = client.get("/v1/agents/resolve", params={"ref": ref})
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()["plugins"]]


def test_capability_advertised(client: TestClient) -> None:
    capabilities = client.get("/v1/capabilities").json()["capabilities"]
    # No GACT agent-write routes: user agents are saved through the plugin routes.
    assert capabilities["agent_write"] is False
    assert capabilities["x_clio_agent_plugins"] is True


def test_post_user_agent_then_list(client: TestClient) -> None:
    new = client.post(
        "/v1/agents/user",
        json={
            "id": "code-reviewer",
            "name": "Code Reviewer",
            "description": "Reviews diffs for style + correctness",
            "extends": "clio/standard",
            "add": ["orchestration"],
            "remove": ["visuals"],
        },
    )
    assert new.status_code == 201, new.text
    body = new.json()
    assert body["ref"] == "user/code-reviewer"
    assert body["id"] == "code-reviewer"
    assert body["kind"] == "user"
    assert body["marketplace"] == "user"
    assert body["name"] == "Code Reviewer"
    assert body["description"] == "Reviews diffs for style + correctness"
    assert body["extends"] == "clio/standard@^1"
    assert body["delta"] == {"add": ["orchestration"], "remove": ["visuals"]}

    rows = client.get("/v1/agents").json()["agents"]
    refs = {a["ref"] for a in rows}
    assert "user/code-reviewer" in refs
    assert "clio/standard" in refs  # the shipped agent is still listed
    listed = next(a for a in rows if a["ref"] == "user/code-reviewer")
    assert listed["delta"] == {"add": ["orchestration"], "remove": ["visuals"]}

    plugins = _plugins(client, "user/code-reviewer")
    assert "orchestration" in plugins
    assert "visuals" not in plugins
    assert "visuals" in _plugins(client, "clio/standard"), "the parent is not changed"


def test_saving_again_replaces_the_user_agent(client: TestClient) -> None:
    first = client.post("/v1/agents/user", json={"id": "reviewer", "extends": "clio/standard"})
    assert first.status_code == 201, first.text
    resp = client.post(
        "/v1/agents/user",
        json={
            "id": "reviewer",
            "extends": "clio/standard",
            "name": "Strict Code Reviewer",
            "remove": ["shell"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Strict Code Reviewer"
    assert body["delta"] == {"add": [], "remove": ["shell"]}

    rows = [a for a in client.get("/v1/agents").json()["agents"] if a["ref"] == "user/reviewer"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Strict Code Reviewer"
    assert "shell" not in _plugins(client, "user/reviewer")


def test_post_user_agent_refuses_an_invalid_id(client: TestClient) -> None:
    resp = client.post(
        "/v1/agents/user", json={"id": "Not A Valid Id!", "extends": "clio/standard"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "user_agent_id_invalid"


def test_post_user_agent_refuses_an_unresolvable_delta(client: TestClient) -> None:
    resp = client.post(
        "/v1/agents/user",
        json={"id": "ghostly", "extends": "clio/standard", "add": ["ghost"]},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["error"] == "agent_resolution_failed"
    refs = {a["ref"] for a in client.get("/v1/agents").json()["agents"]}
    assert "user/ghostly" not in refs, "a refused user agent must not be saved"


def test_delete_user_agent_works(client: TestClient) -> None:
    client.post("/v1/agents/user", json={"id": "to-drop", "extends": "clio/standard"})
    resp = client.delete("/v1/agents/user/to-drop")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "user/to-drop"}
    rows = client.get("/v1/agents").json()["agents"]
    assert all(a["ref"] != "user/to-drop" for a in rows)


def test_delete_unknown_user_agent_404s(client: TestClient) -> None:
    resp = client.delete("/v1/agents/user/never-existed")
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "user_agent_not_found"


def test_delete_cannot_remove_a_shipped_agent(client: TestClient) -> None:
    """Only user agents are deletable; a shipped agent's id is not a user agent."""

    resp = client.delete("/v1/agents/user/standard")
    assert resp.status_code == 404
    refs = {a["ref"] for a in client.get("/v1/agents").json()["agents"]}
    assert "clio/standard" in refs


def test_persistence_round_trip(tmp_path: Path) -> None:
    """First app instance saves a user agent; a second instance sees it."""

    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c1:
        saved = c1.post(
            "/v1/agents/user",
            json={
                "id": "persisted",
                "name": "Persisted",
                "description": "Persist this agent.",
                "extends": "clio/standard",
                "remove": ["delegation"],
            },
        )
        assert saved.status_code == 201, saved.text
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as c2:
        rows = c2.get("/v1/agents").json()["agents"]
        restored = next(a for a in rows if a["ref"] == "user/persisted")
        assert restored["name"] == "Persisted"
        assert restored["description"] == "Persist this agent."
        assert restored["extends"] == "clio/standard@^1"
        assert restored["delta"] == {"add": [], "remove": ["delegation"]}
        assert "delegation" not in _plugins(c2, "user/persisted")


def test_session_runs_a_user_agent_by_ref(client: TestClient) -> None:
    client.post(
        "/v1/agents/user",
        json={"id": "lean", "extends": "clio/standard", "remove": ["visuals"]},
    )
    created = client.post(
        "/v1/sessions", json={"title": "lean", "metadata": {"agent_ref": "user/lean"}}
    )
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]
    assert created.json()["metadata"]["agent_ref"] == "user/lean"

    agent = client.get(f"/v1/sessions/{sid}/agent").json()["resolution"]
    assert agent["agent"]["kind"] == "user"
    assert "visuals" not in [row["id"] for row in agent["plugins"]]


def test_skill_files_do_not_materialize_agents(monkeypatch, tmp_path: Path) -> None:
    """Sabotage twin (#918): a SKILL.md on disk must not appear as an agent,
    and using its id as a session's agent is a typed refusal, not a silent run."""

    skill_dir = tmp_path / ".claude" / "skills" / "tui-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: tui-test\ndescription: Tests TUI behavior\n---\nBody.",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as local:
        listed = local.get("/v1/agents").json()["agents"]
        assert all("tui-test" not in (a["id"], a["ref"]) for a in listed)

        resp = local.post(
            "/v1/sessions", json={"title": "skill", "metadata": {"agent_ref": "tui-test"}}
        )
        assert resp.status_code == 422, resp.text
        error = resp.json()["error"]
        assert error["error"] == "agent_unavailable"
        assert error["details"]["agent_ref"] == "tui-test"
        assert error["details"]["reason"] == "agent_not_installed"


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
    (nested / "SKILL.md").write_text("Help users with source-command workflows.", encoding="utf-8")

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
