"""The H4/H5 export: determinism, and every seeded finding readable from the files."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyarrow.parquet as pq
import pytest

from scripts.eval.tool_calling.fixtures.common import hash_tree
from scripts.eval.tool_calling.fixtures.export_h4 import effect_key, plant_growth, write_export
from scripts.eval.tool_calling.fixtures.export_h4_layout import RUN_FILES

SCALE = 360  # gas exchange every 6 min, environment every hour


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    root = tmp_path_factory.mktemp("h4") / "h4-export"
    return root, write_export(root, 7, scale=SCALE)


def _finding(key: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(a for a in key["anomalies"] if a["id"] == kind)


def _run_dirs(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.glob("data/*/*/*/*/run.json"))


def _sidecar(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "run.json").read_text())


def _start(folder: Path) -> datetime:
    return datetime.fromisoformat(_sidecar(folder)["start_utc"].replace("Z", "+00:00"))


def _env(folder: Path) -> list[dict[str, str]]:
    with (folder / "env.csv").open() as handle:
        return list(csv.DictReader(handle))


def _a_net(folder: Path) -> tuple[np.ndarray, np.ndarray]:
    with h5py.File(folder / "gas_exchange.h5") as handle:
        return handle["gas_exchange/time"][:], handle["gas_exchange/A_net"][:]


def _manifest_ids(root: Path) -> list[str]:
    with (root / "MANIFEST.csv").open() as handle:
        return [row["run_id"] for row in csv.DictReader(handle)]


def test_same_seed_same_bytes(export, tmp_path: Path) -> None:
    root, key = export
    again = write_export(tmp_path / "again", 7, scale=SCALE)
    assert json.dumps(again, sort_keys=True) == json.dumps(key, sort_keys=True)
    assert hash_tree(tmp_path / "again") == hash_tree(root)
    other = write_export(tmp_path / "other", 8, scale=SCALE)
    assert hash_tree(tmp_path / "other") != hash_tree(root)
    assert other["effect"] != key["effect"]


def test_layout_and_scale(export, tmp_path: Path) -> None:
    root, key = export
    files = [p for p in root.rglob("*") if p.is_file()]
    assert len(files) == 140
    runs = _run_dirs(root)
    assert len(runs) == key["run_folders"] == 25
    assert all(sorted(p.name for p in run.iterdir()) == sorted(RUN_FILES) for run in runs)
    _, a_net = _a_net(runs[0])
    assert a_net.shape == (8, 72 * 3600 // SCALE)
    with pytest.raises(ValueError, match="divide 360"):
        write_export(tmp_path / "bad", 7, scale=7)


def test_calibration_offset_shows_in_the_log_and_in_darkness(export) -> None:
    root, key = export
    cal = root / _finding(key, "calibration")["path"]
    for run in _run_dirs(root):
        log = (run / "instrument.log").read_text()
        seconds, a_net = _a_net(run)
        hours = (seconds % 86400) / 3600
        dark = a_net[:, (hours < 6) | (hours >= 22)].mean()
        if run == cal:
            assert "zero check failed" in log and "WARN" in log
            assert dark > 0.5
        else:
            assert "zero check passed" in log
            assert dark < -1.0


def test_unit_run_stores_heights_in_cm(export) -> None:
    root, key = export
    unit = root / _finding(key, "unit")["path"]
    medians = {}
    for run in _run_dirs(root):
        table = pq.read_table(run / "growth.parquet")
        stated = table.schema.field("height").metadata[b"unit"].decode()
        assert stated == ("cm" if run == unit else "mm")
        assert _sidecar(run)["units"]["height"] == "mm"
        medians[run] = float(np.median(table.column("height").to_numpy()))
    others = np.median([v for run, v in medians.items() if run != unit])
    assert 0.07 < medians[unit] / others < 0.13


def test_duplicate_is_a_byte_identical_unindexed_copy(export) -> None:
    root, key = export
    dup = _finding(key, "duplicate")
    original, copy = root / dup["path"], root / dup["copy"]
    for name in RUN_FILES[1:]:
        assert (original / name).read_bytes() == (copy / name).read_bytes()
    assert _sidecar(copy)["run_id"] == copy.name != original.name
    assert _start(copy) == _start(original)
    assert copy.name not in _manifest_ids(root)
    campaigns = "".join(p.read_text() for p in (root / "campaigns").glob("*.json"))
    assert original.name in campaigns and copy.name not in campaigns
    log = (root / "export" / "export_log.txt").read_text()
    assert f"export of {original.name} timed out" in log and f"exported {copy.name}" in log


def test_clock_run_env_is_an_hour_late(export) -> None:
    root, key = export
    clock = root / _finding(key, "clock")["path"]
    for run in _run_dirs(root):
        rows = _env(run)
        first = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
        lit = next(r for r in rows if float(r["par_umol_m2_s"]) > 0)
        shift = timedelta(hours=1) if run == clock else timedelta(0)
        assert first == _start(run) + shift
        assert lit["timestamp"].endswith("T07:00:00Z" if run == clock else "T06:00:00Z")
        seconds, _ = _a_net(run)
        assert datetime.fromtimestamp(seconds[0], UTC) == _start(run)
    assert "chamber panel clock (UTC+01:00)" in (clock / "instrument.log").read_text()


def test_effect_is_recomputed_from_the_files(export) -> None:
    root, key = export
    copy = root / _finding(key, "duplicate")["copy"]
    plants = []
    assimilation: dict[str, list[float]] = {"A": [], "B": []}
    cal = root / _finding(key, "calibration")["path"]
    for run in _run_dirs(root):
        if run == copy:
            continue
        condition = _sidecar(run)["condition"]
        table = pq.read_table(run / "growth.parquet")
        factor = 10.0 if table.schema.field("height").metadata[b"unit"] == b"cm" else 1.0
        rows = table.to_pylist()
        start = _start(run)
        for plant in sorted({r["plant_id"] for r in rows}):
            mine = [r for r in rows if r["plant_id"] == plant]
            days = np.array([(r["timestamp"] - start).total_seconds() / 86400 for r in mine])
            height = np.array([r["height"] * factor for r in mine])
            plants.append((condition, plant_growth(days, height)))
        if run != cal:
            seconds, a_net = _a_net(run)
            hours = (seconds % 86400) / 3600
            assimilation[condition].append(float(a_net[:, (hours >= 6) & (hours < 22)].mean()))
    metrics = {m["name"]: m for m in key["effect"]["metrics"]}
    for name, field in (("elongation rate", "rate"), ("height gain over the run", "gain")):
        for condition in "AB":
            median = np.median([g[field] for c, g in plants if c == condition])
            assert metrics[name][condition] == pytest.approx(median, abs=2e-3), name
    assert effect_key(plants) == key["effect"]
    assert metrics["elongation rate"]["relative"] > 0.3
    a, b = (float(np.mean(assimilation[c])) for c in "AB")
    assert abs(b - a) / a < 0.05  # the light changes elongation, not photosynthesis


def test_decoy_dark_interval_is_real_and_documented(export) -> None:
    root, key = export
    decoy = key["decoy"]
    folder = root / decoy["path"]
    start, end = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in decoy["dark_utc"])
    for run in _run_dirs(root):
        rows = _env(run)
        inside = [
            float(r["par_umol_m2_s"])
            for r in rows
            if start <= datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) < end
        ]
        if run == folder:
            assert inside and not any(inside)
        elif inside and run.parents[2] == folder.parents[2]:
            assert all(v > 0 for v in inside)
    seconds, a_net = _a_net(folder)
    dark = (seconds >= start.timestamp()) & (seconds < end.timestamp())
    assert dark.any() and a_net[:, dark].max() < 0
    assert "dark-adaptation" in _sidecar(folder)["notes"]
    readme = (root / "README.md").read_text()
    assert folder.name in readme and "5.2" in readme
    assert "5.2 Dark adaptation" in (root / "PROTOCOL.md").read_text()


def test_index_lists_a_phantom_and_omits_a_run(export) -> None:
    root, key = export
    ids = _manifest_ids(root)
    on_disk = {p.name for p in _run_dirs(root)}
    phantom = key["index"]["phantom"]["runs"][0]
    unlisted = key["index"]["unlisted"]["runs"][0]
    assert phantom in ids and phantom not in on_disk
    assert unlisted in on_disk and unlisted not in ids
    assert set(ids) - {phantom} == on_disk - {
        unlisted,
        Path(_finding(key, "duplicate")["copy"]).name,
    }
