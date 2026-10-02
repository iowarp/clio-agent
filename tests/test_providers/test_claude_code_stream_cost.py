"""The Claude Code SDK's own usage and cost, normalized once (#775).

The Agent SDK's ``ResultMessage`` carries the turn's REAL subscription-priced cost as
``total_cost_usd`` (a field sibling to ``usage``) and sometimes reports tokens only in
the per-model camelCase ``model_usage`` breakdown.
:func:`~clio_agent.providers.claude_code_bridge.sdk_result_usage` is the one
normalization; the engine turns it into the call's ``dspy.lm15.Usage`` (fresh input,
output, cache reads and cache writes kept apart).
"""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.providers import claude_code_engine
from clio_agent.providers.claude_code_bridge import sdk_result_usage
from clio_agent.providers.claude_code_sessions import _reset_sessions_for_tests
from tests import _fake_claude_sdk as fake


@pytest.fixture(autouse=True)
def _clean_pool() -> Any:
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()
    yield
    _reset_sessions_for_tests()
    claude_code_engine._CONVERSATIONS.clear_for_tests()


def test_the_sdk_reported_cost_is_kept_beside_the_tokens() -> None:
    usage = sdk_result_usage(
        fake.ResultMessage(
            usage={"input_tokens": 2, "cache_creation_input_tokens": 49563, "output_tokens": 4},
            total_cost_usd=0.19924,
        )
    )
    assert usage["cost_usd"] == pytest.approx(0.19924)
    assert usage["cache_creation_input_tokens"] == 49563


def test_model_usage_is_summed_when_the_flat_usage_is_empty() -> None:
    usage = sdk_result_usage(
        fake.ResultMessage(
            model_usage={
                "claude-sonnet-5": {
                    "inputTokens": 300,
                    "outputTokens": 90,
                    "cacheReadInputTokens": 7,
                    "cacheCreationInputTokens": 0,
                    "costUSD": 0.0018,
                },
            }
        )
    )
    assert (usage["input_tokens"], usage["output_tokens"]) == (300, 90)
    assert usage["cache_read_input_tokens"] == 7
    assert usage["cost_usd"] == pytest.approx(0.0018)


def test_no_cost_source_means_no_cost_field_never_a_fabricated_zero() -> None:
    usage = sdk_result_usage(fake.ResultMessage(usage={"input_tokens": 1, "output_tokens": 1}))
    assert "cost_usd" not in usage


async def test_the_engine_usage_keeps_cache_reads_and_writes_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = {
        "input_tokens": 120,
        "output_tokens": 45,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 30,
    }
    fake.install(monkeypatch, reply=lambda: fake.answer("Hello", usage=usage))
    response = await fake.drive(fake.request())

    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 45)
    assert (response.usage.cache_read_tokens, response.usage.cache_write_tokens) == (10, 30)
