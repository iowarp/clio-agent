"""iowarp/clio-agent#8: when the LM reports no cost, the turn is priced at the
model's list price from the community cost map -- never a table compiled
into CLIO -- and a model with no listed price stays unpriced."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.app import _estimate_cost_usd
from clio_agent.gact.usage import _price_table_match
from clio_agent.providers.handshake.sources import litellm_catalog

_MAP = {
    "claude-haiku-4-5-20251001": {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6},
    "openrouter/openai/gpt-oss-120b:free": {
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
    },
    "local-without-prices": {"max_tokens": 8192},
}


@pytest.fixture(autouse=True)
def _cost_map(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(model_id: str, *, allow_fetch: bool = True) -> tuple[str, dict[str, Any]] | None:
        del allow_fetch
        info = _MAP.get(model_id)
        return (model_id, info) if info is not None else None

    monkeypatch.setattr(litellm_catalog, "lookup_litellm_info", lookup)


def test_unknown_model_returns_zero() -> None:
    assert _estimate_cost_usd("", 100, 50) == 0.0
    assert _estimate_cost_usd("non-existent-model-x", 100, 50) == 0.0
    assert _price_table_match("non-existent-model-x") is None


def test_listed_price_prices_the_turn() -> None:
    # $1/M input, $5/M output: 1000 input + 500 output = 0.001 + 0.0025.
    cost = _estimate_cost_usd("claude-haiku-4-5-20251001", 1000, 500)
    assert abs(cost - 0.0035) < 1e-9


def test_a_listed_price_of_zero_is_known_and_free() -> None:
    assert _price_table_match("openrouter/openai/gpt-oss-120b:free") == (0.0, 0.0)
    assert _estimate_cost_usd("openrouter/openai/gpt-oss-120b:free", 1000, 1000) == 0.0


def test_a_listed_model_without_prices_is_unpriced() -> None:
    assert _price_table_match("local-without-prices") is None
