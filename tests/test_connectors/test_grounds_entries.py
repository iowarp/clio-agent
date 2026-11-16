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
    # The CLIO fork, one stdio process run by uvx from a pinned commit (every tool).
    assert not burette.is_url and burette.needs == () and burette.tools == ()
    assert burette.server["command"] == "uvx" and burette.plugin == "burette"
    source = burette.server["args"][1]
    assert source.startswith("git+https://github.com/JaimeCernuda/Burette@")
    assert source.endswith("#subdirectory=packages/clio-burette")
    assert len(source.split("@")[1].split("#")[0]) == 40  # a full commit, not a branch
    spice = catalog.entries["spice"]
    assert spice.server["command"] == "clio-kit" and list(spice.server["args"]) == [
        "mcp-server",
        "spice",
    ]
    assert spice.kit == "spice" and spice.logo == "spice.svg" and spice.tools == ()
    paraview = catalog.entries["paraview"]
    assert paraview.plugin == "paraview"
    assert [(n.key, n.required, n.agent_settable) for n in paraview.needs] == [
        ("address", False, True),
        ("via", False, True),
        ("prepare", False, True),
    ]
    quantum = catalog.entries["ibm-quantum"]
    assert quantum.plugin == "ibm-quantum" and quantum.logo == "ibm-quantum.svg"
    assert [(n.key, n.kind, n.required) for n in quantum.needs] == [
        ("token", "secret", False),
        ("instance", "value", False),
    ]
    assert quantum.server["env"]["QISKIT_MCP_NO_SAVED_ACCOUNT"] == "1"
    assert quantum.server["env"]["QISKIT_IBM_TOKEN"] == "${need.token}"
    for entry in (burette, paraview, quantum):
        logo = entry.logo_path
        assert logo is not None and logo.read_text().lstrip().startswith(("<svg", "<?xml"))


def test_an_unknown_entry_field_is_a_typed_diagnostic(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "- {id: bad, name: Bad, description: d, server: {url: 'https://x.invalid'}, tools: []}\n"
    )
    catalog = entries.Catalog()
    entries.load_folder(tmp_path, catalog)
    assert [d.code for d in catalog.diagnostics] == [entries.ENTRY_INVALID]


def test_offered_keeps_only_the_listed_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "burette.yaml").write_text(
        "- id: burette\n  name: Burette\n  description: d\n"
        "  server: {url: 'https://burette.invalid/mcp'}\n"
        "  tools: [preview_pdb_structure, render_molecular_scene, open_ketcher,"
        " control_ketcher]\n"
    )
    catalog = entries.load_catalog([tmp_path])
    assert catalog.entries["burette"].to_dict()["tools"] == list(catalog.entries["burette"].tools)
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


#: The connectors whose servers clio-kit runs from a pinned upstream release (apps ledger,
#: "Connectors moved into clio-kit (KM)"): connector id == clio-kit server name.
KIT_UPSTREAM = ("pdf", "earthdata", "home-assistant", "spice")


@pytest.mark.parametrize("connector", KIT_UPSTREAM)
def test_an_upstream_server_starts_through_the_clio_kit_launcher(connector: str) -> None:
    """Moved into clio-kit (KM): the id is kept, the command is ``clio-kit mcp-server <id>``,
    and the hand-written entry keeps its own name, logo and needs."""

    entry = entries.load_catalog([SHIPPED]).entries[connector]
    assert entry.server["command"] == "clio-kit"
    assert list(entry.server["args"]) == ["mcp-server", connector]
    assert entry.kit == connector and entry.logo == f"{connector}.svg"
    assert "npx" not in str(entry.server) and "uvx" not in str(entry.server)


def test_more_connectors_are_data_only() -> None:
    """The PDF viewer (an MCP App) and NASA Earthdata are catalog entries only."""

    catalog = entries.load_catalog([SHIPPED])
    pdf = catalog.entries["pdf"]
    assert pdf.needs == () and pdf.tools == ()
    assert pdf.logo_path is not None and pdf.logo_path.read_text().startswith("<svg")
    assert pdf.logo_dark == "pdf-dark.svg"
    earthdata = catalog.entries["earthdata"]
    assert [(n.key, n.kind, n.required) for n in earthdata.needs] == [
        ("username", "value", False),
        ("password", "secret", False),
    ]
    assert earthdata.server["env"]["EARTHDATA_PASSWORD"] == "${need.password}"
    assert earthdata.logo == "earthdata.svg" and earthdata.tools == ()


