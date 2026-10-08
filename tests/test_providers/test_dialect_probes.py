"""Unit tests for the active deployment probes (model-capabilities brief 5.3).

Each probe takes an injected async ``send(body) -> ProbeResponse`` seam, so
these tests drive it with a small in-memory fake rather than any real network
call -- no server, local or remote, is touched.
"""

from __future__ import annotations

import pytest

from clio_agent.providers.capabilities.dialects import probes


def _ok(body: dict) -> probes.ProbeResponse:
    return probes.ProbeResponse(200, body)


def _chat_response(message: dict) -> probes.ProbeResponse:
    return _ok({"choices": [{"message": message}]})


# --------------------------------------------------------------------------- tools


@pytest.mark.asyncio
async def test_probe_tools_passes_on_a_valid_record_sum_call() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        assert body["tools"][0]["function"]["name"] == "record_sum"
        return _chat_response(
            {
                "tool_calls": [
                    {"function": {"name": "record_sum", "arguments": '{"a": 21, "b": 21}'}}
                ]
            }
        )

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is True
    assert fact.source == "probe"


@pytest.mark.asyncio
async def test_probe_tools_fails_when_no_tool_call_comes_back() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"content": "42"})

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is False


def _truncated(message: dict) -> probes.ProbeResponse:
    return probes.ProbeResponse(200, {"choices": [{"message": message, "finish_reason": "length"}]})


@pytest.mark.asyncio
async def test_probe_tools_retries_a_reply_truncated_while_thinking() -> None:
    """F016: Qwen3 on vLLM spends a 32-token budget reasoning; that is not 'no tools'."""
    budgets: list[int] = []

    async def send(body: dict) -> probes.ProbeResponse:
        budgets.append(body["max_tokens"])
        if body["max_tokens"] < probes.TOOLS_PROBE_RETRY_MAX_TOKENS:
            return _truncated({"content": None, "reasoning_content": "Okay, the user"})
        return _chat_response(
            {"tool_calls": [{"function": {"name": "record_sum", "arguments": '{"a":21,"b":21}'}}]}
        )

    fact = await probes.probe_tools(send, model_id="m", max_tokens=32)

    assert budgets == [32, probes.TOOLS_PROBE_RETRY_MAX_TOKENS]
    assert fact.value is True


@pytest.mark.asyncio
async def test_probe_tools_stays_unknown_when_every_reply_is_truncated() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _truncated({"content": "21 + 21"})

    fact = await probes.probe_tools(send, model_id="m", max_tokens=32)

    assert fact.value is None
    assert "truncated" in fact.detail


@pytest.mark.asyncio
async def test_probe_tools_fails_when_arguments_do_not_match_schema() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response(
            {"tool_calls": [{"function": {"name": "record_sum", "arguments": "{}"}}]}
        )

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is False


@pytest.mark.asyncio
async def test_probe_tools_unknown_on_transport_failure() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        raise ConnectionError("boom")

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is None
    assert fact.source == "unknown"


@pytest.mark.asyncio
async def test_probe_tools_unknown_on_an_unrelated_http_error_never_false() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return probes.ProbeResponse(400, {"error": {"message": "model 'x' does not exist"}})

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is None


@pytest.mark.asyncio
async def test_probe_tools_false_when_the_server_refuses_the_tools_field() -> None:
    """Verbatim vLLM 0.28 answer on ares, started without a tool-call parser."""

    async def send(body: dict) -> probes.ProbeResponse:
        assert body["tool_choice"] == "auto"
        return probes.ProbeResponse(
            400,
            {
                "error": {
                    "message": '"auto" tool choice requires --enable-auto-tool-choice and '
                    "--tool-call-parser to be set",
                    "type": "BadRequestError",
                    "param": None,
                    "code": 400,
                }
            },
        )

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is False
    assert fact.source == "probe"
    assert "--enable-auto-tool-choice" in fact.detail
