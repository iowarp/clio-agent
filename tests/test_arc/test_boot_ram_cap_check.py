"""#906 — boot-time environment conformance for the clio-core ram cap.

The 12.3 GiB incident (2026-07-13): the box ran a stale pre-#890 ``cte.yaml``
with ``0g`` in TWO places (tier ``capacity_limit`` AND ram bdev ``capacity``)
for weeks — the #890 fix was tested and CI-green, but test isolation means no
test ever observes the real deployed file, and the generator deliberately
never rewrites user configs. Conformance is therefore a RUNTIME job: these
tests pin the boot-time check (:func:`boot_check_ram_cap`), the two-field
reader, and the doctor's bdev visibility.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clio_agent.arc.clio_core_config import (
    CLIO_CORE_RAM_UNCAPPED,
    boot_check_ram_cap,
    effective_ram_cap,
)
from clio_agent.runtime.clio_core_health import probe_clio_core_ram_cap

# The INCIDENT SHAPE, verbatim: a pre-#890 generated config with 0g in both
# places. This is the real object the check exists to catch.
_INCIDENT_CONFIG = """\
runtime:
  num_threads: 4
  conf_dir: "{root}/conf"
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
        capacity_limit: "0g"
        score: 1.0
      - path: "{root}/storage.bin"
        bdev_type: "file"
        capacity_limit: "50GB"
        score: 0.0
