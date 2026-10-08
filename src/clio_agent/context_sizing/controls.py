"""The context control a client renders: a number, a Max button and a Fit-to-GPU selector.

One wire shape serves both semantics:

* ``deployment`` -- a CLIO-managed server: the value becomes its launch
  setting (vLLM ``--max-model-len``, llama.cpp ``--ctx-size``, Ollama
  ``num_ctx``);
* ``model`` -- a model CLIO binds but cannot configure (a cloud model, a server
  someone else runs): the value is CLIO's working context for it, what the
  agent loop and compaction budget against, bounded by the reported maximum.

"Max" and a typed number are always offered; "Fit to GPU" only when its
inputs are known (``fit_to_gpu.available``), otherwise ``fit_to_gpu.reason``
says why, and a client hides it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from clio_agent.context_sizing.strategies import (
    configured_strategy_id,
    strategies,
)

ContextChoice = Literal["number", "max", "fit_to_gpu"]
ContextSemantics = Literal["deployment", "model"]

#: Deployment configuration keys of the context control (beside the engine's
#: own ``param.<context parameter>`` carrying a typed number).
CHOICE_KEY = "context.choice"
STRATEGY_KEY = "context.strategy"
SHARE_KEY = "context.gpu_share"


class ContextStrategyInfo(BaseModel):
    """One selectable Fit-to-GPU strategy."""

    id: str
    label: str
    description: str


class FitToGpu(BaseModel):
    """Whether Fit to GPU can be offered, with which strategies, and what it gives."""

    available: bool
    #: Why it cannot be computed, or how the value was computed.
    reason: str = ""
    strategy: str = ""
    strategies: list[ContextStrategyInfo] = Field(default_factory=list)
    value: int | None = None


class ContextControls(BaseModel):
    """The context control for one deployment or one bound model."""

    semantics: ContextSemantics
    #: The model's own maximum (trained or reported); None when unknown.
    maximum: int | None = None
    maximum_reason: str = ""
    minimum: int = 256
    #: The context in force (or that will be), and how it was chosen.
    current: int | None = None
    current_choice: ContextChoice | None = None
    current_reason: str = ""
    fit_to_gpu: FitToGpu


class ContextSizingSpec(BaseModel):
    """What a deployment form needs to render the context control of one engine.

    Attached to the engine's context :class:`ServerParameter` (``context_sizing``):
    the number goes in that parameter as before; the choice, strategy and GPU
    share travel under the configuration keys named here. ``preview_path``
    computes the concrete values for a model before deploying.
    """

    choice_key: str = CHOICE_KEY
    strategy_key: str = STRATEGY_KEY
    share_key: str = SHARE_KEY
    choices: list[ContextChoice] = Field(default_factory=lambda: ["number", "max", "fit_to_gpu"])
    default_choice: ContextChoice = "fit_to_gpu"
    default_strategy: str = ""
    strategies: list[ContextStrategyInfo] = Field(default_factory=list)
    fit_to_gpu_available: bool = False
    fit_to_gpu_reason: str = ""
    preview_path: str = ""


def strategy_infos() -> list[ContextStrategyInfo]:
    """Every registered strategy as a selector row (the default first)."""

    return [
        ContextStrategyInfo(id=item.id, label=item.label, description=item.description)
        for item in strategies()
    ]


def unavailable_fit(reason: str, strategy: str = "") -> FitToGpu:
    """A Fit-to-GPU block that cannot be offered, saying why."""

    return FitToGpu(
        available=False,
        reason=reason,
        strategy=strategy or configured_strategy_id(),
        strategies=strategy_infos(),
    )


__all__ = [
    "CHOICE_KEY",
    "SHARE_KEY",
    "STRATEGY_KEY",
    "ContextChoice",
    "ContextControls",
    "ContextSemantics",
    "ContextSizingSpec",
    "ContextStrategyInfo",
    "FitToGpu",
    "strategy_infos",
    "unavailable_fit",
]
