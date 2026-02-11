"""Optimizer package for CLIO Agent self-improvement.

Provides instrumentation, training set generation, metric functions,
SIMBA optimization runner, and variant management for expert modules.

Exports:
    instrumented_forward: Decorator that logs expert invocations to ARC
    MetricsAggregator: Computes per-expert performance metrics from ARC
    TrainingSetGenerator: Converts ARC invocations to dspy.Example lists
    clio_expert_metric: Multi-signal metric function for SIMBA optimization
    SIMBARunner: Runs SIMBA optimization with statistical validation
    VariantManager: Saves, loads, deploys, and rolls back optimized variants
"""

from clio_agent.optimizer.instrumentation import MetricsAggregator, instrumented_forward
from clio_agent.optimizer.runner import SIMBARunner
from clio_agent.optimizer.trainer import TrainingSetGenerator, clio_expert_metric
from clio_agent.optimizer.variants import VariantManager

__all__ = [
    "instrumented_forward",
    "MetricsAggregator",
    "TrainingSetGenerator",
    "clio_expert_metric",
    "SIMBARunner",
    "VariantManager",
]
