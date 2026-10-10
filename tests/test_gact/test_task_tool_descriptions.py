"""Agent-visible declarations explain which submissions become background tasks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.types import Tool, ToolExecution

from clio_agent.gact.agents.builders import _enabled_external_mcp_dspy_tools
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_spawn_skill_task_tool
from clio_agent.gact.storage.download_tool import build_connected_data_download_tool
from clio_agent.gact.types import AgentDef
from clio_agent.tools.execution import _make_dspy_tool
from clio_agent.tools.gateway import _list_tools_sync
from clio_agent.tools.servers.shell_server import (
    ShellEnvFacts,
    build_shell_tool_description,
    shell_server,
)
from clio_agent.tools.task_descriptions import mcp_task_description


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_shell_description_explains_task_switch(windows: bool) -> None:
    """Both real host declarations expose the task switch and its consequences."""
    text = build_shell_tool_description(
        ShellEnvFacts(
            windows,
            "Windows" if windows else "Linux",
            "PowerShell" if windows else "bash",
            not windows,
        )
    )
    assert "background=true" in text and "background=false" in text
    assert "durable Shell task handle" in text
    assert "query_tasks" in text and "observe_tasks" in text and "wait_tasks" in text
    assert "cancel_tasks" in text and "wake" in text
    assert "timeout_s" in text and "execution" in text


def test_shell_parameter_schema_explains_background_acceptance() -> None:
    """FastMCP's actual parameter schema distinguishes acceptance from completion."""
    listed = next(tool for tool in _list_tools_sync(shell_server) if tool.name == "bash")
    field = listed.input_schema["properties"]["background"]
    assert field["default"] is False
    assert "task handle" in field["description"]
    assert "completion" in field["description"]
    assert "background" not in listed.input_schema.get("required", [])


@pytest.mark.parametrize("support", ["required", "optional"])
def test_mcp_task_capability_reaches_model_description(support: str) -> None:
    """Per-tool task declarations survive the generic DSPy/provider bridge."""
    upstream = Tool(
        name="work",
        description="Perform domain work.",
        input_schema={"type": "object", "properties": {}},
        execution=ToolExecution(task_support=support),
    )
    tool = _make_dspy_tool("work", upstream, lambda _name, _args: "unused")
    assert tool.desc.startswith("Perform domain work.")
    assert "MCP task" in tool.desc and "durable task handle" in tool.desc
    assert "query_tasks" in tool.desc and "wait_tasks" in tool.desc and "wake" in tool.desc
    assert ("may instead return" in tool.desc) is (support == "optional")
    schema = tool.format_as_litellm_function_call()["function"]["parameters"]
    assert "does not enable" in schema["properties"]["_clio_task_description"]["description"]


@pytest.mark.parametrize("support", [None, "forbidden"])
def test_plain_mcp_tool_is_not_labelled_as_a_task(support: str | None) -> None:
    """Server-wide task capability is not evidence that every tool is asynchronous."""
    upstream = Tool(
        name="read",
        description="Read domain data.",
        input_schema={"type": "object", "properties": {}},
        execution=ToolExecution(task_support=support) if support else None,
    )
    assert (
        _make_dspy_tool("read", upstream, lambda _name, _args: "unused").desc
        == upstream.description
    )


def test_blueprint_mcp_task_capability_reaches_model_description() -> None:
    """Cached blueprint tools receive the same capability-derived description."""
    app = SimpleNamespace(
        state=SimpleNamespace(
            external_mcp_servers={
                "domain": {
                    "status": "ready",
                    "tools": [
                        {
                            "name": "domain_work",
                            "enabled": True,
                            "status": "ready",
                            "description": "Domain work.",
                            "input_schema": {},
                            "execution": {"taskSupport": "required"},
                        }
                    ],
                }
            }
        )
    )
    tools = _enabled_external_mcp_dspy_tools(app, ["domain_work"], {})
    assert "durable task handle" in tools["domain_work"].desc
    assert "MCP task" in tools["domain_work"].desc


def test_download_declaration_explains_collection_and_wake() -> None:
    """Downloads return a task receipt, not downloaded bytes or completion."""
    text = build_connected_data_download_tool().desc
    assert "task handle" in text and "Download" in text
    assert "observe_tasks" in text and "wait_tasks" in text and "cancel_tasks" in text
    assert "wake" in text


def test_skill_task_declaration_uses_shared_controls() -> None:
    """Skill delegation exposes a Subagent handle and the universal lifecycle."""
    tool = build_spawn_skill_task_tool(AgentDef(id="main", title="Main"), SkillRuntime())
    assert "Subagent task handle" in tool.desc and "wait_tasks" in tool.desc
    assert "observe_tasks" in tool.desc and "wake" in tool.desc
    assert "wait_agent_tasks" not in tool.desc


def test_malformed_mcp_capability_does_not_promise_a_task() -> None:
    """Malformed cached capability metadata cannot label a plain tool asynchronous."""
    assert mcp_task_description({"name": "read", "execution": {"taskSupport": []}}) == "read"
