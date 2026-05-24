"""SIMBA optimization runner with statistical significance testing.

Provides SIMBARunner for executing the full optimization pipeline:
evaluate before score, run SIMBA compilation, evaluate after score,
test statistical significance, and save the optimized variant.

Key design decisions:
- num_threads=1 for Evaluate to avoid sync tool executor contention (Pitfall 6)
- 20%/80% train/validation split per DSPy recommendation
- scipy imported lazily inside test_significance (optional dependency)
- Two-proportion z-test for statistical significance (Pattern 5 from research)
"""

import math
from typing import Any, Callable

import dspy

from clio_agent.optimizer.trainer import clio_expert_metric
from clio_agent.optimizer.variants import VariantManager


class SIMBARunner:
    """Runs SIMBA optimization on expert modules with statistical validation.

    Executes the full optimization pipeline: split data, evaluate baseline,
    run SIMBA compile, evaluate optimized, test significance, save variant.

    Args:
        arc_memory: ARCMemory instance
        variant_manager: VariantManager for saving optimized variants

    Example:
        >>> from clio_agent.arc.memory import ARCMemory
        >>> from clio_agent.optimizer.variants import VariantManager
        >>> arc = ARCMemory()
        >>> vm = VariantManager(arc)
        >>> runner = SIMBARunner(arc, vm)
        >>> result = runner.run(expert, "data", trainset, max_steps=12)
        >>> print(f"Improvement: {result['improvement_delta']:.2%}")
    """

    def __init__(
        self,
        arc_memory: Any,
        variant_manager: VariantManager,
    ) -> None:
        """Initialize SIMBARunner.

        Args:
            arc_memory: ARCMemory instance
            variant_manager: VariantManager for saving optimized variants
        """
        self._arc = arc_memory
        self._variant_manager = variant_manager

    def run(
        self,
        expert_module: dspy.Module,
        agent_id: str,
        trainset: list[dspy.Example],
        metric_fn: Callable | None = None,
        max_steps: int = 12,
        max_demos: int = 10,
    ) -> dict[str, Any]:
        """Run full SIMBA optimization pipeline.

        Steps:
        1. Split trainset 20% train / 80% validation
        2. Evaluate BEFORE score on validation set
        3. Run dspy.SIMBA compile on training split
        4. Evaluate AFTER score on validation set
        5. Test statistical significance (two-proportion z-test)
        6. Save variant via VariantManager

        Args:
            expert_module: dspy.Module to optimize
            agent_id: Expert identifier (e.g., "data", "analysis")
            trainset: List of dspy.Example training examples
            metric_fn: Metric function (default: clio_expert_metric)
            max_steps: Maximum SIMBA optimization steps (default: 12)
            max_demos: Maximum demos per predictor (default: 10)

        Returns:
            Dict with keys: optimized, before_score, after_score,
            improvement_delta, p_value, is_significant, variant_record,
            train_size, val_size

        Raises:
            ValueError: If trainset has fewer than 5 examples

        Example:
            >>> result = runner.run(expert, "data", trainset)
            >>> if result["is_significant"]:
            ...     vm.deploy(result["variant_record"].variant_id, "data")
        """
        if len(trainset) < 5:
            raise ValueError(
                f"Need at least 5 training examples for 20/80 split. Got {len(trainset)}."
            )

        if metric_fn is None:
            metric_fn = clio_expert_metric

        # Step 1: Split trainset 20% train / 80% validation
        split_idx = max(1, len(trainset) // 5)  # at least 1 for train
        train_split = trainset[:split_idx]
        val_split = trainset[split_idx:]

        # Step 2: Evaluate BEFORE score
        evaluator = dspy.evaluate.Evaluate(
            devset=val_split,
            metric=metric_fn,
            num_threads=1,  # Pitfall 6: sync tool executor threading
            display_progress=True,
            display_table=0,
        )
        before_score = evaluator(expert_module)

        # Step 3: Run SIMBA
        optimizer = dspy.SIMBA(
            metric=metric_fn,
            max_steps=max_steps,
            max_demos=max_demos,
        )
        optimized = optimizer.compile(
            student=expert_module,
            trainset=train_split,
        )

        # Step 4: Evaluate AFTER score
        after_score = evaluator(optimized)

        # Step 5: Statistical significance test
        is_significant, p_value, improvement_delta = self.test_significance(
            before_score=before_score / 100.0 if before_score > 1 else before_score,
            before_total=len(val_split),
            after_score=after_score / 100.0 if after_score > 1 else after_score,
            after_total=len(val_split),
        )

        # Step 6: Save variant
        variant_record = self._variant_manager.save_variant(
            module=optimized,
            agent_id=agent_id,
            before_score=before_score,
            after_score=after_score,
            training_examples=len(trainset),
            p_value=p_value,
            is_significant=is_significant,
        )

        return {
            "optimized": optimized,
            "before_score": before_score,
            "after_score": after_score,
            "improvement_delta": improvement_delta,
            "p_value": p_value,
            "is_significant": is_significant,
            "variant_record": variant_record,
            "train_size": len(train_split),
            "val_size": len(val_split),
        }

    @staticmethod
    def test_significance(
        before_score: float,
        before_total: int,
        after_score: float,
        after_total: int,
        alpha: float = 0.05,
    ) -> tuple[bool, float, float]:
        """Test statistical significance using two-proportion z-test.

        Implements the two-proportion z-test from research Pattern 5.
        Converts scores to success counts and tests whether the after
        proportion is significantly higher than the before proportion.

        Args:
            before_score: Success rate before optimization (0.0-1.0)
            before_total: Total samples in before evaluation
            after_score: Success rate after optimization (0.0-1.0)
            after_total: Total samples in after evaluation
            alpha: Significance level (default: 0.05)

        Returns:
            Tuple of (is_significant, p_value, improvement_delta)

        Example:
            >>> is_sig, p, delta = SIMBARunner.test_significance(0.6, 100, 0.8, 100)
            >>> print(f"Significant: {is_sig}, p={p:.4f}, delta={delta:.2%}")
        """
        try:
            from scipy.stats import norm
        except ImportError as err:
            raise ImportError(
                "scipy is required for statistical significance testing. "
                "Install it: uv pip install -e '.[optimizers]'"
            ) from err

        improvement_delta = after_score - before_score

        # Convert to success counts
        before_successes = round(before_score * before_total)
        after_successes = round(after_score * after_total)

        # Pooled proportion
        total_successes = before_successes + after_successes
        total_n = before_total + after_total

        if total_n == 0:
            return (False, 1.0, 0.0)

        pooled_p = total_successes / total_n

        # Standard error
        se_term = pooled_p * (1 - pooled_p)
        if se_term <= 0:
            # All successes or all failures -- no variance
            return (False, 1.0, improvement_delta)

        se = math.sqrt(se_term * (1 / before_total + 1 / after_total))

        if se == 0:
            return (False, 1.0, improvement_delta)

        # Z-score (one-sided: after > before)
        z = (after_score - before_score) / se

        # P-value (one-sided)
        p_value = float(1 - norm.cdf(z))

        is_significant = bool(p_value < alpha)

        return (is_significant, p_value, improvement_delta)
