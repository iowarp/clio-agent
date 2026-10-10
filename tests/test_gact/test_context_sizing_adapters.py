"""Memory profiles from real config.json, GGUF metadata and Ollama model_info shapes."""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest

from clio_agent.context_sizing.adapters import (
    gguf_profile,
    hf_profile,
    hf_trained_context,
    ollama_profile,
)
from clio_agent.gact.infrastructure.context_sizing import target_probe

#: Qwen/Qwen3-4B-Instruct-2507 config.json (the fields sizing reads, as published).
QWEN3_4B_CONFIG = {
    "architectures": ["Qwen3ForCausalLM"],
    "model_type": "qwen3",
    "head_dim": 128,
    "hidden_size": 2560,
    "max_position_embeddings": 262144,
    "max_window_layers": 36,
    "num_attention_heads": 32,
    "num_hidden_layers": 36,
    "num_key_value_heads": 8,
    "rope_scaling": None,
    "sliding_window": None,
    "torch_dtype": "bfloat16",
    "use_sliding_window": False,
}
#: google/gemma-3-4b-it config.json: a multimodal wrapper, 5:1 sliding/full layers.
GEMMA3_4B_CONFIG = {
    "model_type": "gemma3",
    "torch_dtype": "bfloat16",
    "text_config": {
        "model_type": "gemma3_text",
        "head_dim": 256,
        "hidden_size": 2560,
        "max_position_embeddings": 131072,
        "num_attention_heads": 8,
        "num_hidden_layers": 34,
        "num_key_value_heads": 4,
        "rope_scaling": {"factor": 8.0, "rope_type": "linear"},
        "sliding_window": 1024,
        "sliding_window_pattern": 6,
    },
    "vision_config": {"model_type": "siglip_vision_model"},
}
#: openai/gpt-oss-20b config.json: alternating sliding/full layers, YaRN-scaled.
GPT_OSS_20B_CONFIG = {
    "model_type": "gpt_oss",
    "head_dim": 64,
    "hidden_size": 2880,
    "layer_types": ["sliding_attention", "full_attention"] * 12,
    "max_position_embeddings": 131072,
    "num_attention_heads": 64,
    "num_hidden_layers": 24,
    "num_key_value_heads": 8,
    "rope_scaling": {
        "beta_fast": 32.0,
        "beta_slow": 1.0,
        "factor": 32.0,
        "original_max_position_embeddings": 4096,
        "rope_type": "yarn",
        "truncate": False,
    },
    "sliding_window": 128,
    "torch_dtype": "bfloat16",
}
#: deepseek-ai/DeepSeek-V3 config.json: multi-head latent attention.
DEEPSEEK_V3_CONFIG = {
    "model_type": "deepseek_v3",
    "hidden_size": 7168,
    "kv_lora_rank": 512,
    "max_position_embeddings": 163840,
    "num_attention_heads": 128,
    "num_hidden_layers": 61,
    "num_key_value_heads": 128,
    "qk_rope_head_dim": 64,
    "rope_scaling": {
        "factor": 40,
        "original_max_position_embeddings": 4096,
        "type": "yarn",
    },
    "torch_dtype": "bfloat16",
}
#: Qwen/Qwen3-Next-80B-A3B config.json: three linear-attention layers per full one.
QWEN3_NEXT_CONFIG = {
    "model_type": "qwen3_next",
    "head_dim": 256,
    "hidden_size": 2048,
    "layer_types": ["linear_attention", "linear_attention", "linear_attention", "full_attention"]
    * 12,
    "max_position_embeddings": 262144,
    "num_attention_heads": 16,
    "num_hidden_layers": 48,
    "num_key_value_heads": 2,
    "torch_dtype": "bfloat16",
}
#: qwen3:4b as Ollama 0.34.4 reports it (/api/show model_info, live on an A100).
QWEN3_4B_MODEL_INFO = {
    "general.architecture": "qwen3",
    "qwen3.context_length": 262144,
    "qwen3.block_count": 36,
    "qwen3.embedding_length": 2560,
    "qwen3.attention.head_count": 32,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}


def test_dense_gqa_config_matches_vllms_kv_cost() -> None:
    profile = hf_profile(QWEN3_4B_CONFIG, weights_bytes=8_044_936_192)
    assert profile.trained_context == 262144
    assert profile.full_bytes_per_token == 2 * 8 * 128 * 2 * 36  # K+V x heads x dim x bf16
    assert (profile.sliding_bytes_per_token, profile.sliding_window) == (0, 0)
    assert profile.weights_bytes == 8_044_936_192
    assert "qwen3" in profile.source


