"""Enabled descriptor bindings change with their blueprint revision, never independently."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import blueprint_descriptor_reload as descriptors
from clio_agent.gact.blueprint_install_files import tree_checksum
from clio_agent.gact.blueprint_reload import apply_blueprint_change
from clio_agent.gact.blueprint_runtime_preparation import runtime_workspaces
from tests.test_gact.test_blueprint_install_revision import _install, _pack, _roots
from tests.test_gact.test_blueprint_reload import _app


def _descriptor(pack: Path, name: str, url: str) -> None:
    (pack / "tools").mkdir(exist_ok=True)
    (pack / f"tools/{name}.md").write_text(
        f"---\nid: {name}\ntransport: http\nurl: {url}\ntools: [revision]\n---\nRevision tool\n"
    )


@pytest.mark.asyncio
async def test_reload_updates_only_previously_enabled_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    pack = _pack(source, "demo")
    _descriptor(pack, "trusted", "http://old.test/mcp")
    result = _install(source, tmp_path)
    identity = result["installed"][0]["identity"]
    app = _app()
    app.state.external_mcp_servers = {
        "enabled": {
            "id": "enabled",
            "source": "agent_blueprint",
            "agent_blueprint_id": identity,
            "descriptor_id": "trusted",
            "status": "ready",
            "spec": {"url": "http://old.test/mcp"},
        }
    }
    _pack(source, "demo", "2")
    _descriptor(pack, "trusted", "http://new.test/mcp")
    _descriptor(pack, "not-enabled", "http://never-probed.test/mcp")
    seen: list[dict[str, Any]] = []

    def probe(spec: dict[str, Any]) -> list[Any]:
        seen.append(spec)
        return [
            SimpleNamespace(
                name="revision", description="Current revision", inputSchema={"type": "object"}
            )
        ]

    monkeypatch.setattr(descriptors, "_probe", probe)
    reloaded = await apply_blueprint_change(
        app, lambda: _install(source, tmp_path), label="Reload", verify_runtime=True
    )
    assert reloaded["operation"]["status"] == "applied"
    assert seen == [{"transport": "http", "url": "http://new.test/mcp"}]
    assert list(app.state.external_mcp_servers) == ["enabled"]
    assert app.state.external_mcp_servers["enabled"]["spec"]["url"] == "http://new.test/mcp"
    assert app.state.external_mcp_servers["enabled"]["tools"][0]["name"] == "revision"


@pytest.mark.asyncio
async def test_descriptor_failure_preserves_files_and_enabled_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    pack = _pack(source, "demo")
    _descriptor(pack, "trusted", "http://old.test/mcp")
    result = _install(source, tmp_path)
    root = _roots(result)["demo"]
    before = tree_checksum(root)
    app = _app()
    app.state.external_mcp_servers = {
        "enabled": {
            "source": "agent_blueprint",
            "agent_blueprint_id": result["installed"][0]["identity"],
            "descriptor_id": "trusted",
            "status": "ready",
            "spec": {"url": "http://old.test/mcp"},
        }
    }
    previous = deepcopy(app.state.external_mcp_servers)
    _pack(source, "demo", "2")
    _descriptor(pack, "trusted", "http://new.test/mcp")
    monkeypatch.setattr(descriptors, "_probe", lambda spec: [])
    with pytest.raises(ValueError, match="missing declared tools"):
        await apply_blueprint_change(
            app, lambda: _install(source, tmp_path), label="Reload", verify_runtime=True
        )
    assert tree_checksum(root) == before
    assert app.state.external_mcp_servers == previous


def test_descriptor_credentials_are_not_forwarded_to_a_changed_endpoint() -> None:
    with pytest.raises(ValueError, match="reconnect its credentials"):
        descriptors._spec(
            {"transport": "http", "url": "https://new.test/mcp"},
            {"url": "https://original.test/mcp", "headers": {"Authorization": "private"}},
        )


def test_runtime_preparation_covers_every_workspace_using_the_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact.agents import resolution

    destination = tmp_path / "blueprint"
    first, second = tmp_path / "workspace-a", tmp_path / "workspace-b"
    first.mkdir()
    second.mkdir()
    sessions = [
        SimpleNamespace(id="a", metadata={"active_agent_blueprint_path": str(destination)}),
        SimpleNamespace(
            id="b",
            metadata={"active_agent_blueprint_definition_path": str(destination / "AGENT.md")},
        ),
        SimpleNamespace(
            id="other", metadata={"active_agent_blueprint_path": str(tmp_path / "other")}
        ),
    ]
    app = SimpleNamespace(state=SimpleNamespace(sessions=SimpleNamespace(list=lambda: sessions)))
    monkeypatch.setattr(
        resolution,
        "_runtime_workspace_catalog_cwd",
        lambda app, session_id: {"a": first, "b": second}[session_id],
    )
    assert runtime_workspaces(app, destination, fallback=tmp_path) == [
        first.resolve(),
        second.resolve(),
    ]
