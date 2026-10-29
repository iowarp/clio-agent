"""Local environments cannot change plugin trust or force repeated installation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from clio_agent.plugins.loader import tree_digests
from clio_agent.plugins.package_files import package_files
from clio_agent.plugins.service import PluginService
from clio_agent.plugins.store import PluginStore, _copy_tree
from tests.test_plugins.helpers import plugin, write


def test_source_and_snapshot_digests_share_the_same_file_boundary(tmp_path: Path) -> None:
    source = plugin(tmp_path, "source")
    write(source / "ai.iowarp.clio/tools/calculate.py", "value = 42\n")
    write(source / ".git", "gitdir: ../../.git/modules/marketplace\n")
    for name in (".venv", "node_modules", "__pycache__"):
        write(source / "nested" / name / "generated.py", "generated = True\n")
    write(source / "nested/cached.pyc", "bytecode")
    snapshot = tmp_path / "snapshot"
    _copy_tree(source, snapshot)
    assert tree_digests(source) == tree_digests(snapshot)
    before = tree_digests(source)
    write(source / "nested/.venv/generated.py", "generated = False\n")
    assert tree_digests(source) == before
    write(source / "ai.iowarp.clio/tools/calculate.py", "value = 43\n")
    assert tree_digests(source)[0] != before[0]


def test_generated_directories_are_not_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    write(source / ".venv/deep/package.py", "value = 1")
    write(source / "readme.md", "Authored content")
    original = os.scandir

    def guarded_scandir(path: Any) -> Any:
        if Path(path).name == ".venv":
            raise AssertionError("The excluded environment was traversed")
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert list(package_files(source)) == [source / "readme.md"]


def test_unchanged_local_marketplace_does_not_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.plugins import service

    source = tmp_path / "marketplace"
    write(source / "ai.iowarp.clio/marketplace.json", '{"name": "clio"}')
    plugin(source / "plugins", "example")
    write(source / ".git", "gitdir: absent\n")
    write(source / "nested/.venv/library.py", "value = 1\n")
    monkeypatch.setattr(service, "default_marketplace_source", lambda: str(source))
    instance = PluginService(PluginStore(tmp_path / "store"))
    assert instance.ensure_default_marketplace() is not None
    assert instance.ensure_default_marketplace() is None


@pytest.mark.parametrize("legacy", [False, True])
def test_relative_local_install_updates_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    source = plugin(tmp_path, "relative-source")
    code = source / "ai.iowarp.clio/tools/calculate.py"
    write(code, "value = 42\n")
    store = PluginStore(tmp_path / "store")
    monkeypatch.chdir(tmp_path)
    store.install("relative-source", name="example")
    store.trust("example")
    assert store.marketplaces()["example"]["source"] == str(source.resolve())
    if legacy:
        state = store.read_state()
        state["marketplaces"]["example"]["source"] = "relative-source"
        store._write_state(state)
    write(code, "value = 43\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    updated = store.update("example")
    assert updated["pending_trust"] == ["relative-source"]
