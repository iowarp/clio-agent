"""Expert call instrumentation for optimization data collection.

Research-pending (#801; tracked in
https://github.com/iowarp/clio-agent/issues/633): this is the live half of
the optimizer vertical — per-turn invocation records are written by
``ClioAgent._store_expert_invocation`` (which reuses ``_extract_output``
here) and ``MetricsAggregator`` feeds ``/metrics`` today. The
``instrumented_forward`` decorator itself has no callers in the current
blueprint runtime.

Provides a decorator to wrap expert forward() calls, logging Invocation
records to ARC memory with input, output, status, duration, and agent_id.
Also provides MetricsAggregator for computing per-expert performance metrics.

The instrumented data becomes the training set for SIMBA optimization.
"""

import functools
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict

from clio_agent.arc.schema import Invocation

logger = logging.getLogger(__name__)


def instrumented_forward(arc_memory: Any, agent_id: str) -> Callable:
    """Decorator that wraps expert forward() calls to log Invocations to ARC.

    Captures input (question, file_context), output (string fields from
    dspy.Prediction, truncated to 500 chars), status (success/failure),
    duration_ms, and agent_id. Uses try/finally to log even on failure.

    Args:
        arc_memory: ARCMemory instance for storing invocations
        agent_id: Expert identifier (e.g., "data", "analysis", "visualization")

    Returns:
        Decorator function

    Example:
        >>> @instrumented_forward(arc, "data")
        ... def forward(self, question, file_context=""):
        ...     return dspy.Prediction(analysis="...", recommendations="...")
    """

    def decorator(forward_fn: Callable) -> Callable:
        @functools.wraps(forward_fn)
        def wrapper(*args, **kwargs):
            start = time.time()
            start_ns = time.perf_counter_ns()
            trace_id = str(uuid.uuid4())

            # Extract input fields from args/kwargs
            # forward(question, file_context="") or forward(self, question, ...)
            input_data = _extract_input(args, kwargs)
            session_id = kwargs.get("session_id", "default")

            status = "failure"
            output_data: Dict[str, Any] = {}

            try:
                result = forward_fn(*args, **kwargs)
                status = "success"
                output_data = _extract_output(result)
                return result
            except Exception as e:
                status = "failure"
                output_data = {"error": str(e)[:500]}
                raise
            finally:
                completed_at = time.time()
                duration_ms = max(
                    (time.perf_counter_ns() - start_ns) / 1_000_000,
                    0.001,
                )
                invocation = Invocation(
                    trace_id=trace_id,
                    session_id=session_id,
                    parent_trace_id=None,
                    agent_id=agent_id,
                    tier=2,
                    source="native",
                    started_at=start,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                    status=status,
                    input=input_data,
                    output=output_data,
                    tools_called=[],
                    nanoagents_spawned=[],
                    performance={"success": status == "success", "duration_ms": duration_ms},
                    storage_tier="warm",
                )
                arc_memory.store_invocation(invocation)

        return wrapper

    return decorator


def _extract_input(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Extract input fields from forward() call arguments.

    Handles both bound method calls (self, question, ...) and
    plain function calls (question, ...).

    Args:
        args: Positional arguments
        kwargs: Keyword arguments

    Returns:
        Dict with question and file_context fields
    """
    input_data: Dict[str, Any] = {}

    # Try kwargs first
    if "question" in kwargs:
        input_data["question"] = str(kwargs["question"])[:500]
    elif len(args) >= 2:
        # Bound method: (self, question, ...)
        input_data["question"] = str(args[1])[:500]
    elif len(args) >= 1:
        # Plain function: (question, ...)
        input_data["question"] = str(args[0])[:500]

    if "file_context" in kwargs:
        input_data["file_context"] = str(kwargs["file_context"])[:500]
    elif len(args) >= 3:
        input_data["file_context"] = str(args[2])[:500]

    return input_data


def _extract_output(result: Any) -> Dict[str, Any]:
    """Extract string fields from a dspy.Prediction result.

    DSPy Predictions are not msgspec-serializable, so we extract
    string field values and truncate to 500 chars.

    Args:
        result: dspy.Prediction or similar object with string attributes

    Returns:
        Dict of field_name -> truncated string value
    """
    output_data: Dict[str, Any] = {}

    # dspy.Prediction stores fields that can be iterated
    if hasattr(result, "keys"):
        try:
            for key in result.keys():
                val = getattr(result, key, "")
                if val is not None:
                    output_data[key] = _to_safe_text(val)[:500]
        except Exception as exc:  # noqa: BLE001 - instrumentation must not fail the call
            logger.warning(
                "prediction fields not captured; invocation record will be incomplete "
                "reason=prediction_output_capture_failed result_type=%s error=%s",
                type(result).__name__,
                exc,
            )
    else:
        # Fallback: try common expert output fields
        for field in (
            "analysis",
            "recommendations",
            "visualization_description",
            "file_path",
            "answer",
        ):
            val = getattr(result, field, None)
            if val is not None:
                output_data[field] = _to_safe_text(val)[:500]

    return output_data


def _to_safe_text(value: Any) -> str:
    """Convert model/tool outputs to a stable string without serializer warnings."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:  # noqa: BLE001 - value->JSON coercion falls back to str()
            return str(value)

    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json", warnings="none")
            return json.dumps(dumped, ensure_ascii=False)
        except Exception:  # noqa: BLE001,S110 - value coercion cascade; falls through
            pass

    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content

    return str(value)


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
