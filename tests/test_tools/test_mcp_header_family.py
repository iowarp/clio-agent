"""x-mcp-header family: Mcp-Param-* mirroring, -32020 re-list-and-retry, invalid-header
tool dropping (#1285, C1-S5 item 1).

Mcp-Method/Mcp-Protocol-Version/Mcp-Name/Mcp-Param-* emission and invalid-tool dropping
are ALL already implemented by the mcp SDK's ``ClientSession`` (``_make_modern_stamp``,
``_absorb_tool_listing``) -- verified by reading ``mcp/client/session.py`` directly, not
duplicated here. What CLIO owns: (1) the exerciser gaining tools that actually EXERCISE
those SDK mechanisms (none existed before this slice -- no tool anywhere declared an
``x-mcp-header``-annotated param, and none was deliberately invalid), and (2) the one
retryable failure mode (-32020 HeaderMismatch) the SDK does NOT auto-recover from.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import find_invalid_x_mcp_header, mcp_param_headers, x_mcp_header_map
from mcp_types.jsonrpc import HEADER_MISMATCH

from clio_agent.tools.mcp_header_mismatch import (
    call_tool_with_header_retry,
    trace_dropped_x_mcp_header_tools,
)
from tests.test_tools.mcp_exerciser import (
    HEADER_ANNOTATED_TOOL_NAME,
    INVALID_HEADER_TOOL_NAME,
    build_exerciser_server,
)

# --------------------------------------------------------------------------- #
# (a) Mcp-Param-* mirroring: OUR tool's schema actually maps the way the SDK  #
# expects (a real, ANNOTATED param existing at all was the C1-S5 gap).       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_header_annotated_tool_schema_maps_to_mcp_param_header() -> None:
    async with Client(build_exerciser_server()) as client:
        tools = await client.list_tools()
    tool = next(t for t in tools if t.name == HEADER_ANNOTATED_TOOL_NAME)
    header_map = x_mcp_header_map(tool.input_schema)
    assert header_map, "the exerciser tool must declare at least one x-mcp-header property"
    headers = mcp_param_headers(header_map, {"trace_id": "abc-123", "payload": "hi"})
    assert headers == {"Mcp-Param-Trace-Id": "abc-123"}


# --------------------------------------------------------------------------- #
# (c) invalid-header-value tool dropping: the SDK's own client.list_tools()  #
# absorption drops a tool whose x-mcp-header annotation is invalid.          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_header_tool_dropped_from_listing() -> None:
    async with Client(build_exerciser_server()) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert INVALID_HEADER_TOOL_NAME not in names, (
        "a tool whose x-mcp-header annotation fails find_invalid_x_mcp_header "
        "must never reach the resolved catalog"
    )


@pytest.mark.asyncio
async def test_invalid_header_tool_schema_is_actually_invalid() -> None:
    """Regression guard: prove the fixture tool really IS invalid (a 'number' type
    x-mcp-header is forbidden by SEP-2578), so the drop test above is not vacuous."""

    server = build_exerciser_server()
    tool = await server.get_tool(INVALID_HEADER_TOOL_NAME)
    assert tool is not None
    reason = find_invalid_x_mcp_header(tool.parameters)
    assert reason is not None
    assert "number" in reason or "type" in reason


# --------------------------------------------------------------------------- #
# (b) -32020 HeaderMismatch -> re-list-and-retry                             #
# --------------------------------------------------------------------------- #


class _FakeClient:
    """Minimal call_tool/list_tools double recording call order."""

    def __init__(self, *, fail_times: int) -> None:
        self.fail_times = fail_times
        self.call_count = 0
        self.list_count = 0

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> str:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise MCPError(HEADER_MISMATCH, "header mismatch")
        return "ok"

    async def list_tools(self) -> list[Any]:
        self.list_count += 1
        return []


@pytest.mark.asyncio
async def test_header_mismatch_relists_once_and_retries() -> None:
    client = _FakeClient(fail_times=1)
    result = await call_tool_with_header_retry(client, "some_tool", {"x": 1})
    assert result == "ok"
    assert client.call_count == 2
    assert client.list_count == 1


@pytest.mark.asyncio
async def test_header_mismatch_twice_propagates_never_loops() -> None:
    client = _FakeClient(fail_times=2)
    with pytest.raises(MCPError) as exc_info:
        await call_tool_with_header_retry(client, "some_tool", {"x": 1})
    assert exc_info.value.code == HEADER_MISMATCH
    assert client.call_count == 2, "exactly one retry, never a loop"
    assert client.list_count == 1


@pytest.mark.asyncio
async def test_non_header_mismatch_error_propagates_without_relist() -> None:
    class _OtherErrorClient(_FakeClient):
        async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> str:
            self.call_count += 1
            raise MCPError(-32021, "missing required client capability")

    client = _OtherErrorClient(fail_times=0)
    with pytest.raises(MCPError) as exc_info:
        await call_tool_with_header_retry(client, "some_tool", {"x": 1})
    assert exc_info.value.code == -32021
    assert client.list_count == 0, "only HeaderMismatch triggers a re-list"


@pytest.mark.asyncio
async def test_success_never_relists() -> None:
    client = _FakeClient(fail_times=0)
    result = await call_tool_with_header_retry(client, "some_tool", {"x": 1})
    assert result == "ok"
    assert client.call_count == 1
    assert client.list_count == 0


# --------------------------------------------------------------------------- #
# trace_dropped_x_mcp_header_tools (#1285 review round, SHOULD 3): the SDK's  #
# own drop of INVALID_HEADER_TOOL_NAME is library-side-log only -- this must  #
# reach CLIO's OWN typed trace, and the tool must still be absent from the    #
# listing served.                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dropped_invalid_header_tool_yields_a_typed_reason_and_stays_absent(
    monkeypatch,
) -> None:
    import clio_agent.tools.mcp_header_mismatch as header_mismatch_mod

    events: list[tuple[Any, ...]] = []
    orig_event = header_mismatch_mod.trace.event

    def _spy(tag: str, fmt: str, *args: Any) -> None:
        events.append((tag, fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(header_mismatch_mod.trace, "event", _spy)

    server = build_exerciser_server()
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {t.name for t in listed}
        assert INVALID_HEADER_TOOL_NAME not in names, (
            "regression guard: the SDK must still drop the invalid tool "
            "before this diagnostic even runs"
        )

        await trace_dropped_x_mcp_header_tools(client, "v2ex", listed)

    drop_events = [e for e in events if e[1].startswith("mcp_x_mcp_header_dropped")]
    assert len(drop_events) == 1, f"expected exactly one drop event, got {events!r}"
    _tag, fmt, namespace, tool, reason = drop_events[0]
    assert namespace == "v2ex"
    assert tool == INVALID_HEADER_TOOL_NAME
    assert "number" in reason or "type" in reason


@pytest.mark.asyncio
async def test_no_drop_events_when_every_tool_is_valid(monkeypatch) -> None:
    """Regression guard: the diagnostic must not fire spuriously.

    Uses a fresh, minimal server with no ``x-mcp-header`` annotation at
    all (the real exerciser always carries the deliberately-invalid tool,
    so it cannot exercise the zero-drops case)."""

    from fastmcp import FastMCP

    import clio_agent.tools.mcp_header_mismatch as header_mismatch_mod

    server = FastMCP("clean")

    @server.tool
    async def plain(payload: str) -> str:
        return f"echo:{payload}"

    events: list[tuple[Any, ...]] = []
    orig_event = header_mismatch_mod.trace.event

    def _spy(tag: str, fmt: str, *args: Any) -> None:
        events.append((tag, fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(header_mismatch_mod.trace, "event", _spy)

    async with Client(server) as client:
        listed = await client.list_tools()
        await trace_dropped_x_mcp_header_tools(client, "clean", listed)

    assert not [e for e in events if e[1].startswith("mcp_x_mcp_header_dropped")]
