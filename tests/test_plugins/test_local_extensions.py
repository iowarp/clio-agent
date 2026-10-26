"""Local extension lifecycle and credential-free connection composition."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.local import archive_files, create_local
from clio_agent.plugins.servers import server_declarations
from clio_agent.plugins.store import PluginStore
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
    original = store.root / "local/ha-guidance/skills/ha-guidance/SKILL.md"
    assert original.is_file()
    create_local(
        store,
        {"name": "ha", "requires": ["ha-guidance/ha-guidance"], "connections": {"home": "ha-home"}},
    )
    manifest = store.root / "local/ha/plugin.json"
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
            "headers": {"X-Key": "private"},
            "auth": {"type": "oauth"},
        }
    }
    declarations = server_declarations(servers, connections=saved)
    assert declarations["ha"]["home"]["type"] == "sse"
    assert declarations["ha"]["home"]["auth"] == {"type": "oauth"}
    assert declarations["ha"]["home"]["headers"] == {"X-Key": "private"}
    assert "private" not in manifest.read_text()
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
    assert not (store.root / "local/bad").exists()
    assert untouched.read_text() == "keep"


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
