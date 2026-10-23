"""Plugin, agent and session-agent routes (``gact/routes/plugins.py``).

Every app here installs the hermetic test marketplace (``tests/fixtures/
plugin_marketplace``) as the trusted default ``clio`` marketplace: agent
``clio/standard`` over core + workspace, shell, visuals, delegation and
document-runtime (which hard-requires shell).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._harness import runner_module_builder
from tests.test_plugins.helpers import plugin, tools_module, write

FIXTURE_PLUGINS = {"workspace", "shell", "visuals", "delegation", "document-runtime"}
STANDARD_PLUGINS = [
    "core",
    "memory",
    "workspace",
    "shell",
    "visuals",
    "delegation",
    "document-runtime",
]


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as started:
        yield started


def _error(resp: Any) -> dict[str, Any]:
    return resp.json()["error"]


def _demo_marketplace(root: Path) -> Path:
    """A native marketplace ``demo`` with one executable plugin and one agent."""

    write(root / "ai.iowarp.clio" / "marketplace.json", json.dumps({"name": "demo"}))
    plugin(
        root / "plugins",
        "notes",
        ext={"requires": ["shell"]},
        files={"ai.iowarp.clio/tools/t.py": tools_module("get_weather_forecast")},
    )
    write(
        root / "agents" / "helper.py",
        """
        from clio_agent.sdk import Agent


        class Helper(Agent):
            id = "helper"
            version = "1.0.0"
            role = "a note-taking helper"
            plugins = Agent.plugins + ["notes"]
        """,
    )
    return root


# --------------------------------------------------------------------- plugins


def test_list_plugins_shows_core_and_the_default_marketplace_trusted(
    client: TestClient,
) -> None:
    resp = client.get("/v1/plugins")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_marketplace_error"] is None
    by_id = {row["id"]: row for row in body["plugins"]}

    assert FIXTURE_PLUGINS | {"core"} <= set(by_id)
    for plugin_id in FIXTURE_PLUGINS:
        row = by_id[plugin_id]
        assert row["marketplace"] == "clio"
        assert row["ref"] == f"clio/{plugin_id}"
        assert row["trusted"] is True
        assert row["pending_trust"] is False
    assert by_id["core"]["trusted"] is True
    assert by_id["document-runtime"]["requires"] == ["shell"]
    assert {"shell_bash", "prepare_execution_runtime"} <= set(by_id["shell"]["tools"])
    assert "ask_user" in by_id["core"]["tools"]


def test_installed_marketplace_is_untrusted_until_the_trust_step(
    client: TestClient, tmp_path: Path
) -> None:
    source = _demo_marketplace(tmp_path / "demo-market")

    installed = client.post("/v1/plugins/marketplaces", json={"source": str(source)})
    assert installed.status_code == 201, installed.text
    summary = installed.json()
    assert summary["marketplace"] == "demo"
    assert summary["trusted"] is False
    assert summary["agents"] == []

    markets = {
        row["name"]: row for row in client.get("/v1/plugins/marketplaces").json()["marketplaces"]
    }
    assert markets["demo"]["trusted"] is False
    assert markets["clio"]["trusted"] is True and markets["clio"]["default"] is True
    demo_rows = [
        r for r in client.get("/v1/plugins").json()["plugins"] if r["marketplace"] == "demo"
    ]
    assert demo_rows and all(row["trusted"] is False for row in demo_rows)

    # The untrusted marketplace's agent is not installed yet.
    missing = client.get("/v1/agents/resolve", params={"ref": "demo/helper"})
    assert missing.status_code == 404
    assert _error(missing)["error"] == "agent_not_installed"

    trusted = client.post("/v1/plugins/marketplaces/demo/trust")
    assert trusted.status_code == 200, trusted.text
    assert trusted.json()["agents"] == ["helper"]
    demo_rows = [
        r for r in client.get("/v1/plugins").json()["plugins"] if r["marketplace"] == "demo"
    ]
    assert {row["id"] for row in demo_rows} == {"notes"}
    assert all(row["trusted"] is True for row in demo_rows)

    resolved = client.get("/v1/agents/resolve", params={"ref": "demo/helper"})
    assert resolved.status_code == 200, resolved.text
    # ``notes`` hard-requires ``shell``, which comes from the default marketplace.
    assert resolved.json()["agent"]["id"] == "helper"
    assert [row["id"] for row in resolved.json()["plugins"]] == ["core", "memory", "notes", "shell"]


def test_marketplace_install_requires_a_source(client: TestClient) -> None:
    resp = client.post("/v1/plugins/marketplaces", json={})
    assert resp.status_code == 422
    assert _error(resp)["error"] == "invalid_request"


def test_trusting_an_unknown_marketplace_is_a_typed_404(client: TestClient) -> None:
    resp = client.post("/v1/plugins/marketplaces/nope/trust")
    assert resp.status_code == 404
    assert _error(resp)["error"] == "marketplace_not_installed"


# ---------------------------------------------------------------------- agents


def test_list_agents_lists_the_installed_standard_agent(client: TestClient) -> None:
    resp = client.get("/v1/agents")
    assert resp.status_code == 200, resp.text
    agents = {row["ref"]: row for row in resp.json()["agents"]}
    standard = agents["clio/standard"]
    assert standard["id"] == "standard"
    assert standard["kind"] == "agent"
    assert standard["marketplace"] == "clio"
    assert standard["name"] == "CLIO"


def test_resolve_preview_applies_add_and_remove(client: TestClient) -> None:
    base = client.get("/v1/agents/resolve", params={"ref": "clio/standard"})
    assert base.status_code == 200, base.text
    body = base.json()
    assert [row["id"] for row in body["plugins"]] == STANDARD_PLUGINS
    assert {"shell_bash", "fs_read_file", "spawn_agent_task"} <= set(body["tool_names"])
    assert body["prompt"]["text"]
    assert body["digest"]

    removed = client.get(
        "/v1/agents/resolve", params=[("ref", "clio/standard"), ("remove", "visuals")]
    )
    assert removed.status_code == 200, removed.text
    assert "visuals" not in [row["id"] for row in removed.json()["plugins"]]
    assert removed.json()["digest"] != body["digest"]

    added = client.get(
        "/v1/agents/resolve", params=[("ref", "clio/standard"), ("add", "orchestration")]
    )
    assert added.status_code == 200, added.text
    assert [row["id"] for row in added.json()["plugins"]] == [*STANDARD_PLUGINS, "orchestration"]
    assert {"run_workflow", "run_flow"} <= set(added.json()["tool_names"])

    # Additions apply before removals: a plugin both added and removed stays out.
    both = client.get(
        "/v1/agents/resolve",
        params=[("ref", "clio/standard"), ("add", "orchestration"), ("remove", "orchestration")],
    )
    assert both.status_code == 200, both.text
    assert "orchestration" not in [row["id"] for row in both.json()["plugins"]]


def test_resolve_preview_with_unknown_agent_is_a_typed_404(client: TestClient) -> None:
    resp = client.get("/v1/agents/resolve", params={"ref": "clio/nope"})
    assert resp.status_code == 404
    error = _error(resp)
    assert error["error"] == "agent_not_installed"
    assert error["details"]["ref"] == "clio/nope"


def test_resolve_preview_adding_an_uninstalled_plugin_is_a_typed_422(
    client: TestClient,
) -> None:
    resp = client.get("/v1/agents/resolve", params=[("ref", "clio/standard"), ("add", "ghost")])
    assert resp.status_code == 422, resp.text
    error = _error(resp)
    assert error["error"] == "agent_resolution_failed"
    assert any(row["code"] == "plugin_not_installed" for row in error["details"]["errors"])


def test_preview_removal_shows_the_hard_requirement_cascade(client: TestClient) -> None:
    resp = client.post(
        "/v1/agents/preview-removal", json={"ref": "clio/standard", "plugin": "shell"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plugin"] == "shell"
    leaving = {row["id"] for row in body["removes"]}
    assert leaving == {"shell", "document-runtime"}
    assert {"shell_bash", "prepare_document_runtime", "prepare_document"} <= set(
        body["tools_removed"]
    )
    assert "fs_read_file" not in body["tools_removed"]


def test_preview_removal_requires_a_plugin(client: TestClient) -> None:
    resp = client.post("/v1/agents/preview-removal", json={"ref": "clio/standard"})
    assert resp.status_code == 422
    assert _error(resp)["error"] == "invalid_request"


def test_user_agent_save_list_resolve_and_delete(client: TestClient) -> None:
    saved = client.post(
        "/v1/agents/user",
        json={
            "id": "lean",
            "extends": "clio/standard",
            "remove": ["visuals"],
            "name": "Lean CLIO",
            "description": "standard without visuals",
        },
    )
    assert saved.status_code == 201, saved.text
    entry = saved.json()
    assert entry["ref"] == "user/lean"
    assert entry["kind"] == "user"
    assert entry["name"] == "Lean CLIO"
    # A shipped parent is followed within its major version.
    assert entry["extends"] == "clio/standard@^1"
    assert entry["delta"] == {"add": [], "remove": ["visuals"]}

    refs = {row["ref"] for row in client.get("/v1/agents").json()["agents"]}
    assert {"clio/standard", "user/lean"} <= refs

    resolved = client.get("/v1/agents/resolve", params={"ref": "user/lean"})
    assert resolved.status_code == 200, resolved.text
    plugins = [row["id"] for row in resolved.json()["plugins"]]
    assert "visuals" not in plugins and "shell" in plugins
    assert resolved.json()["agent"]["kind"] == "user"

    deleted = client.delete("/v1/agents/user/lean")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": "user/lean"}
    refs = {row["ref"] for row in client.get("/v1/agents").json()["agents"]}
    assert "user/lean" not in refs


def test_user_agent_requires_id_and_parent(client: TestClient) -> None:
    resp = client.post("/v1/agents/user", json={"id": "lean"})
    assert resp.status_code == 422
    assert _error(resp)["error"] == "invalid_request"


def test_user_agent_with_unknown_parent_is_a_typed_404(client: TestClient) -> None:
    resp = client.post("/v1/agents/user", json={"id": "lean", "extends": "clio/nope"})
    assert resp.status_code == 404
    assert _error(resp)["error"] == "agent_not_installed"


def test_deleting_an_unknown_user_agent_is_a_typed_404(client: TestClient) -> None:
    resp = client.delete("/v1/agents/user/never")
    assert resp.status_code == 404
    assert _error(resp)["error"] == "user_agent_not_found"


# --------------------------------------------------------------- session agent


def test_session_agent_reports_the_pinned_resolution(client: TestClient) -> None:
    created = client.post("/v1/sessions", json={"title": "pinned"})
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]
    assert created.json()["metadata"]["agent_ref"] == "clio/standard"

    resp = client.get(f"/v1/sessions/{sid}/agent")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == sid
    assert body["subagent"] is False
    assert body["delta"] == {"add": [], "remove": []}
    assert [row["id"] for row in body["resolution"]["plugins"]] == STANDARD_PLUGINS
    assert body["resolution"]["digest"] == created.json()["metadata"]["agent_resolution"]


def test_session_agent_patch_repins_and_the_next_turn_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents import builders

    from .conftest import complete_turn

    seen: list[dict[str, Any]] = []

    def runner(base_agent: Any, agent_def: Any, question: str, session_id: str) -> Any:
        del base_agent, session_id
        seen.append(
            {
                "question": question,
                "plugins": list(agent_def.metadata["plugins"]),
                "tools": list(agent_def.tools),
                "digest": agent_def.metadata["resolution_digest"],
            }
        )
        from types import SimpleNamespace

        return SimpleNamespace(answer=f"ok {len(seen)}")

    monkeypatch.setattr(builders, "build_agent_module", runner_module_builder(runner))
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=object())) as client:
        sid = client.post("/v1/sessions", json={"title": "live delta"}).json()["id"]
        complete_turn(client, sid, "first")

        patched = client.patch(f"/v1/sessions/{sid}/agent", json={"remove": ["shell"]})
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["delta"] == {"add": [], "remove": ["shell"]}
        plugins = [row["id"] for row in body["resolution"]["plugins"]]
        assert "shell" not in plugins and "document-runtime" not in plugins
        new_digest = body["resolution"]["digest"]

        metadata = client.get(f"/v1/sessions/{sid}").json()["metadata"]
        assert metadata["agent_delta"] == {"add": [], "remove": ["shell"]}
        assert metadata["agent_resolution"] == new_digest

        complete_turn(client, sid, "second")

    assert [row["question"] for row in seen] == ["first", "second"]
    assert "shell" in seen[0]["plugins"] and "shell_bash" in seen[0]["tools"]
    assert seen[1]["digest"] == new_digest != seen[0]["digest"]
    assert "shell" not in seen[1]["plugins"] and "document-runtime" not in seen[1]["plugins"]
    assert "shell_bash" not in seen[1]["tools"]
    assert "prepare_document" not in seen[1]["tools"]


def test_session_agent_patch_with_unresolvable_delta_is_a_typed_422(
    client: TestClient,
) -> None:
    sid = client.post("/v1/sessions", json={"title": "bad delta"}).json()["id"]
    before = client.get(f"/v1/sessions/{sid}").json()["metadata"]["agent_resolution"]

    resp = client.patch(f"/v1/sessions/{sid}/agent", json={"add": ["ghost"]})
    assert resp.status_code == 422, resp.text
    error = _error(resp)
    assert error["error"] == "agent_resolution_failed"
    assert error["details"]["ref"] == "clio/standard"
    assert any(row["code"] == "plugin_not_installed" for row in error["details"]["errors"])
    # A refused change leaves the session's pin untouched.
    assert client.get(f"/v1/sessions/{sid}").json()["metadata"]["agent_resolution"] == before


def test_session_agent_routes_404_for_an_unknown_session(client: TestClient) -> None:
    for resp in (
        client.get("/v1/sessions/sess_missing/agent"),
        client.patch("/v1/sessions/sess_missing/agent", json={"remove": ["shell"]}),
    ):
        assert resp.status_code == 404
        error = _error(resp)
        assert error["error"] == "not_found"
        assert error["details"]["session_id"] == "sess_missing"


def test_session_create_refuses_an_unresolvable_agent(client: TestClient) -> None:
    resp = client.post(
        "/v1/sessions", json={"title": "nope", "metadata": {"agent_ref": "clio/nope"}}
    )
    assert resp.status_code == 422, resp.text
    error = _error(resp)
    assert error["error"] == "agent_unavailable"
    assert error["details"]["agent_ref"] == "clio/nope"
    assert error["details"]["reason"] == "agent_not_installed"
