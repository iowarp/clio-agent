"""A clio-kit server's prerequisite note reaches its derived connector (SL-D8, LZ-O4).

The pinned clio-kit's catalogue records, per server, what must already be on the
machine (``prerequisites.note``, such as "Needs the gamma program installed …"). The
derived connector carries it as ``requires``; a hand-written entry that says none takes
it too; the connector row, the agent-load card item and the offer card show it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.connectors import entries, kit_entries, kit_readiness, server_gate

from .test_lazy_install import CATALOGUE, kit  # noqa: F401 - the shared stand-in catalogue

NOTE = "Needs the gamma program installed on this machine; it is never downloaded."


@pytest.fixture
def noted(kit: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: F811 - fixture reuse
    catalogue = copy.deepcopy(CATALOGUE)
    catalogue["servers"]["gamma"]["prerequisites"] = {"executables": ["g"], "note": NOTE}
    catalogue["servers"]["beta-tool"]["prerequisites"] = {"note": "  Needs   beta  here. "}
    catalogue["servers"]["alpha"]["prerequisites"] = {"executables": ["a"]}
    monkeypatch.setattr(kit_entries, "components", lambda: catalogue)
    entries._CACHE.clear()
    return kit


def test_the_prerequisite_note_becomes_requires(noted: Path) -> None:
    catalog = entries.load_catalog()
    assert catalog.diagnostics == []
    alpha, beta, gamma = (catalog.entries[cid] for cid in ("alpha", "beta-tool", "gamma"))
    assert gamma.requires == NOTE
    assert alpha.requires == ""  # executables alone say nothing in words
    # The hand-written entry says none, so it takes the catalogue's, whitespace folded.
    assert (beta.name, beta.requires) == ("Beta by hand", "Needs beta here.")
    assert gamma.to_dict()["requires"] == NOTE
    assert "requires" not in alpha.to_dict()


def test_a_long_note_is_cut_to_the_schema_limit(
    noted: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = copy.deepcopy(CATALOGUE)
    catalogue["servers"]["gamma"]["prerequisites"] = {"note": "word " * 100}
    monkeypatch.setattr(kit_entries, "components", lambda: catalogue)
    entries._CACHE.clear()
    catalog = entries.load_catalog()
    assert catalog.diagnostics == []
    assert len(catalog.entries["gamma"].requires) == 300


def test_a_written_requires_wins(noted: Path) -> None:
    (noted / "written.yaml").write_text(
        "- id: beta-tool\n  name: Beta by hand\n  description: Written by hand\n"
        "  requires: Needs the beta program.\n"
        "  server:\n    command: clio-kit\n    args: [mcp-server, beta-tool]\n"
    )
    entries._CACHE.clear()
    assert entries.load_catalog().entries["beta-tool"].requires == "Needs the beta program."


def test_the_connector_row_and_the_offer_carry_requires(
    noted: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.connectors import state
    from clio_agent.gact import connector_offer, connectors_service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = SimpleNamespace(state=SimpleNamespace(plugin_service=None))
    found = state.all_entries()
    gamma = connectors_service.row(app, found["gamma"], state.Choice())
    alpha = connectors_service.row(app, found["alpha"], state.Choice())
    assert (gamma["requires"], alpha["requires"]) == (NOTE, None)
    assert connector_offer._brief(found["gamma"])["requires"] == NOTE
    assert connector_offer._brief(found["alpha"])["requires"] is None


def test_the_agent_load_item_carries_requires(
    noted: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clio_agent.gact import agent_needs

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(kit_readiness, "prepared", lambda server: None)
    resolution = SimpleNamespace(
        mcp_servers={
            name: {
                "plugin": f"p-{name}",
                "spec": {"command": "clio-kit", "args": ["mcp-server", name]},
            }
            for name in ("alpha", "gamma")
        },
        tools=[],
        diagnostics=[],
        removed=[],
        skills=[],
        prompt={},
        plugins=[],
        agent={"ref": "m/agent", "name": "Agent"},
    )
    server_gate.gate(resolution)
    items = {item["id"]: item for item in agent_needs.items_for(resolution)}
    assert (items["gamma"]["requires"], items["alpha"]["requires"]) == (NOTE, None)
