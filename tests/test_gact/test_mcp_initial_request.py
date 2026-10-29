"""Real MCP listings reach the first model request, without a provider call."""

from __future__ import annotations

from typing import Any

import dspy
import pytest
from fastmcp import FastMCP

from clio_agent.gact.agents.builders import _gateway_and_namespace_tools
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.tools import mcp_prepare
from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.mcp_config import MCPServerSpec
from tests._scripted_engine import calls, scripted_lm


@pytest.mark.usefixtures("clio_core_plane")
def test_real_mcp_schema_and_call_are_available_in_the_initial_request() -> None:
    """Exercise tools/list, schema conversion, the model boundary and tool execution."""
    server = FastMCP("initial-request-fixture")
    invocations: list[str] = []

    @server.tool(annotations={"readOnlyHint": True})
    def find_station(station: str) -> dict[str, str]:
        """Find a station by its identifier."""
        invocations.append(station)
        return {"station": station, "status": "found"}

    lm, engine = scripted_lm(
        [calls(("survey_find_station", {"station": "TEST"})), calls(("submit", {"answer": "done"}))]
    )
    # In-memory MCP transport uses the production executor and protocol; no launcher,
    # network service, saved connection or live model account is required by this test.
    with create_sync_tool_executor(
        FastMCP("initial-request-gateway"),
        preloaded_tools={},
        namespace_servers={"survey": server},
    ) as executor:
        executor._clio_namespace_specs = {
            "survey": MCPServerSpec(name="survey", transport="http", url="http://fixture.test/mcp")
        }
        try:
            tools, failures = _gateway_and_namespace_tools(executor, [], ["survey"])
            assert failures == {}
            with dspy.context(lm=lm):
                agent = ClioReAct("question -> answer", tools=list(tools.values()), max_iters=3)
                result = agent(question="Find TEST")
        finally:
            mcp_prepare.forget("survey")

    first = {tool.name: tool for tool in engine.requests[0].tools}
    assert set(first) == {"survey_find_station", "submit"}
    schema: Any = first["survey_find_station"].parameters
    assert schema["properties"]["station"]["type"] == "string"
    assert "station" in schema["required"]
    assert "Find a station" in first["survey_find_station"].description
    assert invocations == ["TEST"]
    assert result.answer == "done"
