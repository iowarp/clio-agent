"""Reload must preserve the previous complete revision across preparation/swap failures."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from clio_agent.gact.agent_blueprints import install_agent_blueprint
from clio_agent.gact.blueprint_install_files import read_install_metadata, tree_checksum
from clio_agent.gact.blueprint_install_revision import recover_install_revisions
from clio_agent.gact.blueprint_runtime_preparation import runtime_preparation


def _pack(root: Path, name: str, version: str = "1", **metadata: Any) -> Path:
    path = root / name
    (path / "experts").mkdir(parents=True, exist_ok=True)
    frontmatter = {"id": name, "title": name, "version": version, "root_expert": "main", **metadata}
    (path / "AGENT.md").write_text(
        "---\n" + yaml.safe_dump(frontmatter) + "---\nRevision " + version
    )
    (path / "experts/main.md").write_text(
        "---\nid: main\ntitle: Main\ntier: 1\nmodule:\n  kind: react\nprompt_id: demo.main\n---\nPlan.\n"
    )
    return path


def _install(source: Path, cwd: Path) -> dict[str, Any]:
    return install_agent_blueprint(source=str(source), scope="workspace", cwd=cwd)


def _roots(result: dict[str, Any]) -> dict[str, Path]:
    return {row["id"]: Path(row["root"]) for row in result["installed"]}


def test_invalid_later_pack_does_not_apply_earlier_pack(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pack(source, "first")
    _pack(source, "second")
    roots = _roots(_install(source, tmp_path))
    before = {name: tree_checksum(root) for name, root in roots.items()}
    _pack(source, "first", "2")
    _pack(source, "second", "2", root_expert="missing")
    with pytest.raises(ValueError):
        _install(source, tmp_path)
    assert {name: tree_checksum(root) for name, root in roots.items()} == before


def test_failed_final_swap_restores_all_packs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import blueprint_install_revision as module

    source = tmp_path / "source"
    _pack(source, "first")
    _pack(source, "second")
    roots = _roots(_install(source, tmp_path))
    before = {name: tree_checksum(root) for name, root in roots.items()}
    _pack(source, "first", "2")
    _pack(source, "second", "2")
    original = module.rename_extended

    def fail_second(staged: Path, destination: Path) -> None:
        if staged.name == "new-1":
            raise OSError("simulated disk failure")
        original(staged, destination)

    monkeypatch.setattr(module, "rename_extended", fail_second)
    with pytest.raises(OSError, match="simulated disk failure"):
        _install(source, tmp_path)
    assert {name: tree_checksum(root) for name, root in roots.items()} == before
    assert all(
        read_install_metadata(root)["checksum"] == before[name] for name, root in roots.items()
    )


def test_recovery_restores_an_interrupted_marketplace_swap(tmp_path: Path) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    directory = tmp_path / ".installed-revisions" / "interrupted"
    directory.mkdir(parents=True)
    old = _pack(directory, "old-0", "1")
    _pack(root, "demo", "2")
    _pack(root, "added", "2")
    original = tree_checksum(old)
    (directory / "revision.json").write_text(
        json.dumps(
            {
                "status": "applying",
                "entries": [
                    {"destination": "demo", "backup": "old-0", "staged": "new-0", "existed": True},
                    {
                        "destination": "added",
                        "backup": "old-1",
                        "staged": "new-1",
                        "existed": False,
                    },
                ],
            }
        )
    )
    recover_install_revisions(root)
    assert tree_checksum(root / "demo") == original
    assert not (root / "added").exists()
    assert not directory.exists()
    recover_install_revisions(root)  # Recovery remains idempotent.


def test_recovery_refuses_a_destination_outside_the_registry(tmp_path: Path) -> None:
    root = tmp_path / "installed"
    root.mkdir()
    directory = tmp_path / ".installed-revisions" / "interrupted"
    directory.mkdir(parents=True)
    (directory / "revision.json").write_text(
        json.dumps(
            {
                "status": "applying",
                "entries": [
                    {
                        "destination": "../outside",
                        "backup": "old-0",
                        "staged": "new-0",
                        "existed": True,
                    },
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid blueprint revision entry"):
        recover_install_revisions(root)
    assert directory.exists()


def test_edit_during_runtime_preparation_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import blueprint_installer

    source = tmp_path / "source"
    _pack(source, "demo")
    root = _roots(_install(source, tmp_path))["demo"]
    _pack(source, "demo", "2")

    def external_edit(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        (root / "AGENT.md").write_text((root / "AGENT.md").read_text() + "\nExternal draft")
        return []

    monkeypatch.setattr(blueprint_installer, "prepare_blueprint_runtime", external_edit)
    with pytest.raises(ValueError, match="changed during preparation"):
        _install(source, tmp_path)
    assert "External draft" in (root / "AGENT.md").read_text()
    assert "Revision 1" in (root / "AGENT.md").read_text()


def test_missing_mcp_launcher_retains_old_revision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pack(source, "demo")
    root = _roots(_install(source, tmp_path))["demo"]
    before = tree_checksum(root)
    _pack(
        source, "demo", "2", mcp_servers={"broken": {"command": "clio-nonexistent-test-launcher"}}
    )
    app = SimpleNamespace(state=SimpleNamespace())
    with runtime_preparation(app, enabled=True), pytest.raises(ValueError, match="MCP broken"):
        _install(source, tmp_path)
    assert tree_checksum(root) == before


def test_audit_failure_rolls_back_files_and_uninstall_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import agent_blueprint_refresh

    source = tmp_path / "source"
    _pack(source, "demo")
    root = _roots(_install(source, tmp_path))["demo"]
    before = tree_checksum(root)
    ledger = root.parent / ".uninstalled.json"
    original = b'{"uninstalled": ["another-source::removed"]}'
    ledger.write_bytes(original)
    _pack(source, "demo", "2")

    def failed_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ledger.write_text('{"uninstalled": []}')
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(agent_blueprint_refresh, "install_row", failed_receipt)
    with pytest.raises(RuntimeError, match="audit store unavailable"):
        _install(source, tmp_path)
    assert tree_checksum(root) == before
    assert ledger.read_bytes() == original


def test_nested_discovery_does_not_recover_a_live_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import blueprint_installer

    source = tmp_path / "source"
    _pack(source, "demo")

    def nested_discovery(staged: Path, **kwargs: Any) -> list[dict[str, Any]]:
        # Both locks are reentrant; discovery must skip this live transaction.
        registry = staged.parent.parent.parent / "agent-blueprints"
        recover_install_revisions(registry)
        assert (staged / "AGENT.md").is_file()
        return []

    monkeypatch.setattr(blueprint_installer, "prepare_blueprint_runtime", nested_discovery)
    assert _install(source, tmp_path)["installed"]


@pytest.mark.timeout(60)  # Real FastMCP stdio startup on Windows/CI; no inference.
def test_reload_prepares_a_real_mcp_without_invoking_its_tools(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _pack(source, "demo")
    root = _roots(_install(source, tmp_path))["demo"]
    pack = _pack(
        source,
        "demo",
        "2",
        mcp_servers={
            "demo": {
                "command": sys.executable,
                "args": ["${CLIO_BLUEPRINT_DIR}/server.py"],
            }
        },
    )
    (pack / "server.py").write_text(
        "from fastmcp import FastMCP\nfrom pathlib import Path\n"
        "server = FastMCP('reload-proof')\n@server.tool\n"
        "def current_revision() -> str:\n"
        "    Path('tool-was-called').write_text('called')\n    return '2'\n"
        "server.run(transport='stdio', show_banner=False)\n"
    )
    app = SimpleNamespace(state=SimpleNamespace())
    with runtime_preparation(app, enabled=True):
        result = _install(source, tmp_path)
    assert result["installed"][0]["runtime_checks"] == [
        {
            "namespace": "demo",
            "status": "ready",
            "tool_count": 1,
            "workspace": str(tmp_path.resolve()),
        }
    ]
    assert "Revision 2" in (root / "AGENT.md").read_text()
    assert not (tmp_path / "tool-was-called").exists()
