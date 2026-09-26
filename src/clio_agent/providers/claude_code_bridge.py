"""LiteLLM response construction for the Claude Code provider.

Split out of :mod:`clio_agent.providers.claude_code_litellm` (#891) so the
provider module stays under its file-size ratchet while the #891 stream-audit
instrumentation lands. This holds the pure translation from a Claude Code
result (text + raw SDK usage dict) into a LiteLLM ``ModelResponse`` — no I/O,
no SDK calls.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from litellm.types.utils import Choices, Message, ModelResponse, Usage


def sdk_result_usage(msg: Any) -> dict[str, Any]:
    """Normalize one Claude Agent SDK ``ResultMessage``'s usage + real cost.

    Duck-typed on ``getattr`` (never imports ``claude_agent_sdk``, keeping this
    module SDK-free) so both the blocking (:mod:`claude_code_sdk_pool`) and
    streaming (:mod:`claude_code_litellm`) transports share ONE normalization.

    Token counts prefer the flat ``usage`` dict the SDK reports (Anthropic-style
    snake_case keys). Some CLI turns only populate the per-model camelCase
    ``model_usage`` breakdown (:class:`claude_agent_sdk.ModelUsage`) instead of
    the flat field -- when the flat dict reports nothing, this sums
    ``model_usage`` across every model that served the turn rather than
    leaving the turn's tokens at zero.

    Cost prefers ``total_cost_usd`` -- the SDK's own real, subscription-priced
    number for this turn -- falling back to summing each model's ``costUSD``
    from ``model_usage`` when that field is absent. The returned dict omits
    ``cost_usd`` entirely (not ``0.0``) when NEITHER source reports anything,
    so a caller can tell "genuinely free" apart from "the SDK never told us"
    (#775 no silent fallback).
    """
    raw_usage = getattr(msg, "usage", None)
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    model_usage = getattr(msg, "model_usage", None)
    model_usage = model_usage if isinstance(model_usage, dict) and model_usage else None

    if not (usage.get("input_tokens") or usage.get("output_tokens")) and model_usage:
        usage = {
            "input_tokens": sum(int(m.get("inputTokens", 0) or 0) for m in model_usage.values()),
            "output_tokens": sum(int(m.get("outputTokens", 0) or 0) for m in model_usage.values()),
            "cache_creation_input_tokens": sum(
                int(m.get("cacheCreationInputTokens", 0) or 0) for m in model_usage.values()
            ),
            "cache_read_input_tokens": sum(
                int(m.get("cacheReadInputTokens", 0) or 0) for m in model_usage.values()
            ),
        }

    cost_usd = getattr(msg, "total_cost_usd", None)
    if cost_usd is None and model_usage:
        costs = [float(m["costUSD"]) for m in model_usage.values() if "costUSD" in m]
        if costs:
            cost_usd = sum(costs)
    if cost_usd is not None:
        usage["cost_usd"] = float(cost_usd)

    return usage


def usage_chunk_fields(usage_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert one turn's raw SDK usage dict into LiteLLM's token field names.

    The three input-side token counts the SDK reports (fresh input, cache
    creation, cache read) are summed into LiteLLM's single ``prompt_tokens`` —
    the cache breakdown is preserved only in the ``provider.call_usage`` audit
    row, not here. A real ``usage_payload["cost_usd"]`` (see
    :func:`sdk_result_usage`) rides along under the same key; absent (not
    ``0.0``) when the SDK reported no cost for this turn.

    Shared by the blocking (:func:`build_model_response`) and streaming
    (``claude_code_sessions._streaming_chunk``) transports so this conversion
    has exactly one implementation.
    """
    prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_creation_input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_read_input_tokens", 0) or 0)
    completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
    fields: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cost_usd = usage_payload.get("cost_usd")
    if cost_usd is not None:
        fields["cost_usd"] = float(cost_usd)
    return fields


def build_model_response(
    *,
    text: str,
    model: str,
    usage_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Claude Code result in a LiteLLM ``ModelResponse``.

    A real ``cost_usd`` (see :func:`usage_chunk_fields`) is set BOTH on the
    returned ``Usage`` object (litellm's ``Usage`` accepts and round-trips
    arbitrary kwargs, so ``dict(response.usage)`` -- what DSPy's history
    records -- carries it through) and on ``ModelResponse._hidden_params
    ["response_cost"]`` (the field DSPy's non-streaming history entry reads
    directly, ``entry["cost"]``). Both are honest no-ops when the SDK reported
    no cost for this turn: neither is set, so a caller (``clio_agent.gact.usage``)
    sees an absent cost, not a fabricated ``0.0``.

    Args:
        text: The assistant response text.
        model: The clean model name (rendered back as ``claude_code/<model>``).
        usage_payload: The raw SDK usage dict, or ``None`` when unavailable.
        request_id: Optional response id; a random one is minted when absent.

    Returns:
        A populated LiteLLM ``ModelResponse`` with a single assistant choice.
    """
    fields = usage_chunk_fields(usage_payload or {})
    cost_usd = fields.get("cost_usd")
    response = ModelResponse(
        id=request_id or f"claude-code-{uuid.uuid4().hex}",
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        created=int(time.time()),
        model=f"claude_code/{model}",
        object="chat.completion",
        usage=Usage(**fields),
    )
    if cost_usd is not None:
        response._hidden_params["response_cost"] = cost_usd  # noqa: SLF001 - litellm's own contract
    return response


__all__ = ["build_model_response", "sdk_result_usage", "usage_chunk_fields"]
