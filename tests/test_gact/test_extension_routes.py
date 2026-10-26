"""Use the real extension routes, store, loader and resolver without external services."""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agents.resolution import SessionAgentUnavailable, agent_server_set
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_user_configuration import remove_user_mcp_server, set_user_mcp_server
from clio_agent.plugins.servers import server_set_declarations


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as started:
        yield started


def test_create_inspect_variation_and_compose_saved_connection(client: TestClient) -> None:
    response = client.post(
        "/v1/plugins/local",
        json={
            "name": "ha-notes",
            "skill": "---\nname: ha-notes\ndescription: Home notes\n---\n# Original guidance",
        },
    )
    assert response.status_code == 201, response.text
    catalog = client.get("/v1/plugins").json()["plugins"]
    original = next(row for row in catalog if row["id"] == "ha-notes")
    inspection = client.get("/v1/plugins/inspect", params={"ref": original["ref"]}).json()
    assert not inspection["incomplete"]
    scoped = client.get("/v1/plugins/inspect", params={"ref": original["ref"], "skill": "ha-notes"})
    assert scoped.status_code == 200, scoped.text
    assert [row["path"] for row in scoped.json()["files"]] == ["SKILL.md"]
    assert (
        client.get(
            "/v1/plugins/inspect", params={"ref": original["ref"], "skill": "../outside"}
        ).status_code
        == 404
    )
    files = {row["path"]: row["text"] for row in inspection["files"]}
    skill_path = next(path for path in files if path.endswith("SKILL.md"))
    files[skill_path] = files[skill_path].replace("Original", "Local")
    variant = client.post("/v1/plugins/local", json={"name": "ha-notes-local", "files": files})
    assert variant.status_code == 201, variant.text
    unchanged = client.get("/v1/plugins/inspect", params={"ref": original["ref"]}).json()
    assert "Original guidance" in next(
        row["text"] for row in unchanged["files"] if row["path"] == skill_path
    )
    assert client.post("/v1/plugins/marketplaces/ha-notes/trust").status_code == 200
    set_user_mcp_server(
        "home",
        {
            "transport": "http",
            "url": "https://example.invalid/mcp",
            "headers": {"Secret": "private"},
        },
    )
    composed = client.post(
        "/v1/plugins/local",
        json={"name": "my-ha", "requires": [original["ref"]], "connections": {"ha": "home"}},
    )
    assert composed.status_code == 201, composed.text
    assert client.post("/v1/plugins/marketplaces/my-ha/trust").status_code == 200
    preview = client.get(
        "/v1/agents/resolve", params={"ref": "clio/standard", "add": "my-ha/my-ha"}
    )
    assert preview.status_code == 200, preview.text
    assert "ha" in preview.json()["mcp_servers"]
    # Mounting receives the runtime connection, while the portable source contains only its name.
    saved = client.post(
        "/v1/agents/user",
        json={"id": "test-ha", "extends": "clio/standard", "add": ["my-ha/my-ha"]},
    )
    assert saved.status_code == 201, saved.text
    declarations = server_set_declarations(agent_server_set(client.app, "user/test-ha"))
    assert declarations["my-ha"]["ha"]["headers"]["Secret"] == "private"
    inspect_composed = client.get("/v1/plugins/inspect", params={"ref": "my-ha/my-ha"})
    assert "private" not in inspect_composed.text
    remove_user_mcp_server("home")
    with pytest.raises(SessionAgentUnavailable, match="Add the saved MCP connection 'home'"):
        agent_server_set(client.app, "user/test-ha")


def test_upload_portable_skill_and_reject_duplicate_name(client: TestClient) -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "portable/SKILL.md",
            "---\nname: portable\ndescription: Portable guidance\n---\n# Guidance",
        )
        archive.writestr("portable/references/details.md", "# Reference")
    body = {"archive": base64.b64encode(raw.getvalue()).decode()}
    response = client.post("/v1/plugins/upload?name=portable", json=body)
    assert response.status_code == 201, response.text
    duplicate = client.post("/v1/plugins/upload?name=portable", json=body)
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.text
    assert (
        client.post("/v1/plugins/upload?name=bad", json={"archive": "!not-base64"}).status_code
        == 422
    )
