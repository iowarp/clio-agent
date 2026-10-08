"""The managed Ollama context default: trained context, capped to the GPU's KV budget."""

from __future__ import annotations

from clio_agent.gact.infrastructure.ollama_context import (
    CONTEXT_GRANULE,
    kv_bytes_per_token,
    ollama_context_default,
)

GIB = 1 << 30
#: qwen3:4b as Ollama 0.34.4 reports it (/api/show model_info, live on an A100 in c13).
QWEN3_4B = {
    "general.architecture": "qwen3",
    "qwen3.context_length": 262144,
    "qwen3.block_count": 36,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}
QWEN3_4B_SIZE = 2_497_293_575


def test_kv_bytes_per_token_from_model_info() -> None:
    assert kv_bytes_per_token(QWEN3_4B) == 147_456
    assert kv_bytes_per_token({"general.architecture": "x", "x.block_count": 2}) is None


def test_trained_context_is_capped_to_fit_a_40gb_a100() -> None:
    chosen = ollama_context_default(QWEN3_4B, int(38.6 * GIB), QWEN3_4B_SIZE)
    assert chosen is not None
    assert chosen.trained == 262144
    assert 131072 < chosen.tokens < 262144
    assert chosen.tokens % CONTEXT_GRANULE == 0
    assert "capped" in chosen.reason
    budget = 38.6 * GIB - QWEN3_4B_SIZE * 1.25 - GIB
    assert chosen.tokens * 147_456 <= budget


def test_trained_context_kept_when_it_fits() -> None:
    chosen = ollama_context_default(QWEN3_4B, 80 * GIB, QWEN3_4B_SIZE)
    assert chosen is not None
    assert chosen.tokens == 262144
    assert chosen.reason == "model's trained context 262144"


def test_parallel_sequences_share_the_budget() -> None:
    one = ollama_context_default(QWEN3_4B, int(38.6 * GIB), QWEN3_4B_SIZE)
    four = ollama_context_default(QWEN3_4B, int(38.6 * GIB), QWEN3_4B_SIZE, num_parallel=4)
    assert one is not None and four is not None
    assert four.tokens <= one.tokens // 4 + CONTEXT_GRANULE


def test_no_room_still_serves_a_minimal_context() -> None:
    chosen = ollama_context_default(QWEN3_4B, 2 * GIB, QWEN3_4B_SIZE)
    assert chosen is not None
    assert chosen.tokens == CONTEXT_GRANULE


def test_unknown_gpu_or_layout_falls_back_to_the_trained_context() -> None:
    chosen = ollama_context_default(QWEN3_4B, None, QWEN3_4B_SIZE)
    assert chosen is not None and chosen.tokens == 262144
    partial = {"general.architecture": "qwen3", "qwen3.context_length": 40960}
    chosen = ollama_context_default(partial, 38 * GIB, QWEN3_4B_SIZE)
    assert chosen is not None and chosen.tokens == 40960


def test_no_trained_context_leaves_ollamas_default() -> None:
    assert ollama_context_default({"general.architecture": "qwen3"}, 38 * GIB, 1) is None
