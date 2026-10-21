"""A plugin claims the native download; its agent and defined subagent each get it once."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.agents.resolution import resolution_agent_def
from clio_agent.plugins.service import PluginService
from tests._plugin_fixtures import custom_marketplace, tools_module

AGENT = """
from clio_agent.sdk import Agent


class Downloads(Agent):
    id = "downloads"
    version = "1.0.0"
    role = "an agent that downloads approved data"
    plugins = Agent.plugins + ["downloads"]
"""

SUBAGENT = """
from clio_agent.sdk import Subagent

worker = Subagent("worker", prompt="Download the selected approved paths.")
"""


def test_root_and_subagent_download_claims_resolve_and_build_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's claim reaches the existing builtin with no duplicate attachment."""
    custom_marketplace(
        tmp_path,
        monkeypatch,
        plugins={
            "downloads": {
                "files": {
                    "ai.iowarp.clio/tools/builtin.py": tools_module("connected_data_download"),
                    "ai.iowarp.clio/subagents/worker.py": SUBAGENT,
                }
            }
        },
        agents={"downloads.py": AGENT},
    )
    service = PluginService()
    root = service.resolve("clio/downloads", pin=False)
    child = service.resolve_subagent(root, "worker", pin=False)
    for resolution, tier in ((root, 1), (child, 2)):
        rows = [row for row in resolution.tools if row["name"] == "connected_data_download"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "native" and rows[0]["plugin"] == "downloads"
        definition = resolution_agent_def(resolution)
        assert definition.tier == tier
        assert definition.tools.count("connected_data_download") == 1
        claimed = [row["name"] for row in resolution.tools if row["kind"] == "native"]
        available, gateway, withheld = build_native_tools(definition, claimed, {})
        assert available["connected_data_download"].name == "connected_data_download"
        assert "connected_data_download" not in gateway
        assert "connected_data_download" not in withheld
    # An agent whose plugins do not claim the download does not get it.
    standard = service.resolve("clio/standard", pin=False)
    assert "connected_data_download" not in {row["name"] for row in standard.tools}
