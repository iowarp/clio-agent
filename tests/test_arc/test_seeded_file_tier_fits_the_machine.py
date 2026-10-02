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


def _old_seed(tmp_path: Path) -> Path:
    """A config seeded before the tier was sized to the disk: the fixed 50 GB."""
    cfg = tmp_path / "cte" / "cte.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=9413,
            conf_dir=clio_core_config._cte_yaml_path(tmp_path / "cte" / "conf"),
            file_tier=clio_core_config._cte_yaml_path(tmp_path / "cte" / "storage.bin"),
            file_capacity="50GB",
            ram_budget="1GB",
            metadata_log=clio_core_config._cte_yaml_path(tmp_path / "cte" / "metadata.log"),
        ),
        encoding="utf-8",
    )
    return cfg


def test_an_old_fixed_seed_that_does_not_fit_is_resized_before_first_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: a 50 GB seed from before the sizing failed the first run (42 GB free)."""
    cfg = _old_seed(tmp_path)

    _seed(tmp_path, monkeypatch, free_gb=42)

    assert 'capacity_limit: "4GB"' in cfg.read_text(encoding="utf-8")
    clio_core_file_capacity.preflight_clio_core_config(str(cfg), env={})


def test_an_allocated_tier_is_never_resized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _old_seed(tmp_path)
    (tmp_path / "cte" / "storage.bin_node0").write_bytes(b"\0" * 16)  # data may live here

    _seed(tmp_path, monkeypatch, free_gb=42)

    assert 'capacity_limit: "50GB"' in cfg.read_text(encoding="utf-8")


def test_an_old_seed_that_fits_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _old_seed(tmp_path)

    _seed(tmp_path, monkeypatch, free_gb=900)

    assert 'capacity_limit: "50GB"' in cfg.read_text(encoding="utf-8")
