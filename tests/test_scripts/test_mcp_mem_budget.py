"""The budget-gate verdict logic (#930 S1/#931) — wrong inputs included.

`check_budget` is the pure core of scripts/mcp_mem_attribution.py's
``--assert-budget``: a malformed budget or a vacuous measurement must FAIL,
never pass silently.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "mcp_mem_attribution", REPO / "scripts" / "mcp_mem_attribution.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["mcp_mem_attribution"] = _mod
_spec.loader.exec_module(_mod)

check_budget = _mod.check_budget
BUDGET_TOLERANCE = _mod.BUDGET_TOLERANCE


def test_under_budget_passes() -> None:
    ok, detail = check_budget(3.0, 2.8, {"peak_gb": 3.57, "final_gb": 3.57})
    assert ok
    assert "True" in detail


def test_within_tolerance_passes_but_over_fails() -> None:
    budget = {"peak_gb": 3.0, "final_gb": 3.0}
    ok, _ = check_budget(3.0 * BUDGET_TOLERANCE - 0.01, 2.9, budget)
    assert ok
    ok, _ = check_budget(3.0 * BUDGET_TOLERANCE + 0.01, 2.9, budget)
    assert not ok


def test_final_alone_can_fail() -> None:
    ok, _ = check_budget(2.0, 4.0, {"peak_gb": 3.57, "final_gb": 3.57})
    assert not ok


def test_zero_measurement_is_rejected() -> None:
    """A dead server tree measures 0.00 GB — that must NEVER pass."""

    ok, detail = check_budget(0.0, 0.0, {"peak_gb": 3.57, "final_gb": 3.57})
    assert not ok
    assert "non-positive" in detail
    ok, _ = check_budget(2.0, -1.0, {"peak_gb": 3.57, "final_gb": 3.57})
    assert not ok


def test_malformed_budget_is_typed_fail() -> None:
    for bad in (
        {},
        {"peak_gb": 3.5},
        {"peak_gb": "x", "final_gb": 3.5},
        {"peak_gb": None, "final_gb": 3.5},
    ):
        ok, detail = check_budget(1.0, 1.0, bad)
        assert not ok, bad
        assert "malformed budget" in detail


def test_recorded_budget_file_is_wellformed() -> None:
    budget = json.loads((REPO / "scripts" / "mcp_mem_budget.json").read_text(encoding="utf-8"))
    ok, _ = check_budget(budget["peak_gb"], budget["final_gb"], budget)
    assert ok, "the recorded baseline must pass its own gate"


def test_recorded_budget_never_regresses_above_campaign_targets() -> None:
    """The #930 campaign-done contract: the recorded budget landed UNDER the
    campaign targets (<=1.8 GB peak / <=1.3 GB post-idle on the acceptance
    load). Raising the recorded numbers past the targets — to make a memory
    regression pass — must fail HERE, in plain CI, before any live gate runs."""

    budget = json.loads((REPO / "scripts" / "mcp_mem_budget.json").read_text(encoding="utf-8"))
    targets = budget["campaign_targets"]
    assert budget["peak_gb"] <= targets["peak_gb"], (
        "recorded peak budget regressed above the campaign target"
    )
    assert budget["final_gb"] <= targets["final_gb"], (
        "recorded final budget regressed above the campaign target"
    )


def test_children_scenario_budget_is_wellformed_and_under_targets() -> None:
    """The #955 background-children block obeys the same contract as the
    baseline: it passes its own gate and stays at or under the campaign
    targets, so a children-scenario memory regression cannot be smuggled in by
    raising the recorded numbers."""

    budget = json.loads((REPO / "scripts" / "mcp_mem_budget.json").read_text(encoding="utf-8"))
    children = budget["children"]
    ok, _ = check_budget(children["peak_gb"], children["final_gb"], children)
    assert ok, "the recorded children baseline must pass its own gate"
    targets = children["campaign_targets"]
    assert children["peak_gb"] <= targets["peak_gb"], (
        "recorded children peak budget regressed above the campaign target"
    )
    assert children["final_gb"] <= targets["final_gb"], (
        "recorded children final budget regressed above the campaign target"
    )


summarize_runs = _mod.summarize_runs
RunMeasurement = _mod.RunMeasurement


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return ["mcp_mem_attribution.py", "--pack", str(tmp_path), "--workspace", str(tmp_path), *extra]


def test_median_absorbs_one_noisy_run_that_a_single_run_gate_would_fail() -> None:
    """The observed noise: two clean runs of ONE commit measured final 0.97 and 0.95
    against a 0.966 cap (0.92 x 1.05). A one-run gate passed or failed by luck; the
    median of three honest runs is judged instead, and the spread is reported."""

    budget = {"peak_gb": 3.2, "final_gb": 0.92}
    noisy = RunMeasurement(peak_gb=2.3, final_gb=0.97)
    assert not check_budget(noisy.peak_gb, noisy.final_gb, budget)[0]

    summary = summarize_runs([noisy, RunMeasurement(2.25, 0.93), RunMeasurement(2.40, 0.92)])
    assert summary.final_median_gb == 0.93
    assert summary.peak_median_gb == 2.3
    assert summary.final_spread_gb == (0.92, 0.97)
    assert summary.peak_spread_gb == (2.25, 2.40)
    assert check_budget(summary.peak_median_gb, summary.final_median_gb, budget)[0]
    described = summary.describe()
    assert "median of 3" in described
    assert "0.92-0.97" in described  # the spread stays visible


def test_median_over_the_cap_still_fails() -> None:
    """A real regression moves the median: two of three runs over the cap fail."""

    budget = {"peak_gb": 3.2, "final_gb": 0.92}
    summary = summarize_runs(
        [RunMeasurement(2.3, 0.97), RunMeasurement(2.3, 0.98), RunMeasurement(2.3, 0.90)]
    )
    assert summary.final_median_gb == 0.97
    assert not check_budget(summary.peak_median_gb, summary.final_median_gb, budget)[0]


def test_peak_and_final_medians_are_independent() -> None:
    """Peak and final are separate medians over all runs, not one run's pair."""

    summary = summarize_runs(
        [RunMeasurement(3.0, 0.80), RunMeasurement(2.0, 0.95), RunMeasurement(2.5, 0.90)]
    )
    assert (summary.peak_median_gb, summary.final_median_gb) == (2.5, 0.90)


