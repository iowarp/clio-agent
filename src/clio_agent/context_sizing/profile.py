"""The engine-neutral inputs every context-sizing strategy works from.

A :class:`ModelMemoryProfile` says what a model's KV cache costs per token of
context, whatever engine reported it (a Hugging Face ``config.json`` for vLLM,
GGUF metadata for llama.cpp, Ollama's ``/api/show`` ``model_info``). A
:class:`GpuBudget` says how much accelerator memory one deployment may give its
KV cache. Both are plain values: the strategies in
:mod:`~clio_agent.context_sizing.strategies` are pure
functions of them.

Attention layers are counted in three kinds: full attention (the cache grows
with the whole context), sliding-window attention (it grows only up to the
window) and layers with no per-token cache (recurrent / linear-attention layers
of hybrid models), which cost nothing here.
"""

from __future__ import annotations

from dataclasses import dataclass

GIB = 1 << 30


@dataclass(frozen=True)
class ModelMemoryProfile:
    """What one model's KV cache costs, and the longest context it supports.

    Attributes:
        trained_context: The model's own maximum context (trained, or the
            derived length the engine serves it at), or None when unknown.
        full_bytes_per_token: KV-cache bytes one token costs across every
            full-attention layer; None when the layer layout is unknown.
        sliding_bytes_per_token: The same across sliding-window layers.
        sliding_window: Tokens a sliding-window layer keeps (0: none).
        weights_bytes: The weights' size on disk (0 when unknown).
        source: Where the layout was read (shown in reasons).
    """

    trained_context: int | None
    full_bytes_per_token: int | None
    sliding_bytes_per_token: int = 0
    sliding_window: int = 0
    weights_bytes: int = 0
    source: str = ""

    @property
    def layout_known(self) -> bool:
        """Whether the per-token KV cost is known."""

        return self.full_bytes_per_token is not None

    def kv_bytes(self, tokens: int) -> int | None:
        """KV-cache bytes one sequence of ``tokens`` costs, or None when unknown."""

        if self.full_bytes_per_token is None:
            return None
        window = min(tokens, self.sliding_window) if self.sliding_window else tokens
        return self.full_bytes_per_token * tokens + self.sliding_bytes_per_token * window


@dataclass(frozen=True)
class GpuBudget:
    """The accelerator memory one deployment sizes its KV cache within.

    Attributes:
        memory_bytes: Memory this deployment may use across its devices: free
            memory (or, for vLLM, the reserved fraction of each device) already
            multiplied by the deployment's GPU-memory share.
        reserved_bytes: What is not KV cache: the weights, runtime buffers and
            headroom.
        sequences: Sequences the engine allocates a full context for at once.
        detail: How ``memory_bytes`` was obtained (shown in reasons).
    """

    memory_bytes: int
    reserved_bytes: int = 0
    sequences: int = 1
    detail: str = ""

    @property
    def kv_bytes(self) -> int:
        """Bytes left for the KV cache of all sequences (never negative)."""

        return max(self.memory_bytes - self.reserved_bytes, 0)

    @property
    def per_sequence_bytes(self) -> int:
        """Bytes left for one sequence's KV cache."""

        return self.kv_bytes // max(self.sequences, 1)

    def describe(self) -> str:
        """A short phrase naming the budget, for reasons."""

        text = f"{self.memory_bytes / GIB:.1f} GiB of GPU memory"
        return f"{text} ({self.detail})" if self.detail else text


__all__ = ["GIB", "GpuBudget", "ModelMemoryProfile"]
