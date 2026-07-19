"""Optimizer package for CLIO Agent self-improvement.

Research-pending (#801 owner decision; tracked in
https://github.com/iowarp/clio-agent/issues/633): the optimizer vertical is
kept as planned research — state-of-the-art optimization semantics over the
per-turn invocation corpus — but no optimization run is wired today. Every
user-facing entry point (the gact ``/optimize`` command, the
``optimizer_command`` capability-gap row, the ``--tune`` CLI hook) returns
the uniform structured not-implemented payload from
:mod:`clio_agent.optimizer.stub`. The live pieces are the per-turn invocation
collection (the ``instrumented_forward`` decorator persisting through
``arc_memory.store_invocation``, the future training corpus) and
:class:`MetricsAggregator`, which feeds ``/metrics``.

Provides instrumentation, training set generation, metric functions,
SIMBA optimization runner, and variant management for expert modules.

Exports:
    OPTIMIZER_NOT_IMPLEMENTED_REASON / OPTIMIZER_TRACKING_ISSUE /
        optimizer_not_implemented_payload: The uniform not-implemented stub
    instrumented_forward: Decorator that logs expert invocations to ARC
    MetricsAggregator: Computes per-expert performance metrics from ARC
    TrainingSetGenerator: Converts ARC invocations to dspy.Example lists
    clio_expert_metric: Multi-signal metric function for SIMBA optimization
    SIMBARunner: Runs SIMBA optimization with statistical validation
    VariantManager: Saves, loads, deploys, and rolls back optimized variants

The research exports are resolved lazily (PEP 562) so that importing the
dependency-free :mod:`clio_agent.optimizer.stub` — which the gact runtime
leaves do at import time — never pulls DSPy.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from clio_agent.optimizer.stub import (
    OPTIMIZER_NOT_IMPLEMENTED_MESSAGE,
    OPTIMIZER_NOT_IMPLEMENTED_REASON,
    OPTIMIZER_TRACKING_ISSUE,
    optimizer_not_implemented_payload,
)

if TYPE_CHECKING:
    from clio_agent.optimizer.instrumentation import (
        MetricsAggregator,
        instrumented_forward,
    )
    from clio_agent.optimizer.runner import SIMBARunner
    from clio_agent.optimizer.trainer import TrainingSetGenerator, clio_expert_metric
    from clio_agent.optimizer.variants import VariantManager

__all__ = [
    "OPTIMIZER_NOT_IMPLEMENTED_MESSAGE",
    "OPTIMIZER_NOT_IMPLEMENTED_REASON",
    "OPTIMIZER_TRACKING_ISSUE",
    "optimizer_not_implemented_payload",
    "instrumented_forward",
    "MetricsAggregator",
    "TrainingSetGenerator",
    "clio_expert_metric",
    "SIMBARunner",
    "VariantManager",
]

_LAZY_EXPORTS: dict[str, str] = {
    "instrumented_forward": "clio_agent.optimizer.instrumentation",
    "MetricsAggregator": "clio_agent.optimizer.instrumentation",
    "TrainingSetGenerator": "clio_agent.optimizer.trainer",
    "clio_expert_metric": "clio_agent.optimizer.trainer",
    "SIMBARunner": "clio_agent.optimizer.runner",
    "VariantManager": "clio_agent.optimizer.variants",
}


def __getattr__(name: str) -> Any:
    """Resolve the research exports lazily (PEP 562)."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name), name)
