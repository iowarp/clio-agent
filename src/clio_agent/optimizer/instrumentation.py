"""Per-expert performance metrics from ARC (feeds ``/metrics``).

Research-pending (#801; tracked in
https://github.com/iowarp/clio-agent/issues/633): :class:`MetricsAggregator`
computes per-expert metrics from the invocation records ARC holds; it is the live
half of the optimizer vertical.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class MetricsAggregator:
    """Computes per-expert metrics from ARC invocation history.

    Scans invocations on disk for a given agent_id and computes
    success_rate, avg_latency_ms, total_invocations, and cache_hit_rate.

    Args:
        arc_memory: ARCMemory instance

    Example:
        >>> aggregator = MetricsAggregator(arc)
        >>> metrics = aggregator.compute_expert_metrics("data")
        >>> print(f"Success rate: {metrics['success_rate']:.2%}")
    """

    def __init__(self, arc_memory: Any) -> None:
        """Initialize MetricsAggregator.

        Args:
            arc_memory: ARCMemory instance for querying invocations
        """
        self._arc = arc_memory

    def compute_expert_metrics(self, agent_id: str) -> Dict[str, Any]:
        """Compute aggregate metrics for an expert.

        Scans all invocations for the given agent_id and computes:
        - success_rate: fraction of successful invocations
        - avg_latency_ms: average duration in milliseconds
        - total_invocations: total count
        - cache_hit_rate: from ARC tool cache stats

        Args:
            agent_id: Expert identifier (e.g., "data", "analysis")

        Returns:
            Dict with success_rate, avg_latency_ms, total_invocations,
            cache_hit_rate keys

        Example:
            >>> metrics = aggregator.compute_expert_metrics("data")
            >>> assert 0.0 <= metrics["success_rate"] <= 1.0
        """
        invocations = self._arc.get_invocations_by_agent(agent_id)

        if not invocations:
            return {
                "success_rate": 0.0,
                "avg_latency_ms": 0.0,
                "total_invocations": 0,
                "cache_hit_rate": 0.0,
            }

        total = len(invocations)
        successes = sum(1 for inv in invocations if inv.status == "success")
        total_latency = sum(inv.duration_ms for inv in invocations)

        # Get cache stats from ARC
        cache_stats = self._arc.get_tool_cache_stats()

        return {
            "success_rate": successes / total if total > 0 else 0.0,
            "avg_latency_ms": total_latency / total if total > 0 else 0.0,
            "total_invocations": total,
            "cache_hit_rate": cache_stats.get("tool_cache_hit_rate", 0.0),
        }
