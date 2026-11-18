"""A connector's ``hide_tools`` leaves those server tools out of its proxy."""

from __future__ import annotations

import asyncio

from fastmcp import Client, FastMCP

from clio_agent.connectors.entries import entry_from_raw
from clio_agent.tools.hidden_tools import HiddenTools


def _server() -> FastMCP:
    server = FastMCP("kicad")

    @server.tool
    def sim_run_transient() -> str:
        return "simulated"

    @server.tool
    def sim_list_models() -> str:
        return "models"

    @server.tool
    def pcb_move_footprint() -> str:
        return "moved"

    return server


def test_hidden_tools_are_not_listed_and_cannot_be_called() -> None:
    server = _server()
    server.add_transform(HiddenTools(["sim_*"], connector="kicad"))

    async def run() -> tuple[list[str], str, str]:
        async with Client(server) as client:
            names = sorted(tool.name for tool in await client.list_tools())
            moved = (await client.call_tool("pcb_move_footprint", {})).data
            try:
                await client.call_tool("sim_run_transient", {})
                refused = "called"
            except Exception as exc:  # noqa: BLE001 - the unknown-tool error
                refused = str(exc)
            return names, moved, refused

    names, moved, refused = asyncio.run(run())
    assert names == ["pcb_move_footprint"]
    assert moved == "moved"
    assert "sim_run_transient" in refused


def test_entry_reads_hide_tools_and_need_groups() -> None:
    entry = entry_from_raw(
        {
            "id": "kicad",
            "name": "KiCad",
            "description": "Boards",
            "server": {"command": "uvx", "args": ["kicad-mcp-pro"]},
            "hide_tools": ["sim_*"],
            "needs": [
                {
                    "key": "nexar_id",
                    "kind": "value",
                    "label": "Nexar client ID",
                    "required": False,
                    "group": "Part catalogs",
                }
            ],
        },
        folder="",
    )
    assert entry.hide_tools == ("sim_*",)
    assert entry.needs[0].group == "Part catalogs"
    assert entry.to_dict()["hide_tools"] == ["sim_*"]
    assert entry.to_dict()["needs"][0]["group"] == "Part catalogs"
