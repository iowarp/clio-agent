"""Engine-neutral context sizing: what a model's context costs and how long to make it.

* :mod:`.profile` -- what a model's KV cache costs (:class:`ModelMemoryProfile`)
  and a deployment's GPU budget (:class:`GpuBudget`);
* :mod:`.adapters` -- profiles from a Hugging Face ``config.json`` (vLLM), GGUF
  metadata (llama.cpp) and Ollama ``model_info``;
* :mod:`.strategies` -- the Fit-to-GPU strategy registry (default
  ``fit_to_gpu``, chosen by ``lm.context_sizing_strategy``);
* :mod:`.controls` -- the wire shape a client renders (number, Max, Fit to GPU).

Two consumers apply it: CLIO-managed servers, where the value becomes the
launch setting (:mod:`clio_agent.gact.infrastructure.context_sizing`), and
models CLIO binds but cannot configure, where it is CLIO's working context
(:mod:`clio_agent.gact.providers.working_context`).
"""

from clio_agent.context_sizing.profile import GpuBudget, ModelMemoryProfile
from clio_agent.context_sizing.strategies import (
    ContextStrategy,
    SizedContext,
    register_strategy,
    strategies,
)

__all__ = [
    "ContextStrategy",
    "GpuBudget",
    "ModelMemoryProfile",
    "SizedContext",
    "register_strategy",
    "strategies",
]