"""

# The conformant DESKTOP shape (#906 memory budget): bounded ceiling with 2x
# spill headroom above the tier limit, big final layer.
_BOUNDED_CONFIG = (
    _INCIDENT_CONFIG
    .replace('capacity: "0g"', 'capacity: "2GB"')
    .replace('capacity_limit: "0g"', 'capacity_limit: "1GB"')
)


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(body.format(root=tmp_path.as_posix()), encoding="utf-8")
    return cfg


def test_incident_config_reads_both_zero_caps(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _INCIDENT_CONFIG)
    cap = effective_ram_cap(env={}, config_path=cfg)
    assert cap.unbounded is True
    assert cap.cap == "0g"
    assert cap.bdev_capacity == "0g"
    assert cap.file_exists is True
    assert cap.parse_error is None


def test_bounded_config_is_conformant_with_bdev_visible(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _BOUNDED_CONFIG)
    cap = effective_ram_cap(env={}, config_path=cfg)
    assert cap.unbounded is False
    assert cap.cap == "1GB"
    # The desktop shape: a BOUNDED ceiling with 2x headroom (#906).
    assert cap.bdev_capacity == "2GB"


def test_boot_check_warns_typed_on_the_incident_shape(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    cfg = _write(tmp_path, _INCIDENT_CONFIG)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.unbounded is True
    warned = [r for r in caplog.records if CLIO_CORE_RAM_UNCAPPED in r.getMessage()]
    assert warned, "boot check must emit the typed clio_core_ram_uncapped warning"
    message = warned[0].getMessage()
    # The warning must carry the evidence: which file, and both 0g fields.
    assert str(cfg) in message
    assert "80% of total system DRAM" in message
    assert "bdev_capacity=0g" in message


def test_boot_check_is_silent_on_a_bounded_config(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    cfg = _write(tmp_path, _BOUNDED_CONFIG)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        boot_check_ram_cap(cfg, env={})
    assert not [r for r in caplog.records if CLIO_CORE_RAM_UNCAPPED in r.getMessage()]


def test_disk_only_with_bounded_arena_is_silent(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    # The #906 desktop default: NO ram data tier ("data is in memory or is in
    # disk" — owner ruling), arena bounded at the budget. Must be silent — the
    # memory bound is the ARENA, not a tier.
    body = _BOUNDED_CONFIG.replace('        capacity_limit: "1GB"\n', "", 1)
    cfg = _write(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.cap is None
    assert cap.bdev_capacity == "2GB"
    assert not caplog.records


def test_boot_check_warns_on_unparseable_cap(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    body = _BOUNDED_CONFIG.replace('capacity_limit: "1GB"', 'capacity_limit: "tow gigs"')
    cfg = _write(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.parse_error is not None
    assert [r for r in caplog.records if CLIO_CORE_RAM_UNCAPPED in r.getMessage()]


def test_boot_check_never_raises_on_unreadable_file(tmp_path: Path) -> None:
    # Read-only conformance must never block boot: a garbage file degrades to
    # "no ram tier declared" (warned), not an exception.
    cfg = tmp_path / "cte.yaml"
    cfg.write_text(":\tnot yaml {", encoding="utf-8")
    cap = boot_check_ram_cap(cfg, env={})
    assert cap.cap is None


def test_doctor_row_carries_bdev_capacity(tmp_path: Path) -> None:
    cfg = _write(tmp_path, _INCIDENT_CONFIG)
    rows = probe_clio_core_ram_cap(env={"CLIO_ARC_STORE": "cte", "CLIO_ARC_STORE_CONFIG": str(cfg)})
    assert len(rows) == 1
    assert rows[0].details["ram_bdev_capacity"] == "0g"
    assert rows[0].details["reason"] == "ram_cap_unbounded_80pct_dram"


# --------------------------------------------------------------------------- #
# Tier-topology rule (#893 live-gate finding; owner rule 2026-07-13):
# capacity limits bound INTERMEDIATE tiers only — the ram bdev ceiling and the
# FINAL tier must be unbounded. The gate's SF leg proved the failure mode:
# an all-bounded hierarchy fills and PutBlob fails rc=13 instead of spilling.
# --------------------------------------------------------------------------- #

# The exact broken shape from the incident hand-fix: ALL THREE fields bounded.
_ALL_BOUNDED_CONFIG = (
    _BOUNDED_CONFIG
    .replace('capacity: "2GB"', 'capacity: "1GB"')
    .replace('capacity_limit: "50GB"', 'capacity_limit: "1GB"')
)


def test_all_bounded_topology_warns_typed(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    from clio_agent.arc.clio_core_config import CLIO_CORE_TIER_TOPOLOGY

    cfg = _write(tmp_path, _ALL_BOUNDED_CONFIG)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.bdev_capacity == "1GB"
    assert cap.final_tier_capacity == "1GB"
    warned = [r for r in caplog.records if CLIO_CORE_TIER_TOPOLOGY in r.getMessage()]
    assert warned, "headroom-less ceiling + final<=ram must warn clio_core_tier_topology"
    msg = warned[0].getMessage()
    assert "insufficient spill headroom" in msg
    assert "cannot absorb one full hot-tier spill" in msg
    # The ram tier itself is correctly bounded — no ram_uncapped warning.
    assert not [
        r for r in caplog.records if "clio_core_ram_uncapped" in r.getMessage()
    ]


def test_correct_topology_is_silent(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    # Generator-shaped desktop budget: ceiling 2GB (bounded, 2x headroom),
    # tier 1GB, final tier 50GB (a LARGE bound stands in for "unbounded"
    # until clio-core accepts 0 on non-ram tiers).
    cfg = _write(tmp_path, _BOUNDED_CONFIG)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.final_tier_capacity == "50GB"
    assert not caplog.records


def test_generator_default_final_tier_far_exceeds_ram_tier() -> None:
    from clio_agent.arc.clio_core_config import (
        _default_cte_file_capacity,
        _default_cte_ram_capacity,
        parse_capacity_bytes,
    )

    final = parse_capacity_bytes(_default_cte_file_capacity())
    ram = parse_capacity_bytes(_default_cte_ram_capacity())
    assert final >= 10 * ram, "final layer must dwarf the hot tier (engine forbids 0)"


def test_memory_budget_is_the_arena_and_data_is_disk_only() -> None:
    """#906 release gate: the generated arena capacity IS the user's budget.

    'use 1GB of ram, and whatever you want of <disk>' — data lives on the ONE
    disk tier (owner ruling: "data is in memory or is in disk"; no ram data
    tier means nothing to evict and no rc=13 pressure class at all).
    """
    from clio_agent.arc.clio_core_config import _DEFAULT_CTE_CONFIG_TEMPLATE

    rendered = _DEFAULT_CTE_CONFIG_TEMPLATE.format(
        conf_dir="c",
        file_tier="f",
        file_capacity="50GB",
        ram_budget="1GB",
        metadata_log="m",
    )
    assert 'capacity: "1GB"' in rendered  # the arena = the budget, hard bound
    assert "cte_ram_tier" not in rendered  # disk-only: no ram data tier
    assert rendered.count("capacity_limit") == 1  # exactly the file tier


def test_generated_default_config_is_topology_conformant(tmp_path, monkeypatch) -> None:
    """The config the generator writes must pass its own boot check silently."""

    monkeypatch.setenv("CLIO_ARC_CTE_DIR", str(tmp_path / "cte"))
    from clio_agent import conf
    from clio_agent.arc.clio_core_config import default_cte_config_path

    conf.reload()
    try:
        cfg = default_cte_config_path()
        import logging as _l

        caplog_records = []
        handler = _l.Handler()
        handler.emit = lambda r: caplog_records.append(r)
        lg = _l.getLogger("clio_agent.arc.clio_core_config")
        lg.addHandler(handler)
        try:
            cap = boot_check_ram_cap(cfg, env={})
        finally:
            lg.removeHandler(handler)
        assert cap.bdev_capacity == "1GB"  # arena = the default 1GB budget
        assert cap.cap is None  # disk-only: no ram data tier
        assert not caplog_records, [r.getMessage() for r in caplog_records]
    finally:
        conf.reload()