def test_geo_is_a_data_only_entry_on_the_clio_kit_launcher() -> None:
    """Geo (the globe of clio-kit's geo server) starts like ParaView, through clio-kit."""

    catalog = entries.load_catalog([SHIPPED])
    geo = catalog.entries["geo"]
    assert geo.server["command"] == "clio-kit"
    assert list(geo.server["args"]) == ["mcp-server", "geo"]
    assert geo.needs == () and geo.logo == "geo.svg" and geo.tools == ()


def test_home_assistant_needs_an_address_and_a_token_and_offers_a_curated_set() -> None:
    """ha-mcp, pinned in clio-kit, with its URL and long-lived token as needs; the token
    stays a secret."""

    catalog = entries.load_catalog([SHIPPED])
    ha = catalog.entries["home-assistant"]
    assert ha.server["command"] == "clio-kit"
    assert list(ha.server["args"]) == ["mcp-server", "home-assistant"]
    assert [(n.key, n.kind, n.required) for n in ha.needs] == [
        ("url", "address", True),
        ("token", "secret", True),
    ]
    assert ha.server["env"] == {
        "HOMEASSISTANT_URL": "${need.url}",
        "HOMEASSISTANT_TOKEN": "${need.token}",
    }
    assert ha.logo == "home-assistant.svg" and ha.plugin == "home-assistant"
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
            # Only a plugin whose one server is this connector's clio-kit server follows
            # it (LZ-D8); its server is not a saved connection.
            for plugin_id in following.get(entry.id, []):
                assert entry.kit, f"{entry.id} is a connector; only its own server follows it"
                (spec,) = plugins[plugin_id].mcp_servers.values()
                assert spec["args"][:2] == ["mcp-server", entry.kit], plugin_id
            continue
        assert following.get(entry.id) == [entry.plugin], entry.id
        record = plugins[entry.plugin]
        assert record.mcp_servers == {entry.id: {"type": "clio-connection", "connection": entry.id}}
        folders = list((MARKETPLACE_ROOT / "plugins" / entry.plugin / "skills").glob("*/SKILL.md"))
        assert folders, f"{entry.plugin} brings no skill"
        # Every skill loads: a frontmatter YAML error drops a skill with only a diagnostic.
        assert len(record.skills) == len(folders) and not record.diagnostics, entry.plugin
    # The geo plugin earthscope and geodesy require keeps its own server and follows the
    # Geo connector through it (LZ-D8, superseding PL-D6): mounted while Geo is on.
    assert plugins["geo"].follows == "geo" and "geo" in plugins["geo"].mcp_servers


@pytest.mark.parametrize(
    ("connector", "env", "keys"),
    [
        ("ase", {"NWCHEM_COMMAND", "ORCA_COMMAND", "HF_TOKEN"}, ["nwchem", "orca", "hf_token"]),
        ("fdmnes", {"FDMNES_COMMAND", "MP_API_KEY"}, ["fdmnes", "mp_api_key"]),
        ("graspa", {"GRASPA_COMMAND"}, ["graspa"]),
    ],
)
def test_chemistry_servers_start_through_clio_kit_with_optional_program_paths(
    connector: str, env: set[str], keys: list[str]
) -> None:
    """The chemistry servers clio-kit ships: every need is optional (an empty value leaves
    the server's own lookup in charge) and each follows its own plugin."""

    entry = entries.load_catalog([SHIPPED]).entries[connector]
    assert entry.server["command"] == "clio-kit"
    assert list(entry.server["args"]) == ["mcp-server", connector]
    assert set(entry.server["env"]) == env
    assert [n.key for n in entry.needs] == keys and not any(n.required for n in entry.needs)
    assert entry.plugin == connector and entry.logo == f"{connector}.svg"
