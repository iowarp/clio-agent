from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmark"


def _jsonl_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_current_status_points_to_current_marketplace_hierarchy_evidence() -> None:
    status = (BENCHMARK / "CURRENT_STATUS.md").read_text(encoding="utf-8")
    complex_report = (BENCHMARK / "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md").read_text(
        encoding="utf-8"
    )
    retry_report = (BENCHMARK / "MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md").read_text(
        encoding="utf-8"
    )
    complex_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_COMPLEX_HIERARCHY_EVIDENCE.jsonl")
    retry_rows = _jsonl_rows(BENCHMARK / "MARKETPLACE_GEOSPATIAL_RETRY_EVIDENCE.jsonl")

    seismic = next(
        row for row in complex_rows if row["case"] == "marketplace_seismic_waveform_review"
    )
    retry = next(
        row for row in retry_rows if row["case"] == "marketplace_geospatial_field_review"
    )

    assert "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md" in status
    assert "MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md" in status
    assert "FRESH_REAL_ORCHESTRATOR_REPORT.md" in status
    assert "historical" in status.lower()
    assert "5/6" in status
    assert "1/1" in status
    assert "single clean full-lane rerun" in status
    assert seismic["outcome"] == "pass"
    assert seismic["artifact_evidence"]
    assert all(row["exists"] for row in seismic["artifact_evidence"])
    assert retry["outcome"] == "pass"
    assert retry["tool_names"] == ["geospatial_inspect_geojson", "geospatial_inspect_geojson"]
    assert "NDP full SAC/PNG chain verified" not in status
    assert "Result: 5/6 clean passes" in complex_report
    assert "at least three marketplace cases prove complex hierarchy depth | 5 | 3 | pass" in complex_report
    assert "Result: 1/1 clean passes" in retry_report
    assert "main -> spatial_features -> main" in retry_report


def test_superseded_real_orchestrator_report_is_labeled_historical() -> None:
    report = (BENCHMARK / "FRESH_REAL_ORCHESTRATOR_REPORT.md").read_text(encoding="utf-8")

    assert "Historical/superseded evidence" in report[:500]
    assert "CURRENT_STATUS.md" in report[:500]
    assert "MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md" in report[:500]
