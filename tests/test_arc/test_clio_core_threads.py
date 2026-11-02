"""The daemon's thread count is ``arc.core_threads``; old seeds are moved to it once.

CLIO seeded ``num_threads: 4``, one clio-core I/O worker that every put queued on
(P7, 2026-11-02: puts waited on the daemon on more than a third of active stacks).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_threads import SEEDED_THREADS, core_threads, ensure_seeded_threads


def _seed(path: Path, threads: int) -> Path:
    cfg = path / "cte.yaml"
    cfg.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=9413,
            core_threads=threads,
            conf_dir="conf",
            ram_budget="1GB",
            file_tier="storage.bin",
            file_capacity="10GB",
            metadata_log="metadata.log",
            indexer="",
        ),
        encoding="utf-8",
    )
    return cfg


def _threads(cfg: Path) -> int:
    return yaml.safe_load(cfg.read_text(encoding="utf-8"))["runtime"]["num_threads"]


def test_an_old_seed_is_moved_to_the_configured_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: return early in ``ensure_seeded_threads`` and the seed stays at 4."""
    monkeypatch.setenv("CLIO_ARC_CORE_THREADS", "8")
    cfg = _seed(tmp_path, SEEDED_THREADS)
    ensure_seeded_threads(cfg)
    assert _threads(cfg) == 8
    ensure_seeded_threads(cfg)  # once: 8 is not the old seed
    assert _threads(cfg) == 8


def test_an_explicit_count_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CORE_THREADS", "8")
    cfg = _seed(tmp_path, 6)
    ensure_seeded_threads(cfg)
    assert _threads(cfg) == 6


def test_the_count_must_leave_an_io_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CORE_THREADS", "1")
    with pytest.raises(ValueError, match="at least 2"):
        core_threads()
