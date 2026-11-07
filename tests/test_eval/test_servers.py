"""The synthetic MCP servers: catalog sizes, typed schemas, call logging, records data."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from scripts.eval.tool_calling.fixtures.plugins import write_marketplace
from scripts.eval.tool_calling.servers import run
from scripts.eval.tool_calling.servers.records import generate_records, negative_answer
from scripts.eval.tool_calling.servers.toolkit import call_log_path


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"EVAL_SEED": "7", "EVAL_CALL_LOG": str(tmp_path / "calls.jsonl"), **extra}


async def test_tool_counts_and_unique_names(tmp_path: Path) -> None:
    counts = {}
    for name in run.SERVERS:
        async with Client(run.build(name, _env(tmp_path))) as client:
            tools = await client.list_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))
        counts[name] = len(names)
    assert 85 <= counts["synthetic-home"] <= 100
    assert counts["synthetic-calendar"] == counts["synthetic-inventory"] == 15
    assert counts["records"] == 2


async def test_home_tool_has_typed_schema_and_logs_calls(tmp_path: Path) -> None:
    async with Client(run.build("synthetic-home", _env(tmp_path))) as client:
        tools = {t.name: t for t in await client.list_tools()}
        schema = tools["climate_set_temperature"].input_schema
        assert schema["required"] == ["entity_id", "temperature"]
        assert schema["properties"]["temperature"]["type"] == "number"
        assert "enum" in json.dumps(schema["properties"]["hvac_mode"])
        result = await client.call_tool(
            "climate_set_temperature", {"entity_id": "climate.guest_bedroom", "temperature": 19.5}
        )
        assert result.data["ok"] is True
        assert result.data["applied"] == {"target_temperature": 19.5}
        missing = await client.call_tool("light_turn_off", {"entity_id": "light.nowhere"})
        assert missing.data["ok"] is False and "light.porch" in missing.data["known_ids"]
        listing = await client.call_tool("irrigation_list_zones", {})
        assert "irrigation.back_garden_beds" in [i["id"] for i in listing.data["items"]]
    rows = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [r["tool"] for r in rows] == [
        "climate_set_temperature",
        "light_turn_off",
        "irrigation_list_zones",
    ]
    assert rows[0]["server"] == "synthetic-home"
    assert rows[0]["args"]["temperature"] == 19.5


async def test_a_write_lasts_for_its_server_only(tmp_path: Path) -> None:
    server = run.build("synthetic-home", _env(tmp_path))
    guest = {"entity_id": "climate.guest_bedroom"}
    async with Client(server) as client:
        before = (await client.call_tool("climate_get_state", guest)).data
        await client.call_tool("climate_set_temperature", {**guest, "temperature": 19.5})
        after = (await client.call_tool("climate_get_state", guest)).data
        listed = (await client.call_tool("climate_list", {})).data["items"]
    assert after["target_temperature"] == 19.5 != before["target_temperature"]
    assert after in listed
    # CLIO starts the server per session, so the next run starts from the seeded world.
    async with Client(run.build("synthetic-home", _env(tmp_path))) as fresh:
        again = (await fresh.call_tool("climate_get_state", guest)).data
    assert again == before


async def test_records_pages_and_negative_readings(tmp_path: Path) -> None:
    async with Client(run.build("records", _env(tmp_path, EVAL_RECORDS_COUNT="200"))) as client:
        page = (await client.call_tool("list_records", {"page": 2, "page_size": 500})).data
        assert page["page_size"] == 100 and page["total"] == 200 and page["total_pages"] == 2
        assert "reading" not in page["records"][0]
        negatives = []
        for row in (
            page["records"]
            + (await client.call_tool("list_records", {"page_size": 100})).data["records"]
        ):
            full = (await client.call_tool("get_record", {"record_id": row["record_id"]})).data
            if full["reading"] < 0:
                negatives.append(full["record_id"])
    assert sorted(negatives) == negative_answer(200, 7)["ids"]


def test_records_are_seeded() -> None:
    assert generate_records(20, 7) == generate_records(20, 7)
    assert generate_records(20, 7) != generate_records(20, 8)
    assert generate_records(200, 7)[:20] != generate_records(20, 7)


def test_call_log_path_ignores_unexpanded_placeholders(tmp_path: Path) -> None:
    assert call_log_path({"EVAL_CALL_LOG": str(tmp_path / "a.jsonl")}) == tmp_path / "a.jsonl"
    unexpanded = {"EVAL_CALL_LOG": "${CLIO_EVAL_CALL_LOG:-x}", "PLUGIN_DATA": str(tmp_path)}
    assert call_log_path(unexpanded) == tmp_path / "calls.jsonl"
    assert call_log_path({}) is None


@pytest.mark.timeout(60)  # starts a real stdio server process
async def test_plugin_folder_server_runs_over_stdio(tmp_path: Path) -> None:
    root = tmp_path / "marketplace"
    write_marketplace(root, seed=7, python="python")
    folder = root / "plugins" / "eval-records-20"
    spec = json.loads((folder / "mcp.json").read_text())["mcpServers"]["records"]
    args = [a.replace("${PLUGIN_ROOT}", str(folder)) for a in spec["args"]]
    # Outside the package: the copied files must import each other on their own.
    standalone = tmp_path / "elsewhere"
    shutil.copytree(folder / "server", standalone)
    args[0] = str(standalone / "run.py")
    env = {**_env(tmp_path, EVAL_RECORDS_COUNT="20"), "PATH": "/usr/bin:/bin"}
    transport = StdioTransport(command=sys.executable, args=args, env=env, cwd=str(tmp_path))
    async with Client(transport) as client:
        tools = sorted(t.name for t in await client.list_tools())
        result = await client.call_tool("get_record", {"record_id": "REC-00001"})
    assert tools == ["get_record", "list_records"]
    assert result.data["record_id"] == "REC-00001"
    assert (tmp_path / "calls.jsonl").is_file()


async def test_the_garage_announces_the_tool_it_adds(tmp_path: Path) -> None:
    """Qualification T20 needs a server whose tool list grows mid-session: unlocking
    the controls adds ``open_door`` and sends ``notifications/tools/list_changed``."""
    seen: list[str] = []

    async def handler(message: object) -> None:
        seen.append(str(getattr(getattr(message, "root", message), "method", "")))

    server = run.build("garage", {"EVAL_CALL_LOG": str(tmp_path / "calls.jsonl")})
    async with Client(server, message_handler=handler) as client:
        assert sorted(t.name for t in await client.list_tools()) == [
            "panel",
            "status",
            "unlock_controls",
        ]
        resources = await client.list_resources()
        assert "ui://garage/panel" in {str(r.uri) for r in resources}
        await client.call_tool("unlock_controls", {})
        assert "open_door" in {t.name for t in await client.list_tools()}
        assert (await client.call_tool("open_door", {})).data == {"door": "open"}
    assert "notifications/tools/list_changed" in seen


def test_the_dynamic_plugin_declares_its_server_deferred(tmp_path: Path) -> None:
    manifest = write_marketplace(tmp_path, seed=7)
    assert manifest["plugins"]["eval-dynamic"] == ["garage"]
    plugin = json.loads((tmp_path / "plugins" / "eval-dynamic" / "plugin.json").read_text())
    assert plugin["extensions"]["ai.iowarp.clio"]["servers"]["garage"]["defer"] is True
