from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmark"


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_current_status_points_to_passing_marketplace_seismic_evidence() -> None:
    status = (BENCHMARK / "CURRENT_STATUS.md").read_text(encoding="utf-8")
    report = (BENCHMARK / "MARKETPLACE_UNIFIED_REPORT.md").read_text(encoding="utf-8")
    rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_UNIFIED_EVIDENCE.jsonl")

    seismic = next(row for row in rows if row["case"] == "marketplace_seismic_waveform_review")

    assert "MARKETPLACE_UNIFIED_REPORT.md" in status
    assert "FRESH_REAL_ORCHESTRATOR_REPORT.md" in status
    assert "historical" in status.lower()
    assert "6/6" in status
    assert seismic["outcome"] == "pass"
    assert seismic["artifact_evidence"]
    assert all(row["exists"] for row in seismic["artifact_evidence"])
    assert "NDP full SAC/PNG chain verified" not in status
    assert "Result: 6/6 clean passes" in report


def test_superseded_real_orchestrator_report_is_labeled_historical() -> None:
    report = (BENCHMARK / "FRESH_REAL_ORCHESTRATOR_REPORT.md").read_text(encoding="utf-8")

    assert "Historical/superseded evidence" in report[:500]
    assert "CURRENT_STATUS.md" in report[:500]
    assert "MARKETPLACE_UNIFIED_REPORT.md" in report[:500]