def test_multimodal_wrapper_reads_its_text_config_and_sliding_pattern() -> None:
    profile = hf_profile(GEMMA3_4B_CONFIG)
    per_layer = 2 * 4 * 256 * 2
    # 34 layers, every 6th full: layers 6, 12, 18, 24 and 30 (5 full, 29 sliding).
    assert profile.full_bytes_per_token == 5 * per_layer
    assert profile.sliding_bytes_per_token == 29 * per_layer
    assert profile.sliding_window == 1024
    # Gemma 3's stated maximum already includes its linear rope scaling (vLLM).
    assert profile.trained_context == 131072
    linear = {"max_position_embeddings": 4096, "rope_scaling": {"type": "linear", "factor": 4}}
    assert hf_trained_context(linear) == 16384


def test_layer_types_and_yarn_scaling() -> None:
    profile = hf_profile(GPT_OSS_20B_CONFIG)
    per_layer = 2 * 8 * 64 * 2
    assert profile.full_bytes_per_token == 12 * per_layer
    assert profile.sliding_bytes_per_token == 12 * per_layer
    assert profile.sliding_window == 128
    assert profile.trained_context == 4096 * 32


def test_latent_attention_caches_one_compressed_vector_per_layer() -> None:
    profile = hf_profile(DEEPSEEK_V3_CONFIG)
    assert profile.full_bytes_per_token == (512 + 64) * 2 * 61
    assert hf_trained_context(DEEPSEEK_V3_CONFIG) == 4096 * 40


def test_linear_attention_layers_cost_no_kv() -> None:
    profile = hf_profile(QWEN3_NEXT_CONFIG)
    assert profile.full_bytes_per_token == 12 * 2 * 2 * 256 * 2
    assert profile.sliding_bytes_per_token == 0


def test_fp8_kv_cache_halves_the_cost_and_a_bare_config_still_answers_max() -> None:
    assert hf_profile(QWEN3_4B_CONFIG, kv_cache_dtype="fp8").full_bytes_per_token == (
        8 * 128 * 2 * 36
    )
    bare = hf_profile({"max_position_embeddings": 32768})
    assert bare.trained_context == 32768 and not bare.layout_known
    assert hf_trained_context({"rope_scaling": {"rope_type": "llama3", "factor": 8.0}}) is None
    llama31 = {"max_position_embeddings": 131072, "rope_scaling": {"rope_type": "llama3"}}
    assert hf_trained_context(llama31) == 131072


def test_ollama_model_info_is_gguf_metadata() -> None:
    profile = ollama_profile(QWEN3_4B_MODEL_INFO, weights_bytes=2_497_293_575)
    assert profile.trained_context == 262144
    assert profile.full_bytes_per_token == 147_456
    assert profile.weights_bytes == 2_497_293_575
    assert "Ollama" in profile.source
    q8 = ollama_profile(QWEN3_4B_MODEL_INFO, cache_type="q8_0")
    assert q8.full_bytes_per_token == int(36 * 8 * 256 * 34 / 32)


def test_gguf_sliding_pattern_by_architecture_and_per_layer_kv_heads() -> None:
    gemma3 = {
        "general.architecture": "gemma3",
        "gemma3.context_length": 131072,
        "gemma3.block_count": 34,
        "gemma3.attention.head_count": 8,
        "gemma3.attention.head_count_kv": 4,
        "gemma3.attention.key_length": 256,
        "gemma3.attention.value_length": 256,
        "gemma3.attention.sliding_window": 1024,
    }
    profile = gguf_profile(gemma3)
    assert profile.full_bytes_per_token == 5 * 4 * 512 * 2
    assert profile.sliding_bytes_per_token == 29 * 4 * 512 * 2
    hybrid = {
        "general.architecture": "lfm2",
        "lfm2.context_length": 128000,
        "lfm2.block_count": 4,
        "lfm2.embedding_length": 1024,
        "lfm2.attention.head_count": 16,
        "lfm2.attention.head_count_kv": [0, 0, 8, 8],
    }
    profile = gguf_profile(hybrid)
    assert profile.full_bytes_per_token == 2 * 8 * (64 + 64) * 2
    assert not gguf_profile({"general.architecture": "x", "x.context_length": 4096}).layout_known


