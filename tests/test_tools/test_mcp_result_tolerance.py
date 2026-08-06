"""MCP 2026-07-28 result tolerance and typed refusal errors (#1112)."""

from __future__ import annotations

import json
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
    """An explicit unsupported resultType is coerced to complete and recorded.

    ``task`` is deliberately NOT the example any more: #1115 made this a tasks
    client, so ``resultType: "task"`` is a shape it drives (see
    ``tests/test_tools/test_mcp_tasks.py::test_task_result_type_is_handled_not_downgraded``).
    An unknown extension result type still degrades.
    """

    envelope = {
        "content": [{"type": "text", "text": "streamed result"}],
        "structuredContent": {"streamId": "stream-1"},
        "resultType": "streaming",
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
        "originalResultType": "streaming",
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


def test_list_result_round_trip_is_json_not_python_repr() -> None:
    """A list-returning tool's model-facing text is JSON, never Python repr.

    FastMCP wraps a bare-list return in ``{"result": [...]}`` structuredContent
    (``x-fastmcp-wrap-result``); the client's ``CallToolResult.data`` unwraps that
    back to a plain ``list``. ``_result_to_text`` special-cased ``dict`` and fell
    through to ``str(data)`` for everything else, and Python's ``str()`` on a
    list of dicts renders single-quoted repr syntax (``ast.literal_eval``-only,
    not valid JSON) -- observed live as ``geo_geocode`` results rendering as
    repr text in the UI instead of structured JSON.
    """

    server = FastMCP("list-result-probe")

    @server.tool
    def geocode() -> list[dict[str, object]]:
        """Return a list of geocoded results."""

        return [{"display_name": "Los Angeles, CA", "lat": 34.05, "lon": -118.24}]

    with SyncMCPToolExecutor(server, timeout=1.0) as executor:
        text = executor.call_tool("geocode", {})

    expected = [{"display_name": "Los Angeles, CA", "lat": 34.05, "lon": -118.24}]
    assert text == json.dumps(expected)
    # Explicit guard against the historical Python-repr regression: single-quoted
    # keys are never valid JSON and must never reach the model-facing text lane.
    assert "'display_name'" not in text
    assert json.loads(text) == expected


# --------------------------------------------------------------------------- #
# Finding E: ``_result_to_text``'s last-resort ``str()`` fallback must widen   #
# its except tuple past bare ``TypeError`` (a circular structure now raises   #
# ValueError, not TypeError) and must never emit invalid JSON (NaN/Infinity), #
# while logging a typed, structured reason instead of degrading silently.     #
# --------------------------------------------------------------------------- #


def test_result_to_text_circular_structure_falls_back_without_raising(caplog) -> None:
    """A circular structure must degrade to the str() fallback -- never raise.

    json.dumps's own cycle guard raises ValueError ("Circular reference
    detected"), which the OLD ``except TypeError`` alone never caught.
    """

    from clio_agent.tools.mcp_executor import (
        MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON,
        _result_to_text,
    )

    circular: dict[str, Any] = {"self": None}
    circular["self"] = circular

    with caplog.at_level("WARNING"):
        text = _result_to_text(circular)

    assert text == str(circular)
    assert MCP_RESULT_TO_TEXT_REPR_FALLBACK_REASON in caplog.text


def test_result_to_text_nan_falls_back_instead_of_emitting_invalid_json() -> None:
    """NaN/Infinity are non-standard JSON tokens; ``allow_nan=False`` routes
    them to the typed fallback instead of landing on the wire as JSON that
    isn't actually valid JSON everywhere else."""

    from clio_agent.tools.mcp_executor import _result_to_text

    payload = {"value": float("nan")}
    text = _result_to_text(payload)

    assert text == str(payload)
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_result_to_text_infinity_falls_back_instead_of_emitting_invalid_json() -> None:
    from clio_agent.tools.mcp_executor import _result_to_text

    payload = {"value": float("inf")}
    text = _result_to_text(payload)

    assert text == str(payload)
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)


def test_result_to_text_bytes_falls_back_without_raising() -> None:
    """``bytes`` has no JSON mapping -- TypeError, the ORIGINAL caught case,
    stays green."""

    from clio_agent.tools.mcp_executor import _result_to_text

    raw = b"\x00\x01raw"
    assert _result_to_text(raw) == str(raw)


def test_result_to_text_recursion_error_from_json_dumps_falls_back(monkeypatch) -> None:
    """RecursionError raised inside json.dumps (a pathologically deep,
    non-circular structure can exhaust the recursion limit before json's own
    cycle guard fires) must degrade like every other unencodable shape --
    proven via a controlled monkeypatch so the test itself doesn't depend on
    fragile, platform-specific stack-depth math."""

    import clio_agent.tools.mcp_executor as mcp_executor_module

    def _raise_recursion(*_args: Any, **_kwargs: Any) -> str:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(mcp_executor_module.json, "dumps", _raise_recursion)

    payload = {"deep": "structure"}
    assert mcp_executor_module._result_to_text(payload) == str(payload)


def test_result_to_text_overflow_error_from_json_dumps_falls_back(monkeypatch) -> None:
    """An int outside the encoder's range raises OverflowError -- also part of
    the widened except tuple."""

    import clio_agent.tools.mcp_executor as mcp_executor_module

    def _raise_overflow(*_args: Any, **_kwargs: Any) -> str:
        raise OverflowError("int too large to convert")

    monkeypatch.setattr(mcp_executor_module.json, "dumps", _raise_overflow)

    payload = {"huge": 10**400}
    assert mcp_executor_module._result_to_text(payload) == str(payload)


def test_result_to_text_still_prefers_json_for_encodable_values() -> None:
    """Regression guard: the widened except tuple must not change the happy
    path -- an ordinary encodable value still returns real JSON, not repr."""

    from clio_agent.tools.mcp_executor import _result_to_text

    assert _result_to_text([{"a": 1}, {"b": 2}]) == json.dumps([{"a": 1}, {"b": 2}])


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
