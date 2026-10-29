"""GitHub backups restore saved agents through the ordinary marketplace loader."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from clio_agent.plugins.local import create_local
from clio_agent.plugins.local_export import LocalExportError, export_local_marketplace
from clio_agent.plugins.marketplace import NATIVE_INDEX
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore


def service_at(path: Path) -> PluginService:
    """Create a service with one real SDK parent and no network sources."""
    market = path / "parent"
    (market / NATIVE_INDEX).parent.mkdir(parents=True)
    (market / NATIVE_INDEX).write_text(json.dumps({"name": "lab"}))
    (market / "agents").mkdir()
    (market / "agents" / "parent.py").write_text(
        "from clio_agent.sdk import Agent, Flow, Prefers, Requires\n"
        "class Parent(Agent):\n"
        "    id = 'parent'\n"
        "    name = 'Parent'\n"
        "    version = '1.2.0'\n"
        "    role = 'Investigate scientific questions'\n"
        "    plugins = Agent.plugins\n"
        "    requires = Requires(context_tokens=16000)\n"
        "    prefers = Prefers(effort='high')\n"
        "    flow = Flow('react', setting='kept')\n"
    )
    store = PluginStore(path / "store")
    store.install(str(market))
    store.trust("lab")
    return PluginService(store)


def restore(raw: bytes, root: Path, service: PluginService) -> PluginService:
    """Install an unpacked backup under a different marketplace name."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        archive.extractall(root)
    service.store.install(str(root), name="restored")
    service.store.trust("restored")
    return service


def test_agent_only_export_is_installable_and_preserves_removed_memory(tmp_path: Path) -> None:
    source = service_at(tmp_path / "source")
    source.save_user_agent(
        "dememory.clio",
        extends="lab/parent",
        add=[],
        remove=["memory"],
        name="Dememory Clio",
        description="My scientist",
    )
    before = source.resolve("user/dememory.clio", pin=False)
    assert not (source.store.root / "local/private-local").exists()
    raw = export_local_marketplace(source)
    assert raw == export_local_marketplace(source), "unchanged source exports deterministically"
    destination = restore(raw, tmp_path / "backup", service_at(tmp_path / "destination"))
    after = destination.resolve("restored/dememory.clio", pin=False)
    assert before.plugin_ids == after.plugin_ids
    assert before.tool_names == after.tool_names
    assert before.prompt_text == after.prompt_text
    assert "memory" not in after.plugin_ids
    assert after.agent["flow"] == {"id": "react", "options": {"setting": "kept"}}
    assert before.model == after.model
    assert [agent.ref for agent in destination.agents() if agent.marketplace == "restored"] == [
        "restored/dememory.clio"
    ]
    assert not (source.store.root / "local/private-local").exists(), (
        "export never installs a source"
    )
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        original = json.loads(archive.read("user-agents/dememory.clio.json"))
        assert original["remove"] == ["memory"]


def test_mixed_backup_restores_local_plugins_and_nested_variations(tmp_path: Path) -> None:
    source = service_at(tmp_path / "source")
    create_local(
        source.store,
        {"name": "notes", "skill": "---\nname: notes\ndescription: Notes\n---\nKeep notes"},
    )
    source.store.trust("private-local")
    source.save_user_agent("one", extends="lab/parent", add=[], remove=["memory"])
    source.save_user_agent("two", extends="user/one", add=["private-local/notes"], remove=[])
    before = source.resolve("user/two", pin=False)
    raw = export_local_marketplace(source)
    destination = restore(raw, tmp_path / "backup", service_at(tmp_path / "destination"))
    after = destination.resolve("restored/two", pin=False)
    assert before.plugin_ids == after.plugin_ids
    assert before.tool_names == after.tool_names
    assert before.prompt_text == after.prompt_text
    assert "memory" not in after.plugin_ids
    assert (tmp_path / "backup/plugins/notes/skills/notes/SKILL.md").is_file()


def test_export_empty_collection_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(LocalExportError, match="Save an agent, skill or plugin") as raised:
        export_local_marketplace(PluginService(PluginStore(tmp_path / "store")))
    assert raised.value.status == 404
