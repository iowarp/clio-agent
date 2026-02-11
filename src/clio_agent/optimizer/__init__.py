"""Optimizer package for CLIO Agent self-improvement.

Provides instrumentation, training set generation, and metric functions
for DSPy SIMBA optimization of expert modules.

Exports:
    instrumented_forward: Decorator that logs expert invocations to ARC
    MetricsAggregator: Computes per-expert performance metrics from ARC
    TrainingSetGenerator: Converts ARC invocations to dspy.Example lists
    clio_expert_metric: Multi-signal metric function for SIMBA optimization
"""

from clio_agent.optimizer.instrumentation import MetricsAggregator, instrumented_forward
from clio_agent.optimizer.trainer import TrainingSetGenerator, clio_expert_metric

__all__ = [
    "instrumented_forward",
    "MetricsAggregator",
    "TrainingSetGenerator",
    "clio_expert_metric",
]
