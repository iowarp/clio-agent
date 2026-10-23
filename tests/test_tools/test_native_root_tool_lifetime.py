"""A native tool body lives as long as its application, not as long as the process.

FastMCP, Docket and uncalled_for memoize per tool function (uncalled_for without a
bound). Native tool bodies close over their application, so when the bodies themselves
were registered, every application a process built stayed reachable after shutdown: a
test worker held hundreds of them and was OOM-killed at 3-5 GB.

SABOTAGE: register the body with ``root.server.tool(...)`` instead of ``root.tool(...)``
-> the owner below stays reachable through uncalled_for's signature cache.
"""

from __future__ import annotations

import gc
import weakref
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP

from clio_agent.tools.native_roots import build_native_roots


class _Owner:
    """Stands in for the application a native tool body closes over."""

    label = "owner"


def _register(server_tool: Any, owner: _Owner) -> Any:
    @server_tool(name="probe", description="Echo with the owner's label.")
    async def probe(ctx: Context, text: str, repeat: int = 1) -> dict[str, Any]:
        """Echo ``text`` with the owner's label."""
        return {"echo": f"{owner.label}:{text * repeat}"}

    return probe


async def _call_and_list(server: FastMCP) -> tuple[dict[str, Any], dict[str, Any]]:
    async with Client(server) as client:
        listed = {tool.name: tool.inputSchema for tool in await client.list_tools()}
        result = await client.call_tool("probe", {"text": "hi", "repeat": 2})
    return listed["probe"], dict(result.structured_content or {})


@pytest.mark.asyncio
async def test_a_released_root_releases_what_its_tool_bodies_close_over() -> None:
    owner = _Owner()
    alive = weakref.ref(owner)
    roots = build_native_roots()
    root = roots["shell"]
    _register(root.tool, owner)

    schema, result = await _call_and_list(root.server)

    assert result == {"echo": "owner:hihi"}
    del owner, root, roots
    gc.collect()
    assert alive() is None, "a library cache kept the native tool body's owner alive"

    direct = FastMCP("direct")
    _register(direct.tool, _Owner())
    direct_schema, _ = await _call_and_list(direct)
    assert schema == direct_schema, "the forwarder must expose the body's own signature"
