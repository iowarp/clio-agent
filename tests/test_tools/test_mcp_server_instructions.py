"""A server's MCP instructions are kept per namespace, cached, and describe a deferred server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastmcp import Client, FastMCP

from clio_agent.gact.agents.deferred_tools import SEARCH_TOOLS, plan_disclosure
from clio_agent.tools import listing_cache
from clio_agent.tools.mcp_server_instructions import (
    instructions_for,
    record_instructions,
    record_listing_facts,
)

_INSTRUCTIONS = "Controls the lights and locks of one house.\n\nCall list tools first."


@pytest.fixture(autouse=True)
def _forget() -> Any:
    yield
    record_instructions("home", None)


async def test_a_live_listing_keeps_the_servers_instructions() -> None:
    server = FastMCP("home", instructions=_INSTRUCTIONS)

    @server.tool
    def light_set(entity_id: str, on: bool) -> str:
        """Turn a light on or off."""
        return "ok"

    async with Client(server) as client:
        listed = await client.list_tools()
        record_listing_facts("home", client, listed)
    assert instructions_for("home") == _INSTRUCTIONS
    assert instructions_for("home", summary=True) == "Controls the lights and locks of one house."


def test_the_listing_cache_carries_instructions_across_a_restart(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.types import Tool

    monkeypatch.setattr(listing_cache, "_cache_path", lambda: tmp_path / "listing.json")
    monkeypatch.setattr(listing_cache, "_launcher_fingerprint", lambda _command: "fp")
    monkeypatch.setattr(listing_cache, "server_identity", lambda _command, _args: "id")
    tool = Tool(name="light_set", inputSchema={"type": "object", "properties": {}})
    record_instructions("home", _INSTRUCTIONS)
    listing_cache.store_listing("home", "python", ("server.py",), [tool])
    record_instructions("home", None)  # a new process knows nothing yet
    assert listing_cache.load_listing("home", "python", ("server.py",)) is not None
    assert instructions_for("home") == _INSTRUCTIONS


def test_a_deferred_server_without_a_description_is_described_by_its_instructions() -> None:
    record_instructions("home", _INSTRUCTIONS)
    tools = {
        name: dspy.Tool(lambda: "ok", name=name, desc="A tool.")
        for name in (SEARCH_TOOLS, "home_light_set")
    }
    resolution = SimpleNamespace(
        mcp_servers={"home": {"defer": True, "plugin": "house"}},
        tools=[],
        plugins=[{"id": "house", "description": "The plugin's own words."}],
    )
    entry = plan_disclosure(resolution, tools).entry
    assert "- home: Controls the lights and locks of one house." in entry


def test_the_summary_length_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    record_instructions("home", _INSTRUCTIONS)
    monkeypatch.setenv("CLIO_TOOLS_INSTRUCTIONS_CHARS", "9")
    assert instructions_for("home", summary=True) == "Controls…"


def _deferred_entry() -> str:
    tools = {
        name: dspy.Tool(lambda: "ok", name=name, desc="A tool.")
        for name in (SEARCH_TOOLS, "home_light_set")
    }
    resolution = SimpleNamespace(mcp_servers={"home": {"defer": True}}, tools=[], plugins=[])
    return plan_disclosure(resolution, tools).entry


def test_a_shortened_listing_says_where_the_full_instructions_are() -> None:
    record_instructions("home", _INSTRUCTIONS)
    assert "(Its full instructions come with its tools from search_tools.)" in _deferred_entry()
    record_instructions("home", "Controls the lights and locks of one house.")
    assert "search_tools.)" not in _deferred_entry()


def test_loading_a_servers_tools_returns_its_whole_instructions_once() -> None:
    from clio_agent.gact.search_tools import _present, search_result
    from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog

    long_instructions = _INSTRUCTIONS + "\n\n" + ("Lights take an entity id. " * 300).strip()
    record_instructions("home", long_instructions)
    catalog = ToolCatalog(
        [
            CatalogEntry("home_light_set", "home", "Turn a light on or off."),
            CatalogEntry("home_lock_set", "home", "Lock a door."),
            CatalogEntry("calendar_event_create", "calendar", "Create an event."),
        ]
    )
    text, structured = search_result(catalog, "select:home_light_set,home_lock_set")
    assert structured["servers"] == [{"name": "home", "instructions": long_instructions}]
    assert text.count("## home server instructions") == 1 and long_instructions in text
    view = _present({"query": "select:home_light_set"}, text, structured)
    server_block = next(block for block in view["blocks"] if block["id"] == "server-0")
    assert server_block["text"] == long_instructions
    _, other = search_result(catalog, "select:calendar_event_create")
    assert other["servers"] == []
