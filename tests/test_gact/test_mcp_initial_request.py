"""Real MCP listings reach the first model request, without a provider call."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import dspy
import pytest
import uvicorn
from fastmcp import FastMCP
from mcp.shared.auth import OAuthToken

from clio_agent.gact import mcp_auth
from clio_agent.gact.agents.builders import _gateway_and_namespace_tools
from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.tools import mcp_prepare
from clio_agent.tools.execution import create_sync_tool_executor
from clio_agent.tools.gateway import build_gateway, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec, spec_from_declaration
from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage
from tests._scripted_engine import calls, scripted_lm
from tests.fixtures.oauth_mcp_server import ShowcaseOAuth


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


def test_initial_http_listing_honors_saved_sign_in_and_account_isolation(tmp_path: Path) -> None:
    """Use real HTTP and OAuth storage; discovery never initiates browser sign-in."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        authorization = ShowcaseOAuth(base)
        remote = FastMCP("signed-in-initial-tools", auth=authorization)

        @remote.tool(annotations={"readOnlyHint": True})
        def find_station(station: str) -> dict[str, str]:
            """Find a station through the signed-in fixture account."""
            return {"station": station}

        receipt = tmp_path / "http-owner.json"
        receipt.write_text(
            json.dumps({"owner_pid": os.getpid(), "port": port, "state": "starting"})
        )
        server = uvicorn.Server(uvicorn.Config(remote.http_app(), log_level="critical"))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            token_path = tmp_path / "tokens.json"
            first = DurableFileTokenStorage(
                f"{base}/mcp", path=token_path, connection_id="first-account"
            )
            asyncio.run(
                first.set_tokens(
                    OAuthToken(
                        access_token="qualification-header-token",
                        token_type="Bearer",
                        expires_in=3600,
                    )
                )
            )
            second = DurableFileTokenStorage(
                f"{base}/mcp", path=token_path, connection_id="second-account"
            )

            for storage, account, expected in (
                (first, "first-account", {"survey_find_station"}),
                (second, "second-account", set()),
            ):
                auth = mcp_auth.managed_oauth(connection_id=account)
                auth["storage"] = storage
                spec = spec_from_declaration("survey", {"url": f"{base}/mcp", "auth": auth})
                gateway = build_gateway({"survey": spec})
                with create_sync_tool_executor(
                    gateway,
                    preloaded_tools={},
                    namespace_servers=namespace_proxies(gateway),
                ) as executor:
                    executor._clio_namespace_specs = {"survey": spec}
                    tools, failures = _gateway_and_namespace_tools(executor, [], ["survey"])
                    assert set(tools) == expected
                    if expected:
                        assert failures == {}
                        assert "TEST" in str(
                            executor.call_tool("survey_find_station", {"station": "TEST"})
                        )
                    else:
                        assert failures == {"survey": mcp_prepare.START_FAILED}
                mcp_prepare.forget("survey")
            # The first account's cached definitions must not authorize another one.
            # A background conversation opening also must not initiate registration.
            assert authorization.counts["registered"] == 0
            assert authorization.counts["approved"] == 0
            first.clear()
            assert not first.has_tokens()
        finally:
            mcp_prepare.forget("survey")
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "owned HTTP fixture did not retire"
            receipt.write_text(
                json.dumps({"owner_pid": os.getpid(), "port": port, "state": "retired"})
            )
