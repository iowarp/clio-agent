"""iowarp/clio-agent#8: cost estimate falls back to a price table
when the upstream LM doesn't report cost_usd (some OpenAI-compatible
proxies)."""

from __future__ import annotations

from clio_agent.gact.app import _estimate_cost_usd


def test_unknown_model_returns_zero() -> None:
    assert _estimate_cost_usd("", 100, 50) == 0.0
    assert _estimate_cost_usd("non-existent-model-x", 100, 50) == 0.0


def test_haiku_pricing() -> None:
    # Haiku 4.5: $1/M input, $5/M output. 1000 input + 500 output:
    # 1000/1e6 * 1.0 + 500/1e6 * 5.0 = 0.001 + 0.0025 = 0.0035
    cost = _estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 500)
    assert abs(cost - 0.0035) < 1e-9


def test_sonnet_pricing() -> None:
    # Sonnet 4.6: $3/M input, $15/M output.
    cost = _estimate_cost_usd("claude-sonnet-4-6", 1000, 500)
    expected = (1000 * 3 + 500 * 15) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_opus_pricing() -> None:
    cost = _estimate_cost_usd("claude-opus-4-6", 100, 200)
    expected = (100 * 15 + 200 * 75) / 1_000_000
    assert abs(cost - expected) < 1e-9


def test_openrouter_free_tier_zero() -> None:
    assert _estimate_cost_usd("openai/gpt-oss-120b:free", 1000, 1000) == 0.0


def test_openai_models() -> None:
    cost = _estimate_cost_usd("openai/gpt-4o-mini", 1000, 500)
    expected = (1000 * 0.15 + 500 * 0.6) / 1_000_000
    assert abs(cost - expected) < 1e-9
