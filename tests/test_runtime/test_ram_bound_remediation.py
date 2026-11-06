"""The RAM-bound remediation names settings that exist in the generated config.

The generated clio-core config keeps data on one file tier: its only RAM bound is the
ram bdev ``capacity`` (the memory budget, ``arc.cte.ram_capacity``). An unbounded
budget used to be answered with "edit storage[cte_ram_tier]", a tier that config does
not have. The data-tier doctor rows also called the one data tier "cold".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.arc import clio_core_config
from clio_agent.runtime import clio_core_health
from clio_agent.runtime.clio_core_health import probe_clio_core_ram_cap
from clio_agent.runtime.cte_cold_tier import ColdTierUsage, probe_cte_cold_tier_disk
from clio_agent.runtime.status import IntegrationState


def _disk_only_config(tmp_path: Path, ram_budget: str) -> Path:
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=9413,
            core_threads=4,
            conf_dir="c",
            file_tier=str(tmp_path / "cte"),
            file_capacity="1MB",
            ram_budget=ram_budget,
            metadata_log=str(tmp_path / "cte" / "metadata.log"),
            indexer="",
        ),
        encoding="utf-8",
    )
    return cfg


@pytest.mark.parametrize(
    "remediation",
    [
        clio_core_config._BOOT_REMEDIATION,
        clio_core_health._REMEDIATION,
    ],
)
def test_the_remediation_names_the_budget_and_the_ram_bdev(remediation: str) -> None:
    """SABOTAGE: restore "storage[cte_ram_tier]" in either remediation and this fails."""
    assert "arc.cte.ram_capacity" in remediation
    assert "bdev_type: ram" in remediation
    assert "cte_ram_tier" not in remediation
    assert "hot tier" not in remediation


def test_an_unbounded_budget_row_points_at_the_ram_bdev(tmp_path: Path) -> None:
    """The generated (disk-only) shape with a ``0g`` budget: DEGRADED, actionable."""
    cfg = _disk_only_config(tmp_path, "0g")
    [row] = probe_clio_core_ram_cap(
        env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)}
    )
    assert row.state is IntegrationState.DEGRADED
    assert row.details["reason"] == "arena_unbounded"
    assert "bdev_type: ram" in row.next_action and "cte_ram_tier" not in row.next_action


def test_the_data_tier_rows_call_it_the_data_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: restore "CTE cold tier" in the row summaries and this fails."""
    cfg = _disk_only_config(tmp_path, "1GB")
    (tmp_path / "cte").mkdir()
    monkeypatch.setattr(
        "clio_agent.runtime.cte_cold_tier.cold_tier_usage",
        lambda _store: ColdTierUsage(990_000, 990_000, 0),
    )
    env = {"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)}
    [row] = probe_cte_cold_tier_disk(env=env)
    assert row.state is IntegrationState.DEGRADED
    assert row.summary.startswith("CTE data tier files have")
    assert "cold tier" not in row.summary and "cold-tier" not in row.next_action
