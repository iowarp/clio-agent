"""Training set generator and metric function for SIMBA optimization.

Converts ARC invocation history into dspy.Example training sets
and provides a multi-signal metric function for evaluating expert outputs.

The TrainingSetGenerator queries ARC for successful invocations, converts
them to dspy.Example objects matching expert signature fields, and enforces
minimum data requirements. The clio_expert_metric scores expert outputs
using three weighted signals.
"""

from typing import Any

import dspy

from clio_agent.arc.schema import Invocation

# Error keywords that indicate problematic output
_ERROR_KEYWORDS = frozenset(
    [
        "error:",
        "error,",
        "traceback",
        "exception:",
        "failed to",
        "could not",
        "unable to",
        "runtime error",
        "type error",
    ]
)


class TrainingSetGenerator:
    """Generates dspy.Example training sets from ARC invocation history.

    Queries ARC memory for successful invocations by agent_id, converts
    each to a dspy.Example matching expert signature fields (question,
    file_context as inputs; analysis, recommendations as labels).

    Args:
        arc_memory: ARCMemory instance

    Example:
        >>> generator = TrainingSetGenerator(arc)
        >>> examples = generator.generate("data", min_examples=30)
        >>> print(f"Generated {len(examples)} training examples")
    """

    def __init__(self, arc_memory: Any) -> None:
        """Initialize TrainingSetGenerator.

        Args:
            arc_memory: ARCMemory instance for querying invocations
        """
        self._arc = arc_memory

    def generate(self, agent_id: str, min_examples: int = 30) -> list[dspy.Example]:
        """Generate training set from ARC invocations for a specific expert.

        Calls arc_memory.get_invocations_by_agent with status="success",
        converts each invocation to a dspy.Example matching the expert
        signature fields. Returns examples sorted by started_at descending
        (most recent first).

        Args:
            agent_id: Expert identifier (e.g., "data", "analysis", "visualization")
            min_examples: Minimum required examples (default: 30)

        Returns:
            List of dspy.Example objects with question, file_context as inputs
            and analysis, recommendations (or visualization_description, file_path)
            as labels

        Raises:
            ValueError: If fewer than min_examples successful invocations found

        Example:
            >>> examples = generator.generate("data", min_examples=30)
            >>> assert len(examples) >= 30
            >>> assert "question" in examples[0].inputs()
        """
        invocations = self._arc.get_invocations_by_agent(agent_id, status="success")

        if len(invocations) < min_examples:
            raise ValueError(
                f"Need at least {min_examples} successful '{agent_id}' "
                f"invocations for training. Currently have {len(invocations)}. "
                f"Run more queries first."
            )

        examples = []
        for inv in invocations:
            example = self._invocation_to_example(inv, agent_id)
            if example is not None:
                examples.append(example)

        if len(examples) < min_examples:
            raise ValueError(
                f"Need at least {min_examples} valid training examples for "
                f"'{agent_id}'. Only {len(examples)} could be converted from "
                f"{len(invocations)} successful invocations."
            )

        return examples

    def get_available_counts(self) -> dict[str, int]:
        """Get count of available successful invocations per agent.

        Scans all invocation files and counts successful invocations
        grouped by agent_id. Useful for CLI readiness display.

        Returns:
            Dict mapping agent_id to count of successful invocations

        Example:
            >>> counts = generator.get_available_counts()
            >>> for agent, count in counts.items():
            ...     print(f"{agent}: {count} examples")
        """
        counts: dict[str, int] = {}

        for inv in self._arc.iter_invocations():
            if inv.status == "success":
                counts[inv.agent_id] = counts.get(inv.agent_id, 0) + 1

        return counts

    @staticmethod
    def _invocation_to_example(inv: Invocation, agent_id: str) -> dspy.Example | None:
        """Convert a single Invocation to a dspy.Example.

        Maps invocation input/output fields to expert signature fields.
        For data/analysis experts: question, file_context -> analysis, recommendations.
        For visualization expert: question, file_context -> visualization_description, file_path.

        Args:
            inv: Invocation object with input/output dicts
            agent_id: Expert identifier for field mapping

        Returns:
            dspy.Example with correct input/label fields, or None if conversion fails
        """
        try:
            question = inv.input.get("question", "")
            file_context = inv.input.get("file_context", "")

            if not question:
                return None

            if agent_id == "visualization":
                viz_desc = inv.output.get("visualization_description", "")
                file_path = inv.output.get("file_path", "")

                if not viz_desc:
                    return None

                return dspy.Example(
                    question=question,
                    file_context=file_context,
                    visualization_description=viz_desc,
                    file_path=file_path,
                ).with_inputs("question", "file_context")
            else:
                # data, analysis experts
                analysis = inv.output.get("analysis", "")
                recommendations = inv.output.get("recommendations", "")

                if not analysis:
                    return None

                return dspy.Example(
                    question=question,
                    file_context=file_context,
                    analysis=analysis,
                    recommendations=recommendations,
                ).with_inputs("question", "file_context")

        except Exception:
            return None


