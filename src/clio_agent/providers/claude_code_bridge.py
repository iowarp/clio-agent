"""Usage normalization for the Claude Code engine: a raw SDK result's usage and cost.

Pure translation, no I/O, no SDK import (duck-typed on ``getattr``).
"""

from __future__ import annotations

from typing import Any


def sdk_result_usage(msg: Any) -> dict[str, Any]:
    """Normalize one Claude Agent SDK ``ResultMessage``'s usage + real cost.

    Duck-typed on ``getattr`` (never imports ``claude_agent_sdk``, keeping this
    module SDK-free).

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


__all__ = ["sdk_result_usage"]
