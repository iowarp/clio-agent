"""MCP 2026-07-28 result tolerance and typed refusal errors (#1112)."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from mcp.shared.exceptions import MCPError

from clio_agent.tools.execution import SyncMCPToolExecutor
from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_results import call_tool_result_to_observer


class _ResultClient:
    """Minimal async client returning one configured MCP result or error."""

    def __init__(self, *, result: Any = None, error: MCPError | None = None) -> None:
        self.result = result
        self.error = error

    async def __aenter__(self) -> "_ResultClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return [SimpleNamespace(name="probe", input_schema={"properties": {}})]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        del name, arguments
        if self.error is not None:
            raise self.error
        return self.result

    async def read_resource(self, uri: str) -> Any:
        raise AssertionError(f"unexpected resource read: {uri}")


@pytest.mark.asyncio
async def test_result_type_absent_dict_resolves_complete_without_degrade() -> None:
    """An absent resultType is spec-normal completeness, not a downgrade."""

    legacy_envelope = {
        "content": [{"type": "text", "text": "legacy result"}],
        "structuredContent": {"value": 42},
    }
    client = _ResultClient(result=legacy_envelope)
    async with AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
    ) as executor:
        outcome = await executor.call_tool_result("probe", {})

    observed = call_tool_result_to_observer(outcome.raw_result)
    assert "resultType" not in observed
    assert "degrade" not in observed


def test_result_type_default_absent_from_pydantic_fields_set_has_no_degrade() -> None:
    """A defaulted Pydantic resultType absent from fields_set remains spec-normal."""

    result = SimpleNamespace(
        content=[],
        structured_content={"value": 42},
        is_error=False,
        result_type="complete",
        model_fields_set={"content", "structured_content", "is_error"},
    )

    observed = call_tool_result_to_observer(result)

    assert "resultType" not in observed
    assert "degrade" not in observed


def test_explicit_supported_result_type_passes_through_without_degrade() -> None:
    """An explicit supported resultType is preserved without a degrade reason."""

    observed = call_tool_result_to_observer(
        {"content": [], "structuredContent": {"value": 42}, "resultType": "complete"}
    )

    assert observed["resultType"] == "complete"
    assert "degrade" not in observed


@pytest.mark.asyncio
async def test_explicit_unsupported_result_type_records_downgrade() -> None:
    """An explicit unsupported resultType is coerced to complete and recorded."""

    envelope = {
        "content": [{"type": "text", "text": "task result"}],
        "structuredContent": {"taskId": "task-1"},
        "resultType": "task",
    }
    client = _ResultClient(result=envelope)
    async with AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
    ) as executor:
        outcome = await executor.call_tool_result("probe", {})

    observed = call_tool_result_to_observer(outcome.raw_result)
    assert observed["resultType"] == "complete"
    assert observed["degrade"] == {
        "reason": "mcp_result_downgraded_to_complete",
        "resultType": "complete",
        "originalResultType": "task",
    }


def test_real_fastmcp_round_trip_has_no_spurious_degrade() -> None:
    """A healthy real FastMCP CallToolResult carries no synthetic degrade stamp."""

    server = FastMCP("result-tolerance-probe")

    @server.tool
    def probe() -> dict[str, int]:
        """Return a healthy structured result."""

        return {"value": 42}

    observed: list[tuple[str, Mapping[str, Any], str | None, str | None, Any | None]] = []

    def observer(
        name: str,
        args: Mapping[str, Any],
        phase: str | None,
        error: str | None,
        result: Any | None = None,
    ) -> None:
        observed.append((name, args, phase, error, result))

    with SyncMCPToolExecutor(server, timeout=1.0, tool_observer=observer) as executor:
        assert executor.call_tool("probe", {}) == '{"value": 42}'

    completed = [row for row in observed if row[2] == "completed"]
    assert len(completed) == 1
    payload = completed[0][4]
    assert isinstance(payload, dict)
    assert "degrade" not in payload
    assert "resultType" not in payload


@pytest.mark.asyncio
async def test_missing_required_client_capability_is_typed() -> None:
    """JSON-RPC -32021 maps by numeric code to the capability-refused type."""

    from clio_agent.errors import MCPMissingRequiredClientCapabilityError, ToolError

    client = _ResultClient(
        error=MCPError(-32021, "server-controlled wording", {"requiredCapability": "sampling"})
    )
    async with AsyncMCPToolExecutor(
        object(), timeout=1.0, client_factory=lambda _server: client
    ) as executor:
        with pytest.raises(MCPMissingRequiredClientCapabilityError) as caught:
            await executor.call_tool("probe", {})

    error = caught.value
    assert isinstance(error, ToolError)
    assert error.code == -32021
    assert error.reason == "mcp_capability_refused"
    assert error.protocol_data == {"requiredCapability": "sampling"}
    assert error.error_type == "mcp_missing_required_client_capability"
    assert error.to_dict()["details"] == {
        "reason": "mcp_capability_refused",
        "json_rpc_code": -32021,
        "protocol_data": {"requiredCapability": "sampling"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["enter", "list_tools"])
async def test_connect_time_unsupported_protocol_version_is_typed(
    failure_phase: str,
) -> None:
    """JSON-RPC -32022 maps during client entry and initial tool discovery."""

    from clio_agent.errors import MCPUnsupportedProtocolVersionError

    protocol_error = MCPError(
        -32022,
        "server-controlled wording",
        {"requestedVersion": "2026-07-28"},
    )

    class _ConnectErrorClient(_ResultClient):
        async def __aenter__(self) -> "_ConnectErrorClient":
            if failure_phase == "enter":
                raise protocol_error
            return self

        async def list_tools(self) -> list[Any]:
            raise protocol_error

    client = _ConnectErrorClient()
    executor = AsyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
    )
    with pytest.raises(MCPUnsupportedProtocolVersionError) as caught:
        await executor.start()

    error = caught.value
    assert error.code == -32022
    assert error.reason == "mcp_protocol_refused"
    assert error.protocol_data == {"requestedVersion": "2026-07-28"}
    assert error.error_type == "mcp_unsupported_protocol_version"
    assert error.to_dict()["details"] == {
        "reason": "mcp_protocol_refused",
        "json_rpc_code": -32022,
        "protocol_data": {"requestedVersion": "2026-07-28"},
    }


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (-32021, "mcp_capability_refused"),
        (-32022, "mcp_protocol_refused"),
    ],
)
def test_typed_refusal_reaches_structured_tool_trace(code: int, reason: str) -> None:
    """The sync bridge records typed refusal fields in its ordinary observer path."""

    observed: list[tuple[str | None, str | None, Any | None]] = []

    def observer(
        name: str,
        args: Mapping[str, Any],
        phase: str | None,
        error: str | None,
        result: Any | None = None,
    ) -> None:
        del name, args
        observed.append((phase, error, result))

    from clio_agent.errors import MCPProtocolError

    client = _ResultClient(error=MCPError(code, "server-controlled wording", {"probe": True}))
    executor = SyncMCPToolExecutor(
        object(),
        timeout=1.0,
        client_factory=lambda _server: client,
        tool_observer=observer,
    )
    try:
        with pytest.raises(MCPProtocolError) as caught:
            executor.call_tool("probe", {})
    finally:
        executor.close()

    assert caught.value.reason == reason
    completed = [row for row in observed if row[0] == "completed"]
    assert len(completed) == 1
    trace_result = completed[0][2]
    assert isinstance(trace_result, dict)
    assert trace_result["error"]["details"] == {
        "reason": reason,
        "json_rpc_code": code,
        "protocol_data": {"probe": True},
    }
