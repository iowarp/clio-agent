"""The CAS store budget gate (iowarp/clio-agent#972) — wrong inputs included.

``check_footprint`` is the pure verdict of scripts/artifact_store_footprint.py: an
over-budget store or a malformed budget must FAIL, never pass silently. The recorded
budget must pass its own gate and stay at or under the campaign target so a
regression cannot be smuggled in by raising the number. Mirrors
tests/test_scripts/test_disk_budget.py (#930/#1001 pattern).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "artifact_store_footprint", REPO / "scripts" / "artifact_store_footprint.py"
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["artifact_store_footprint"] = _mod
_spec.loader.exec_module(_mod)

check_footprint = _mod.check_footprint
BUDGET_TOLERANCE = _mod.BUDGET_TOLERANCE


def test_under_budget_passes() -> None:
    ok, detail = check_footprint(100.0, 512.0)
    assert ok
    assert "ok=True" in detail


def test_over_budget_fails() -> None:
    ok, _ = check_footprint(600.0, 512.0)
    assert not ok


def test_tolerance_band() -> None:
    budget = 512.0
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
    data = json.loads((REPO / "scripts" / "artifact_store_budget.json").read_text(encoding="utf-8"))
    steady = data["steady_state_mb"]
    target = data["campaign_target_mb"]
    assert steady > 0
    assert steady <= target, "recorded steady-state budget regressed above the campaign target"


def test_recorded_budget_matches_configured_default() -> None:
    """The recorded budget mirrors the in-code ``artifacts.cas_budget_bytes`` default.

    The gate and the runtime must bound to the SAME number — a drift would let the
    store grow past what CI proves. The default is 512 MiB (#966.8).
    """
    from clio_agent.gact.artifacts.cas import _DEFAULT_CAS_BUDGET_BYTES

    data = json.loads((REPO / "scripts" / "artifact_store_budget.json").read_text(encoding="utf-8"))
    assert data["steady_state_mb"] == _DEFAULT_CAS_BUDGET_BYTES // (1024 * 1024)


def test_measure_sums_blobs_excluding_tmp(tmp_path: Path) -> None:
    """``measure`` sums published blob bytes and ignores the ``.tmp`` staging dir."""
    cas = tmp_path / ".clio" / "agent" / "artifacts" / "cas"
    (cas / "ab").mkdir(parents=True)
    (cas / "ab" / ("ab" + "0" * 62)).write_bytes(b"\0" * 1000)
    (cas / ".tmp").mkdir(parents=True)
    (cas / ".tmp" / "ingest-scratch").write_bytes(b"\0" * 500)

    total = _mod.measure(tmp_path)
    assert total == 1000, "the .tmp staging scratch must not count toward the store total"
