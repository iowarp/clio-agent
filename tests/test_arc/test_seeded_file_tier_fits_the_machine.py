"""A fresh install's clio-core file tier fits the machine: the first run never fails on disk.

The seed used a fixed 50 GB file tier, which Windows allocates up front, and the
capacity preflight then demands 50 GB + reserve free -- so a laptop with less free disk
failed its very first turn (and one with more lost 50 GB at once). The seed now sizes
the tier from the target disk's free space (10%, at least 2 GB, at most 50 GB); an
explicit ``arc.cte.file_capacity`` still wins.
"""

from __future__ import annotations

import shutil
from collections import namedtuple
from pathlib import Path

import pytest

from clio_agent.arc import clio_core_config, clio_core_file_capacity

_Usage = namedtuple("_Usage", "total used free")
_GB = 1 << 30


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_gb: float) -> str:
    monkeypatch.setattr(clio_core_config, "_default_cte_dir", lambda: tmp_path / "cte")
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: _Usage(0, 0, int(free_gb * _GB)))
    return clio_core_config.default_cte_config_path()


@pytest.mark.parametrize(("free_gb", "expected"), [(35, "3GB"), (12, "2GB"), (900, "50GB")])
def test_the_seeded_tier_is_sized_from_free_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_gb: float, expected: str
) -> None:
    cfg = Path(_seed(tmp_path, monkeypatch, free_gb))

    assert f'capacity_limit: "{expected}"' in cfg.read_text(encoding="utf-8")


def test_the_seeded_tier_passes_its_own_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _seed(tmp_path, monkeypatch, free_gb=35)

    clio_core_file_capacity.preflight_clio_core_config(cfg, env={})


def test_an_explicit_capacity_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_ARC_CTE_FILE_CAPACITY", "7GB")
    cfg = Path(_seed(tmp_path, monkeypatch, free_gb=35))

    assert 'capacity_limit: "7GB"' in cfg.read_text(encoding="utf-8")
