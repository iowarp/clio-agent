"""Tests for the doctor CTE ram hot-tier cap probe (#890).

The probe surfaces the effective ram ``capacity_limit`` the ARC clio-core backend will run
with: a healthy bounded cap is READY, a ``0g`` (= 80%-DRAM) cap is DEGRADED with
remediation, an unparseable cap is MISCONFIGURED, and the ``local`` backend yields no
row. Assertions read real IntegrationStatus rows built from real on-disk config files.
"""

from __future__ import annotations

from clio_agent.arc import clio_core_config
from clio_agent.arc.clio_core_daemon import DaemonMemorySnapshot
from clio_agent.arc.init_degradation import ArcInitDegradation
from clio_agent.runtime.clio_core_health import (
    probe_clio_core_daemon_memory,
    probe_clio_core_init_degradation,
    probe_clio_core_ram_cap,
)
from clio_agent.runtime.status import IntegrationState

_GiB = 1024**3


def _daemon_snapshot(rss: int, *, live: int = 0, stale: int = 0, pid: int = 4321):
    return DaemonMemorySnapshot(
        pid=pid,
        pid_source="pidfile",
        rss_bytes=rss,
        committed_bytes=rss * 2,
        thread_count=15,
        live_client_count=live,
        stale_client_count=stale,
        registered_client_count=live + stale,
        port=9413,
    )


def _degrade_record(reason: str = "clio_core_binding_absent", was_explicit: bool = False):
    return ArcInitDegradation(
        reason=reason,
        choice="cte",
        was_explicit=was_explicit,
        config_path="/tmp/cte.yaml",
        error_type="ImportError",
        error="clio_cte_core_ext not built",
        data_dir="/tmp/arc",
    )


def test_init_degradation_row_names_cause_and_external_operator(tmp_path):
    """A recorded init degrade surfaces a DEGRADED row naming the cause + external op (#897)."""
    rows = probe_clio_core_init_degradation(record=_degrade_record())
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "clio_core_init"
    assert row.state is IntegrationState.DEGRADED
    # SABOTAGE PIN: swallow/blank the typed reason and these assertions go red.
    assert row.details["reason"] == "clio_core_binding_absent"
    assert "external-operator" in row.summary
    assert row.fallback == "local"


def test_init_degradation_no_record_yields_no_row():
    """No degrade recorded this process -> no row (a healthy/local boot is silent)."""
    assert probe_clio_core_init_degradation(record=None) == []


def test_init_degradation_sabotage_ready_would_go_red():
    """SABOTAGE guard: if the probe reported a recorded degrade as anything but DEGRADED."""
    row = probe_clio_core_init_degradation(record=_degrade_record("clio_core_daemon_spawn_failed"))[0]
    assert row.state is IntegrationState.DEGRADED
    assert row.details["reason"] == "clio_core_daemon_spawn_failed"


# A LEGACY tier-present shape (pre-#906 files still in the wild): these probe
# tests pin the READ semantics for ram-data-tier configs. The PRODUCTION
# template is disk-only (no ram tier) — covered by the disk-only test below.
_LEGACY_TIER_YAML = """\
runtime:
  conf_dir: "c"
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_query: local
    pool_id: "301.0"
    bdev_type: ram
    capacity: "0g"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_query: local
    pool_id: "512.0"
    storage:
      - path: "ram::cte_ram_tier"
        bdev_type: "ram"
        capacity_limit: "{ram_capacity}"
        score: 1.0
      - path: "f"
        bdev_type: "file"
        capacity_limit: "50GB"
        score: 0.0
"""


def _write_cte_yaml(tmp_path, ram_cap: str):
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(_LEGACY_TIER_YAML.format(ram_capacity=ram_cap), encoding="utf-8")
    return cfg


def test_probe_disk_only_default_is_ready(tmp_path):
    """The #906 disk-only desktop default: no ram data tier, bounded arena -> READY."""
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(
        clio_core_config._DEFAULT_CTE_CONFIG_TEMPLATE.format(
            conf_dir="c",
            file_tier="f",
            file_capacity="50GB",
            ram_budget="1GB",
            metadata_log="m",
        ),
        encoding="utf-8",
    )
    rows = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    row = rows[0]
    assert row.state is IntegrationState.READY
    assert row.details["reason"] == "disk_only_arena_bounded"
    assert "1GB" in row.summary


def test_probe_healthy_bounded_cap(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "2GB")
    rows = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "clio_core_ram_cap"
    assert row.state is IntegrationState.READY
    assert "2GB" in row.summary
    assert row.details["ram_capacity_limit"] == "2GB"
    assert row.details["ram_capacity_bytes"] == 2 * 1024**3


