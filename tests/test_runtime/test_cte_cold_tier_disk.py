"""Tests for the CTE cold-tier disk watch doctor row (iowarp/clio-agent#1001).

Visibility-first: the probe WARNS (DEGRADED) when the CTE cold-tier data directory exceeds
a configurable fraction of ``arc.cte.file_capacity``; actual trim is upstream-gated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.runtime.clio_core_health import probe_cte_cold_tier_disk
from clio_agent.runtime.status import IntegrationState

_CTE_YAML = """\
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    bdev_type: ram
    capacity: "1GB"
  - mod_name: clio_cte_core
    pool_name: cte_main
    storage:
      - path: "{store}/storage.bin"
        bdev_type: "file"
        capacity_limit: "1000000"
        score: 1.0
"""


def _seed_cte(dir_: Path, *, data_bytes: int) -> dict[str, str]:
    # A dedicated store subdir (the autouse conftest seeds an ``xdg/`` tree directly in
    # ``tmp_path``, which would otherwise be counted in the cold-tier measurement).
    store = dir_ / "cte"
    store.mkdir(exist_ok=True)
    cfg = store / "cte.yaml"
    cfg.write_text(_CTE_YAML.format(store=store.as_posix()), encoding="utf-8")
    (store / "storage.bin").write_bytes(b"\0" * data_bytes)
    return {"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)}


def test_over_fraction_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _seed_cte(tmp_path, data_bytes=600_000)  # 60% >= default 50%
    monkeypatch.setattr(
        "clio_agent.runtime.clio_core_health._cte_cold_tier_dir_usage",
        lambda _store: (600_000, 600_000),
    )
    rows = probe_cte_cold_tier_disk(env=env)
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "cte_cold_tier_disk"
    assert row.state is IntegrationState.DEGRADED
    assert row.details["used_bytes"] >= 600_000
    assert row.details["capacity_bytes"] == 1_000_000
    assert row.required is False


def test_under_fraction_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _seed_cte(tmp_path, data_bytes=100_000)  # 10% < 50%
    monkeypatch.setattr(
        "clio_agent.runtime.clio_core_health._cte_cold_tier_dir_usage",
        lambda _store: (100_000, 100_000),
    )
    rows = probe_cte_cold_tier_disk(env=env)
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.READY


def test_sparse_preallocation_uses_allocated_not_logical_bytes(tmp_path: Path) -> None:
    env = _seed_cte(tmp_path, data_bytes=0)
    backing = tmp_path / "cte" / "storage.bin"
    with backing.open("wb") as stream:
        stream.truncate(1_000_000)

    stat = os.lstat(backing)
    if not hasattr(stat, "st_blocks"):
        return

    rows = probe_cte_cold_tier_disk(env=env)
    assert len(rows) == 1
    row = rows[0]
    assert row.state is IntegrationState.READY
    assert row.details["logical_bytes"] >= 1_000_000
    assert row.details["allocated_bytes"] < 500_000
    assert "allocated" in row.summary


def test_logical_capacity_is_not_reported_as_usage_when_allocation_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preallocated capacity file must not become a false 100% usage warning."""

    env = _seed_cte(tmp_path, data_bytes=1_000_000)
    monkeypatch.setattr(
        "clio_agent.runtime.clio_core_health._cte_cold_tier_dir_usage",
        lambda _store: (None, 1_000_000),
    )

    rows = probe_cte_cold_tier_disk(env=env)

    assert len(rows) == 1
    row = rows[0]
    assert row.state is IntegrationState.SKIPPED
    assert row.details["used_bytes"] is None
    assert row.details["used_fraction"] is None
    assert row.details["allocation_measurement"] == "unavailable"
    assert "reserved capacity, not current data usage" in row.summary


def test_custom_fraction_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _seed_cte(tmp_path, data_bytes=100_000)  # 10%
    monkeypatch.setenv("CLIO_ARC_CTE_DISK_WARN_FRACTION", "0.05")  # warn at 5%
    from clio_agent import conf

    conf.reload()
    env["CLIO_ARC_CTE_DISK_WARN_FRACTION"] = "0.05"
    monkeypatch.setattr(
        "clio_agent.runtime.clio_core_health._cte_cold_tier_dir_usage",
        lambda _store: (100_000, 100_000),
    )
    rows = probe_cte_cold_tier_disk(env=env)
    assert rows[0].state is IntegrationState.DEGRADED


def test_local_backend_no_row(tmp_path: Path) -> None:
    env = _seed_cte(tmp_path, data_bytes=600_000)
    env["CLIO_ARC_STORE"] = "local"
    assert probe_cte_cold_tier_disk(env=env) == []
