"""The Claude Code SDK's own turn cost survives the streaming path (never litellm's $0).

Live on 0.9.4.19: a claude-sonnet-5 turn whose SDK result carried
``total_cost_usd=0.19924`` was stored and shown as ``Cost: $0.0000``. The
streaming chunk carried the cost as ``cost_usd``, which litellm's
``stream_chunk_builder`` drops; it then priced the turn from its own model map
-- ``0.0`` for a model it does not know -- and DSPy recorded that as the
provider-reported cost.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import litellm
import pytest
from litellm import CustomLLM

from clio_agent.gact.usage import _usage_from_history_slice
from clio_agent.providers.claude_code_bridge import sdk_result_usage
from clio_agent.providers.claude_code_sessions import _streaming_chunk


class _SdkResult:
    """The fields of a real ``ResultMessage`` (captured live, claude-agent-sdk 0.2.156)."""

    total_cost_usd = 0.19924
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 49563, "output_tokens": 4}
    model_usage = None


class _StreamingClaudeCode(CustomLLM):
    def __init__(self, usage_payload: dict[str, Any]) -> None:
        super().__init__()
        self._usage_payload = usage_payload

    async def astreaming(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        yield _streaming_chunk(text="ok", is_finished=False)
        yield _streaming_chunk(text="", is_finished=True, usage_payload=self._usage_payload)


def _stream_through_litellm(
    usage_payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch, model: str
) -> Any:
    monkeypatch.setattr(
        litellm,
        "custom_provider_map",
        [{"provider": "claude_code", "custom_handler": _StreamingClaudeCode(usage_payload)}],
    )

    async def _run() -> Any:
        stream = await litellm.acompletion(
            model=f"claude_code/{model}",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        chunks = [chunk async for chunk in stream]
        return litellm.stream_chunk_builder(chunks)

    return asyncio.run(_run())


class _HistoryLM:
    def __init__(self, entry: dict[str, Any]) -> None:
        self.history = [entry]


def _dspy_entry(response: Any, model: str) -> dict[str, Any]:
    """What ``dspy.clients.base_lm`` records for one completed call."""

    return {
        "usage": dict(response.usage),
        "cost": response._hidden_params.get("response_cost"),
        "model": f"claude_code/{model}",
    }


def test_streamed_turn_keeps_the_sdk_reported_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    model = "claude-unpriced-9"  # a model litellm's price map does not know
    response = _stream_through_litellm(sdk_result_usage(_SdkResult()), monkeypatch, model)

    assert response._hidden_params.get("response_cost") == pytest.approx(0.19924)

    lm = _HistoryLM(_dspy_entry(response, model))
    monkeypatch.setattr("clio_agent.gact.usage._all_known_lms", lambda _app: [lm])
    rolled = _usage_from_history_slice({id(lm): 0}, app=object())

    assert rolled["cost_known"] is True
    assert rolled["cost_usd"] == pytest.approx(0.19924)


def test_blocking_turn_keeps_the_sdk_reported_cost() -> None:
    from clio_agent.providers.claude_code_bridge import build_model_response

    response = build_model_response(
        text="ok", model="claude-sonnet-5", usage_payload=sdk_result_usage(_SdkResult())
    )

    assert response._hidden_params["response_cost"] == pytest.approx(0.19924)
    assert dict(response.usage)["cost"] == pytest.approx(0.19924)
