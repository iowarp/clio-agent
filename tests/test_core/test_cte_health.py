"""Tests for the doctor CTE ram hot-tier cap probe (#890).

The probe surfaces the effective ram ``capacity_limit`` the ARC CTE backend will run
with: a healthy bounded cap is READY, a ``0g`` (= 80%-DRAM) cap is DEGRADED with
remediation, an unparseable cap is MISCONFIGURED, and the ``local`` backend yields no
row. Assertions read real IntegrationStatus rows built from real on-disk config files.
"""

from __future__ import annotations

from clio_agent.arc import cte_config
from clio_agent.runtime.cte_health import probe_cte_ram_cap
from clio_agent.runtime.status import IntegrationState


def _write_cte_yaml(tmp_path, ram_cap: str):
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(
        cte_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            conf_dir="c",
            file_tier="f",
            file_capacity="50GB",
            ram_capacity=ram_cap,
            metadata_log="m",
        ),
        encoding="utf-8",
    )
    return cfg


def test_probe_healthy_bounded_cap(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "2GB")
    rows = probe_cte_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "cte_ram_cap"
    assert row.state is IntegrationState.READY
    assert "2GB" in row.summary
    assert row.details["ram_capacity_limit"] == "2GB"
    assert row.details["ram_capacity_bytes"] == 2 * 1024**3


def test_probe_flags_0g_as_degraded(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "0g")
    rows = probe_cte_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    row = rows[0]
    assert row.state is IntegrationState.DEGRADED
    assert row.details["reason"] == "ram_cap_unbounded_80pct_dram"
    assert "80%" in row.summary
    assert "CLIO_ARC_CTE_RAM_CAPACITY" in row.next_action


def test_probe_sabotage_ignoring_0g_would_go_green(tmp_path):
    """SABOTAGE guard: if the probe treated 0g as a healthy cap, this test goes red.

    Encodes the exact defect the 0g flag exists to prevent — a doctor that reports the
    80%-DRAM config as READY. The invariant is that a 0g cap is NEVER ready.
    """
    cfg = _write_cte_yaml(tmp_path, "0g")
    row = probe_cte_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})[0]
    assert row.state is not IntegrationState.READY
    # And a genuinely bounded cap on the same wire IS ready — proving the flag is
    # specific to 0g, not a blanket "always degraded".
    ok = _write_cte_yaml(tmp_path, "2GB")
    assert (
        probe_cte_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(ok)})[0].state
        is IntegrationState.READY
    )


def test_probe_unparseable_cap_is_misconfigured(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "2gigs!")
    row = probe_cte_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})[0]
    assert row.state is IntegrationState.MISCONFIGURED
    assert row.details["reason"] == "ram_cap_unparseable"


def test_probe_local_backend_yields_no_row():
    assert probe_cte_ram_cap(env={"CLIO_ARC_STORE": "local"}) == []


def test_probe_default_backend_is_cte(tmp_path):
    """Unset CLIO_ARC_STORE defaults to cte, so a row is emitted."""
    cfg = _write_cte_yaml(tmp_path, "2GB")
    rows = probe_cte_ram_cap(env={"CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.READY


def test_probe_wired_into_collect(tmp_path):
    """The cap row appears in the full doctor report from RuntimeProbe.collect()."""
    from clio_agent.runtime.status import RuntimeProbe

    cfg = _write_cte_yaml(tmp_path, "0g")
    report = RuntimeProbe(
        env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)},
        gateway_lister=lambda: [],
        module_checker=lambda _m: False,
    ).collect()
    row = report.by_name("cte_ram_cap")
    assert row.state is IntegrationState.DEGRADED