def _gguf_string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _gguf(pairs: list[tuple[str, int, bytes]]) -> bytes:
    """A GGUF v3 header with ``(key, type, encoded value)`` pairs and no tensors."""

    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(pairs))
    for key, kind, value in pairs:
        out += _gguf_string(key) + struct.pack("<I", kind) + value
    return out


QWEN3_GGUF = _gguf(
    [
        ("general.architecture", 8, _gguf_string("qwen3")),
        ("general.name", 8, _gguf_string("Qwen3 4B")),
        ("qwen3.context_length", 4, struct.pack("<I", 40960)),
        ("qwen3.block_count", 4, struct.pack("<I", 36)),
        ("qwen3.embedding_length", 4, struct.pack("<I", 2560)),
        ("qwen3.attention.head_count", 4, struct.pack("<I", 32)),
        ("qwen3.attention.head_count_kv", 4, struct.pack("<I", 8)),
        ("qwen3.attention.key_length", 4, struct.pack("<I", 128)),
        ("qwen3.attention.value_length", 4, struct.pack("<I", 128)),
        ("qwen3.rope.freq_base", 6, struct.pack("<f", 1_000_000.0)),
        (
            "tokenizer.ggml.tokens",
            9,
            struct.pack("<I", 8) + struct.pack("<Q", 3) + b"".join(map(_gguf_string, "abc")),
        ),
        (
            "tokenizer.ggml.token_type",
            9,
            struct.pack("<I", 5) + struct.pack("<Q", 5000) + struct.pack("<5000i", *[1] * 5000),
        ),
    ]
)


def test_gguf_header_is_read_without_the_token_tables() -> None:
    metadata = target_probe.gguf_metadata(io.BytesIO(QWEN3_GGUF))
    assert metadata["qwen3.block_count"] == 36
    assert metadata["tokenizer.ggml.tokens"] is None  # string arrays are skipped
    assert metadata["tokenizer.ggml.token_type"] is None  # longer than MAX_ARRAY
    kept = target_probe.relevant(metadata)
    assert "general.name" not in kept and kept["general.architecture"] == "qwen3"
    profile = gguf_profile(kept)
    assert profile.trained_context == 40960
    assert profile.full_bytes_per_token == 147_456
    with pytest.raises(ValueError, match="not a GGUF"):
        target_probe.gguf_metadata(io.BytesIO(b"nope" + bytes(20)))


def test_probe_reads_a_gguf_file_and_split_siblings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(target_probe, "gpu_memory", lambda: [{"total": 40, "free": 30}])
    first = tmp_path / "qwen-00001-of-00002.gguf"
    first.write_bytes(QWEN3_GGUF)
    (tmp_path / "qwen-00002-of-00002.gguf").write_bytes(b"x" * 100)
    answer = target_probe.probe({"kind": "gguf", "path": str(first)})
    assert answer["gpus"] == [{"total": 40, "free": 30}]
    assert answer["weights_bytes"] == len(QWEN3_GGUF) + 100
    assert answer["gguf"]["qwen3.block_count"] == 36
    missing = target_probe.probe({"kind": "gguf", "path": str(tmp_path / "absent.gguf")})
    assert missing["error"] == "model not found on this host"


def test_probe_reads_a_model_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(target_probe, "gpu_memory", lambda: [])
    (tmp_path / "config.json").write_text(
        json.dumps({**QWEN3_4B_CONFIG, "vocab_table": ["x" * 5000]})
    )
    (tmp_path / "model-00001.safetensors").write_bytes(b"w" * 300)
    (tmp_path / "model-00002.safetensors").write_bytes(b"w" * 200)
    answer = target_probe.probe({"kind": "hf", "path": str(tmp_path)})
    assert answer["weights_bytes"] == 500
    assert answer["config"]["num_hidden_layers"] == 36
    assert "vocab_table" not in answer["config"]  # oversized entries dropped


def test_gpu_memory_tools_are_parsed() -> None:
    assert target_probe.parse_nvidia_smi("40960, 39000\n81920, 1024\n") == [
        {"total": 40960 << 20, "free": 39000 << 20},
        {"total": 81920 << 20, "free": 1024 << 20},
    ]
    assert target_probe.parse_nvidia_smi("No devices were found") == []
    rocm = json.dumps(
        {
            "card0": {
                "VRAM Total Memory (B)": "68702699520",
                "VRAM Total Used Memory (B)": "10737418240",
            }
        }
    )
    assert target_probe.parse_rocm_smi(rocm) == [
        {"total": 68702699520, "free": 68702699520 - 10737418240}
    ]
