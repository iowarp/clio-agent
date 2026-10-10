"""Build a :class:`ModelMemoryProfile` from what each engine reads about a model.

* :func:`hf_profile` -- a Hugging Face ``config.json`` (vLLM): layers, KV heads
  (grouped-query attention), head size, multi-head latent attention,
  sliding-window and hybrid layer patterns, the KV-cache dtype, and the
  maximum context vLLM derives (YaRN scaling included);
* :func:`gguf_profile` -- GGUF metadata (llama.cpp), ``<arch>.*`` keys,
  per-layer KV-head arrays for hybrid models and the cache types;
* :func:`ollama_profile` -- Ollama's ``/api/show`` ``model_info``, which is
  the model's GGUF metadata.

Every adapter returns a profile even when the layout is incomplete
(``full_bytes_per_token`` None): the maximum context alone still answers "Max".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clio_agent.context_sizing.profile import ModelMemoryProfile

#: Bytes per KV element by Hugging Face / vLLM dtype name.
HF_DTYPE_BYTES: dict[str, float] = {
    "float32": 4,
    "float": 4,
    "float16": 2,
    "half": 2,
    "bfloat16": 2,
    "fp8": 1,
    "fp8_e4m3": 1,
    "fp8_e5m2": 1,
    "fp8_inc": 1,
}
#: Bytes per KV element by GGML cache type (block-quantized types include their scales).
GGML_CACHE_BYTES: dict[str, float] = {
    "f32": 4,
    "f16": 2,
    "bf16": 2,
    "q8_0": 34 / 32,
    "q5_1": 24 / 32,
    "q5_0": 22 / 32,
    "q4_1": 20 / 32,
    "q4_0": 18 / 32,
    "iq4_nl": 18 / 32,
}
#: Sliding-window layer patterns llama.cpp applies by architecture when the GGUF
#: carries no per-layer pattern: every Nth layer is full attention.
GGUF_SWA_PERIOD: dict[str, int] = {"gemma2": 2, "gemma3": 6, "cohere2": 4}
#: Hugging Face model types whose layers alternate sliding/full with this period.
HF_SWA_PERIOD: dict[str, int] = {"gemma2": 2, "cohere2": 4}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _periodic(count: int, period: int) -> list[bool]:
    """Every ``period``-th layer full attention, the rest sliding."""

    return [(index + 1) % period != 0 for index in range(count)]


def _hf_text_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = config.get("text_config") or config.get("llm_config")
    if isinstance(nested, Mapping):
        return {**{k: v for k, v in config.items() if k != "text_config"}, **nested}
    return config


def hf_trained_context(config: Mapping[str, Any]) -> int | None:
    """The maximum context vLLM derives from a ``config.json``.

    The first of the usual length keys, scaled the way vLLM scales it: a YaRN
    rope scaling multiplies its ``original_max_position_embeddings`` by the
    factor; other scalings (not su/longrope/llama3) multiply the length.
    """

    text = _hf_text_config(config)
    keys = ("max_position_embeddings", "n_positions", "max_seq_len", "seq_length")
    length = next((found for key in keys if (found := _int(text.get(key)))), None)
    scaling = text.get("rope_scaling") or text.get("rope_parameters")
    if length is None or not isinstance(scaling, Mapping):
        return length
    kind = str(scaling.get("rope_type") or scaling.get("type") or "")
    factor = scaling.get("factor")
    if kind in {"su", "longrope", "llama3", "default", ""} or not isinstance(factor, int | float):
        return length
    if "gemma3" in str(text.get("model_type", "")):
        return length  # vLLM: Gemma 3's stated maximum already includes its scaling
    if kind == "yarn":
        length = _int(scaling.get("original_max_position_embeddings")) or length
    return int(length * factor)


def _hf_sliding_pattern(text: Mapping[str, Any], layers: int) -> tuple[list[bool], list[bool]]:
    """Per-layer ``(has_kv, sliding)`` flags for a Hugging Face text config."""

    kinds = text.get("layer_types")
    if isinstance(kinds, list) and kinds:
        kinds = [str(kind) for kind in kinds][:layers]
        has_kv = [
            ("attention" in kind and "linear" not in kind) or kind in {"attn", "full", "sliding"}
            for kind in kinds
        ]
        sliding = ["sliding" in kind or "local" in kind for kind in kinds]
        return has_kv, sliding
    pattern = text.get("hybrid_override_pattern")
    if isinstance(pattern, str) and pattern:
        return [symbol == "*" for symbol in pattern[:layers]], [False] * layers
    has_kv = [True] * layers
    period = _int(text.get("attn_layer_period"))
    if period:
        offset = int(text.get("attn_layer_offset") or 0)
        has_kv = [index % period == offset for index in range(layers)]
    window = _int(text.get("sliding_window"))
    if not window or text.get("use_sliding_window") is False:
        return has_kv, [False] * layers
    period = _int(text.get("sliding_window_pattern")) or HF_SWA_PERIOD.get(
        str(text.get("model_type", ""))
    )
    if period:
        return has_kv, _periodic(layers, period)
    first = _int(text.get("max_window_layers"))
    if text.get("use_sliding_window") and first is not None:
        return has_kv, [index >= first for index in range(layers)]
    return has_kv, [True] * layers


def hf_profile(
    config: Mapping[str, Any], *, weights_bytes: int = 0, kv_cache_dtype: str = "auto"
) -> ModelMemoryProfile:
    """A profile from a Hugging Face ``config.json`` as vLLM sizes its KV cache.

    Args:
        config: The parsed ``config.json``.
        weights_bytes: The weights' size on disk.
        kv_cache_dtype: vLLM's ``--kv-cache-dtype`` (``auto`` follows the model dtype).
    """

    text = _hf_text_config(config)
    trained = hf_trained_context(config)
    layers = _int(text.get("num_hidden_layers")) or _int(text.get("n_layer"))
    heads = _int(text.get("num_attention_heads")) or _int(text.get("n_head"))
    if kv_cache_dtype in {"", "auto"}:
        kv_cache_dtype = str(text.get("torch_dtype") or text.get("dtype") or "bfloat16")
    element = HF_DTYPE_BYTES.get(kv_cache_dtype.removeprefix("torch."), 2)
    source = f"config.json ({text.get('model_type') or 'unknown family'})"
    if not layers:
        return ModelMemoryProfile(trained, None, weights_bytes=weights_bytes, source=source)
    lora_rank = _int(text.get("kv_lora_rank"))
    if lora_rank:
        # Multi-head latent attention caches one compressed latent plus the rope key.
        per_layer = (lora_rank + int(text.get("qk_rope_head_dim") or 0)) * element
    else:
        kv_heads = (
            _int(text.get("num_key_value_heads"))
            or _int(text.get("multi_query_group_num"))
            or (1 if text.get("multi_query") else None)
            or heads
        )
        hidden = _int(text.get("hidden_size")) or _int(text.get("n_embd"))
        head_dim = _int(text.get("head_dim")) or (hidden // heads if hidden and heads else None)
        if not kv_heads or not head_dim:
            return ModelMemoryProfile(trained, None, weights_bytes=weights_bytes, source=source)
        per_layer = 2 * kv_heads * head_dim * element
    has_kv, sliding = _hf_sliding_pattern(text, layers)
    full = sum(1 for kv, swa in zip(has_kv, sliding, strict=False) if kv and not swa)
    swa = sum(1 for kv, flag in zip(has_kv, sliding, strict=False) if kv and flag)
    window = (_int(text.get("sliding_window")) or 0) if swa else 0
    return ModelMemoryProfile(
        trained_context=trained,
        full_bytes_per_token=int(full * per_layer),
        sliding_bytes_per_token=int(swa * per_layer),
        sliding_window=window,
        weights_bytes=weights_bytes,
        source=source,
    )


def _gguf_key(metadata: Mapping[str, Any], suffix: str) -> Any:
    architecture = metadata.get("general.architecture")
    if architecture:
        value = metadata.get(f"{architecture}.{suffix}")
        if value is not None:
            return value
    return next((v for k, v in metadata.items() if k.endswith(f".{suffix}")), None)


def gguf_profile(
    metadata: Mapping[str, Any],
    *,
    weights_bytes: int = 0,
    cache_type_k: str = "f16",
    cache_type_v: str = "f16",
    source: str = "GGUF metadata",
) -> ModelMemoryProfile:
    """A profile from GGUF metadata as llama.cpp (and Ollama) size the KV cache.

    Args:
        metadata: The GGUF key/value metadata (``<arch>.block_count`` ...).
        weights_bytes: The model file's size.
        cache_type_k: llama.cpp ``--cache-type-k`` (Ollama ``OLLAMA_KV_CACHE_TYPE``).
        cache_type_v: llama.cpp ``--cache-type-v``.
        source: Where the metadata came from (shown in reasons).
    """

    architecture = str(metadata.get("general.architecture") or "")
    trained = _int(_gguf_key(metadata, "context_length"))
    layers = _int(_gguf_key(metadata, "block_count"))
    label = f"{source} ({architecture or 'unknown architecture'})"
    if not layers:
        return ModelMemoryProfile(trained, None, weights_bytes=weights_bytes, source=label)
    heads_raw = _gguf_key(metadata, "attention.head_count")
    kv_raw = _gguf_key(metadata, "attention.head_count_kv")
    heads = _int(heads_raw)
    if heads is None and isinstance(heads_raw, list) and heads_raw:
        heads = max(int(value or 0) for value in heads_raw) or None
    if isinstance(kv_raw, list):
        per_layer_kv = [int(value or 0) for value in kv_raw[:layers]]
    else:
        per_layer_kv = [_int(kv_raw) or heads or 0] * layers
    embedding = _int(_gguf_key(metadata, "embedding_length"))
    fallback_dim = embedding // heads if embedding and heads else None
    key_length = _int(_gguf_key(metadata, "attention.key_length")) or fallback_dim
    value_length = _int(_gguf_key(metadata, "attention.value_length")) or fallback_dim
    if not key_length or not value_length or not any(per_layer_kv):
        return ModelMemoryProfile(trained, None, weights_bytes=weights_bytes, source=label)
    k_bytes = GGML_CACHE_BYTES.get(cache_type_k, 2)
    v_bytes = GGML_CACHE_BYTES.get(cache_type_v, 2)
    window = _int(_gguf_key(metadata, "attention.sliding_window")) or 0
    pattern = _gguf_key(metadata, "attention.sliding_window_pattern")
    sliding: list[bool] | None = None
    if window and isinstance(pattern, list) and pattern:
        sliding = [bool(flag) for flag in pattern[:layers]]
    elif window and isinstance(_int(pattern), int):
        sliding = _periodic(layers, int(pattern))
    elif window and architecture in GGUF_SWA_PERIOD:
        sliding = _periodic(layers, GGUF_SWA_PERIOD[architecture])
    full_bytes = 0.0
    swa_bytes = 0.0
    for index, kv_heads in enumerate(per_layer_kv):
        cost = kv_heads * (key_length * k_bytes + value_length * v_bytes)
        if sliding is not None and index < len(sliding) and sliding[index]:
            swa_bytes += cost
        else:
            full_bytes += cost
    return ModelMemoryProfile(
        trained_context=trained,
        full_bytes_per_token=int(full_bytes),
        sliding_bytes_per_token=int(swa_bytes),
        sliding_window=window if swa_bytes else 0,
        weights_bytes=weights_bytes,
        source=label,
    )


def ollama_profile(
    model_info: Mapping[str, Any], *, weights_bytes: int = 0, cache_type: str = "f16"
) -> ModelMemoryProfile:
    """A profile from Ollama ``/api/show`` ``model_info`` (GGUF metadata keys)."""

    return gguf_profile(
        model_info,
        weights_bytes=weights_bytes,
        cache_type_k=cache_type,
        cache_type_v=cache_type,
        source="Ollama /api/show model_info",
    )


__all__ = [
    "GGML_CACHE_BYTES",
    "HF_DTYPE_BYTES",
    "gguf_profile",
    "hf_profile",
    "hf_trained_context",
    "ollama_profile",
]
