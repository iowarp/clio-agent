"""MRTR loop (#1114): InputRequiredResult -> retry with inputResponses.

The modern-era (2026-07-28) single mechanism for server-initiated input (SEP-2577
replaced the direct elicitation/sampling/roots back-channel with the Multi-Round-Trip
Request loop). The mcp 2.0 SDK drives it (``mcp.client._input_required.run_input_required_driver``)
and fastmcp's ``Client.call_tool`` invokes that driver, dispatching each embedded input
request through the SAME callback table CLIO wired in P1.3 — so an elicit-flavored MRTR
input reaches the ONE HITL surface automatically. This suite pins CLIO's contributions:
the config-resolved round bound and the typed exhaustion degrade.

SDK shape (cited): ``InputRequiredResult.result_type == "input_required"`` with
``input_requests: dict[str, CreateMessageRequest|ListRootsRequest|ElicitRequest] | None``
and an opaque ``request_state``; the retried ``tools/call`` carries
``input_responses: dict[str, InputResponse] | None`` and echoes ``request_state`` verbatim
(``mcp_types._v2026_07_28``). The driver is bounded by ``input_required_max_rounds``
(``DEFAULT_INPUT_REQUIRED_MAX_ROUNDS = 10``) and raises ``InputRequiredRoundsExceededError``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp_types
import pytest
from fastmcp import Context, FastMCP

from clio_agent.tools.mcp_executor import AsyncMCPToolExecutor
from clio_agent.tools.mcp_runtime import (
    MCPClientCapabilities,
    MCPClientHandlers,
    make_mcp_client,
)


def _elicit_input_request(message: str = "more?") -> Any:
    return mcp_types.ElicitRequest(
        params=mcp_types.ElicitRequestFormParams(
            message=message,
            requested_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )
    )


def _never_terminating_backend() -> FastMCP:
    backend = FastMCP("mrtr-loop")

    @backend.tool
    async def never_done(ctx: Context) -> str:
        # Always ask again -> the client driver never reaches a terminal result.
        return mcp_types.InputRequiredResult(
            inputRequests={"q": _elicit_input_request()},
            requestState="s",
            resultType="input_required",
        )

    return backend


async def _auto_accept(context, message, response_type, params, request_context) -> Any:
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="accept", content={"value": "x"})


def _auto_accept_factory(target: Any) -> Any:
    return make_mcp_client(
        target,
        handlers=MCPClientHandlers(elicitation=_auto_accept),
        capabilities=MCPClientCapabilities(elicitation_form=True),
    )


def _one_round_backend(seen: list[Any]) -> FastMCP:
    """Asks once (InputRequiredResult), then completes — recording each round."""

    backend = FastMCP("mrtr-one-round")

    @backend.tool
    async def pick(ctx: Context) -> str:
        responses = ctx.input_responses
        seen.append((responses, ctx.request_state))
        if responses is None:
            return mcp_types.InputRequiredResult(
                inputRequests={"q1": _elicit_input_request("Pick a value")},
                requestState="state-token-1",
                resultType="input_required",
            )
        answer = responses.get("q1") if isinstance(responses, dict) else None
        return f"done value={getattr(answer, 'content', None)} state={ctx.request_state}"

    return backend


def test_mrtr_retry_carries_input_responses_and_echoes_request_state() -> None:
    """The headline MRTR loop: InputRequiredResult -> gather input -> RETRY the same call
    with ``inputResponses`` and the VERBATIM ``requestState`` -> terminal result (#1114)."""

    seen: list[Any] = []
    executor = AsyncMCPToolExecutor(_one_round_backend(seen), client_factory=_auto_accept_factory)

    async def _run() -> str:
        async with executor:
            return await executor.call_tool("pick", {})

    out = asyncio.run(_run())

    # Two server rounds: the ask, then the retry carrying the gathered input.
    assert len(seen) == 2, seen
    first_responses, first_state = seen[0]
    assert first_responses is None and first_state is None  # the original call
    retry_responses, retry_state = seen[1]
    assert retry_state == "state-token-1"  # requestState echoed VERBATIM
    assert set(retry_responses) == {"q1"}  # keyed by the server's request id
    assert retry_responses["q1"].action == "accept"
    assert retry_responses["q1"].content == {"value": "x"}
    assert "state=state-token-1" in out


def test_mrtr_decline_ends_the_loop_without_hanging() -> None:
    """A declined MRTR input is delivered to the server verbatim and the loop ends.

    SDK semantics (``run_input_required_driver``): a declined ``ElicitResult`` is a
    RESPONSE, not an error — it rides the retry so the server learns the user declined
    and returns its terminal result. Only an ``ErrorData`` dispatch aborts the loop.
    So: exactly one retry, a terminal result, no hang and no unbounded looping.
    """

    seen: list[Any] = []

    async def _decline(context, message, response_type, params, request_context) -> Any:
        from fastmcp.client.elicitation import ElicitResult

        return ElicitResult(action="decline", content=None)

    def _factory(target: Any) -> Any:
        return make_mcp_client(
            target,
            handlers=MCPClientHandlers(elicitation=_decline),
            capabilities=MCPClientCapabilities(elicitation_form=True),
        )

    executor = AsyncMCPToolExecutor(_one_round_backend(seen), client_factory=_factory)

    async def _run() -> str:
        async with executor:
            return await executor.call_tool("pick", {})

    out = asyncio.run(_run())

    assert len(seen) == 2  # ask + ONE retry carrying the decline; no further rounds
    retry_responses, _state = seen[1]
    assert retry_responses["q1"].action == "decline"
    assert "done" in out  # the server's terminal result, not a hang


def test_result_classification_does_not_misfire_on_handled_input_round() -> None:
    """#1112 classification must not emit a degrade for a properly-handled MRTR round.

    The driver resolves ``InputRequiredResult`` internally, so the result that reaches
    the telemetry boundary is an ordinary terminal ``CallToolResult`` — no explicit
    non-complete ``resultType``, therefore no ``degrade`` block."""

    from clio_agent.tools.mcp_results import call_tool_result_to_observer, classify_call_tool_result

    seen: list[Any] = []
    executor = AsyncMCPToolExecutor(_one_round_backend(seen), client_factory=_auto_accept_factory)

    async def _run() -> Any:
        async with executor:
            return await executor.call_tool_result("pick", {})

    outcome = asyncio.run(_run())

    classification = classify_call_tool_result(outcome.raw_result)
    assert classification.result_type == "complete"
    assert classification.degrade_reason is None
    assert "degrade" not in call_tool_result_to_observer(outcome.raw_result)


def test_mrtr_exhaustion_raises_typed_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A never-terminating MRTR server hits the config-resolved round bound and the
    executor surfaces the TYPED degrade (not a raw SDK exception) — #1114 acceptance."""

    import clio_agent.conf as conf
    from clio_agent.errors import (
        MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED,
        MCPInputRequiredRoundsExceededError,
    )

    monkeypatch.setenv("CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS", "2")
    conf.reload()
    try:
        executor = AsyncMCPToolExecutor(
            _never_terminating_backend(), client_factory=_auto_accept_factory
        )

        async def _run() -> None:
            async with executor:
                await executor.call_tool("never_done", {})

        with pytest.raises(MCPInputRequiredRoundsExceededError) as excinfo:
            asyncio.run(_run())
        assert excinfo.value.reason == MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED
        assert excinfo.value.max_rounds == 2
    finally:
        conf.reload()


def test_mrtr_round_bound_reason_is_advertised() -> None:
    """The exhaustion reason is a closed-set entry in the x_clio_stream_fallback_reasons
    capability catalog (#1114: typed, advertised)."""

    from clio_agent.errors import MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED
    from clio_agent.gact.runtime.capabilities import _STREAM_FALLBACK_REASON_DEFINITIONS

    assert MCP_INPUT_REQUIRED_ROUNDS_EXCEEDED in _STREAM_FALLBACK_REASON_DEFINITIONS
