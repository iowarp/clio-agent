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
    for bad in ({}, {"peak_gb": 3.5}, {"peak_gb": "x", "final_gb": 3.5}, {"peak_gb": None, "final_gb": 3.5}):
        ok, detail = check_budget(1.0, 1.0, bad)
        assert not ok, bad
        assert "malformed budget" in detail


def test_recorded_budget_file_is_wellformed() -> None:
    budget = json.loads((REPO / "scripts" / "mcp_mem_budget.json").read_text(encoding="utf-8"))
    ok, _ = check_budget(budget["peak_gb"], budget["final_gb"], budget)
    assert ok, "the recorded baseline must pass its own gate"
    targets = budget["campaign_targets"]
    assert targets["peak_gb"] < budget["peak_gb"]
    assert targets["final_gb"] < budget["final_gb"]
