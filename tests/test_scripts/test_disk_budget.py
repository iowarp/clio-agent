"""The disk-footprint budget gate (iowarp/clio-agent#1001) — wrong inputs included.

``check_footprint`` is the pure verdict of scripts/disk_footprint.py: an over-budget
footprint or a malformed budget must FAIL, never pass silently. The recorded budget must
pass its own gate and stay at or under the campaign target so a regression cannot be
smuggled in by raising the number.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "disk_footprint", REPO / "scripts" / "disk_footprint.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["disk_footprint"] = _mod
_spec.loader.exec_module(_mod)

check_footprint = _mod.check_footprint
BUDGET_TOLERANCE = _mod.BUDGET_TOLERANCE


def test_under_budget_passes() -> None:
    ok, detail = check_footprint(1.0, 2.0)
    assert ok
    assert "ok=True" in detail


def test_over_budget_fails() -> None:
    ok, _ = check_footprint(3.0, 2.0)
    assert not ok


def test_tolerance_band() -> None:
    budget = 2.0
    ok, _ = check_footprint(budget * BUDGET_TOLERANCE - 0.001, budget)
    assert ok
    ok, _ = check_footprint(budget * BUDGET_TOLERANCE + 0.001, budget)
    assert not ok


def test_malformed_budget_is_typed_fail() -> None:
    for bad in (0.0, -1.0, None, "x"):
        ok, detail = check_footprint(0.5, bad)  # type: ignore[arg-type]
        assert not ok, bad
        assert "malformed budget" in detail


def test_recorded_budget_wellformed_and_under_campaign_target() -> None:
    data = json.loads((REPO / "scripts" / "disk_budget.json").read_text(encoding="utf-8"))
    steady = data["steady_state_gb"]
    target = data["campaign_target_gb"]
    assert steady > 0
    assert steady <= target, "recorded steady-state budget regressed above the campaign target"


def test_measure_returns_nonoverlapping_total(tmp_path: Path, monkeypatch) -> None:
    # Point the clio user dir at a tmp so the measured roots are hermetic; the mcp-uv-cache
    # is a subtree of the user cache, so the total must NOT double-count it.
    monkeypatch.setenv("CLIO_USER_DIR", str(tmp_path))
    from clio_agent import paths

    cache = paths.user_cache_dir()
    (cache / "mcp-uv-cache").mkdir(parents=True)
    (cache / "mcp-uv-cache" / "blob.bin").write_bytes(b"\0" * 1000)
    (cache / "models_dev.json").write_bytes(b"\0" * 500)

    per_root, total = _mod.measure()
    labels = dict(per_root)
    assert labels["mcp-uv-cache"] == 1000
    assert labels["user-cache (excl. mcp-uv-cache)"] == 500
    assert total == 1500, "total must not double-count the mcp-uv-cache subtree"
