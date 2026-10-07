"""Trusted setup is a native handoff, never an agent authentication surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact.agents.auto_tools import build_auto_react_tools
from clio_agent.gact.skills import SkillCatalog, read_skill_body
from clio_agent.gact.storage import setup_tool
from clio_agent.gact.storage.linked import link_folder
from clio_agent.gact.storage.models import CreateSource
from clio_agent.gact.storage.service import StorageService
from clio_agent.gact.tool_result_presentation import ToolPresentation
from clio_agent.gact.types import AgentDef


def test_tool_only_returns_approved_owner_workspace_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    source = service.create(
        "w", CreateSource(provider="google_drive", root="private-root", label="Inputs")
    )
    service.create("other", CreateSource(provider="google_drive", root="other-root", label="Other"))
    foreign = service.create(
        "w", CreateSource(provider="globus", root="foreign-root", label="Foreign")
    )
    foreign.source = foreign.source.model_copy(
        update={"owner": foreign.source.owner.model_copy(update={"clio_id": "another-clio"})}
    )
    service.store.put("source", foreign.source.id, foreign)
    app = SimpleNamespace(
        state=SimpleNamespace(
            connected_storage=service,
            sessions={"s": SimpleNamespace(workspace_id="w")},
            workspaces={"w": SimpleNamespace(root_path=str(tmp_path))},
        )
    )
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: app)
    monkeypatch.setattr(setup_tool.context, "active_session_id", lambda: "s")

    def private_token(*args: object, **kwargs: object) -> str:
        raise AssertionError("Status must not obtain a credential")

    monkeypatch.setattr(service.auth, "token", private_token)
    result = setup_tool.connected_data_status()
    assert [row["id"] for row in result["sources"]] == [source.source.id]
    serialized = json.dumps(result)
    assert "private-root" not in serialized and "credentials" not in serialized
    assert result["sources"][0]["local_path"] is None
    assert result["sources"][0]["materialization"] == "not_materialized"
    presentation = ToolPresentation.model_validate(setup_tool.setup_presentation({}, result, None))
    assert presentation.blocks[0].type == "markdown"
    assert not any(block.type == "link" for block in presentation.blocks)
    assert "Inputs" in presentation.blocks[0].text
    assert result["workspace_folders"] == [
        {"path": str(tmp_path), "role": "primary", "access": "workspace"}
    ]
    assert {account["provider"] for account in result["accounts"]} == {
        "github",
        "google_drive",
        "globus",
    }
    assert all(not account["authenticated"] for account in result["accounts"])
    assert result["accounts"][1]["login_action"]["name"] == "data_source/login/google_drive"


def test_missing_session_fails_without_guessing_a_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: None)
    with pytest.raises(ValueError, match="active CLIO workspace"):
        setup_tool.connected_data_status()


def test_setup_skill_and_credential_free_root_tool_are_available() -> None:
    skill = SkillCatalog().resolve("connect-data")
    assert skill.skill is not None
    assert "connected_data_status" in read_skill_body(skill.skill)
    root = AgentDef(id="main", title="Main", source="expert_pack")
    tool = next(
        tool
        for tool in build_auto_react_tools(root, a2ui_producers=False)
        if tool.name == "connected_data_status"
    )
    assert tool.args == {}
    root.parent_id = "parent"
    assert "connected_data_status" not in {
        tool.name for tool in build_auto_react_tools(root, a2ui_producers=False)
    }


def test_linked_listing_is_session_scoped_and_omits_private_locators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (upstream / "data.txt").write_text("approved data", encoding="utf-8")
    service = StorageService(tmp_path / "data", tmp_path / "private" / "credentials.json")
    record = service.create("w", CreateSource(provider="local", root=str(upstream), label="Inputs"))
    link_folder(service, record)
    sessions = {"s": SimpleNamespace(workspace_id="w")}
    app = SimpleNamespace(state=SimpleNamespace(connected_storage=service, sessions=sessions))
    monkeypatch.setattr(setup_tool.context, "active_app", lambda: app)
    monkeypatch.setattr(setup_tool.context, "active_session_id", lambda: "s")
    result = setup_tool.connected_data_open(record.source.id)
    assert [row["path"] for row in result["entries"]] == ["data.txt"]
    assert "locator" not in json.dumps(result)
    assert str(upstream) not in json.dumps(result)
    assert not (service.store.root / record.source.id / record.linked_manifest_id).exists()
    sessions["s"].workspace_id = "other"
    with pytest.raises(KeyError):
        setup_tool.connected_data_open(record.source.id)
