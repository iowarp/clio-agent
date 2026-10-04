"""Marketplace configuration edits retain identity, pins and installed snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprint_sources import (
    install_agent_blueprint_source,
    refresh_agent_blueprint_source,
)
from clio_agent.gact.agent_blueprints import read_install_metadata
from clio_agent.gact.app import build_app
from clio_agent.gact.blueprint_drafts import authoring_root
from clio_agent.gact.blueprint_source_configuration import (
    SourceConfigurationConflict,
    require_unchanged,
    update_configuration,
)


def _pack(root: Path, content: str = "Original") -> Path:
    root.mkdir(parents=True)
    (root / "AGENT.md").write_text(
        f"---\nid: demo\nversion: 1.0.0\ntitle: Demo\nroot_expert: main\n---\n{content}\n",
        encoding="utf-8",
    )
    (root / "experts").mkdir()
    (root / "experts/main.md").write_text(
        "---\nid: main\ntitle: Main\ntier: 1\nmodule:\n  kind: react\nprompt_id: demo.main\n---\nCoordinate.\n",
        encoding="utf-8",
    )
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as current:
        yield current


def test_save_then_reload_preserves_identity_and_runtime_until_reload(
    client: TestClient, tmp_path: Path
) -> None:
    first = _pack(tmp_path / "first")
    second = _pack(tmp_path / "second", "New revision")
    added = client.post("/v1/agent-blueprints/sources", json={"source": str(first)})
    assert added.status_code == 201, added.text
    row = added.json()["source"]
    installed = added.json()["installed"][0]
    root = Path(installed["root"])
    endpoint = f"/v1/agent-blueprints/sources/{row['id']}"
    saved = client.patch(
        endpoint, json={"source": str(second), "expected_updated_at": row["updated_at"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"]["reload_required"] is True
    assert saved.json()["source"]["id"] == row["id"]
    assert (root / "AGENT.md").read_text().endswith("Original\n")
    assert (
        client.patch(
            endpoint, json={"name": "stale", "expected_updated_at": row["updated_at"]}
        ).status_code
        == 409
    )
    with pytest.raises(SourceConfigurationConflict):
        require_unchanged(row)
    reloaded = client.post(endpoint + "/refresh")
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["source"]["reload_required"] is False
    current = reloaded.json()["installed"][0]
    assert current["root"] == installed["root"]
    assert current["identity"] == installed["identity"]
    assert (root / "AGENT.md").read_text().endswith("New revision\n")
    assert read_install_metadata(root)["source"] == str(second)


def test_workspace_registrations_are_distinct_and_cannot_cross_workspaces(
    client: TestClient, tmp_path: Path
) -> None:
    source = _pack(tmp_path / "source")
    rows = [
        client.post("/v1/agent-blueprints/sources", json={"source": str(source)}).json()["source"]
    ]
    for name in ("a", "b"):
        folder = tmp_path / name
        folder.mkdir()
        workspace = client.post(
            "/v1/workspaces", json={"name": name, "root_path": str(folder)}
        ).json()
        response = client.post(
            "/v1/agent-blueprints/sources",
            json={"source": str(source), "scope": "workspace", "workspace_id": workspace["id"]},
        )
        assert response.status_code == 201, response.text
        rows.append(response.json()["source"])
    assert len({row["id"] for row in rows}) == 3
    refused = client.post(
        "/v1/agent-blueprints/install",
        json={
            "source_id": rows[1]["id"],
            "scope": "workspace",
            "workspace_id": rows[2]["workspace_id"],
        },
    )
    assert refused.status_code == 422
    assert (
        client.post(
            "/v1/agent-blueprints/install", json={"source": str(source), "scope": "workspace"}
        ).status_code
        == 404
    )


def test_configured_checkout_is_used_for_authoring_only(client: TestClient, tmp_path: Path) -> None:
    source = _pack(tmp_path / "source")
    checkout = _pack(tmp_path / "checkout", "Author revision")
    response = client.post("/v1/agent-blueprints/sources", json={"source": str(source)}).json()
    row = response["source"]
    root = Path(response["installed"][0]["root"])
    saved = update_configuration(
        row["id"], {"working_checkout": str(checkout), "expected_updated_at": row["updated_at"]}
    )
    assert not saved.get("reload_required")
    assert authoring_root(root) == checkout
    assert (root / "AGENT.md").read_text().endswith("Original\n")


def test_remote_discovery_and_reload_honor_explicit_pin(client: TestClient, tmp_path: Path) -> None:
    source = _pack(tmp_path / "git")
    _git(source, "init", "-b", "main")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "first",
    )
    first = _git(source, "rev-parse", "HEAD")
    (source / "AGENT.md").write_text(
        (source / "AGENT.md").read_text().replace("Original", "Second")
    )
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-am",
        "second",
    )
    second = _git(source, "rev-parse", "HEAD")
    response = client.post(
        "/v1/agent-blueprints/sources",
        json={"source": source.as_uri(), "ref": "main", "pinned_commit": first},
    )
    assert response.status_code == 201, response.text
    row = response.json()["source"]
    assert row["commit"] == first
    installed = response.json()["installed"][0]
    root = Path(installed["root"])
    assert (root / "AGENT.md").read_text().endswith("Original\n")
    update_configuration(
        row["id"], {"pinned_commit": second, "expected_updated_at": row["updated_at"]}
    )
    response = client.post(
        f"/v1/agent-blueprints/{installed['identity']}/reload", json={"scope": "global"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["installed"][0]["identity"] == installed["identity"]
    assert read_install_metadata(root)["pinned_commit"] == second
    assert (root / "AGENT.md").read_text().endswith("Second\n")


def test_discovery_refuses_dirty_pinned_local_source(tmp_path: Path) -> None:
    source = _pack(tmp_path / "git")
    _git(source, "init")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "first",
    )
    pin = _git(source, "rev-parse", "HEAD")
    (source / "AGENT.md").write_text("dirty")
    result = refresh_agent_blueprint_source({"source": str(source), "pinned_commit": pin})
    assert result["status"] == "error"
    assert result["available_blueprints"] == []


def test_reload_refuses_concurrent_configuration_edit(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.routes import blueprints as routes

    source = _pack(tmp_path / "source")
    response = client.post("/v1/agent-blueprints/sources", json={"source": str(source)}).json()
    row = response["source"]
    root = Path(response["installed"][0]["root"])
    prior = read_install_metadata(root)
    original = routes._refresh_agent_blueprint_source

    def edit_during_fetch(current: dict[str, Any]) -> dict[str, Any]:
        prepared = original(current)
        update_configuration(
            current["id"],
            {"name": "Edited elsewhere", "expected_updated_at": current["updated_at"]},
        )
        return prepared

    monkeypatch.setattr(routes, "_refresh_agent_blueprint_source", edit_during_fetch)
    refreshed = client.post(f"/v1/agent-blueprints/sources/{row['id']}/refresh")
    assert refreshed.status_code == 409, refreshed.text
    assert read_install_metadata(root) == prior
    rows = client.get("/v1/agent-blueprints/sources").json()["sources"]
    assert next(item for item in rows if item["id"] == row["id"])["name"] == "Edited elsewhere"


def test_branch_movement_after_discovery_does_not_change_prepared_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path / "user"))
    source = _pack(tmp_path / "git")
    _git(source, "init", "-b", "main")
    _git(source, "add", ".")
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "first",
    )
    prepared = refresh_agent_blueprint_source(
        {"id": "src_prepared", "source": source.as_uri(), "ref": "main"}
    )
    first = prepared["commit"]
    (source / "AGENT.md").write_text(
        (source / "AGENT.md").read_text().replace("Original", "Moved branch")
    )
    _git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-am",
        "second",
    )
    updated, result = install_agent_blueprint_source(prepared, cwd=tmp_path, scope="workspace")
    assert updated["status"] == "ready"
    installed = result["installed"][0]
    assert installed["install"]["commit"] == first
    assert installed["install"]["pinned_commit"] == ""
    assert Path(installed["root"], "AGENT.md").read_text().endswith("Original\n")