def test_probe_flags_0g_as_degraded(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "0g")
    rows = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
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
    row = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})[0]
    assert row.state is not IntegrationState.READY
    # And a genuinely bounded cap on the same wire IS ready — proving the flag is
    # specific to 0g, not a blanket "always degraded".
    ok = _write_cte_yaml(tmp_path, "2GB")
    assert (
        probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(ok)})[0].state
        is IntegrationState.READY
    )


def test_probe_unparseable_cap_is_misconfigured(tmp_path):
    cfg = _write_cte_yaml(tmp_path, "2gigs!")
    row = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})[0]
    assert row.state is IntegrationState.MISCONFIGURED
    assert row.details["reason"] == "ram_cap_unparseable"


def test_probe_local_backend_yields_no_row():
    assert probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "local"}) == []


def test_probe_default_backend_is_clio_core(tmp_path):
    """Unset CLIO_ARC_STORE defaults to cte, so a row is emitted."""
    cfg = _write_cte_yaml(tmp_path, "2GB")
    rows = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    assert rows[0].state is IntegrationState.READY


def test_daemon_memory_ok_is_ready():
    rows = probe_clio_core_daemon_memory(
        env={"CLIO_ARC_STORE": "cte"}, snapshot=_daemon_snapshot(512 * 1024**2, live=1)
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "clio_core_daemon_memory"
    assert row.state is IntegrationState.READY
    assert row.details["daemon_mem_status"] == "ok"
    assert row.details["reason"] == "clio_core_daemon_rss_ok"
    assert row.details["live_client_count"] == 1
    assert row.endpoint == "127.0.0.1:9413"


def test_daemon_memory_elevated_is_degraded():
    row = probe_clio_core_daemon_memory(
        env={"CLIO_ARC_STORE": "cte"}, snapshot=_daemon_snapshot(2 * _GiB)
    )[0]
    assert row.state is IntegrationState.DEGRADED
    assert row.details["daemon_mem_status"] == "elevated"
    assert row.details["reason"] == "clio_core_daemon_rss_elevated"


def test_daemon_memory_critical_is_degraded_and_names_recycle():
    row = probe_clio_core_daemon_memory(
        env={"CLIO_ARC_STORE": "cte"}, snapshot=_daemon_snapshot(5 * _GiB, stale=3)
    )[0]
    assert row.state is IntegrationState.DEGRADED
    assert row.details["daemon_mem_status"] == "critical"
    assert row.details["reason"] == "clio_core_daemon_rss_critical"
    assert row.details["stale_client_count"] == 3
    # The critical remediation surfaces the opt-in recycle knob.
    assert "CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE" in row.next_action


def test_daemon_memory_sabotage_critical_would_go_green():
    """SABOTAGE guard: a critical daemon reported as READY goes red here."""
    row = probe_clio_core_daemon_memory(
        env={"CLIO_ARC_STORE": "cte"}, snapshot=_daemon_snapshot(9 * _GiB)
    )[0]
    assert row.state is not IntegrationState.READY
    # A genuinely small daemon on the same wire IS ready -> the flag is specific.
    ok = probe_clio_core_daemon_memory(
        env={"CLIO_ARC_STORE": "cte"}, snapshot=_daemon_snapshot(64 * 1024**2)
    )[0]
    assert ok.state is IntegrationState.READY


def test_daemon_memory_local_backend_yields_no_row():
    assert probe_clio_core_daemon_memory(env={"CLIO_ARC_STORE": "local"}) == []


def test_daemon_memory_no_daemon_yields_no_row(monkeypatch):
    # cte backend but no daemon located (snapshot gather returns None) -> no row.
    from clio_agent.arc import clio_core_daemon

    monkeypatch.setattr(clio_core_daemon, "collect_daemon_memory_snapshot", lambda **_k: None)
    assert probe_clio_core_daemon_memory(env={"CLIO_ARC_STORE": "cte"}) == []


def test_probe_wired_into_collect(tmp_path):
    """The cap row appears in the full doctor report from RuntimeProbe.collect()."""
    from clio_agent.runtime.status import RuntimeProbe

    cfg = _write_cte_yaml(tmp_path, "0g")
    report = RuntimeProbe(
        env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)},
        gateway_lister=lambda: [],
        module_checker=lambda _m: False,
    ).collect()
    row = report.by_name("clio_core_ram_cap")
    assert row.state is IntegrationState.DEGRADED
