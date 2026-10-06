"""Real catalog, materialization and session-creation regressions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.agent_blueprint_sources import (
    refresh_agent_blueprint_source,
    source_registry_id,
    upsert_agent_blueprint_source,
)
from clio_agent.gact.app import build_app
from clio_agent.gact.storage.boundary import source_policy_change


def _register(root: Path, **extra: Any) -> str:
    """Register a real local catalog without preinstalling its blueprints."""
    root.mkdir(parents=True)
    (root / "AGENT.md").write_text("---\nid: demo\ntitle: Demo\n---\nOriginal source")
    identifier = source_registry_id(str(root))
    row = refresh_agent_blueprint_source(
        {"id": identifier, "source": str(root), "name": root.name, **extra}
    )
    assert row["available_blueprints"][0]["enabled"]
    upsert_agent_blueprint_source(row)
    return f"{extra.get('install_scope', 'global')}::{identifier}::demo"


def test_selection_installs_once_and_retains_snapshot_until_reload(tmp_path: Path) -> None:
    source = tmp_path / "marketplace"
    identity = _register(source)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        catalog = client.get("/v1/agent-blueprints").json()["agent_blueprints"]
        choice = next(row for row in catalog if row["identity"] == identity)
        assert choice["materialized"] is False
        assert choice["definition_path"] == ""
        create = client.post(
            "/v1/sessions",
            json={
                "metadata": {"active_agent_blueprint_id": identity},
            },
        )
        assert create.status_code == 200, create.text
        metadata = create.json()["metadata"]
        root = Path(metadata["active_agent_blueprint_path"])
        assert root != source
        assert metadata["active_agent_blueprint_identity"] == identity
        assert metadata["active_agent_blueprint_checksum"]
        (source / "AGENT.md").write_text("---\nid: demo\ntitle: Demo\n---\nChanged source")
        second = client.post(
            "/v1/sessions",
            json={
                "metadata": {"active_agent_blueprint_id": identity},
            },
        )
        assert second.status_code == 200, second.text
        assert (
            second.json()["metadata"]["active_agent_blueprint_checksum"]
            == metadata["active_agent_blueprint_checksum"]
        )
        assert "Original source" in (root / "AGENT.md").read_text()


def test_ambiguous_and_missing_choices_leave_no_session(tmp_path: Path) -> None:
    _register(tmp_path / "one")
    identity = _register(tmp_path / "two")
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        before = len(app.state.sessions.list())
        for identifier, status in (("demo", 409), ("missing", 404)):
            result = client.post(
                "/v1/sessions",
                json={
                    "metadata": {"active_agent_blueprint_id": identifier},
                },
            )
            assert result.status_code == status, result.text
            assert len(app.state.sessions.list()) == before
        result = client.post(
            "/v1/sessions",
            json={
                "metadata": {"active_agent_blueprint_id": identity},
            },
        )
        assert result.status_code == 200, result.text


def test_workspace_choices_are_confined_to_owner(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    with TestClient(app) as client:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        wid = client.post(
            "/v1/workspaces",
            json={
                "name": "Owner",
                "root_path": str(workspace),
            },
        ).json()["id"]
        identity = _register(tmp_path / "source", install_scope="workspace", workspace_id=wid)
        other = client.get("/v1/agent-blueprints").json()["agent_blueprints"]
        assert all(row["identity"] != identity for row in other)
        refused = client.post(
            "/v1/sessions",
            json={
                "metadata": {"active_agent_blueprint_id": identity},
            },
        )
        assert refused.status_code == 404
        selected = client.post(
            "/v1/sessions",
            json={
                "workspace_id": wid,
                "metadata": {"active_agent_blueprint_id": identity},
            },
        )
        assert selected.status_code == 200, selected.text
        root = Path(selected.json()["metadata"]["active_agent_blueprint_path"])
        assert root.is_relative_to(workspace / ".clio-agent")
        assert not (workspace / ".clio").exists()


def test_custom_tool_owner_cannot_bypass_restart_boundary() -> None:
    with pytest.raises(ValueError, match="safe tool-process restart"):
        with source_policy_change(SimpleNamespace(_active_tool_executor=lambda: None)):
            pytest.fail("A custom tool owner must implement the safety boundary")
