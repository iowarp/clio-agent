"""Local extension lifecycle and credential-free connection composition."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from clio_agent.platform_paths import rmtree_extended, win_extended_path
from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.local import archive_files, create_local
from clio_agent.plugins.servers import server_declarations
from clio_agent.plugins.store import PluginStore, _copy_tree
from clio_agent.tools.mcp_config import load_mcp_servers


def test_create_skill_compose_connection_and_preserve_original(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    create_local(
        store,
        {
            "name": "ha-guidance",
            "skill": "---\nname: ha\ndescription: Test\n---\n# HA\nKeep helpers reusable.",
        },
    )
    original = store.root / "local/private-local/plugins/ha-guidance/skills/ha-guidance/SKILL.md"
    assert original.is_file()
    create_local(
        store,
        {
            "name": "ha",
            "requires": ["private-local/ha-guidance"],
            "connections": {"home": "ha-home"},
        },
    )
    manifest = store.root / "local/private-local/plugins/ha/plugin.json"
    assert "https://" not in json.dumps(json.loads(manifest.read_text())["extensions"])
    record = load_plugin(manifest.parent, marketplace="ha", trusted=False)
    assert record.mcp_servers == {"home": {"type": "clio-connection", "connection": "ha-home"}}
    servers = {
        "home": {"spec": record.mcp_servers["home"], "root": str(manifest.parent), "plugin": "ha"}
    }
    saved = {
        "ha-home": {
            "transport": "sse",
            "url": "https://example.invalid/mcp",
            "headers": {"X-Key": "test-credential-not-for-export"},
            "auth": {"type": "oauth"},
        }
    }
    declarations = server_declarations(servers, connections=saved)
    assert declarations["ha"]["home"]["type"] == "sse"
    assert declarations["ha"]["home"]["auth"] == {"type": "oauth"}
    assert declarations["ha"]["home"]["headers"] == {"X-Key": "test-credential-not-for-export"}
    assert "test-credential-not-for-export" not in manifest.read_text()
    with pytest.raises(ValueError, match="ha-home"):
        server_declarations(servers, connections={})
    with pytest.raises(ValueError, match="already exists"):
        create_local(store, {"name": "ha-guidance", "skill": "overwrite"})
    assert "Keep helpers reusable" in original.read_text()


def test_failed_install_removes_only_its_new_folder(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    untouched = store.root / "local/existing/notes.md"
    untouched.parent.mkdir(parents=True)
    untouched.write_text("keep")
    with pytest.raises(ValueError):
        create_local(store, {"name": "bad", "files": {"plugin.json": "not JSON"}})
    assert not (store.root / "local/private-local/plugins/bad").exists()
    assert untouched.read_text() == "keep"


def test_private_marketplace_keeps_siblings_and_requires_new_executable_trust(
    tmp_path: Path,
) -> None:
    store = PluginStore(tmp_path / "store")
    first = create_local(
        store,
        {"name": "notes", "skill": "---\nname: notes\ndescription: Guidance\n---\nRead carefully."},
    )
    assert first["marketplace"] == "private-local"
    store.trust("private-local")
    second = create_local(store, {"name": "connected", "connections": {"home": "saved-home"}})
    assert set(store.marketplaces()) == {"private-local"}
    assert "connected" in second["pending_trust"]
    state = store.read_state()
    assert {"private-local/notes", "private-local/connected"} <= state["plugins"].keys()
    records = {record.id: record for record in store.records()}
    assert store.trusted(records["notes"])
    assert not store.trusted(records["connected"])


def test_private_marketplace_imports_portable_skill_and_claude_plugin(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    create_local(
        store,
        {
            "name": "portable",
            "files": {
                "SKILL.md": "---\nname: portable\ndescription: Portable guidance\n---\n# Read me",
                "references/guide.md": "Keep this source",
            },
        },
    )
    result = create_local(
        store,
        {
            "name": "claude",
            "files": {
                ".claude-plugin/plugin.json": '{"name":"original","version":"1.0.0"}',
                "skills/help/SKILL.md": "---\nname: help\ndescription: Help\n---\n# Guidance",
            },
        },
    )
    assert set(result["plugins"]) == {"portable", "claude"}
    assert all(key.startswith("private-local/") for key in store.read_state()["plugins"])
    assert (
        store.root / "local/private-local/plugins/portable/references/guide.md"
    ).read_text() == "Keep this source"


@pytest.mark.parametrize(
    "name", ["../escape", "C:/escape", "folder\\escape", "/escape", ".git/config"]
)
def test_archives_reject_escape_paths(name: str) -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        # ZipInfo normally normalizes backslashes on Windows. Preserve the
        # hostile wire path here so this tests the reader, not that writer.
        info = zipfile.ZipInfo()
        info.filename = name
        archive.writestr(info, "untrusted")
    with pytest.raises(ValueError, match="inside its own folder"):
        archive_files(raw.getvalue())


def test_archive_preserves_skill_reference_tree() -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("my-skill/SKILL.md", "# Portable")
        archive.writestr("my-skill/references/guide.md", "# Details")
    assert archive_files(raw.getvalue()) == {
        "SKILL.md": "# Portable",
        "references/guide.md": "# Details",
    }


def test_archive_rejects_links() -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "../outside")
    with pytest.raises(ValueError, match="symbolic links"):
        archive_files(raw.getvalue())


def test_snapshot_copy_supports_long_windows_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("Preserve these exact source bytes")
    target = tmp_path / ("nested-" * 15) / ("snapshot-" * 12) / "snapshot"
    assert len(str(target / "SKILL.md")) > 260
    try:
        _copy_tree(source, target)
        assert (
            Path(win_extended_path(target / "SKILL.md")).read_text()
            == "Preserve these exact source bytes"
        )
    finally:
        rmtree_extended(tmp_path / ("nested-" * 15), ignore_errors=True)


def test_snapshot_copy_failure_retires_partial_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "snapshots" / "digest"

    def fail_copy(_source: str, staging: str, **_kwargs: object) -> None:
        partial = Path(staging)
        partial.mkdir(parents=True)
        (partial / "unfinished.md").write_text("partial")
        raise PermissionError("copy was refused")

    monkeypatch.setattr("clio_agent.plugins.store.shutil.copytree", fail_copy)
    with pytest.raises(PermissionError, match="refused"):
        _copy_tree(source, target)
    assert not target.exists()
    assert not list(target.parent.glob("*.partial-*"))


def test_legacy_local_references_survive_new_private_marketplace(tmp_path: Path) -> None:
    store = PluginStore(tmp_path / "store")
    legacy = store.root / "local/legacy"
    legacy.mkdir(parents=True)
    (legacy / "plugin.json").write_text(
        '{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"legacy","version":"1.0.0"}'
    )
    store.install(str(legacy), name="legacy")
    store.trust("legacy")
    create_local(
        store, {"name": "fresh", "skill": "---\nname: fresh\ndescription: Guidance\n---\n# Fresh"}
    )
    legacy_record = next(record for record in store.records() if record.marketplace == "legacy")
    assert legacy_record.id == "legacy" and store.trusted(legacy_record)
    assert set(store.marketplaces()) == {"legacy", "private-local"}


def test_saved_connection_is_not_mounted_until_a_plugin_uses_it(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "mcp.yaml").write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "home": {
                        "transport": "http",
                        "url": "https://example.invalid/mcp",
                        "connection_only": True,
                    },
                    "legacy": {
                        "transport": "http",
                        "url": "https://example.invalid/legacy",
                        "always_load": False,
                    },
                }
            }
        )
    )
    env = {"CLIO_AGENT_CONFIG_DIR": str(config)}
    assert "home" not in load_mcp_servers(home=tmp_path, cwd=tmp_path, env=env)
    assert "legacy" in load_mcp_servers(home=tmp_path, cwd=tmp_path, env=env)
    mounted = load_mcp_servers(
        home=tmp_path,
        cwd=tmp_path,
        env=env,
        plugin_servers={"ha": {"home": {"type": "http", "url": "https://example.invalid/mcp"}}},
    )
    assert mounted["home"].source == "plugin:ha"


def test_archive_rejects_expansion_bombs() -> None:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", "x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="uncompressed"):
        archive_files(raw.getvalue())
