"""The channel fixture server used by the live clio.mcp-app.v1 scenarios (AQ27-AQ29)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_apps" / "channel_app_server.py"


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("channel_app_server", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_fixture_opens_a_view_and_logs_each_set_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("CHANNEL_APP_LOG", str(log))
    module = _module()
    async with Client(module.build_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert tools["open_timeline"].meta["ui"]["resourceUri"] == module.VIEW_URI
        opened = await client.call_tool("open_timeline", {"time": 2, "steps": 12})
        assert opened.structured_content == {"time": 2, "steps": 12}
        moved = await client.call_tool("set_time", {"time": 7})
        assert moved.structured_content == {"time": 7, "steps": 12}
        resource = await client.read_resource(module.VIEW_URI)
        assert resource[0].mime_type == "text/html;profile=mcp-app"
        assert "ui/update-model-context" in resource[0].text
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(row["tool"], row["time"]) for row in rows] == [("open_timeline", 2), ("set_time", 7)]
