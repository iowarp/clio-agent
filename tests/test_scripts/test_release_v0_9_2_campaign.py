"""Focused checks for the v0.9.2 live-verification package."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = ROOT / "scripts" / "live_verification" / "release_v0_9_2"


def test_materials_fixture_pins_release_edge_cases() -> None:
    """The synthetic science fixture keeps its intended bounded shape."""

    with (CAMPAIGN / "fixtures" / "materials_scan_speed_fatigue.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))

    assert len(rows) == 18
    assert {row["scan_speed_mm_s"] for row in rows} == {"600", "800", "1000"}
    assert {row["build_orientation"] for row in rows} == {"XY", "Z"}
    assert sum(row["runout"] == "true" for row in rows) == 4
    assert sum(not row["roughness_ra_um"] for row in rows) == 1


def test_apps_ui_only_dry_run_selects_one_avenue() -> None:
    """The release wrapper can isolate MCP Apps from the larger historical leg."""

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "live_verification" / "leg_c2_v2_avenues.py"),
            "--dry-run",
            "--apps-ui-only",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert [row["avenue"] for row in payload["avenues"]] == ["apps-ui"]


def test_tracked_manifest_separates_runtime_evidence() -> None:
    """The recipe manifest keeps runtime evidence outside the candidate tree."""

    payload = json.loads((CAMPAIGN / "manifest.json").read_text(encoding="utf-8"))
    assert payload["provider"] == "codex"
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["evidence_root"] == "out/live-verification/release_v0_9_2"
    assert payload["historical_evidence_allowed"] is False


def test_evidence_packager_excludes_private_runtime_state(tmp_path: Path) -> None:
    """The release archive is bounded to curated evidence and campaign inputs."""

    script = CAMPAIGN / "scripts" / "package_evidence.py"
    spec = importlib.util.spec_from_file_location("release_evidence_packager", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    evidence = tmp_path / "evidence"
    campaign = tmp_path / "campaign"
    (evidence / "screenshots").mkdir(parents=True)
    (evidence / "state-final-candidate").mkdir()
    campaign.mkdir()
    (evidence / "manifest.json").write_text("{}", encoding="utf-8")
    (evidence / "verdict.md").write_text("pass", encoding="utf-8")
    (evidence / "screenshots" / "final.png").write_bytes(b"png")
    (evidence / "state-final-candidate" / "cte.sqlite3").write_bytes(b"private")
    (campaign / "README.md").write_text("runbook", encoding="utf-8")

    archive_paths = {
        archive_path.as_posix() for _, archive_path in module.iter_archive_files(evidence, campaign)
    }
    assert archive_paths == {
        "campaign/README.md",
        "evidence/manifest.json",
        "evidence/screenshots/final.png",
        "evidence/verdict.md",
    }
