"""The shipped test-ground connectors load, and an entry's tools allowlist narrows a listing."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import Tool

from clio_agent.connectors import entries, listing, state

SHIPPED = Path(__file__).resolve().parents[2] / "catalogs" / "connectors"


def _tool(name: str) -> Tool:
    return Tool(name=name, inputSchema={"type": "object"})


def test_shipped_entries_load_without_diagnostics() -> None:
    catalog = entries.load_catalog([SHIPPED])
    assert catalog.diagnostics == []
    assert {"burette", "spice", "paraview"} <= set(catalog.entries)
    burette = catalog.entries["burette"]
    assert burette.is_url and burette.needs == ()
    assert "preview_molecular_file" not in burette.tools
    assert set(burette.tools) == {
        "preview_pdb_structure",
        "render_molecular_scene",
        "open_ketcher",
        "control_ketcher",
    }
    assert burette.to_dict()["tools"] == list(burette.tools)
    spice = catalog.entries["spice"]
    assert spice.server["command"] == "uvx" and spice.logo == "" and spice.tools == ()
    paraview = catalog.entries["paraview"]
    assert paraview.plugin == "paraview"
    assert [(n.key, n.required, n.agent_settable) for n in paraview.needs] == [
        ("address", False, True),
        ("via", False, True),
        ("prepare", False, True),
    ]
    for entry in (burette, paraview):
        logo = entry.logo_path
        assert logo is not None and logo.read_text().lstrip().startswith(("<svg", "<?xml"))


def test_an_unknown_entry_field_is_a_typed_diagnostic(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "- {id: bad, name: Bad, description: d, server: {url: 'https://x.invalid'}, tools: []}\n"
    )
    catalog = entries.Catalog()
    entries.load_folder(tmp_path, catalog)
    assert [d.code for d in catalog.diagnostics] == [entries.ENTRY_INVALID]


def test_offered_keeps_only_the_listed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = entries.load_catalog([SHIPPED])
    monkeypatch.setattr(state, "all_entries", lambda: dict(catalog.entries))
    events: list[str] = []
    monkeypatch.setattr(listing.trace, "event", lambda _cat, fmt, *args: events.append(fmt % args))
    listed = [_tool(n) for n in ("preview_pdb_structure", "preview_molecular_file", "open_ketcher")]

    kept = listing.offered("burette", listed)

    assert [t.name for t in kept] == ["preview_pdb_structure", "open_ketcher"]
    assert sorted(events) == [
        "connector_tool_not_listed connector=burette tool=control_ketcher",
        "connector_tool_not_listed connector=burette tool=render_molecular_scene",
    ]
    rows = [{"name": "open_ketcher"}, {"name": "preview_molecular_file"}]
    assert listing.offered("burette", rows) == [{"name": "open_ketcher"}]
    assert listing.offered("spice", listed) == listed
    assert listing.offered("unknown", listed) == listed


def test_more_connectors_are_data_only() -> None:
    """The PDF viewer and the 3D globe (MCP Apps) and NASA Earthdata are catalog entries only."""

    catalog = entries.load_catalog([SHIPPED])
    pdf = catalog.entries["pdf"]
    assert pdf.server["command"] == "npx" and pdf.needs == () and pdf.tools == ()
    assert "@modelcontextprotocol/server-pdf@2.0.4" in pdf.server["args"]
    assert pdf.logo_path is not None and pdf.logo_path.read_text().startswith("<svg")
    earthdata = catalog.entries["earthdata"]
    assert earthdata.server["args"][0] == "earthdata-mcp-server==0.6.0"
    assert [(n.key, n.kind, n.required) for n in earthdata.needs] == [
        ("username", "value", False),
        ("password", "secret", False),
    ]
    assert earthdata.server["env"]["EARTHDATA_PASSWORD"] == "${need.password}"
    assert earthdata.logo == "" and earthdata.tools == ()
    globe = catalog.entries["globe"]
    assert "@modelcontextprotocol/server-map@2.0.4" in globe.server["args"]
    assert globe.needs == () and globe.logo == "" and globe.tools == ()


def test_geo_is_a_data_only_entry_on_the_clio_kit_launcher() -> None:
    """Geo (the globe of clio-kit's geo server) starts like ParaView, through clio-kit."""

    catalog = entries.load_catalog([SHIPPED])
    geo = catalog.entries["geo"]
    assert geo.server["command"] == "clio-kit"
    assert list(geo.server["args"]) == ["mcp-server", "geo"]
    assert geo.needs == () and geo.logo == "" and geo.tools == ()


def test_home_assistant_needs_an_address_and_a_token_and_offers_a_curated_set() -> None:
    """ha-mcp, pinned, with its URL and long-lived token as needs; the token stays a secret."""

    catalog = entries.load_catalog([SHIPPED])
    ha = catalog.entries["home-assistant"]
    assert ha.server["command"] == "uvx" and list(ha.server["args"]) == ["ha-mcp==9.2.0"]
    assert [(n.key, n.kind, n.required) for n in ha.needs] == [
        ("url", "address", True),
        ("token", "secret", True),
    ]
    assert ha.server["env"] == {
        "HOMEASSISTANT_URL": "${need.url}",
        "HOMEASSISTANT_TOKEN": "${need.token}",
    }
    assert ha.logo == "" and ha.plugin == "home-assistant"
    assert {"ha_get_state", "ha_get_history", "ha_call_service"} <= set(ha.tools)
    # Admin and destructive tools stay in Home Assistant's own UI.
    assert not {"ha_restart", "ha_write_file", "ha_delete_file", "ha_remove_device"} & set(ha.tools)


@pytest.mark.marketplace
def test_a_catalog_plugin_is_followed_by_a_marketplace_plugin_with_skills() -> None:
    """Connectors and plugins are different things (PL-D1). An entry that names a
    ``plugin`` is a plugin: the marketplace plugin of that id follows it, binds its
    connection and brings skills. Every other entry is a connector: no marketplace plugin
    follows it, and a conversation adds its carrier."""

    from clio_agent.plugins.loader import load_plugin
    from tests._marketplace import MARKETPLACE_ROOT

    catalog = entries.load_catalog([SHIPPED])
    plugins = {
        path.parent.name: load_plugin(path.parent, trusted=False)
        for path in sorted((MARKETPLACE_ROOT / "plugins").glob("*/plugin.json"))
    }
    following: dict[str, list[str]] = {}
    for record in plugins.values():
        if record.follows:
            following.setdefault(record.follows, []).append(record.id)
    named = {cid for cid, entry in catalog.entries.items() if entry.plugin}
    assert {"paraview", "home-assistant"} <= named
    for entry in catalog.entries.values():
        if not entry.plugin:
            assert entry.id not in following, f"{entry.id} is a connector; nothing follows it"
            continue
        assert following.get(entry.id) == [entry.plugin], entry.id
        record = plugins[entry.plugin]
        assert record.mcp_servers == {entry.id: {"type": "clio-connection", "connection": entry.id}}
        folders = list((MARKETPLACE_ROOT / "plugins" / entry.plugin / "skills").glob("*/SKILL.md"))
        assert folders, f"{entry.plugin} brings no skill"
        # Every skill loads: a frontmatter YAML error drops a skill with only a diagnostic.
        assert len(record.skills) == len(folders) and not record.diagnostics, entry.plugin
    # The geo plugin earthscope and geodesy require keeps its own server and follows none.
    assert plugins["geo"].follows == "" and "geo" in plugins["geo"].mcp_servers
