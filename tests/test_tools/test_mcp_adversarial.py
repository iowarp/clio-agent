"""Adversarial MUST-violation coverage (#1285, C1-S5 item 4).

Drives ``mcp_adversarial_fixture.py`` (a hand-rolled server that answers four
specific requests with deliberately protocol-violating JSON-RPC frames) over
a REAL HTTP transport (in-process ASGI, no socket) and asserts clio's typed
handling of each violation -- what the client DOES, not a guess about what it
should do. One finding here is a verified LIBRARY gap, not a clio gap (see
``test_pagination_empty_string_cursor_is_treated_as_terminal_fastmcp_bug``).
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError

from clio_agent.errors import MCPMissingRequiredClientCapabilityError
from clio_agent.tools.mcp_errors import typed_mcp_protocol_error
from clio_agent.tools.mcp_header_mismatch import call_tool_with_header_retry
from tests.test_tools.mcp_adversarial_fixture import (
    BAD_HEADER_MISMATCH_TOOL,
    BAD_MISSING_CAPS_TOOL,
    BAD_RESULT_TYPE_TOOL,
    PAGINATED_TOOL,
    PAGINATED_TOOL_2,
    adversarial_in_process_transport,
    build_adversarial_app,
    run_adversarial_lifespan,
)


@pytest.mark.asyncio
async def test_unrecognized_result_type_does_not_crash_the_client() -> None:
    """SEP-2322/obligations A2 says an unrecognized resultType should be
    "invalid" -- empirically the mcp SDK client does NOT distinguish it from
    "complete": the extra field is silently ignored and the call succeeds as a
    normal CallToolResult. This is the OBSERVED, current SDK behavior (not a
    clio decision -- clio never inspects resultType itself), pinned so a
    future SDK change that starts raising here is a deliberate, visible diff."""

    app = build_adversarial_app()
    async with run_adversarial_lifespan(app):
        transport = adversarial_in_process_transport(app)
        async with Client(transport) as client:
            result = await client.call_tool(BAD_RESULT_TYPE_TOOL, {"payload": "x"})

    assert result.is_error is False
    assert result.data == {"result": "bad-result-type"}
    assert result.content[0].text == "bad-result-type"


@pytest.mark.asyncio
async def test_missing_required_capabilities_data_never_crashes_typed_mapping() -> None:
    """A REAL wire -32021 whose data carries no requiredCapabilities key (a
    malformed-but-plausible server bug) must still map to CLIO's typed error
    without raising during construction (#1282 F13's defensive hint-building,
    proven here against an actual malformed wire response, not synthetic data
    passed straight to the constructor)."""

    app = build_adversarial_app()
    async with run_adversarial_lifespan(app):
        transport = adversarial_in_process_transport(app)
        async with Client(transport) as client:
            with pytest.raises(MCPError) as exc_info:
                await client.call_tool(BAD_MISSING_CAPS_TOOL, {"payload": "x"})

    assert exc_info.value.code == -32021
    typed = typed_mcp_protocol_error(exc_info.value)
    assert isinstance(typed, MCPMissingRequiredClientCapabilityError)
    # No actionable hint could be built (no requiredCapabilities key) -- the
    # message must still be the plain, non-crashing base message.
    assert "missing required client capability" in str(typed)


@pytest.mark.asyncio
async def test_header_mismatch_retry_is_bounded_against_a_hostile_always_broken_server() -> None:
    """A server that ALWAYS answers -32020 (never actually resolves the
    mismatch) must never turn call_tool_with_header_retry into an infinite
    loop: exactly one retry, then the typed failure propagates."""

    app = build_adversarial_app()
    async with run_adversarial_lifespan(app):
        transport = adversarial_in_process_transport(app)
        async with Client(transport) as client:
            with pytest.raises(MCPError) as exc_info:
                await call_tool_with_header_retry(client, BAD_HEADER_MISMATCH_TOOL, {"payload": "x"})

    assert exc_info.value.code == -32020


@pytest.mark.asyncio
async def test_pagination_empty_string_cursor_is_treated_as_terminal_fastmcp_bug() -> None:
    """Verified LIBRARY gap, not a clio gap: fastmcp's Client.list_tools()
    auto-pagination checks ``if not result.next_cursor: break``
    (fastmcp/client/mixins/tools.py) -- an EMPTY-STRING cursor is falsy in
    Python, so pagination stops one page early even though E10 says only
    null/missing ends it. clio never implements its own pagination (a
    repo-wide grep found zero list_page_size/pagination logic anywhere in
    clio_agent -- it always calls client.list_tools() and trusts the result),
    so there is no clio code to fix; this pins the OBSERVED behavior as a
    finding, not an endorsement."""

    app = build_adversarial_app()
    async with run_adversarial_lifespan(app):
        transport = adversarial_in_process_transport(app)
        async with Client(transport) as client:
            tools = await client.list_tools()

    names = {t.name for t in tools}
    assert names == {PAGINATED_TOOL}, (
        "if this ever becomes {PAGINATED_TOOL, PAGINATED_TOOL_2}, fastmcp fixed "
        "the empty-string-cursor bug -- update this test's expectation, don't delete it"
    )
    assert PAGINATED_TOOL_2 not in names
