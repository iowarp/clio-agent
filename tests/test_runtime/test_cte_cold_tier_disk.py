"""Tests for the CTE cold-tier doctor rows (iowarp/clio-agent#1001).

Visibility-first: ``cte_cold_tier_disk`` WARNS (DEGRADED) when the cold tier's data files
exceed a configurable fraction of ``arc.cte.file_capacity``; actual trim is upstream-gated.
The metadata log is reported by its own row, ``cte_metadata_log``: it is not bounded by the
capacity, and adding it to the data files once read as "203% of capacity".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.arc.clio_core_daemon import DaemonMemorySnapshot
from clio_agent.runtime.clio_core_health import (
    probe_clio_core_daemon_memory,
    probe_cte_cold_tier_disk,
    probe_cte_metadata_log,
)
from clio_agent.runtime.cte_cold_tier import ColdTierUsage
from clio_agent.runtime.status import IntegrationState

_USAGE = "clio_agent.runtime.cte_cold_tier.cold_tier_usage"

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
    monkeypatch.setattr(_USAGE, lambda _store: ColdTierUsage(600_000, 600_000, 0))
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
    monkeypatch.setattr(_USAGE, lambda _store: ColdTierUsage(100_000, 100_000, 0))
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
    monkeypatch.setattr(_USAGE, lambda _store: ColdTierUsage(None, 1_000_000, 0))

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
    monkeypatch.setattr(_USAGE, lambda _store: ColdTierUsage(100_000, 100_000, 0))
    rows = probe_cte_cold_tier_disk(env=env)
    assert rows[0].state is IntegrationState.DEGRADED


def test_a_preallocated_data_file_is_not_reported_as_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """iowarp-core 2.3.2 allocates the whole data file before writing anything to it."""
    env = _seed_cte(tmp_path, data_bytes=0)
    monkeypatch.setattr(_USAGE, lambda _store: ColdTierUsage(1_000_000, 1_000_000, 0))

    (row,) = probe_cte_cold_tier_disk(env=env)

    assert row.state is IntegrationState.SKIPPED
    assert row.details["reason"] == "cte_cold_tier_occupancy_unmeasurable"
    assert row.details["allocation_measurement"] == "preallocated"
    assert row.details["used_fraction"] is None


def test_the_metadata_log_is_measured_apart_from_the_data_files(tmp_path: Path) -> None:
    """The live store read 203%: a full data file plus a log the capacity never bounded."""
    env = _seed_cte(tmp_path, data_bytes=100_000)
    (tmp_path / "cte" / "metadata.log.blob.0").write_bytes(b"\1" * 900_000)

    (disk,) = probe_cte_cold_tier_disk(env=env)
    (log,) = probe_cte_metadata_log(env=env)

    if hasattr(os.lstat(tmp_path / "cte" / "storage.bin"), "st_blocks"):
        assert disk.state is IntegrationState.READY
        assert disk.details["allocated_bytes"] < 500_000
    assert disk.details["metadata_log_bytes"] == 900_000
    assert log.name == "cte_metadata_log"
    assert log.details["metadata_log_bytes"] == 900_000
    assert log.state is IntegrationState.READY  # under the 1 GiB default


def _small_warn(monkeypatch: pytest.MonkeyPatch, warn: int) -> None:
    from clio_agent import conf

    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN", str(warn))
    monkeypatch.setenv("CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL", str(warn * 100))
    conf.reload()


def test_a_large_metadata_log_degrades_with_a_typed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _seed_cte(tmp_path, data_bytes=0)
    (tmp_path / "cte" / "metadata.log.blob.4").write_bytes(b"\1" * 600_000)
    _small_warn(monkeypatch, 500_000)

    (row,) = probe_cte_metadata_log(env=env)

    assert row.state is IntegrationState.DEGRADED
    assert row.details["reason"] == "cte_metadata_log_large"
    assert row.details["warn_bytes"] == 500_000
    assert "replays" in row.summary


def test_daemon_memory_remediation_names_the_replayed_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restarting does not help a daemon whose start replays a log above the threshold."""
    env = _seed_cte(tmp_path, data_bytes=0)
    (tmp_path / "cte" / "metadata.log.blob.0").write_bytes(b"\1" * 600_000)
    _small_warn(monkeypatch, 500_000)
    snapshot = DaemonMemorySnapshot(
        pid=4242,
        pid_source="injected",
        rss_bytes=700_000,
        committed_bytes=1_400_000,
        thread_count=8,
        live_client_count=1,
        stale_client_count=0,
        registered_client_count=1,
        port=9413,
    )

    (row,) = probe_clio_core_daemon_memory(env=env, snapshot=snapshot)

    assert row.state is IntegrationState.DEGRADED
    assert row.details["metadata_log_bytes"] == 600_000
    assert row.next_action.startswith("Its metadata log is")
    assert "will not bring it under the threshold" in row.next_action
