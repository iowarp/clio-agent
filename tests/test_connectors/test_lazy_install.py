"""Lazy install, ask on agent load (apps ledger LZ).

Nothing is built at install (LZ-D1). The pinned clio-kit's servers are connectors
derived from its catalogue (LZ-D7), a plugin whose one server is such a server follows
its connector and its server is mounted only while that connector is on (LZ-D8), and
a conversation whose agent needs what is not ready shows one card (LZ-D2 to LZ-D6).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.connectors import entries, kit_entries, kit_readiness, server_gate, state

# A small stand-in for clio_kit/_components.json: three servers, two workflow packages.
CATALOGUE = {
    "servers": {
        "alpha": {"artifact": "server/alpha", "runtime": "python"},
        "beta-tool": {"artifact": "server/beta-tool", "runtime": "python"},
        "gamma": {"artifact": "server/gamma", "runtime": "node"},
    },
    "artifacts": {
        "server/alpha": {"sha256": "a" * 64},
        "server/beta-tool": {"sha256": "b" * 64},
        "server/gamma": {"sha256": "c" * 64},
    },
    "packages": {
        "kit-alpha": {
            "manifest": {"description": "Read alpha files."},
            "servers": {"kit-alpha": {"command": "clio-kit", "args": ["mcp-server", "alpha"]}},
        },
        "kit-beta-tool": {
            "manifest": {"description": "Beta things."},
            "servers": {"kit-beta": {"command": "clio-kit", "args": ["mcp-server", "beta-tool"]}},
        },
        "kit-data": {"manifest": {"dependencies": ["kit-alpha"]}, "servers": {}},
        "kit-more": {"manifest": {"dependencies": ["kit-alpha", "kit-beta-tool"]}, "servers": {}},
    },
}
DERIVE = "names:\n  alpha: ALPHA\ngroups:\n  kit-data: Data formats\n  kit-more: More\n"


@pytest.fixture
def kit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The stand-in catalogue, a shipped folder with one hand-written kit entry."""

    shipped = tmp_path / "shipped"
    (shipped / "clio-kit").mkdir(parents=True)
    (shipped / "clio-kit" / "derive.yaml").write_text(DERIVE)
    (shipped / "connector.schema.json").write_text(
        (entries.shipped_dir() / "connector.schema.json").read_text()
    )
    (shipped / "written.yaml").write_text(
        "- id: beta-tool\n  name: Beta by hand\n  description: Written by hand\n"
        "  server:\n    command: clio-kit\n    args: [mcp-server, beta-tool]\n"
    )
    monkeypatch.setattr(kit_entries, "components", lambda: CATALOGUE)
    monkeypatch.setattr(entries, "shipped_dir", lambda: shipped)
    monkeypatch.delenv("CLIO_CONNECTOR_CATALOG_DIRS", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    entries._CACHE.clear()
    entries._validator.cache_clear()
    yield shipped
    entries._CACHE.clear()
    entries._validator.cache_clear()


def test_every_kit_server_is_a_connector_and_a_written_entry_wins(kit: Path) -> None:
    catalog = entries.load_catalog()
    assert catalog.diagnostics == []
    alpha, beta, gamma = (catalog.entries[cid] for cid in ("alpha", "beta-tool", "gamma"))
    assert (alpha.name, alpha.description, alpha.group, alpha.kit) == (
        "ALPHA",
        "Read alpha files.",
        "Data formats",  # the first group listed wins
        "alpha",
    )
    assert dict(alpha.server) == {"command": "clio-kit", "args": ["mcp-server", "alpha"]}
    # The hand-written entry keeps its name and description and takes only the group.
    assert (beta.name, beta.description, beta.group, beta.kit) == (
        "Beta by hand",
        "Written by hand",
        "More",
        "beta-tool",
    )
    assert (gamma.name, gamma.group) == ("Gamma", "")  # words; no workflow lists it


def test_derivation_can_be_turned_off(kit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_CONNECTOR_DERIVE_KIT", "0")
    assert set(entries.load_catalog().entries) == {"beta-tool"}


def test_a_plugin_whose_one_server_is_a_kit_server_follows_it(kit: Path) -> None:
    from clio_agent.plugins.records import PluginRecord

    monkeypatch_names = {"command": "clio-kit", "args": ["mcp-server", "alpha"]}
    assert kit_entries.kit_server(monkeypatch_names) == "alpha"
    assert kit_entries.kit_server({"command": "C:/x/clio-kit.exe", "args": ["mcp-server", "A"]})
    assert (
        kit_entries.kit_server({"command": "clio-kit", "args": ["mcp-server", "--prepare"]}) == ""
    )
    assert kit_entries.kit_server({"type": "clio-connection", "connection": "alpha"}) == ""

    def record(servers: dict[str, Any], follows: str = "") -> PluginRecord:
        return PluginRecord(
            id="p",
            version="1",
            description="",
            kind="plugin",
            format="agent-plugins",
            root="/tmp",
            mcp_servers=servers,
            follows=follows,
        )

    assert record({"a": monkeypatch_names}).follows == "alpha"
    assert record({"a": monkeypatch_names}, follows="other").follows == "other"
    two = {"a": monkeypatch_names, "b": {"command": "uv", "args": ["run"]}}
    assert record(two).follows == ""
    assert record({"a": {"command": "clio-kit", "args": ["mcp-server", "unknown"]}}).follows == ""


def _resolution(servers: dict[str, Any], **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        mcp_servers=servers,
        tools=[{"name": f"{name}_"} for name in servers] + [{"name": "native"}],
        diagnostics=[],
        removed=fields.get("removed", []),
        skills=fields.get("skills", []),
        prompt={},
        plugins=[],
        agent={"ref": "m/agent", "name": "Agent"},
    )


def test_a_server_is_mounted_only_while_its_connector_is_on(kit: Path) -> None:
    servers = {
        "alpha": {"plugin": "pa", "spec": {"command": "clio-kit", "args": ["mcp-server", "alpha"]}},
        "beta-tool": {
            "plugin": "pb",
            "spec": {"type": "clio-connection", "connection": "beta-tool"},
        },
        "own": {"plugin": "po", "spec": {"command": "uv", "args": ["run"]}},
    }
    resolution = _resolution(dict(servers))
    assert server_gate.gate(resolution) is True
    assert set(resolution.mcp_servers) == {"own"}
    assert [row["name"] for row in resolution.tools] == ["own_", "native"]
    assert sorted(
        (row["code"], row["plugin"], row["server"], row["because"])
        for row in resolution.diagnostics
    ) == [
        ("connector_off", "pa", "alpha", "alpha"),
        ("connector_off", "pb", "beta-tool", "beta-tool"),
    ]
    assert sorted(server_gate.left_out(resolution)) == ["alpha", "beta-tool"]
    state.update("alpha", enabled=True)
    on = _resolution(dict(servers))
    assert server_gate.gate(on) is True and set(on.mcp_servers) == {"alpha", "own"}
    assert server_gate.bound_connectors(on) == {"alpha"}
    state.update("beta-tool", enabled=True)
    assert server_gate.gate(_resolution(dict(servers))) is False


def test_kit_readiness_reads_files_only(kit: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = kit.parent / "kit-cache"
    monkeypatch.setattr(kit_readiness, "cache_root", lambda: root)
    monkeypatch.setattr(kit_readiness, "_environment_name", lambda component: "alpha-0123")
    assert kit_readiness.prepared("alpha") is False  # no component, so nothing was built
    assert kit_readiness.prepared("gamma") is None  # not Python: unknown, never "ready"
    assert kit_readiness.prepared("missing") is None
    (root / "components" / ("a" * 64) / "alpha").mkdir(parents=True)
    assert kit_readiness.prepared("alpha") is False
    environment = root / "mcp-environments" / "alpha-0123"
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /x\n")
    assert kit_readiness.prepared("alpha") is False  # no interpreter yet
    (environment / "bin" / "python").write_text("")
    assert kit_readiness.prepared("alpha") is True


# --------------------------------------------------------------- the card


class _Workspaces:
    def __init__(self) -> None:
        self.rows = {"ws": SimpleNamespace(id="ws", metadata={})}

    def get(self, wid: str) -> Any:
        return self.rows.get(wid)

    def update(self, wid: str, *, metadata_patch: dict[str, Any]) -> Any:
        self.rows[wid].metadata.update(metadata_patch)
        return self.rows[wid]


def _app() -> Any:
    sessions = {
        sid: SimpleNamespace(id=sid, metadata={}, workspace_id="ws", parent_session_id="")
        for sid in ("s1", "s2", "child")
    }
    sessions["child"].parent_session_id = "s1"
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions=SimpleNamespace(get=sessions.get), workspaces=_Workspaces(), bus=None
        )
    )


@pytest.fixture
def card(kit: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from clio_agent.gact import agent_needs, connector_offer

    monkeypatch.setattr(connector_offer, "session_agent", lambda app, sid: ((), "m/agent", "Agent"))
    monkeypatch.setattr(kit_readiness, "prepared", lambda server: server != "alpha")
    monkeypatch.setattr(
        "clio_agent.gact.permission_delivery.attended_session_id", lambda app, sid: sid
    )
    return agent_needs


def _needing() -> SimpleNamespace:
    resolution = _resolution(
        {
            "alpha": {
                "plugin": "pa",
                "spec": {"command": "clio-kit", "args": ["mcp-server", "alpha"]},
            },
            "beta-tool": {
                "plugin": "pb",
                "spec": {"command": "clio-kit", "args": ["mcp-server", "beta-tool"]},
            },
        },
        skills=[{"id": "s", "plugin": "pa"}],
    )
    server_gate.gate(resolution)
    return resolution


def test_the_items_are_the_facts_of_the_resolution(card: Any) -> None:
    items = card.items_for(_needing())
    assert [(i["id"], i["kind"], i["reasons"], i["download"]) for i in items] == [
        ("alpha", "plugin", ["off", "download"], True),  # pa brings a skill
        ("beta-tool", "connector", ["off"], False),
    ]
    state.update("alpha", enabled=True)
    state.update("beta-tool", enabled=True)
    ready = _needing()
    # On, but alpha's tools are not downloaded yet: it still needs the download.
    assert [(i["id"], i["reasons"]) for i in card.items_for(ready)] == [("alpha", ["download"])]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kit_readiness, "prepared", lambda server: True)
        assert card.items_for(_needing()) == []  # all ready: no card


def test_a_connector_with_a_required_value_missing_needs_it(card: Any, fixture_catalog) -> None:
    entries._CACHE.clear()
    resolution = _resolution(
        {
            "fixture-secret": {
                "plugin": "p",
                "spec": {"type": "clio-connection", "connection": "fixture-secret"},
            }
        }
    )
    state.update("fixture-secret", enabled=True)
    (item,) = card.items_for(resolution)
    assert item["reasons"] == ["needs"] and item["download"] is False
    assert [row["key"] for row in item["missing"]] == ["token"]
    assert [row["key"] for row in item["settings"]] == ["where"]


def test_one_card_per_conversation_and_not_now_is_remembered(card: Any) -> None:
    app = _app()
    row = card.ensure_card(app, "s1", _needing())
    assert row is not None and [i["id"] for i in row["items"]] == ["alpha", "beta-tool"]
    assert card.ensure_card(app, "s1", _needing()) is row
    assert card.ensure_card(app, "child", _needing()) is None  # a child never asks
    shown = card.interaction(app, row)
    assert shown.kind == "connector_setup" and shown.payload["intent"] == card.INTENT
    assert shown.actions == ["submit", "decline"] and shown.requires_human_response
    asyncio.run(card.answer(app, row, "decline", {}, {}))
    assert row["status"] == "declined"
    remembered = app.state.workspaces.rows["ws"].metadata[card.NOT_NOW_KEY]
    assert remembered == {"m/agent": ["alpha", "beta-tool"]}
    # A new conversation with the same agent in the same workspace does not ask again.
    assert card.ensure_card(app, "s2", _needing()) is None


def test_the_card_follows_what_was_enabled_elsewhere(card: Any) -> None:
    app = _app()
    row = card.ensure_card(app, "s1", _needing())
    state.update("beta-tool", enabled=True)
    card.ensure_card(app, "s1", _needing())
    assert [i["id"] for i in row["items"]] == ["alpha"]
    state.update("alpha", enabled=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(kit_readiness, "prepared", lambda server: True)
        card.ensure_card(app, "s1", _needing())
    assert row["status"] == "submitted" and row["settled"] == "ready_elsewhere"
    # The settled card still names what it asked for, now ready (not "uses ").
    assert [i["id"] for i in row["items"]] == ["alpha"]
    assert row["items"][0]["progress"] == card.READY
    shown = card.interaction(app, row)
    assert shown.title.endswith("uses " + row["items"][0]["name"])


def test_enable_all_downloads_then_enables_with_progress_in_words(
    card: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import connectors_service as service
    from clio_agent.gact.connector_setup import SetupResponseError

    prepared: list[str] = []
    enabled: list[str] = []
    monkeypatch.setattr(card, "_prepare_kit", lambda cid, server: prepared.append(server))

    async def enable(app: Any, cid: str) -> dict[str, Any]:
        enabled.append(cid)
        if cid == "beta-tool":
            raise service.ConnectorError("connection_name_taken", "A saved connection exists")
        return {"tool_count": 7, "state": {"status": "ready"}}

    monkeypatch.setattr(service, "enable", enable)
    app = _app()
    row = card.ensure_card(app, "s1", _needing())

    async def run() -> None:
        await card.answer(app, row, "submit", {}, {})
        assert row["phase"] == card.WORKING
        assert card.interaction(app, row).actions == ["dismiss"]
        with pytest.raises(SetupResponseError):
            await card.answer(app, row, "submit", {}, {})  # not while it works
        await asyncio.gather(*app.state.agent_needs_tasks)

    asyncio.run(run())
    assert prepared == ["alpha", "beta-tool"] and sorted(enabled) == ["alpha", "beta-tool"]
    alpha, beta = row["items"]
    assert (alpha["progress"], alpha["tool_count"], alpha["download"]) == ("ready", 7, False)
    assert (beta["progress"], beta["reason"]) == ("failed", "connection_name_taken")
    assert row["phase"] == card.DONE
    assert card.interaction(app, row).actions == ["dismiss", "submit"]  # retry the failed one
    asyncio.run(card.answer(app, row, "dismiss", {}, {}))
    assert row["status"] == "submitted"


def test_enable_all_takes_only_the_chosen_items(card: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import connectors_service as service

    monkeypatch.setattr(card, "_prepare_kit", lambda cid, server: None)

    async def enable(app: Any, cid: str) -> dict[str, Any]:
        return {"tool_count": 1, "state": {"status": "ready"}}

    monkeypatch.setattr(service, "enable", enable)
    app = _app()
    row = card.ensure_card(app, "s1", _needing())

    async def run() -> None:
        await card.answer(app, row, "submit", {}, {"items": ["beta-tool"]})
        await asyncio.gather(*app.state.agent_needs_tasks)

    asyncio.run(run())
    assert [i["progress"] for i in row["items"]] == ["skipped", "ready"]
    json.dumps(card.interaction(app, row).payload)  # plain data on the wire
