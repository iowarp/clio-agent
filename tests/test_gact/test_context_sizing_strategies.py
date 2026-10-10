"""Engine-neutral context sizing: profiles, the strategy registry, the fallback and the setting."""

from __future__ import annotations

import pytest

from clio_agent import conf
from clio_agent.context_sizing.profile import GIB, GpuBudget, ModelMemoryProfile
from clio_agent.context_sizing.strategies import (
    CONTEXT_GRANULE,
    DEFAULT_STRATEGY,
    UNKNOWN_BUDGET_CAP,
    ContextStrategy,
    SizedContext,
    configured_strategy_id,
    default_context,
    fit_unavailable_reason,
    get_strategy,
    register_strategy,
    size_with,
    strategies,
    unregister_strategy,
)
from tests._config_layer import set_config

#: qwen3:4b: 36 layers x 8 KV heads x (128 + 128) x f16 = 147 456 B per token.
QWEN3_4B = ModelMemoryProfile(262144, 147_456, weights_bytes=2_497_293_575, source="test")
#: Ollama's formula for qwen3:4b on a 40 GB A100 (38.6 GiB available).
A100 = GpuBudget(int(38.6 * GIB), int(2_497_293_575 * 1.25) + GIB, detail="test")


def test_kv_bytes_grow_with_the_context_and_stop_at_the_sliding_window() -> None:
    assert QWEN3_4B.kv_bytes(1000) == 147_456_000
    gemma = ModelMemoryProfile(131072, 20_480, 118_784, 1024)
    assert gemma.kv_bytes(512) == (20_480 + 118_784) * 512
    assert gemma.kv_bytes(8192) == 20_480 * 8192 + 118_784 * 1024
    assert ModelMemoryProfile(4096, None).kv_bytes(10) is None


def test_fit_to_gpu_caps_the_trained_context_to_the_kv_budget() -> None:
    sized = size_with(DEFAULT_STRATEGY, QWEN3_4B, A100)
    assert 131072 < sized.tokens < 262144
    assert sized.tokens % CONTEXT_GRANULE == 0
    assert sized.tokens * 147_456 <= A100.kv_bytes
    assert (sized.tokens + CONTEXT_GRANULE) * 147_456 > A100.kv_bytes
    assert "capped" in sized.reason and "GiB of GPU memory" in sized.reason


def test_fit_to_gpu_keeps_the_trained_context_when_it_fits() -> None:
    sized = size_with(DEFAULT_STRATEGY, QWEN3_4B, GpuBudget(80 * GIB, 4 * GIB))
    assert sized == SizedContext(262144, "model's trained context 262144")


def test_parallel_sequences_share_the_budget_and_no_room_serves_one_granule() -> None:
    one = size_with(DEFAULT_STRATEGY, QWEN3_4B, A100)
    four = size_with(
        DEFAULT_STRATEGY, QWEN3_4B, GpuBudget(A100.memory_bytes, A100.reserved_bytes, 4)
    )
    assert four.tokens <= one.tokens // 4 + CONTEXT_GRANULE
    tight = size_with(DEFAULT_STRATEGY, QWEN3_4B, GpuBudget(2 * GIB, 4 * GIB))
    assert tight.tokens == CONTEXT_GRANULE


def test_sliding_window_layers_let_a_longer_context_fit() -> None:
    full = ModelMemoryProfile(131072, 139_264)
    hybrid = ModelMemoryProfile(131072, 20_480, 118_784, 1024)
    budget = GpuBudget(6 * GIB, GIB)
    assert size_with(DEFAULT_STRATEGY, hybrid, budget).tokens > (
        size_with(DEFAULT_STRATEGY, full, budget).tokens
    )


def test_missing_inputs_are_named_and_the_strategy_refuses_to_guess() -> None:
    assert fit_unavailable_reason(None, A100) == "the model's layout could not be read"
    assert "maximum context" in fit_unavailable_reason(ModelMemoryProfile(None, 1), A100)
    assert "attention layout" in fit_unavailable_reason(ModelMemoryProfile(4096, None), A100)
    assert "no GPU memory budget" in fit_unavailable_reason(QWEN3_4B, None)
    assert fit_unavailable_reason(QWEN3_4B, A100) == ""
    with pytest.raises(ValueError, match="cannot be computed"):
        size_with(DEFAULT_STRATEGY, QWEN3_4B, None)


def test_unknown_budget_falls_back_to_a_ram_safe_context() -> None:
    chosen = default_context(QWEN3_4B, None, DEFAULT_STRATEGY)
    assert chosen is not None and chosen.tokens == UNKNOWN_BUDGET_CAP
    assert "no GPU memory budget" in chosen.reason
    layoutless = default_context(ModelMemoryProfile(40960, None), A100, DEFAULT_STRATEGY)
    assert layoutless is not None and layoutless.tokens == UNKNOWN_BUDGET_CAP
    small = default_context(ModelMemoryProfile(8192, None), None, DEFAULT_STRATEGY)
    assert small is not None and small.tokens == 8192
    assert default_context(ModelMemoryProfile(None, 1), A100, DEFAULT_STRATEGY) is None
    assert default_context(None, A100, DEFAULT_STRATEGY) is None


def test_a_research_strategy_registers_and_is_used_by_id() -> None:
    def half(profile: ModelMemoryProfile, budget: GpuBudget) -> SizedContext:
        return SizedContext(int(profile.trained_context or 0) // 2, "half the trained context")

    strategy = register_strategy(ContextStrategy("half", "Half", "Half the maximum.", half))
    try:
        assert [s.id for s in strategies()][0] == DEFAULT_STRATEGY
        assert get_strategy("half") is strategy
        assert size_with("half", QWEN3_4B, A100) == SizedContext(131072, "half the trained context")
        with pytest.raises(ValueError, match="already registered"):
            register_strategy(strategy)
    finally:
        unregister_strategy("half")
    with pytest.raises(ValueError, match="registered: fit_to_gpu"):
        get_strategy("half")
    with pytest.raises(ValueError, match="cannot be removed"):
        unregister_strategy(DEFAULT_STRATEGY)


def test_a_strategy_result_is_clamped_to_the_models_maximum() -> None:
    def greedy(profile: ModelMemoryProfile, budget: GpuBudget) -> SizedContext:
        return SizedContext(10_000_000, "everything")

    register_strategy(ContextStrategy("greedy", "Greedy", "Too much.", greedy))
    try:
        assert size_with("greedy", QWEN3_4B, A100).tokens == 262144
    finally:
        unregister_strategy("greedy")


def test_the_default_strategy_is_a_configuration_setting(monkeypatch) -> None:
    monkeypatch.delenv("CLIO_LM_CONTEXT_SIZING_STRATEGY", raising=False)
    conf.reload()
    assert configured_strategy_id() == "fit_to_gpu"
    monkeypatch.setenv("CLIO_LM_CONTEXT_SIZING_STRATEGY", "research_a")
    assert configured_strategy_id() == "research_a"
    set_config("lm.context_sizing_strategy", "research_b")  # the file wins over the env
    assert configured_strategy_id() == "research_b"
