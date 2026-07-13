from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Only ``caseNN-*`` directories are numbered case contracts; the benchmark dir
# also holds named demo/helper dirs (ndp-*, agenttest, _cleanup) that are not
# case contracts and must not be counted here.
_CASE_DIR = re.compile(r"case\d+-")
BENCHMARK = ROOT / "benchmark"


def test_benchmark_directory_is_clean_contract_only() -> None:
    top_level_files = {path.name for path in BENCHMARK.iterdir() if path.is_file()}

    assert top_level_files == {
        "README.md",
        "CASE_MATRIX.md",
        "CASE_EVIDENCE_CONTRACT.md",
    }
    assert not any(name.endswith("_REPORT.md") for name in top_level_files)
    assert not any(name.endswith("_EVIDENCE.jsonl") for name in top_level_files)


def test_benchmark_has_exactly_twelve_case_contracts() -> None:
    case_dirs = sorted(
        path
        for path in BENCHMARK.iterdir()
        if path.is_dir() and _CASE_DIR.match(path.name)
    )

    assert [path.name for path in case_dirs] == [
        "case01-ndp-geographic-hazard-brief",
        "case02-earthscope-csv-seismic-geography",
        "case03-ndp-wildfire-weather-fusion",
        "case04-ndp-cimis-fire-risk-profile",
        "case05-genomics-cohort-qc",
        "case06-genomics-memory-followup",
        "case07-proteomics-lfq-cohort-review",
        "case08-hpc-io-regression-root-cause",
        "case09-format-bridge-integrity",
        "case10-terrain-lidar-suitability",
        "case11-custom-mcp-scientific-workflow",
        "case12-workspace-marketplace-swap",
    ]
    for case_dir in case_dirs:
        readme = case_dir / "README.md"
        assert readme.exists()
        text = readme.read_text(encoding="utf-8")
        # Every case declares a Status (most "not passed."; the active demo case,
        # e.g. case02, may read "active pre-1.0 case") plus the contract sections.
        assert "Status:" in text
        assert "## Semantics To Prove" in text
        assert "## Current Core Problem" in text


def test_case_matrix_rejects_old_shortcut_semantics() -> None:
    # Collapse whitespace so phrase checks are tolerant of Markdown line wrapping
    # (e.g. "built-in location hints" reflowed across a newline).
    matrix = " ".join((BENCHMARK / "CASE_MATRIX.md").read_text(encoding="utf-8").split())
    earthscope_case = " ".join(
        (BENCHMARK / "case02-earthscope-csv-seismic-geography" / "README.md")
        .read_text(encoding="utf-8")
        .split()
    )

    assert "contracts, not pass claims" in matrix
    assert "hardcoded city hints" in matrix
    assert "proper EarthScope CSV/tabular event, station, or channel evidence" in matrix
    assert "SAC is a waveform file format, not a geography or discovery semantic" in matrix
    assert "Waveform/SAC handling is optional" in earthscope_case
    assert "SAC is not the benchmark target" in earthscope_case
    assert "built-in location hints" in earthscope_case


def test_case_evidence_contract_requires_live_semantic_audit() -> None:
    contract = (BENCHMARK / "CASE_EVIDENCE_CONTRACT.md").read_text(encoding="utf-8")

    for required in (
        "prompt.txt",
        "run.md",
        "trace.jsonl",
        "report.md",
        "artifacts/",
    ):
        assert required in contract
    assert "active agent was a marketplace Agent Blueprint" in contract
    assert "Any benchmark-specific shortcut or hardcoded hint caused the case to fail" in contract
