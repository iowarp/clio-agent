"""CLIO's seed caps clio-core's shared-memory segments; older seeds get the caps once (#906)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_growth import _digest
from clio_agent.arc.clio_core_segments import (
    MAIN_SEGMENT_SIZE,
    METADATA_SEGMENT_SIZE,
    SEGMENT_LINES,
    ensure_seeded_segments,
)


def _seed(path: Path) -> str:
    return clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
        core_port=9413,
        core_threads=8,
        conf_dir="conf",
        ram_budget="1GB",
        file_tier="storage.bin",
        file_capacity="10GB",
        metadata_log="metadata.log",
        indexer="",
    )


def _runtime(cfg: Path) -> dict:
    return yaml.safe_load(cfg.read_text(encoding="utf-8"))["runtime"]


def test_the_seed_caps_both_segments(tmp_path: Path) -> None:
    runtime = yaml.safe_load(_seed(tmp_path))["runtime"]
    assert runtime["main_segment_size"] == MAIN_SEGMENT_SIZE
    assert runtime["metadata_segment_size"] == METADATA_SEGMENT_SIZE


def test_an_uncapped_seed_is_capped_once_and_keeps_growth_ownership(tmp_path: Path) -> None:
    """SABOTAGE: return early in ``ensure_seeded_segments`` and the old seed stays uncapped."""
    cfg = tmp_path / "cte.yaml"
    old = _seed(tmp_path).replace(SEGMENT_LINES, "", 1)
    cfg.write_text(old, encoding="utf-8")
    journal = tmp_path / "cte-capacity.json"
    journal.write_text(json.dumps({"schema": 1, "config_sha256": _digest(old)}), encoding="utf-8")

    ensure_seeded_segments(cfg)
    assert cfg.read_text(encoding="utf-8") == _seed(tmp_path)
    assert json.loads(journal.read_text(encoding="utf-8"))["config_sha256"] == _digest(
        _seed(tmp_path)
    )
    ensure_seeded_segments(cfg)
    assert cfg.read_text(encoding="utf-8") == _seed(tmp_path)


def test_an_explicit_segment_size_is_left_alone(tmp_path: Path) -> None:
    cfg = tmp_path / "cte.yaml"
    explicit = (
        _seed(tmp_path)
        .replace(SEGMENT_LINES, "", 1)
        .replace("  conf_dir:", '  metadata_segment_size: "2GB"\n  conf_dir:', 1)
    )
    cfg.write_text(explicit, encoding="utf-8")
    ensure_seeded_segments(cfg)
    runtime = _runtime(cfg)
    assert runtime["metadata_segment_size"] == "2GB"
    assert "main_segment_size" not in runtime
