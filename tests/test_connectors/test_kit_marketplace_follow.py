"""A plugin of clio-kit installed as a marketplace follows its server's connector (LZ-O3).

clio-kit publishes Claude Code packages (``.claude-plugin/plugin.json``, a bare-map
``.mcp.json`` naming ``clio-kit mcp-server <name>``, ``skills/``) and workflow bundles
that only list dependencies. Read through the Claude Code importer, the package with
the server and a skill follows the derived connector of that server, so Discover shows
the connector once, as that plugin; the bundle is a pack and follows nothing; and
while the connector is off the server leaves the resolution while the skill stays.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from clio_agent.connectors import follow, server_gate, state
from clio_agent.plugins.marketplace import read_claude_marketplace
from clio_agent.plugins.records import PluginRecord

from .test_lazy_install import kit  # noqa: F401 - the shared stand-in catalogue

SKILL = "---\nname: alpha-use\ndescription: Use when reading alpha files.\n---\n\nRead them.\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _kit_marketplace(root: Path) -> Path:
    """A marketplace laid out as clio-kit lays out a server package and its bundle."""

    plugin = root / "plugins" / "kit-alpha"
    _write(
        plugin / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "kit-alpha", "version": "1.0.0", "description": "Read alpha files."}),
    )
    _write(
        plugin / ".mcp.json",
        json.dumps({"kit-alpha": {"command": "clio-kit", "args": ["mcp-server", "alpha"]}}),
    )
    _write(plugin / "skills" / "alpha-use" / "SKILL.md", SKILL)
    _write(
        root / "plugins" / "kit-data" / ".claude-plugin" / "plugin.json",
        json.dumps({"name": "kit-data", "version": "1.0.0", "dependencies": ["kit-alpha"]}),
    )
    _write(
        root / ".claude-plugin" / "marketplace.json",
        json.dumps(
            {
                "name": "kit",
                "plugins": [
                    {"name": "kit-alpha", "source": "./plugins/kit-alpha"},
                    {"name": "kit-data", "source": "./plugins/kit-data"},
                ],
            }
        ),
    )
    return root


def test_the_server_package_follows_its_connector_and_the_bundle_does_not(
    kit: Path,  # noqa: F811 - fixture reuse
    tmp_path: Path,
) -> None:
    from clio_agent.gact import connectors_service

    contents = read_claude_marketplace(_kit_marketplace(tmp_path / "market"))
    assert contents.diagnostics == []
    records = {record.id: record for record in contents.plugins}
    alpha, bundle = records["kit-alpha"], records["kit-data"]
    assert (alpha.format, alpha.kind, alpha.follows) == ("claude-code", "plugin", "alpha")
    assert [skill.id for skill in alpha.skills] == ["alpha-use"]
    assert set(alpha.mcp_servers) == {"kit-alpha"}
    assert (bundle.kind, bundle.members, bundle.follows) == ("pack", ("kit-alpha",), "")
    # The store keeps it across a restart.
    assert PluginRecord.from_dict(alpha.to_dict()).follows == "alpha"
    # Discover shows the connector once, as this plugin; no carrier is offered for it.
    rows = list(records.values())
    entry = state.all_entries()["alpha"]
    assert connectors_service._plugin(rows, entry) == "kit-alpha"
    assert follow.plugin_refs("alpha", rows) == ["kit/kit-alpha"]
    state.update("alpha", enabled=True)
    assert follow.listed_carriers(rows) == []


def test_while_the_connector_is_off_the_server_leaves_and_the_skill_stays(
    kit: Path,  # noqa: F811 - fixture reuse
    tmp_path: Path,
) -> None:
    contents = read_claude_marketplace(_kit_marketplace(tmp_path / "market"))
    alpha = next(record for record in contents.plugins if record.id == "kit-alpha")
    resolution = SimpleNamespace(
        mcp_servers={
            name: {"plugin": alpha.id, "spec": dict(spec)}
            for name, spec in alpha.mcp_servers.items()
        },
        tools=[{"name": "kit-alpha_read"}],
        diagnostics=[],
        removed=[],
        skills=[{"id": "alpha-use", "plugin": alpha.id}],
        prompt={},
        plugins=[],
        agent={"ref": "m/agent", "name": "Agent"},
    )
    assert server_gate.gate(resolution) is True
    assert resolution.mcp_servers == {}
    assert [(row["code"], row["server"], row["because"]) for row in resolution.diagnostics] == [
        ("connector_off", "kit-alpha", "alpha")
    ]
    assert resolution.skills == [{"id": "alpha-use", "plugin": "kit-alpha"}]
    # A delta naming the plugin keeps it: its skill stays while its server is gated.
    from clio_agent.plugins.resolve import Delta

    kept, dropped = follow.drop_off(Delta(("kit/kit-alpha",), ()), contents.plugins)
    assert (kept.add, dropped) == (("kit/kit-alpha",), [])
