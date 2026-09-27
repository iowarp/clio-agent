"""A catalog row's context fields: the context in force, what it rests on, and the native one.

``context_window`` alone could not tell a person whether a number is what the
server serves now, what it will apply once the model loads, or only the
model's own ceiling (Ollama before load showed the native 32768 while it would
load the model at 4096). ``context_basis`` names which:

* ``served`` -- the server reports serving it now (llama.cpp ``/props``, vLLM
  ``max_model_len``, Ollama ``/api/ps``);
* ``configured`` -- nothing is loaded yet; this is what will apply when it is
  (an Ollama Modelfile ``num_ctx`` or the server's default);
* ``native`` -- neither is known; this is the model's own ceiling.

``native_context_window`` is that ceiling, whatever the basis.
"""

from __future__ import annotations

from typing import Any, Literal

from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.combine import EffectiveCapabilities
from clio_agent.providers.capabilities.records import DeploymentCapabilities

ContextBasis = Literal["served", "configured", "native"]


def context_basis(decided_by: str) -> ContextBasis | None:
    """Which record decided the effective context (``Decision.decided_by`` owners)."""

    owners = set(decided_by.split("+"))
    if "deployment" in owners:
        return "served"
    if "configured" in owners:
        return "configured"
    if "model" in owners:
        return "native"
    return None


def context_wire(
    effective: EffectiveCapabilities, deployment: DeploymentCapabilities | None
) -> dict[str, Any]:
    """The context keys of one catalog model row."""

    served = deployment.context_served if deployment else None
    model_key = deployment.model_key.value if deployment and deployment.model_key.known else None
    model = invalidation.get_model_capabilities(model_key or effective.model_key)
    native = model.context_max.value if model is not None and model.context_max.known else None
    return {
        "context_window": effective.context.value,
        "loaded_context_window": served.value if served is not None and served.known else None,
        "native_context_window": native,
        "context_basis": context_basis(effective.context.decided_by)
        if effective.context.known
        else None,
    }


__all__ = ["ContextBasis", "context_basis", "context_wire"]
