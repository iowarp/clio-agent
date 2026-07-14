"""The LocalFS ARC store must announce itself loudly (owner ruling 2026-07-14).

CLIO_ARC_STORE=local is an underperforming fallback with limited support for
clio-agent semantics: selecting it — even explicitly — prints the DEGRADED
banner on stdout, warns on the log, and the doctor reports the arc integration
as DEGRADED. Never silent, never memory-only tribal knowledge.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from clio_agent.arc import init_degradation as arc_deg
from clio_agent.arc import storage as arc_storage


@pytest.fixture(autouse=True)
def _reset_banner_guard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(arc_deg, "_local_banner_emitted", False)


def test_explicit_local_prints_degraded_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="clio_agent.arc.init_degradation"):
        arc_storage.make_arc_store(backend="local", data_dir=tmp_path / "arc")
    out = capsys.readouterr().out
    assert "DEGRADED TO LOCAL BACKEND" in out
    assert "underperforming" in out
    assert any("DEGRADED TO LOCAL BACKEND" in r.message for r in caplog.records)


def test_banner_emits_once_per_process(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    arc_storage.make_arc_store(backend="local", data_dir=tmp_path / "a")
    capsys.readouterr()
    arc_storage.make_arc_store(backend="local", data_dir=tmp_path / "b")
    assert "DEGRADED TO LOCAL BACKEND" not in capsys.readouterr().out


def test_cte_selection_does_not_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage twin: the banner belongs to the local path only."""

    monkeypatch.setattr(arc_storage, "ClioCoreStore", lambda *a, **kw: object())
    monkeypatch.setattr(
        "clio_agent.arc.clio_core_config.boot_check_ram_cap", lambda cfg, env: None
    )
    arc_storage.make_arc_store(backend="cte", config_path=str(tmp_path / "cte.yaml"))
    assert "DEGRADED TO LOCAL BACKEND" not in capsys.readouterr().out


def test_doctor_reports_local_as_degraded(tmp_path: Path) -> None:
    from clio_agent.runtime.status import IntegrationState, RuntimeProbe

    probe = RuntimeProbe(env={"CLIO_ARC_STORE": "local", "CLIO_DATA_DIR": str(tmp_path)})
    row = probe.probe_arc()
    assert row.state is IntegrationState.DEGRADED
    assert "DEGRADED TO LOCAL BACKEND" in row.summary
    assert row.details.get("reason") == "local_backend_underperforming"
