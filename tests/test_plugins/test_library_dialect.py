"""clio-coder's library manifests and federated install names (live scenario L8)."""

from __future__ import annotations

import json
from pathlib import Path

from clio_agent.plugins.loader import load_plugin
from clio_agent.plugins.manifest import PLUGIN_SCHEMA
from clio_agent.plugins.marketplace import read_claude_marketplace
from tests.test_plugins.helpers import write


def _library_plugin(root: Path) -> Path:
    base = root / "archify"
    write(
        base / "plugin.json",
        json.dumps(
            {
                "$schema": PLUGIN_SCHEMA,
                "name": "archify",
                "version": "0.1.0",
                "extensions": {
                    "ai.iowarp.clio": {
                        "manifestVersion": 1,
                        "kind": "skill",
                        "resources": {"skills": "."},
                        "components": [
                            {"kind": "skill", "id": "archify", "path": "SKILL.md"},
                            {"kind": "prompt", "id": "map-repo", "path": "prompts/map.md"},
                        ],
                    }
                },
            }
        ),
    )
    write(base / "SKILL.md", "---\nname: archify\ndescription: Draw system maps.\n---\nBody.\n")
    return base


def test_library_manifest_maps_skill_components_and_types_the_rest(tmp_path: Path) -> None:
    record = load_plugin(_library_plugin(tmp_path), marketplace="coder", trusted=True)
    assert record.kind == "plugin"
    assert [(skill.id, Path(skill.path).name) for skill in record.skills] == [
        ("archify", "SKILL.md")
    ]
    unmapped = [d for d in record.diagnostics if d.code == "library_component_unmapped"]
    assert len(unmapped) == 1 and "map-repo" in unmapped[0].message
    assert unmapped[0].severity == "warning"


def test_federated_marketplace_records_carry_the_install_name(tmp_path: Path) -> None:
    root = tmp_path / "coder"
    _library_plugin(root / "plugins")
    write(
        root / ".claude-plugin" / "marketplace.json",
        json.dumps(
            {
                "name": "clio-coder",
                "owner": {"name": "iowarp"},
                "plugins": [{"name": "archify", "source": "./plugins/archify"}],
            }
        ),
    )
    contents = read_claude_marketplace(root, "iowarp-clio-coder")
    assert contents.name == "iowarp-clio-coder"
    assert {record.marketplace for record in contents.plugins} == {"iowarp-clio-coder"}
    assert read_claude_marketplace(root).name == "clio-coder"