def clio_expert_metric(example: dspy.Example, pred: Any, trace: Any = None) -> float | bool:
    """Multi-signal metric for CLIO expert optimization.

    Scores expert outputs on three weighted signals:
        - Signal 1 (0.4): analysis field non-empty and >20 chars
        - Signal 2 (0.3): recommendations field non-empty and >20 chars
        - Signal 3 (0.3): no error keywords in output

    Handles VisualizationExpert outputs by mapping:
        - visualization_description -> analysis weight (Signal 1)
        - file_path existence -> recommendations weight (Signal 2)

    When trace is not None (optimization mode): returns boolean (score >= 0.7).
    When trace is None (evaluation mode): returns float score.

    Args:
        example: Ground truth example (unused in scoring, present for DSPy API)
        pred: Model prediction with analysis/recommendations or
              visualization_description/file_path fields
        trace: Optimization trace. None for evaluation mode, non-None
               for optimization mode (stricter boolean gate)

    Returns:
        Float score (0.0-1.0) in evaluation mode, or
        bool (True if score >= 0.7) in optimization mode

    Example:
        >>> score = clio_expert_metric(example, pred)  # eval mode -> float
        >>> passed = clio_expert_metric(example, pred, trace="opt")  # opt mode -> bool
    """
    score = 0.0

    # Determine field names based on what the prediction has
    # VisualizationExpert uses different field names
    analysis_text = _get_field(pred, "analysis")
    if not analysis_text:
        analysis_text = _get_field(pred, "visualization_description")

    recommendations_text = _get_field(pred, "recommendations")
    has_viz_file = bool(_get_field(pred, "file_path"))

    # Signal 1 (0.4 weight): Non-empty analysis/visualization_description > 20 chars
    if analysis_text and len(analysis_text.strip()) > 20:
        score += 0.4

    # Signal 2 (0.3 weight): Non-empty recommendations > 20 chars OR file_path exists
    if recommendations_text and len(recommendations_text.strip()) > 20:
        score += 0.3
    elif has_viz_file:
        score += 0.3

    # Signal 3 (0.3 weight): No error keywords in output
    combined_text = (analysis_text + " " + recommendations_text).lower()
    has_error = any(keyword in combined_text for keyword in _ERROR_KEYWORDS)
    if not has_error:
        score += 0.3

    if trace is not None:
        # Optimization mode: stricter boolean gate
        return score >= 0.7
    return score


def _get_field(obj: Any, field: str) -> str:
    """Safely extract a string field from a prediction object.

    Args:
        obj: Object to extract field from
        field: Field name

    Returns:
        String value or empty string if not found
    """
    val = getattr(obj, field, None)
    if val is None:
        return ""
    return str(val)
