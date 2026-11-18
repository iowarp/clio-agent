"""A search question stays answerable while the person decides (KiCad ledger KN9).

The MCP SDK seals each input-required round with a ten-minute expiry by default. A person
who answered later got "Invalid or expired requestState", the search failed and the same
question came back. CLIO's search servers seal their rounds for a week.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import mcp_types
import pytest
from fastmcp import Client, Context, FastMCP
from mcp.server import request_state
from mcp.shared.exceptions import MCPError

from clio_agent.tools.servers.literature_server import literature_server
from clio_agent.tools.servers.search_questions import PERSON_ROUND_TTL_S, person_round_security
from clio_agent.tools.servers.web_server import web_server

ELEVEN_MINUTES = 11 * 60


def _server(security: Any) -> FastMCP:
    server = FastMCP("rounds", request_state_security=security)

    @server.tool()
    async def ask(ctx: Context) -> Any:
        if ctx.input_responses:
            answer = ctx.input_responses.get("q")
            return f"answered {getattr(answer, 'content', {}).get('choice')}"
        schema = {"type": "object", "properties": {"choice": {"type": "string"}}}
        request = mcp_types.ElicitRequest(
            params=mcp_types.ElicitRequestFormParams(message="Allow?", requested_schema=schema)
        )
        return mcp_types.InputRequiredResult(
            input_requests={"q": request},
            request_state=json.dumps({"topic": "approve:x"}),
            result_type="input_required",
        )

    return server


def _answer_late(monkeypatch: pytest.MonkeyPatch) -> Any:
    real = request_state.time.time

    async def handler(message: str, response_type: Any, params: Any, context: Any) -> Any:
        # Only the request-state module's clock moves on: the person answered 11 min later.
        later = SimpleNamespace(time=lambda: real() + ELEVEN_MINUTES)
        monkeypatch.setattr(request_state, "time", later)
        return {"choice": "approve"}

    return handler


async def _call(server: FastMCP, handler: Any) -> str:
    async with Client(server, elicitation_handler=handler) as client:
        result = await client.call_tool("ask", {}, raise_on_error=False)
    return " ".join(getattr(block, "text", "") for block in result.content)


def test_the_sdk_default_loses_an_answer_given_after_ten_minutes(monkeypatch) -> None:
    with pytest.raises(MCPError, match="Invalid or expired requestState"):
        asyncio.run(_call(_server(None), _answer_late(monkeypatch)))


def test_a_search_round_takes_an_answer_given_after_ten_minutes(monkeypatch) -> None:
    text = asyncio.run(_call(_server(person_round_security()), _answer_late(monkeypatch)))
    assert text == "answered approve"


def test_both_search_servers_seal_rounds_for_a_week() -> None:
    assert PERSON_ROUND_TTL_S >= 7 * 24 * 3600
    for server in (web_server, literature_server):
        assert server._request_state_security.ttl == PERSON_ROUND_TTL_S
