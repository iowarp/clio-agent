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

_BOUNDED_CONFIG = _INCIDENT_CONFIG.replace('capacity_limit: "0g"', 'capacity_limit: "1GB"')


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
    # bdev 0g stays the device ceiling of the proven-safe topology — visible,
    # not judged.
    assert cap.bdev_capacity == "0g"


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


def test_boot_check_warns_on_missing_capacity_limit(
    tmp_path: Path, caplog: "logging.LogCaptureFixture"
) -> None:
    # A declared ram tier WITHOUT capacity_limit: clio-core falls back to its
    # own 0g default (80% DRAM) — must warn, not pass silently.
    body = _BOUNDED_CONFIG.replace('        capacity_limit: "1GB"\n', "", 1)
    cfg = _write(tmp_path, body)
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.clio_core_config"):
        cap = boot_check_ram_cap(cfg, env={})
    assert cap.cap is None
    assert [r for r in caplog.records if CLIO_CORE_RAM_UNCAPPED in r.getMessage()]


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