def test_no_runs_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError, match="no runs"):
        summarize_runs([])


def test_assert_budget_refuses_fewer_than_three_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate cannot be judged on one noisy sample: refused before any server boots."""

    argv = _argv(tmp_path, "--settle-s", "180", "--runs", "1", "--assert-budget")
    monkeypatch.setattr(sys, "argv", argv)
    booted: list[object] = []
    monkeypatch.setattr(_mod, "_run_once", lambda args: booted.append(args) or 2)
    assert _mod.main() == 2
    assert booted == []
    assert f"--runs >= {_mod.MIN_ASSERT_RUNS}" in capsys.readouterr().out


def test_each_run_is_measured_and_the_median_is_judged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Over main(): N complete runs, the verdict on the median, the spread printed."""

    runs = iter([RunMeasurement(2.3, 0.97), RunMeasurement(2.25, 0.93), RunMeasurement(2.4, 0.92)])
    calls: list[object] = []

    def fake_run_once(args: object) -> object:
        calls.append(args)
        return next(runs)

    monkeypatch.setattr(_mod, "_run_once", fake_run_once)
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, "--settle-s", "180", "--assert-budget"))
    assert _mod.main() == 0
    out = capsys.readouterr().out
    assert len(calls) == _mod.DEFAULT_RUNS == 3
    assert "median of 3" in out
    assert "0.92-0.97" in out
    assert "GATE: PASS" in out


def test_a_run_that_cannot_measure_fails_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dead or degraded run FAILS the gate; it is never dropped from the median."""

    results = iter([RunMeasurement(2.3, 0.9), 2])
    monkeypatch.setattr(_mod, "_run_once", lambda args: next(results))
    monkeypatch.setattr(sys, "argv", _argv(tmp_path, "--runs", "3"))
    assert _mod.main() == 2
