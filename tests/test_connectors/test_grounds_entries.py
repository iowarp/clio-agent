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
    """The PDF viewer (an MCP App) and arXiv (no view) are catalog entries, nothing more."""

    catalog = entries.load_catalog([SHIPPED])
    pdf = catalog.entries["pdf"]
    assert pdf.server["command"] == "npx" and pdf.needs == () and pdf.tools == ()
    assert "@modelcontextprotocol/server-pdf@2.0.4" in pdf.server["args"]
    assert pdf.logo_path is not None and pdf.logo_path.read_text().startswith("<svg")
    arxiv = catalog.entries["arxiv"]
    assert arxiv.server["args"][:2] == ["arxiv-mcp-server==0.8.2", "--storage-path"]
    assert [(n.key, n.kind, n.required, n.agent_settable) for n in arxiv.needs] == [
        ("papers", "value", False, True)
    ]
    assert arxiv.logo == "" and "search_papers" in arxiv.tools
