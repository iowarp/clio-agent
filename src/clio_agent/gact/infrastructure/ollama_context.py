"""The context a managed Ollama model runs with when the person sets none.

Ollama's own default (``OLLAMA_CONTEXT_LENGTH`` unset) is a VRAM-tiered value
far below what current models are trained for (qwen3:4b: 32768 served, 262144
trained). An agent needs the model's own context, but the KV cache for it must
fit beside the weights, or Ollama silently offloads layers to the CPU. This
module derives the default from what Ollama reports about the pulled model
(``/api/show`` ``model_info``): the trained context, capped to the GPU's KV
budget, with the reason stated so the effective value can be shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Contexts are floored to a multiple of this many tokens.
CONTEXT_GRANULE = 4096
#: Memory kept free beside the weights and KV cache (compute graph, CUDA context).
HEADROOM_BYTES = 1 << 30
#: The weights' resident size is grown by this factor for their runtime buffers.
WEIGHTS_OVERHEAD = 1.25
#: f16 KV cache: bytes per element (Ollama's default cache type).
KV_ELEMENT_BYTES = 2
#: The ceiling when no GPU budget is known: the KV cache then lands in system RAM.
UNKNOWN_BUDGET_CAP = 32768


@dataclass(frozen=True)
class ContextDefault:
    """The chosen context and why.

    Attributes:
        tokens: The context length to serve the model with.
        trained: The model's trained context length.
        reason: A sentence a person can read next to the value.
    """

    tokens: int
    trained: int
    reason: str


def _model_key(model_info: dict[str, Any], suffix: str) -> int | None:
    """``<architecture>.<suffix>`` from ``model_info`` as an int, if present."""
    architecture = model_info.get("general.architecture")
    value = model_info.get(f"{architecture}.{suffix}") if architecture else None
    if value is None:
        value = next((v for k, v in model_info.items() if k.endswith(f".{suffix}")), None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def kv_bytes_per_token(model_info: dict[str, Any]) -> int | None:
    """f16 KV-cache bytes one token costs across every layer, or None when unknown."""
    layers = _model_key(model_info, "block_count")
    kv_heads = _model_key(model_info, "attention.head_count_kv")
    key_length = _model_key(model_info, "attention.key_length")
    value_length = _model_key(model_info, "attention.value_length")
    if not all((layers, kv_heads, key_length, value_length)):
        return None
    return (key_length + value_length) * kv_heads * layers * KV_ELEMENT_BYTES  # type: ignore[operator]


def ollama_context_default(
    model_info: dict[str, Any],
    gpu_free_bytes: int | None,
    weights_bytes: int,
    num_parallel: int = 1,
) -> ContextDefault | None:
    """The model's trained context, capped to what its KV cache can hold on the GPU.

    Args:
        model_info: Ollama ``/api/show`` ``model_info`` for the pulled model.
        gpu_free_bytes: Free GPU memory before the model loads; None when unknown
            (the context is then capped at :data:`UNKNOWN_BUDGET_CAP`).
        weights_bytes: The model's size (``/api/tags`` ``size``).
        num_parallel: Sequences Ollama allocates a context for at once.

    Returns:
        The default, or None when the model does not report its trained context
        (Ollama's own default then stays in force).
    """
    trained = _model_key(model_info, "context_length")
    if not trained:
        return None
    per_token = kv_bytes_per_token(model_info)
    if per_token is None or gpu_free_bytes is None:
        if trained <= UNKNOWN_BUDGET_CAP:
            return ContextDefault(trained, trained, f"model's trained context {trained}")
        return ContextDefault(
            UNKNOWN_BUDGET_CAP,
            trained,
            f"trained context {trained} capped to {UNKNOWN_BUDGET_CAP}: "
            "no GPU memory budget is known to size the KV cache against",
        )
    budget = gpu_free_bytes - int(weights_bytes * WEIGHTS_OVERHEAD) - HEADROOM_BYTES
    fit = max(budget, 0) // (per_token * max(num_parallel, 1))
    fit -= fit % CONTEXT_GRANULE
    if fit >= trained:
        return ContextDefault(trained, trained, f"model's trained context {trained}")
    tokens = max(fit, CONTEXT_GRANULE)
    gib = gpu_free_bytes / (1 << 30)
    return ContextDefault(
        tokens,
        trained,
        f"trained context {trained} capped to {tokens} so the KV cache fits "
        f"the GPU's {gib:.1f} GiB beside the weights",
    )
