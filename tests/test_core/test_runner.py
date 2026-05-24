"""Tests for SIMBARunner -- optimization pipeline with statistical testing.

Tests cover:
- test_significance with known significant improvement (60% vs 80% on 100)
- test_significance with insignificant improvement (60% vs 62% on 50)
- test_significance with identical scores
- test_significance with zero standard error
- run() with mocked SIMBA and Evaluate
- run() rejects trainset smaller than 5
- run() returns all expected keys
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import dspy
import pytest

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import VariantRecord
from clio_agent.optimizer.runner import SIMBARunner
from clio_agent.optimizer.variants import VariantManager


class MockModule(dspy.Module):
    """Mock dspy.Module for testing."""

    def __init__(self):
        super().__init__()

    def save(self, path: str, *args, **kwargs) -> None:
        Path(path).write_text('{"mock": true}')

    def load(self, path: str, *args, **kwargs) -> None:
        pass

    def forward(self, **kwargs):
        pass


@pytest.fixture
def arc(tmp_path):
    return ARCMemory(data_dir=str(tmp_path / "arc"))


@pytest.fixture
def vm(arc, tmp_path):
    return VariantManager(arc, variants_dir=str(tmp_path / "variants"))


@pytest.fixture
def runner(arc, vm):
    return SIMBARunner(arc, vm)


class TestStatisticalSignificance:
    """Tests for SIMBARunner.test_significance."""

    def test_significant_improvement(self, runner):
        """60% vs 80% on 100 samples should be significant."""
        is_sig, p_value, delta = runner.test_significance(
            before_score=0.60,
            before_total=100,
            after_score=0.80,
            after_total=100,
        )

        assert is_sig is True
        assert p_value < 0.05
        assert delta == pytest.approx(0.20)

    def test_insignificant_improvement(self, runner):
        """60% vs 62% on 50 samples should NOT be significant."""
        is_sig, p_value, delta = runner.test_significance(
            before_score=0.60,
            before_total=50,
            after_score=0.62,
            after_total=50,
        )

        assert is_sig is False
        assert p_value >= 0.05
        assert delta == pytest.approx(0.02)

    def test_identical_scores(self, runner):
        """Identical scores should NOT be significant, p_value=1.0."""
        is_sig, p_value, delta = runner.test_significance(
            before_score=0.70,
            before_total=100,
            after_score=0.70,
            after_total=100,
        )

        assert is_sig is False
        # p-value for z=0 in one-sided test is 0.5
        assert p_value >= 0.05
        assert delta == pytest.approx(0.0)

    def test_zero_standard_error(self, runner):
        """All successes: pooled_p=1.0 => se_term=0 => (False, 1.0, 0.0)."""
        is_sig, p_value, delta = runner.test_significance(
            before_score=1.0,
            before_total=50,
            after_score=1.0,
            after_total=50,
        )

        assert is_sig is False
        assert p_value == 1.0
        assert delta == pytest.approx(0.0)

    def test_zero_total_samples(self, runner):
        """Zero total samples returns (False, 1.0, 0.0)."""
        is_sig, p_value, delta = runner.test_significance(
            before_score=0.0,
            before_total=0,
            after_score=0.0,
            after_total=0,
        )

        assert is_sig is False
        assert p_value == 1.0
        assert delta == 0.0


class TestRun:
    """Tests for SIMBARunner.run."""

    def test_run_rejects_small_trainset(self, runner):
        """run() rejects trainset with fewer than 5 examples."""
        small_trainset = [
            dspy.Example(question="q", analysis="a").with_inputs("question") for _ in range(4)
        ]

        with pytest.raises(ValueError, match="at least 5"):
            runner.run(MockModule(), "data", small_trainset)

    @patch("clio_agent.optimizer.runner.dspy.SIMBA")
    @patch("clio_agent.optimizer.runner.dspy.evaluate.Evaluate")
    def test_run_full_pipeline(self, mock_evaluate_cls, mock_simba_cls, runner, tmp_path):
        """run() executes full pipeline with mocked SIMBA and Evaluate."""
        # Set up mock Evaluate: returns 60.0 first (before), then 85.0 (after)
        mock_evaluator = MagicMock()
        mock_evaluator.side_effect = [60.0, 85.0]
        mock_evaluate_cls.return_value = mock_evaluator

        # Set up mock SIMBA
        optimized_module = MockModule()
        mock_optimizer = MagicMock()
        mock_optimizer.compile.return_value = optimized_module
        mock_simba_cls.return_value = mock_optimizer

        # Create trainset with 10 examples
        trainset = [
            dspy.Example(question=f"q{i}", analysis=f"a{i}").with_inputs("question")
            for i in range(10)
        ]

        result = runner.run(MockModule(), "data", trainset)

        # Verify SIMBA was called
        mock_simba_cls.assert_called_once()
        mock_optimizer.compile.assert_called_once()

        # Verify result dict has all expected keys
        expected_keys = {
            "optimized",
            "before_score",
            "after_score",
            "improvement_delta",
            "p_value",
            "is_significant",
            "variant_record",
            "train_size",
            "val_size",
        }
        assert set(result.keys()) == expected_keys

        # Verify scores
        assert result["before_score"] == 60.0
        assert result["after_score"] == 85.0

        # Verify variant was saved
        assert isinstance(result["variant_record"], VariantRecord)
        assert result["variant_record"].variant_id == "data_v1"
        assert result["variant_record"].before_score == 60.0
        assert result["variant_record"].after_score == 85.0

        # Verify train/val split sizes (10 examples, 20/80 = 2/8)
        assert result["train_size"] == 2
        assert result["val_size"] == 8

    @patch("clio_agent.optimizer.runner.dspy.SIMBA")
    @patch("clio_agent.optimizer.runner.dspy.evaluate.Evaluate")
    def test_run_saves_variant_with_correct_args(
        self, mock_evaluate_cls, mock_simba_cls, runner, tmp_path
    ):
        """run() calls variant_manager.save_variant with correct arguments."""
        mock_evaluator = MagicMock()
        mock_evaluator.side_effect = [50.0, 70.0]
        mock_evaluate_cls.return_value = mock_evaluator

        optimized = MockModule()
        mock_optimizer = MagicMock()
        mock_optimizer.compile.return_value = optimized
        mock_simba_cls.return_value = mock_optimizer

        trainset = [
            dspy.Example(question=f"q{i}", analysis=f"a{i}").with_inputs("question")
            for i in range(20)
        ]

        metric_fn = MagicMock()
        result = runner.run(MockModule(), "analysis", trainset, metric_fn=metric_fn)

        # Variant saved with correct agent_id and training_examples
        assert result["variant_record"].agent_id == "analysis"
        assert result["variant_record"].training_examples == 20
        assert result["variant_record"].before_score == 50.0
        assert result["variant_record"].after_score == 70.0

        # Evaluate was constructed with the custom metric_fn
        mock_evaluate_cls.assert_called()
        call_kwargs = mock_evaluate_cls.call_args
        assert (
            call_kwargs.kwargs.get("metric") == metric_fn
            or call_kwargs[1].get("metric") == metric_fn
        )
