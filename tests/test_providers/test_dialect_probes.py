"""Unit tests for the active deployment probes (model-capabilities brief 5.3).

Each probe takes an injected async ``send(body) -> ProbeResponse`` seam, so
these tests drive it with a small in-memory fake rather than any real network
call -- no server, local or remote, is touched.
"""

from __future__ import annotations

import base64

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


@pytest.mark.asyncio
async def test_probe_tools_fails_when_arguments_do_not_match_schema() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"tool_calls": [{"function": {"name": "record_sum", "arguments": "{}"}}]})

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
async def test_probe_tools_unknown_on_http_error_never_false() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return probes.ProbeResponse(400, {"error": {"message": "tools not supported"}})

    fact = await probes.probe_tools(send, model_id="m")

    assert fact.value is None


# --------------------------------------------------------------------------- vision


@pytest.mark.asyncio
async def test_probe_vision_passes_when_a_color_is_named() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        content = body["messages"][0]["content"]
        image_block = next(c for c in content if c["type"] == "image_url")
        data_url = image_block["image_url"]["url"]
        assert data_url.startswith("data:image/png;base64,")
        base64.b64decode(data_url.split(",", 1)[1])  # must be valid base64 PNG bytes
        return _chat_response({"content": "Red"})

    fact = await probes.probe_vision(send, model_id="m")

    assert fact.value is True


@pytest.mark.asyncio
async def test_probe_vision_unknown_on_http_error_is_ambiguous_not_false() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return probes.ProbeResponse(400, {"error": "bad request"})

    fact = await probes.probe_vision(send, model_id="m")

    assert fact.value is None


@pytest.mark.asyncio
async def test_probe_vision_unknown_when_no_content_returned() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"content": ""})

    fact = await probes.probe_vision(send, model_id="m")

    assert fact.value is None


# --------------------------------------------------------------------------- reasoning


@pytest.mark.asyncio
async def test_probe_reasoning_passes_on_reasoning_content_field() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        assert body["reasoning_effort"] == "low"
        return _chat_response({"reasoning_content": "2+2 is 4.", "content": "4"})

    fact = await probes.probe_reasoning(
        send, model_id="m", control="reasoning_effort", control_value="low"
    )

    assert fact.value is True
    assert "reasoning_content" in fact.detail


@pytest.mark.asyncio
async def test_probe_reasoning_checks_every_field_in_order() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"reasoning_text": "thinking...", "content": "4"})

    fact = await probes.probe_reasoning(send, model_id="m", control="think", control_value=True)

    assert fact.value is True
    assert "reasoning_text" in fact.detail


@pytest.mark.asyncio
async def test_probe_reasoning_false_when_no_field_populated() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"content": "4"})

    fact = await probes.probe_reasoning(send, model_id="m", control="think", control_value=True)

    assert fact.value is False


@pytest.mark.asyncio
async def test_probe_reasoning_unknown_on_http_error_never_false() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return probes.ProbeResponse(400, {"error": "unsupported"})

    fact = await probes.probe_reasoning(send, model_id="m", control="think", control_value=True)

    assert fact.value is None


# --------------------------------------------------------------------------- parameters


@pytest.mark.asyncio
async def test_probe_parameter_rejected_when_400_names_the_field() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        assert body["top_k"] == 40
        return probes.ProbeResponse(400, {"error": {"message": "Unknown parameter: top_k"}})

    fact = await probes.probe_parameter(send, model_id="m", param_name="top_k", param_value=40)

    assert fact.value is False
    assert "top_k" in fact.detail


@pytest.mark.asyncio
async def test_probe_parameter_accepted_silently_on_a_clean_200() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return _chat_response({"content": "OK"})

    fact = await probes.probe_parameter(send, model_id="m", param_name="min_p", param_value=0.1)

    assert fact.value is True
    assert fact.detail == "probe: accepted silently"


@pytest.mark.asyncio
async def test_probe_parameter_unknown_when_400_is_unrelated() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        return probes.ProbeResponse(400, {"error": {"message": "model not found"}})

    fact = await probes.probe_parameter(send, model_id="m", param_name="top_k", param_value=40)

    assert fact.value is None


@pytest.mark.asyncio
async def test_probe_parameter_unknown_on_transport_failure() -> None:
    async def send(body: dict) -> probes.ProbeResponse:
        raise TimeoutError("timed out")

    fact = await probes.probe_parameter(send, model_id="m", param_name="top_k", param_value=40)

    assert fact.value is None
