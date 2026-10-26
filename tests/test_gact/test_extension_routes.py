"""Use the real extension routes, store, loader and resolver without external services."""

from __future__ import annotations

import base64
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from clio_agent.gact.agents.resolution import SessionAgentUnavailable, agent_server_set
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_user_configuration import remove_user_mcp_server, set_user_mcp_server
from clio_agent.plugins.servers import server_set_declarations
from fastapi.testclient import TestClient


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
    assert client.post("/v1/plugins/marketplaces/private-local/trust").status_code == 200
    set_user_mcp_server(
        "home",
        {
            "transport": "http",
            "url": "https://example.invalid/mcp",
            "headers": {"Secret": "test-credential-not-for-export"},
        },
    )
    composed = client.post(
        "/v1/plugins/local",
        json={"name": "my-ha", "requires": [original["ref"]], "connections": {"ha": "home"}},
    )
    assert composed.status_code == 201, composed.text
    assert client.post("/v1/plugins/marketplaces/private-local/trust").status_code == 200
    preview = client.get(
        "/v1/agents/resolve", params={"ref": "clio/standard", "add": "private-local/my-ha"}
    )
    assert preview.status_code == 200, preview.text
    assert "ha" in preview.json()["mcp_servers"]
    # Mounting receives the runtime connection, while the portable source contains only its name.
    saved = client.post(
        "/v1/agents/user",
        json={"id": "test-ha", "extends": "clio/standard", "add": ["private-local/my-ha"]},
    )
    assert saved.status_code == 201, saved.text
    declarations = server_set_declarations(agent_server_set(client.app, "user/test-ha"))
    assert declarations["my-ha"]["ha"]["headers"]["Secret"] == "test-credential-not-for-export"
    inspect_composed = client.get("/v1/plugins/inspect", params={"ref": "private-local/my-ha"})
    assert "test-credential-not-for-export" not in inspect_composed.text
    remove_user_mcp_server("home")
    with pytest.raises(SessionAgentUnavailable, match="Add the saved MCP connection 'home'"):
        agent_server_set(client.app, "user/test-ha")


def test_private_marketplace_export_and_contextual_upload(client: TestClient) -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "portable/SKILL.md", "---\nname: portable\ndescription: Guidance\n---\n# Keep this"
        )
        archive.writestr("portable/references/details.md", "Reference contents")
    encoded = base64.b64encode(raw.getvalue()).decode()
    wrong = client.post("/v1/plugins/upload?name=portable&kind=plugin", json={"archive": encoded})
    assert wrong.status_code == 422
    saved = client.post("/v1/plugins/upload?name=portable&kind=skill", json={"archive": encoded})
    assert saved.status_code == 201, saved.text
    assert saved.json()["marketplace"] == "private-local"
    exported = client.get("/v1/plugins/local/export")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        files = archive.namelist()
        assert "ai.iowarp.clio/marketplace.json" in files
        assert archive.read("plugins/portable/references/details.md") == b"Reference contents"
        assert not any("oauth" in path or path.endswith("mcp.yaml") for path in files)


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
