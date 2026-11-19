"""A server's MCP instructions are kept per namespace, cached, shown in the prompt and searched."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import dspy
import pytest
from fastmcp import Client, FastMCP

from clio_agent.gact.agents.deferred_tools import SEARCH_TOOLS, plan_disclosure
from clio_agent.tools import listing_cache
from clio_agent.tools.mcp_server_instructions import (
    forget_instructions,
    instructions_for,
    record_connection_instructions,
    record_instructions,
    record_listing_facts,
    rediscover_instructions,
)

_INSTRUCTIONS = "Controls the lights and locks of one house.\n\nCall list tools first."


@pytest.fixture(autouse=True)
def _forget() -> Any:
    yield
    for namespace in ("home", "grid"):
        forget_instructions(namespace)


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
    assert instructions_for("home", summary=True) == (
        "Controls the lights and locks of one house.\nCall list tools first."
    )


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
    forget_instructions("home")  # a new process knows nothing yet
    assert listing_cache.load_listing("home", "python", ("server.py",)) is not None
    assert instructions_for("home") == _INSTRUCTIONS


def test_a_deferred_servers_entry_carries_its_plugins_words_and_its_instructions() -> None:
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
    assert "- home: The plugin's own words." in entry
    assert (
        "  It says about itself:\n"
        "    Controls the lights and locks of one house.\n"
        "    Call list tools first."
    ) in entry


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
    record_instructions("home", _INSTRUCTIONS + "\n\n" + ("Lights take an entity id. " * 80))
    assert "(Shortened: its full instructions come with its tools from search_tools.)" in (
        _deferred_entry()
    )
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


_GRID = (
    "Runs your work on compute sites.\n\n"
    "Sites configured: alpha (ssh, slurm).\n"
    "Packages staged to run on them: fluidsolver 0.2.1."
)


def test_the_excerpt_keeps_what_a_server_lists_after_its_first_paragraph() -> None:
    record_instructions("grid", _GRID)
    text = instructions_for("grid", summary=True)
    assert "alpha (ssh, slurm)" in text and "fluidsolver 0.2.1" in text


async def test_each_new_connection_refreshes_the_instructions() -> None:
    record_instructions("grid", "Sites configured: none.")
    server = FastMCP("grid", instructions=_GRID)
    async with Client(server) as client:
        record_connection_instructions("grid", client)
    assert instructions_for("grid") == _GRID


def test_a_client_that_is_not_connected_leaves_the_instructions_alone() -> None:
    record_instructions("grid", _GRID)
    record_connection_instructions("grid", Client(FastMCP("grid")))
    assert instructions_for("grid") == _GRID


async def test_an_open_connection_reads_what_the_server_says_now() -> None:
    server = FastMCP("grid", instructions="Sites configured: none.")
    async with Client(server) as client:
        record_connection_instructions("grid", client)
        server.instructions = _GRID
        assert await rediscover_instructions("grid", client, timeout=5) is True
        assert client.is_connected()
    assert instructions_for("grid") == _GRID


class _HeldClient:
    """A connected client whose server answers ``server/discover`` with ``answer``."""

    def __init__(self, answer: str, *, delay: float = 0.0, handshake: bool = False) -> None:
        self.initialize_result = object() if handshake else None
        self.protocol_version = "2026-07-28"
        self.asked = 0

        async def send_discover(version: str) -> dict[str, Any]:
            self.asked += 1
            await asyncio.sleep(delay)
            return {"instructions": answer}

        self.session = SimpleNamespace(send_discover=send_discover)

    def is_connected(self) -> bool:
        return True


async def test_a_slow_answer_keeps_the_recorded_instructions() -> None:
    record_instructions("grid", _GRID)
    client = _HeldClient("Sites configured: none.", delay=5)
    assert await rediscover_instructions("grid", client, timeout=0.05) is False
    assert instructions_for("grid") == _GRID


async def test_a_handshake_era_connection_keeps_its_initialize_text() -> None:
    record_instructions("grid", _GRID)
    client = _HeldClient("Sites configured: none.", handshake=True)
    assert await rediscover_instructions("grid", client, timeout=1) is False
    assert client.asked == 0 and instructions_for("grid") == _GRID


async def test_a_turn_asks_each_kept_alive_server_once() -> None:
    from clio_agent.gact.external_task_connections import refresh_instructions

    grid, home = _HeldClient(_GRID), _HeldClient(_INSTRUCTIONS)
    fleet = ("", "", "in-process:1")

    def host(server: str, client: Any, pool: Any = fleet) -> Any:
        return SimpleNamespace(
            client=client,
            pool=pool,
            cache_key=(server, "", "in-process:1"),
            closing=False,
            superseded=False,
            broken=False,
        )

    tasks = SimpleNamespace(
        connections={
            "a": host("grid", grid),
            "b": host("grid", _HeldClient("other fleet")),
            "c": host("home", home),
            "d": host("home", _HeldClient("not kept alive"), pool=None),
        }
    )
    assert await refresh_instructions(tasks, timeout=1) == 2
    assert grid.asked == 1 and home.asked == 1
    assert instructions_for("grid") == _GRID and instructions_for("home") == _INSTRUCTIONS


def test_a_cached_listing_never_replaces_what_a_live_connection_said() -> None:
    record_instructions("grid", _GRID)
    record_instructions("grid", "Sites configured: none.", cached=True)
    assert instructions_for("grid") == _GRID
    forget_instructions("grid")
    record_instructions("grid", "Sites configured: none.", cached=True)
    assert instructions_for("grid") == "Sites configured: none."


def test_a_listed_servers_instructions_have_their_own_section() -> None:
    from clio_agent.gact.agents.deferred_tools import _listed_entry

    record_instructions("grid", _GRID)
    tools = {"grid_run": object(), "home_light_set": object(), "read_file": object()}
    entry = _listed_entry(tools, frozenset({"grid", "home"}), frozenset())
    assert entry.startswith("## What your MCP servers say about themselves")
    assert "- grid\n  It says about itself:\n    Runs your work on compute sites." in entry
    assert "- home" not in entry  # it sent no instructions
    assert _listed_entry(tools, frozenset({"grid", "home"}), frozenset({"grid"})) == ""


def test_search_finds_a_servers_tools_by_the_words_of_its_instructions() -> None:
    from clio_agent.gact.tool_catalog import CatalogEntry, ToolCatalog

    catalog = ToolCatalog(
        [
            CatalogEntry("grid_run", "grid", "Run a staged package or a command."),
            CatalogEntry("grid_describe", "grid", "Describe what is configured."),
            CatalogEntry("home_light_set", "home", "Turn a light on or off."),
        ],
        owner_text=instructions_for,
    )
    assert catalog.search("fluidsolver on alpha")[0] == []
    record_instructions("grid", _GRID)  # read at search time, not when built
    found, _ = catalog.search("run fluidsolver on alpha")
    assert [entry.name for entry in found] == ["grid_run", "grid_describe"]
